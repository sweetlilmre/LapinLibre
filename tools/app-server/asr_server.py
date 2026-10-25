#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""A speech-recognition server for the rabbit's voice-daemon (voice.conf [asr]).

voice-daemon connects over TCP, sends the grammar (an ABNF text, or the URL
of one: the controller's launch grammar, /asr/grammar/<id>.abnf), then the
microphone's audio in chunks, each an AsrMsg (userland/proto/asr-message.proto)
preceded by its length (4 bytes, network order), and waits for an AsrMsg with
a Response (code, words, semantic, score). This server recognises nothing:
it answers with a chosen result, which is enough to run the whole path on the
rabbit (grammar, audio streaming, the answer, the application's callback, an
ASR launch). What it answers, for each session:

  "first"        the grammar's first alternative (the default)
  "nomatch"      no match (code NOMATCH)
  any text       that text as the recognised words

with the semantic the grammar gives the answered alternative ({<app>key</app>
<par>instance</par>}: what the controller starts an application from). A
grammar given by URL comes from set_grammar_source's function when the app
server runs this one (app_server.py --asr-port), or is fetched.

The answer comes once AFTER_S seconds of audio have arrived (the rabbit keeps
streaming until it is answered). The grammar and the audio received are
logged; the audio of the last session is kept as asr-last.raw (16-bit
mono, as voice-daemon records it) next to the uploads when a folder is given.

    python3 asr_server.py --port 8443 [--answer first|nomatch|TEXT]

Standard library only (the message is small enough to encode by hand).
"""

import argparse
import re
import socketserver
import struct
import time
import urllib.request
from pathlib import Path

AFTER_S = 1.5
CODES = {"OK": 1, "ERROR": 2, "TIMEOUT": 3, "TERMINATED": 4, "CANCELLED": 5, "BUSY": 6, "COMPFAILURE": 7,
         "NOMATCH": 8, "NOINPUT": 9}


def log(text):
    print(time.strftime("%H:%M:%S ") + "asr: " + text, flush=True)


# ---- protobuf wire format, for AsrMsg's few fields ----

def varint(n):
    out = b""
    while True:
        byte, n = n & 0x7f, n >> 7
        out += bytes([byte | (0x80 if n else 0)])
        if not n:
            return out


def read_varint(data, i):
    shift = value = 0
    while True:
        byte = data[i]
        i += 1
        value |= (byte & 0x7f) << shift
        shift += 7
        if not byte & 0x80:
            return value, i


def fields(data):
    """(field number, wire type, value) for each field of a message."""
    i = 0
    while i < len(data):
        key, i = read_varint(data, i)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, i = read_varint(data, i)
        elif wire == 2:
            length, i = read_varint(data, i)
            value, i = data[i:i + length], i + length
        elif wire == 5:
            value, i = data[i:i + 4], i + 4
        elif wire == 1:
            value, i = data[i:i + 8], i + 8
        else:
            raise ValueError(f"wire type {wire}")
        yield number, wire, value


def length_field(number, payload):
    return varint(number << 3 | 2) + varint(len(payload)) + payload


def response_message(code, words="", semantic="", score=0):
    """An AsrMsg holding a Response."""
    response = varint(1 << 3) + varint(CODES[code]) + varint(2 << 3) + varint(score)
    if words:
        response += length_field(3, words.encode())
    if semantic:
        response += length_field(4, semantic.encode())
    return length_field(3, response)


def choices(grammar):
    """(words, semantic) of each alternative of the grammar's rule ("public $App = a | b {<app>k</app>};"
    or the rule a launch grammar uses), in order; the semantic is the alternative's {...} tag, or "".
    """
    m = re.search(r"public \$\w+ = (.*?);", grammar, re.S)
    if not m:
        return []
    out = []
    for a in m.group(1).split("|"):
        if not a.strip():
            continue
        tag = re.search(r"\{([^}]*)\}", a)
        words = " ".join(re.sub(r"\{[^}]*\}|<[^>]*>", "", a).split())
        out.append((words, tag.group(1).strip() if tag else ""))
    return out


def alternatives(grammar):
    """The words of the grammar's rule's alternatives, in order."""
    return [words for words, _ in choices(grammar)]


def resolve(grammar):
    """The grammar's text: a URL is read through the grammar source (the app server's own grammar),
    else fetched; "" when it can't be."""
    if not re.fullmatch(r"https?://\S+", grammar.strip()):
        return grammar
    url = grammar.strip()
    if Session.grammar_source is not None:
        text = Session.grammar_source(url)
        if text is not None:
            return text
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            return r.read(65536).decode(errors="replace")
    except OSError as e:
        log(f"grammar {url}: {e}")
        return ""


class Session(socketserver.BaseRequestHandler):
    answer = "first"            # the class's setting, changed by set_answer()
    grammar_source = None       # URL -> grammar text (or None), set by set_grammar_source()
    keep = None                 # where to keep the last session's audio, or None

    def recv_exact(self, n):
        data = b""
        while len(data) < n:
            chunk = self.request.recv(n - len(data))
            if not chunk:
                raise EOFError
            data += chunk
        return data

    def handle(self):
        peer = self.client_address[0]
        log(f"session from {peer}")
        grammar, audio, started, answered = "", bytearray(), None, False
        self.request.settimeout(30)
        try:
            while True:
                size = struct.unpack(">I", self.recv_exact(4))[0]
                message = self.recv_exact(size)
                for number, _, value in fields(message):
                    if number == 1:
                        grammar = value.decode(errors="replace")
                        log(f"grammar: {' '.join(grammar.split())[:200]}")
                    elif number == 2:
                        audio += value
                        started = started or time.time()
                if not answered and started and time.time() - started >= AFTER_S:
                    self.request.sendall(self.reply(grammar))
                    answered = True
        except (EOFError, OSError):
            pass
        log(f"session ended: {len(audio)} bytes of audio ({len(audio) / 32000:.1f} s at 16 kHz)")
        if Session.keep and audio:
            (Session.keep / "asr-last.raw").write_bytes(bytes(audio))

    def reply(self, grammar):
        answer = Session.answer
        if answer == "nomatch":
            log("answering: no match")
            payload = response_message("NOMATCH", score=0)
        else:
            found = choices(resolve(grammar))
            if answer == "first":
                words, semantic = found[0] if found else ("", "")
            else:
                words = answer
                semantic = next((s for w, s in found if w.lower() == answer.lower()), "")
            log(f"answering: {words!r}" + (f" ({semantic})" if semantic else ""))
            payload = response_message("OK", words=words, semantic=semantic, score=80)
        return struct.pack(">I", len(payload)) + payload


def set_answer(answer):
    Session.answer = answer


def set_grammar_source(source):
    """source(url): the grammar text for a grammar given by URL, or None to fetch it."""
    Session.grammar_source = source


def asr_server(listen, port, answer="first", keep=None):
    Session.answer, Session.keep = answer, Path(keep) if keep else None
    server = socketserver.ThreadingTCPServer((listen, port), Session)
    server.daemon_threads = True
    return server


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--listen", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8443)
    p.add_argument("--answer", default="first", help='"first" (default), "nomatch" or the words to answer')
    p.add_argument("--keep", metavar="DIR", help="keep the last session's audio in DIR/asr-last.raw")
    a = p.parse_args()
    server = asr_server(a.listen, a.port, a.answer, a.keep)
    log(f"listening on {a.listen}:{a.port}, answering {a.answer!r}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
