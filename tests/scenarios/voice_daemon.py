# SPDX-License-Identifier: MIT
"""voice-daemon: text to speech over HTTP and speech recognition over TCP.

voice.conf (the scenario's own, tests/scenarios/conf_fixture.py) names fixed
servers: TTS URLs on 192.0.2.64/65 port 80, ASR servers 192.0.2.64/65 port
443 (one picked at random at start-up, then the next one when a connection
fails).

Phase 1, no servers: the daemon's empty network namespace can't reach those
addresses, so ASR sessions fail to connect to both servers
(asr_event(uid, "<error_server_timeout>", "cancel", 2), reply false). TTS
requests for languages without a server are refused; stops with nothing
running just succeed.

Phase 2, fake servers: the scenario enters the daemon's own user and network
namespaces with nsenter (they belong to the harness user, created by its
"unshare -r -n"), adds both server addresses to that namespace's loopback
interface and runs a fake HTTP TTS server and a fake ASR server there (this
file, run with "serve"). They are started and stopped within each run and
answer identically; what they receive (HTTP requests, grammars, audio chunk
sizes) is recorded. Nothing leaves the namespace. The ASR server answers each
session as told through a control file: recognised, no match with and without
words, a response lacking its required code, an empty message, unparsable
bytes, hanging up, or holding the session until asr_stop.

/dev/audio is a regular file (read() returns 0) and its OSS ioctls (type
'P') are faked, so recording "succeeds" and streams whatever is in the
daemon's read buffer: only chunk sizes are recorded, not their contents.
Deleting the file covers the open failure. (qemu's strace prints these ioctls
as SNDCTL_DSP_*, which the harness doesn't map, so they don't appear in the
device I/O record.)

A successful tts_speak comes last: after fork(), g_child_watch_add() in the
parent starts a GLib helper thread (an NPTL thread, which qemu-user creates),
and the call returns true (tests/expected/README.md). The child's HTTP
request is recorded, and its "madplay" popen() is seen.
"""

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

# Also when run as the fake servers (python -I, so tests/ isn't on the path).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scenarios import conf_fixture  # noqa: E402

DAEMON = "voice-daemon"
DEVICES = ["audio"]
REGULAR_FILE_DEVICES = ["audio"]
FAKE_IOCTL_TYPES = [0x50]  # OSS SNDCTL_DSP_*
SERVICE = "com.mindscape.karotz.Voice"
PATH = "/com/mindscape/karotz/Voice"
IFACE = "com.mindscape.karotz.KarotzInterface"

SERVER_ADDRESSES = conf_fixture.SERVER_ADDRESSES


def sandbox_setup(workdir, sysroot):
    return conf_fixture.sandbox_setup(workdir, sysroot, voice=conf_fixture.VOICE_CONF)


# --- the fake servers (run inside the daemon's network namespace) ----------

def varint(n):
    out = b""
    n &= (1 << 64) - 1
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out += bytes([b | 0x80])
        else:
            return out + bytes([b])


def field(number, value):
    if isinstance(value, int):
        return varint(number << 3) + varint(value)
    return varint(number << 3 | 2) + varint(len(value)) + value


def parse_fields(data):
    """(field number, value) pairs of a protobuf message (varint and bytes only)."""
    i, out = 0, []
    while i < len(data):
        key, shift = 0, 0
        while True:
            b = data[i]; i += 1
            key |= (b & 0x7F) << shift; shift += 7
            if not b & 0x80:
                break
        number, wire = key >> 3, key & 7
        if wire == 0:
            v, shift = 0, 0
            while True:
                b = data[i]; i += 1
                v |= (b & 0x7F) << shift; shift += 7
                if not b & 0x80:
                    break
        elif wire == 2:
            n, shift = 0, 0
            while True:
                b = data[i]; i += 1
                n |= (b & 0x7F) << shift; shift += 7
                if not b & 0x80:
                    break
            v = data[i:i + n]; i += n
        else:
            return out + [("?", data[i - 1:])]
        out.append((number, v))
    return out


def response(code=None, score=None, words=None, semantic=None):
    body = b""
    if code is not None:
        body += field(1, code)
    if score is not None:
        body += field(2, score)
    if words is not None:
        body += field(3, words.encode())
    if semantic is not None:
        body += field(4, semantic.encode())
    return field(3, body)


# What the ASR server answers, by mode (None: hold until the daemon hangs up,
# "close": hang up without answering). Answers come after 3 audio chunks.
ASR_ANSWERS = {
    "ok": response(1, 87, "bonjour lapin", "<hello/>"),
    "ok-empty": response(1),
    "nomatch-words": response(8, 12, "something else"),
    "nomatch": response(8),
    "timeout": response(3, 0, "", ""),
    "no-code": response(None, 5, "partial", "sem"),  # required code missing
    "no-response": b"",                              # an empty AsrMsg
    "garbage": b"\xff\xff\xff",
    "close": "close",
    "hold": None,
}


def serve(ctrl):
    """Run the fake TTS and ASR servers; log what they receive to ctrl/log."""
    for addr in SERVER_ADDRESSES:
        for ip in ("/usr/sbin/ip", "/sbin/ip", "ip"):
            if subprocess.run([ip, "addr", "add", f"{addr}/32", "dev", "lo"], capture_output=True).returncode == 0:
                break
    lock = threading.Lock()
    logf = open(os.path.join(ctrl, "log"), "a", buffering=1)

    def log(line):
        with lock:
            logf.write(line + "\n")

    def read_exact(conn, n):
        data = b""
        while len(data) < n:
            chunk = conn.recv(n - len(data))
            if not chunk:
                return None
            data += chunk
        return data

    def asr_session(conn):
        try:
            mode = open(os.path.join(ctrl, "asr_mode")).read().strip()
        except OSError:
            mode = "hold"
        log(f"asr: connection (mode {mode})")
        answer = ASR_ANSWERS.get(mode)
        audio_sizes, audio_count, answered = set(), 0, False
        while True:
            header = read_exact(conn, 4)
            if header is None:
                break
            body = read_exact(conn, int.from_bytes(header, "big"))
            if body is None:
                break
            for number, value in parse_fields(body):
                if number == 1:
                    log(f"asr: grammar {value.decode(errors='replace')!r}")
                elif number == 2:
                    audio_count += 1
                    if len(value) not in audio_sizes:
                        audio_sizes.add(len(value))
                        log(f"asr: audio chunk of {len(value)} bytes")
                    # tests/aggressive/voice_short_reads: /dev/audio here gives 0
                    # bytes per read, so every chunk is unread audio, which
                    # must be silence, not stale memory.
                    if os.environ.get("KAROTZ_ASR_LOG_SILENCE"):
                        log(f"asr: audio chunk {'silent' if not value.strip(bytes(1)) else 'NOT silent'}")
                else:
                    log(f"asr: field {number} {value!r}")
            if audio_count >= 3 and not answered and mode != "hold":
                answered = True
                if answer == "close":
                    log("asr: hanging up")
                    break
                conn.sendall(len(answer).to_bytes(4, "big") + answer)
                log("asr: answered")
        if not (answered and answer == "close"):
            log("asr: closed by the daemon")
        conn.close()

    def http_session(conn):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                return
            data += chunk
        head, body = data.split(b"\r\n\r\n", 1)
        lines = head.decode(errors="replace").split("\r\n")
        headers = dict(l.split(": ", 1) for l in lines[1:] if ": " in l)
        length = int(headers.get("Content-Length", "0"))
        while len(body) < length:
            chunk = conn.recv(4096)
            if not chunk:
                break
            body += chunk
        log(f"tts: {lines[0]}")
        for name in sorted(headers):
            log(f"tts:   {name}: {headers[name]}")
        log(f"tts:   body {body.decode(errors='replace')!r}")
        try:
            mp3 = b"ID3" + bytes(4093)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: audio/mpeg\r\nContent-Length: %d\r\n\r\n" % len(mp3) + mp3)
        except OSError:
            pass
        conn.close()

    def listen(port, handler):
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", port))
        s.listen(8)

        def loop():
            while True:
                conn, _ = s.accept()
                threading.Thread(target=handler, args=(conn,), daemon=True).start()
        threading.Thread(target=loop, daemon=True).start()

    listen(80, http_session)
    listen(443, asr_session)
    open(os.path.join(ctrl, "ready"), "w").close()
    while True:
        time.sleep(3600)


# --- the scenario --------------------------------------------------------

def call(bus, method, *args):
    out = bus.send(f"--dest={SERVICE}", PATH, f"{IFACE}.{method}", *args)
    lines = [l.strip() for l in out.splitlines() if not l.startswith("method return")]
    return f"{method}{args}: {' | '.join(lines)}"


def wait_signals(bus, count, timeout=20):
    """Wait until at least `count` signals have been seen (or the timeout)."""
    deadline = time.time() + timeout
    while time.time() < deadline and len(bus.signals()) < count:
        time.sleep(0.1)
    time.sleep(0.5)  # anything that would follow
    return len(bus.signals())


class FakeServers:
    def __init__(self, pid):
        self.ctrl = tempfile.mkdtemp(prefix="karotz-voice-fake-")
        self.seen = 0
        # Only enter the namespaces once the daemon is in them.
        assert os.readlink(f"/proc/{pid}/ns/net") != os.readlink("/proc/self/ns/net")
        self.proc = subprocess.Popen(["nsenter", "-t", str(pid), "-U", "-n", "--preserve-credentials", "--", sys.executable, "-I",
                                      os.path.abspath(__file__), "serve", self.ctrl],
                                     stdout=subprocess.DEVNULL, stderr=open(os.path.join(self.ctrl, "err"), "w"))
        deadline = time.time() + 20
        while time.time() < deadline and not os.path.exists(os.path.join(self.ctrl, "ready")):
            time.sleep(0.1)
        self.ready = os.path.exists(os.path.join(self.ctrl, "ready"))

    def asr_mode(self, mode):
        with open(os.path.join(self.ctrl, "asr_mode"), "w") as f:
            f.write(mode)

    def new_log(self):
        try:
            lines = open(os.path.join(self.ctrl, "log")).read().splitlines()
        except OSError:
            lines = []
        new, self.seen = lines[self.seen:], len(lines)
        return ["  server: " + l for l in new]

    def new_log_peek(self):
        try:
            return open(os.path.join(self.ctrl, "log")).read().splitlines()[self.seen:]
        except OSError:
            return []

    def stop(self):
        self.proc.kill()
        self.proc.wait()
        shutil.rmtree(self.ctrl, ignore_errors=True)


def drive(d, bus):
    from harness import introspection

    record = [f"== registered: {bus.wait_for_name(SERVICE)}"]
    record.append("== introspection")
    record.extend(introspection(bus, SERVICE, PATH))
    n = wait_signals(bus, 1)

    record.append("== phase 1: no servers reachable")
    record.append(call(bus, "tts_stop", "string:nothing", "int32:0"))
    record.append(call(bus, "asr_stop", "string:nothing", "int32:0"))
    # Languages without a server: refused before anything is started.
    for lang in ["", "f", "it", "xx", "1", "fé"]:
        record.append(call(bus, "tts_speak", "string:bad-lang", "string:bonjour", f"string:{lang}", "int32:0"))
    record.append(call(bus, "tts_stop", "string:bad-lang", "int32:0"))
    record.append(call(bus, "asr_start_string", "string:asr-a", "string:oui | non", "string:fr-FR", "int32:0"))
    n = wait_signals(bus, n + 1)
    record.append(call(bus, "asr_start_file", "string:asr-b", "string:/nonexistent.grxml", "int32:1"))
    n = wait_signals(bus, n + 1)
    record.append(call(bus, "asr_stop", "string:asr-b", "int32:0"))

    record.append("== phase 2: fake servers")
    fake = FakeServers(d.proc.pid)
    # Named after the harness's pid, which the harness masks in the record.
    grammar_file = open(f"/tmp/karotz-voice-grammar-{os.getpid()}.abnf", "w")
    grammar_file.write("#ABNF 1.0 UTF-8;\nroot $x;\n$x = " + "word | " * 40 + "end;\n")
    grammar_file.close()
    try:
        record.append(f"fake servers ready: {fake.ready}")

        record.append("-- asr sessions")
        sessions = [
            ("ok", "asr_start_string", ["string:s-ok", "string:bonjour lapin | au revoir", "string:fr-FR"]),
            ("ok-empty", "asr_start_string", ["string:s-ok-empty", "string:", "string:"]),
            ("nomatch-words", "asr_start_string", ["string:s-nmw", "string:http://example.com/g.grxml", "string:en"]),
            ("nomatch", "asr_start_string", ["string:s-nm", "string:xhttp: not quite", "string:de-DE"]),
            ("timeout", "asr_start_string", ["string:s-to", "string:0123456789http", "string:es"]),
            ("no-code", "asr_start_file", ["string:s-nc", f"string:{grammar_file.name}"]),
            ("no-response", "asr_start_string", ["string:s-nr", "string:a", "string:en-US"]),
            ("garbage", "asr_start_string", ["string:s-gb", "string:b", "string:en-US"]),
            ("close", "asr_start_string", ["string:s-cl", "string:c", "string:en-US"]),
        ]
        for mode, method, args in sessions:
            fake.asr_mode(mode)
            record.append(f"[{mode}] " + call(bus, method, *args, "int32:0"))
            # Sessions that end without a signal still end within seconds.
            n = wait_signals(bus, n + 1, timeout=6 if mode in ("no-response", "garbage") else 20)
            time.sleep(0.5)
            record.extend(fake.new_log())

        record.append("-- a grammar file that can't be read")
        fake.asr_mode("hold")
        record.append(call(bus, "asr_start_file", "string:s-nofile", "string:/nonexistent/grammar.abnf", "int32:0"))
        n = wait_signals(bus, n + 1)
        time.sleep(0.5)
        record.extend(fake.new_log())

        record.append("-- a session held open: busy, then stopped")
        record.append(call(bus, "asr_start_string", "string:s-hold", "string:yes | no", "string:en", "int32:0"))
        time.sleep(1)
        record.append(call(bus, "asr_start_string", "string:s-busy", "string:other", "string:en", "int32:0"))
        record.append(call(bus, "asr_start_file", "string:s-busy2", f"string:{grammar_file.name}", "int32:0"))
        record.append(call(bus, "asr_stop", "string:whatever", "int32:0"))
        time.sleep(1)
        record.append(call(bus, "asr_stop", "string:again", "int32:0"))
        record.extend(fake.new_log())

        record.append("-- /dev/audio missing")
        audio = d.fifos["audio"]
        audio.unlink()
        record.append(call(bus, "asr_start_string", "string:s-noaudio", "string:x", "string:fr", "int32:0"))
        n = wait_signals(bus, n + 1)
        audio.touch()
        time.sleep(0.5)
        record.extend(fake.new_log())

        record.append("-- one more session")
        fake.asr_mode("ok")
        record.append(call(bus, "asr_start_string", "string:s-last", "string:fin", "string:fr", "int32:0"))
        n = wait_signals(bus, n + 1)
        time.sleep(0.5)
        record.extend(fake.new_log())

        # Last: the daemon aborts here under qemu (see the module docstring).
        record.append("-- tts (the daemon aborts here under qemu)")
        record.append(call(bus, "tts_speak", "string:t-fr", "string:Bonjour, je suis \"Karotz\" & toi ?",
                           "string:FRA", "int32:0"))
        deadline = time.time() + 20
        while time.time() < deadline and not any("body" in l for l in fake.new_log_peek()):
            time.sleep(0.2)
        time.sleep(1)
        record.extend(fake.new_log())
    finally:
        fake.stop()
        os.unlink(grammar_file.name)
    return record


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "serve":
    serve(sys.argv[2])
