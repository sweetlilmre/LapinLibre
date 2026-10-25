# SPDX-License-Identifier: MIT
"""Fake controller for the karotzVM scenario (run as a script, inside an
empty network namespace with only loopback up).

    python3 karotzvm_controller.py CONFIG.json

It starts karotzVM (the qemu command line in the configuration, plus
--app_folder), writes one launch line at a time on its standard input and
waits for the child to end (watch_children() sees its process go), while it

- plays the controller: accepts karotzVM's connections on 127.0.0.1:9123,
  decodes every PlatformMessage with protoc (--decode, with userland/proto/platform-message.proto)
  and answers each request the way the real controller would (protoc
  --encode builds the replies);
- obeys the test applications: a JavaScript log("CMD:<name>") (seen on
  karotzVM's stderr as "JSLOG: CMD:<name>") makes it send the unsolicited
  message <name> (a button press, an RFID tag, ...), see EVENTS, or change
  how it answers (CMD:code=R[/E], CMD:hold, CMD:flush; see answer()), and
  for tests/aggressive: CMD:repeat, CMD:split (each Response and Event a
  message of its own), CMD:age=N (karotzVM's clock N seconds ahead, with
  tests/fakes/timeshift-arm.c), CMD:rss (the application process's
  memory, as a "MEM: rss" line) and CMD:burst=<event>,<n> (an event n
  times at once);
  a launch's options can refuse interactive mode or answer it late;
- serves HTTP on 127.0.0.1:8088 for http.get/post, recording each request.

It writes a JSON list of record lines: per launch, karotzVM's stderr, the
messages karotzVM sent, the HTTP requests, and at the end its stdout. UUIDs
(message and interactive ids) are numbered in order of appearance and
pointers and pids masked, so every run gives the same record.
"""

import http.server
import json
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import threading
import time

CONFIG = json.load(open(sys.argv[1]))
PROTOC = CONFIG["protoc"]
PROTO_DIR = CONFIG["proto_dir"]
TYPE = "lapinlibre.platform.PlatformMessage"

lock = threading.Lock()
stderr_lines = []      # karotzVM's stderr, line by line
sent_messages = []     # decoded messages karotzVM sent
http_requests = []     # requests the HTTP server saw
connections = []       # sockets karotzVM opened
ended = threading.Event()
children_ended = [0]
reply_counter = [0]
options = {}           # per-launch options (e.g. refuse interactive mode)
karotzvm_pid = [0]


def protoc(mode, data):
    r = subprocess.run([PROTOC, f"--{mode}={TYPE}", f"--proto_path={PROTO_DIR}", os.path.join(PROTO_DIR, "platform-message.proto")],
                       input=data, capture_output=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="replace"))
    return r.stdout


def decode(payload):
    try:
        return protoc("decode", payload).decode(errors="replace")
    except RuntimeError as e:
        return f"<undecodable {payload.hex()}: {e}>"


def send_text(conn, text):
    """Encodes a PlatformMessage from protobuf text format and sends it framed."""
    payload = protoc("encode", text.encode())
    with lock:
        conn.sendall(struct.pack(">I", len(payload)) + payload)


def send_raw(conn, payload):
    with lock:
        conn.sendall(struct.pack(">I", len(payload)) + payload)


def next_id():
    reply_counter[0] += 1
    return f"ctl-{reply_counter[0]}"


def split_body(body):
    """A reply's Response, then the rest (its Event...), as two bodies, the
    way the real controller sends them; the body itself without both."""
    blocks, depth, start = [], 0, 0
    for i, ch in enumerate(body):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                blocks.append(body[start:i + 1].strip())
                start = i + 1
    response = [b for b in blocks if b.startswith("response ")]
    rest = [b for b in blocks if not b.startswith("response ")]
    if not response or not rest:
        return [body]
    return [response[0], " ".join(rest)]


def reply(conn, request_id, body):
    # CMD:split sends the Response and the Event as messages of their own
    # (for the reply handlers that end on a reply).
    for part in split_body(body) if options.get("split") else [body]:
        send_text(conn, f'id: "{next_id()}" correlationId: "{request_id}" {part}')
        if options.get("repeat"):  # CMD:repeat, a misbehaving controller
            send_text(conn, f'id: "{next_id()}" correlationId: "{request_id}" {part}')


DONE = 'response { code: OK description: "ok" } event { code: TERMINATED description: "done" }'

held = []              # requests kept unanswered after CMD:hold


def answer(conn, text):
    """The controller's side: reply to one request.

    An application can change the answers with log("CMD:..."): code=R[/E]
    answers every later request (but interactive mode and social) with just
    a Response of code R and, if given, an Event of code E; hold keeps the
    requests unanswered until flush answers them."""
    m = re.search(r'^id: "([^"]*)"', text, re.M)
    if not m:
        return
    rid = m.group(1)
    top = set(re.findall(r"^(\w+) \{", text, re.M))
    if "interactiveMode" in top:
        if "action: START" in text:
            code = "ERROR" if options.get("refuse") else "OK"
            delay = options.get("start_delay")
            if delay:
                def late(body=f"response {{ code: {code} }}"):
                    try:
                        reply(conn, rid, body)
                    except OSError:  # the application was autokilled first
                        pass
                threading.Timer(delay, late).start()
            else:
                reply(conn, rid, f"response {{ code: {code} }}")
        return
    if options.get("hold"):
        held.append((conn, text))
        return
    if options.get("code") and "socialQuery" not in top:
        response, _, event = options["code"].partition("/")
        reply(conn, rid, f"response {{ code: {response} }}" + (f" event {{ code: {event} }}" if event else ""))
        return
    if "ears" in top:
        if "reset: true" in text:
            reply(conn, rid, 'response { code: OK } ears { left: 3 right: 4 }')
        else:
            reply(conn, rid, DONE)
        return
    if "multimedia" in top:
        lists = {"getartist": 'artistlist: "Artist A" artistlist: "Artist B"',
                 "getfolder": 'folderlist: "Folder A"',
                 "getplaylist": 'playlistlist: "List A"',
                 "getgenre": 'genrelist: "Rock" genrelist: "Jazz"',
                 "getsong": ''}
        for request, body in lists.items():
            if f"  {request} {{" in text:
                reply(conn, rid, f"multimedia {{ {body} }}")
                return
        if "  record {" in text:
            reply(conn, rid, 'response { code: OK } event { code: OK description: "recorded" }')
            return
        reply(conn, rid, DONE)
        return
    if "webcam" in top and 'photo: "direct"' in text:
        # The controller's own snapshot: the JPEG in the reply (a fixed few bytes here).
        reply(conn, rid, 'response { code: OK } webcamDirectResponse { data: "\\377\\330JPEG\\000bytes\\377\\331" }')
        return
    if "webcam" in top:
        reply(conn, rid, 'response { code: OK description: "taken" } event { code: OK description: "sent" }')
        return
    if top & {"led", "tts", "dock", "ping"}:
        reply(conn, rid, DONE)
        return
    if "asr" in top and options.get("asr_norec"):
        reply(conn, rid, 'response { code: OK }')
        reply(conn, rid, 'asrCallback { }')
        return
    if "asr" in top:
        reply(conn, rid, 'response { code: OK }')
        reply(conn, rid, 'asrCallback { recognition { text: "oui" confident: 80 '
                         'semantic: "<result><answer>yes</answer><x><y>1</y><z>2</z></x></result>" } }')
        return
    if "getcontactList" in top:
        reply(conn, rid, 'contactList { contacts { firstname: "Ann" lastname: "Lee" '
                         'objects { idDB: "o1" name: "rabbit" } objects { idDB: "o2" } uuid: "u-ann" } '
                         'contacts { alias: "bob" objects { idDB: "o3" } } }')
        return
    if "karotzMessageQuery" in top:
        reply(conn, rid, 'response { code: OK } karotzMessageResponse { karotzMessageList { '
                         'header { id: "m1" date: 1234 status: READ from { firstname: "Ann" } '
                         'to { uuid: "u-me" objects { idDB: "o9" name: "me" } } } '
                         'content { type: TEXT content: "hi there" header: "h" } } }')
        return
    if "karotzinfo_query" in top:
        reply(conn, rid, 'karotzinfo_response { infos { key: "lang" value: "fr" } infos { key: "tz" value: "UTC" } }')
        return
    if "socialQuery" in top:
        reply(conn, rid, 'social { twitterConsumerKey: "ck" twitterConsumerSecret: "cs" twitterToken: "tk" '
                         'twitterTokenSecret: "ts" facebookToken: "fb" soundcloudToken: "sc" }')
        return


# Unsolicited messages the applications ask for with log("CMD:<name>").
EVENTS = {
    "button": 'buttonCallback { type: DOUBLE }',
    "button-long": 'buttonCallback { type: LONG_START }',
    "ears": 'earsCallback { type: START_LEFT step: 2 }',
    "ears-blocked": 'earsCallback { type: BLOCKED_RIGHT step: 1 length: 7 }',
    "rfid": 'rfidCallback { id: "e0040100c0ffee1a" content: "abc" direction: IN app: 1 type: FLAT pict: 3 color: RED }',
    "rfid-bare": 'rfidCallback { id: "d002" }',
    "multimedia": 'multimediaCallback { action: PLAY }',
    "k2k": 'k2kCallback { type: INCOMING correspondant: "bob" }',
    "serial": 'serialCallback { readline { port: "/dev/ttyUSB0" data: "line" base: "a" } }',
    "dock": 'dock { status: 1 change: 2 }',
    "nocorr": 'correlationId: "no-such-message" response { code: OK }',
    "stop": 'interactiveMode { action: STOP }',
}


def command(name):
    # Hostile modes for tests/aggressive: every reply twice, an
    # ASR result without recognition, a reply to a request never made.
    if name in ("repeat", "repeat-off"):
        options["repeat"] = name == "repeat"
        return
    if name == "asr-norec":
        options["asr_norec"] = True
        return
    if name in ("split", "split-off"):
        options["split"] = name == "split"
        return
    if name.startswith("age="):
        # karotzVM's time() runs this many seconds ahead from now on
        # (tests/fakes/timeshift-arm.c, preloaded by the test).
        with open(CONFIG["timeshift"], "w") as f:
            f.write(name[len("age="):])
        return
    if name == "rss":
        # The memory the application's process (karotzVM's child) holds.
        with lock:
            stderr_lines.append(f"MEM: rss {child_rss()}")
        return
    if name == "stray":
        if connections:
            send_text(connections[-1], f'id: "{next_id()}" correlationId: "no-such-request" response {{ code: OK }}')
        return
    if name.startswith("code="):
        options["code"] = name[len("code="):]
        return
    if name == "code-off":
        options.pop("code", None)
        return
    if name == "hold":
        options["hold"] = True
        return
    if name == "close":
        # The controller drops the application's connection.
        if connections:
            try:
                connections[-1].shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        return
    if name == "flush":
        options["hold"] = False
        while held:
            answer(*held.pop(0))
        return
    if name == "malformed-replies":
        # Each held request answered with a reply that doesn't parse (an
        # Event without its required code).
        options["hold"] = False
        while held:
            conn, text = held.pop(0)
            rid = re.search(r'^id: "([^"]*)"', text, re.M).group(1)
            send_raw(conn, protoc("encode", f'id: "{next_id()}" correlationId: "{rid}" event {{ }}'.encode()))
        return
    if not connections:
        return
    if name.startswith("burst="):
        # burst=<event>,<n>: the event n times, encoded once (protoc per
        # message would be far too slow for the thousands a memory test needs).
        event, count = name[len("burst="):].split(",")
        payload = protoc("encode", f'id: "{next_id()}" {EVENTS[event]}'.encode())
        frame = struct.pack(">I", len(payload)) + payload
        with lock:
            connections[-1].sendall(frame * int(count))
        return
    if name.endswith("-all"):
        # The same event to every application connected (several at once).
        for conn in list(connections):
            send_text(conn, f'id: "{next_id()}" {EVENTS[name[:-len("-all")]]}')
        return
    conn = connections[-1]
    if name == "noid":
        # A message missing its required id (protobuf logs an error).
        send_raw(conn, protoc("encode", b'recipient: "x"'))
        return
    if name == "malformed":
        # Two messages that don't parse: a button event missing its
        # required type, and a button event cut off mid-field.
        send_raw(conn, protoc("encode", f'id: "{next_id()}" buttonCallback {{ }}'.encode()))
        send_raw(conn, protoc("encode", f'id: "{next_id()}" {EVENTS["button"]}'.encode())[:-1])
        return
    send_text(conn, f'id: "{next_id()}" {EVENTS[name]}')


def child_rss():
    """VmRSS in kB of karotzVM's largest child process (qemu's, for the
    application's fork), or -1."""
    best = -1
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            stat = open(f"/proc/{entry}/stat").read()
            ppid = int(stat.rsplit(")", 1)[1].split()[1])
            if ppid != karotzvm_pid[0]:
                continue
            for line in open(f"/proc/{entry}/status"):
                if line.startswith("VmRSS:"):
                    best = max(best, int(line.split()[1]))
        except (OSError, ValueError, IndexError):
            continue
    return best


def live_children(parent):
    """The pids of parent's child processes that are not zombies."""
    found = set()
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            fields = open(f"/proc/{entry}/stat").read().rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            continue
        if fields[1] == str(parent) and fields[0] != "Z":
            found.add(int(entry))
    return found


def watch_children():
    """Counts karotzVM's children as they end (karotzVM logs nothing then):
    every 20 ms its live children are listed, and one seen before and gone
    now has ended. Under qemu even a launch refused at once starts V8 first,
    so a child lives far longer than that; one never seen would leave its
    launch "still running" in the record."""
    alive = set()
    while True:
        now = live_children(karotzvm_pid[0])
        gone = len(alive - now)
        if gone:
            with lock:
                children_ended[0] += gone
        alive = now
        time.sleep(0.02)


def freeze_parent(after, duration):
    """Stops the karotzVM parent for a while (tests/aggressive): children
    ending meanwhile leave one pending SIGCHLD between them."""
    time.sleep(after)
    with lock:
        stderr_lines.append(f"<controller: karotzVM parent stopped for {duration} s>")
    os.kill(karotzvm_pid[0], signal.SIGSTOP)
    time.sleep(duration)
    os.kill(karotzvm_pid[0], signal.SIGCONT)


def serve_connection(conn):
    while True:
        try:
            head = recv_exact(conn, 4)
            payload = recv_exact(conn, struct.unpack(">I", head)[0])
        except (OSError, EOFError):
            return
        text = decode(payload)
        with lock:
            sent_messages.append(text)
        try:
            answer(conn, text)
        except OSError:
            return
        except Exception as e:  # recorded, so a broken answer shows instead of the reading stopping
            with lock:
                sent_messages.append(f"<fake controller error: {type(e).__name__}: {e}>")


def recv_exact(conn, n):
    data = b""
    while len(data) < n:
        chunk = conn.recv(n - len(data))
        if not chunk:
            raise EOFError
        data += chunk
    return data


def controller_server():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 9123))
    server.listen(8)
    while True:
        conn, _ = server.accept()
        connections.append(conn)
        threading.Thread(target=serve_connection, args=(conn,), daemon=True).start()


class HttpHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def record(self, body=b""):
        headers = [f"{k}: {v}" for k, v in self.headers.items()]
        # A long body is recorded as its length, start and end.
        text = repr(body) if len(body) <= 1024 else f"<{len(body)} bytes: {body[:24]!r} ... {body[-24:]!r}>"
        with lock:
            http_requests.append(f"{self.command} {self.path} {headers} {text}")

    def respond(self):
        if self.path.startswith("/latin1"):
            body, ctype = "caf\xe9 cr\xe8me".encode("latin-1"), "text/plain; charset=ISO-8859-1"
        elif self.path.startswith("/meta"):
            body = b'<html><head><meta http-equiv="Content-Type" content="text/html; charset=ISO-8859-1"></head>caf\xe9</html>'
            ctype = "text/html"
        elif self.path.startswith("/endenc"):
            # tests/aggressive: the charset word at the very end.
            body, ctype = b"tail encoding", "text/plain"
        elif self.path.startswith("/accents"):
            # tests/aggressive: Latin-1 that is all accents.
            body, ctype = ("\xe9" * 20).encode("latin-1"), "text/plain; charset=ISO-8859-1"
        elif self.path.startswith("/stall"):
            # tests/aggressive: the headers, then nothing for a minute (a server that stalls).
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "100")
            self.end_headers()
            self.wfile.flush()
            time.sleep(60)
            return
        elif self.path.startswith("/missing"):
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"not found")
            return
        else:
            body, ctype = b"hello from 127.0.0.1", "text/plain; charset=UTF-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("X-Test", "yes")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.record()
        self.respond()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        self.record(self.rfile.read(length) if length else b"")
        self.respond()

    def log_message(self, *args):
        pass

    def version_string(self):
        return "test"

    def date_time_string(self, timestamp=None):
        return "Thu, 01 Jan 1970 00:00:00 GMT"


def http_server():
    http.server.ThreadingHTTPServer(("127.0.0.1", 8088), HttpHandler).serve_forever()


def read_stderr(stream):
    for raw in iter(stream.readline, b""):
        line = raw.decode(errors="replace").rstrip("\n")
        with lock:
            stderr_lines.append(line)
        m = re.match(r"MESSAGE: JSLOG: CMD:(\S+)", line)
        if m:
            try:
                command(m.group(1))
            except Exception as e:  # recorded, the run goes on
                with lock:
                    stderr_lines.append(f"<controller: {e}>")
    ended.set()


UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def normalise(lines):
    """Numbered UUIDs, masked pointers, pids, random numbers and MIME boundaries."""
    ids = {}

    def uuid(m):
        return ids.setdefault(m.group(0), f"<uuid{len(ids) + 1}>")

    out = []
    for line in lines:
        line = UUID.sub(uuid, line)
        line = re.sub(r"0x[0-9a-f]+", "0x?", line)
        line = re.sub(r"rand: \d+", "rand: <n>", line)
        # curl's random MIME boundary: 12 hex digits (curl 7.21), 22 letters and digits (curl 8)
        line = re.sub(r"(-{20,})[0-9A-Za-z]{12,}", r"\1<boundary>", line)
        out.append(line)
    return out


def main():
    threading.Thread(target=controller_server, daemon=True).start()
    threading.Thread(target=http_server, daemon=True).start()
    time.sleep(0.2)
    stdout = open(CONFIG["stdout"], "wb")
    proc = subprocess.Popen(CONFIG["command"] + CONFIG["args"], stdin=subprocess.PIPE, stdout=stdout,
                            stderr=subprocess.PIPE, start_new_session=True)
    karotzvm_pid[0] = proc.pid
    threading.Thread(target=read_stderr, args=(proc.stderr,), daemon=True).start()
    threading.Thread(target=watch_children, daemon=True).start()
    record = []
    seen = [0, 0, 0]

    def collect(title, parallel=False, summary=False):
        time.sleep(0.5)
        with lock:
            err, msgs, reqs = stderr_lines[seen[0]:], sent_messages[seen[1]:], http_requests[seen[2]:]
            seen[:] = [len(stderr_lines), len(sent_messages), len(http_requests)]
        if summary:
            # Only what the application logged, the warnings and the end, each
            # line tagged with the launch, and how many messages it sent: the
            # launches that test errors stay short and readable.
            record.append(f"== {title}: summary")
            keep = [l for l in normalise(err)
                    if "JSLOG:" in l or "nbChild" in l or l.startswith(("WARNING:", "CRITICAL:", "ERROR:", "qemu:"))]
            record.extend(f"{title}| {l}" for l in keep)
            record.append(f"{title}| messages sent: {len(msgs)}")
            return
        if parallel:
            # Several children write at once: compare what each application
            # logged and sent, as sorted lists, with every UUID masked (their
            # numbering would follow the interleaving).
            def masked(lines):
                return sorted(normalise([UUID.sub("<uuid>", line) for line in lines]))
            record.append(f"== {title}: application logs and warnings, sorted")
            record.extend(masked(l for l in err if "JSLOG:" in l or l.startswith(("WARNING:", "<controller:"))
                                 or "nbChild" in l))
            record.append(f"== {title}: messages sent, sorted")
            record.extend(" ".join(m.split()) for m in masked(msgs))
            return
        record.append(f"== {title}: stderr")
        record.extend(normalise(err))
        record.append(f"== {title}: messages sent")
        for text in normalise(msgs):
            record.extend(text.rstrip("\n").split("\n") + ["--"])
        record.append(f"== {title}: http requests")
        record.extend(normalise(reqs))

    time.sleep(CONFIG.get("startup", 3))
    collect("start-up")
    for launch in CONFIG["launches"]:
        options.clear()
        options.update(launch.get("options", {}))
        held.clear()
        reply_counter[0] = 0  # each launch numbers its replies from 1
        before = children_ended[0]
        # A launch can also be several lines written at once ("lines"), with
        # the number of children expected to end ("ends").
        lines = launch.get("lines", [launch["line"]])
        ends = launch.get("ends", 1)
        for i, line in enumerate(lines):
            try:
                proc.stdin.write((line + "\n").encode())
                proc.stdin.flush()
            except OSError:  # karotzVM itself has gone (a crash): record it, go on
                record.append(f"== {launch['line']}: karotzVM gone, launch not written")
                break
            if len(lines) > 1:
                time.sleep(launch.get("gap", 0.5))
        if launch.get("freeze"):
            threading.Thread(target=freeze_parent, args=launch["freeze"], daemon=True).start()
        deadline = time.time() + launch.get("timeout", 60)
        while children_ended[0] - before < ends and time.time() < deadline and proc.poll() is None:
            time.sleep(0.1)
        if children_ended[0] - before < ends:
            record.append(f"== {launch['line']}: still running after {launch.get('timeout', 60)}s")
        collect(launch["line"], parallel=len(lines) > 1, summary=launch.get("summary", False))
        for conn in connections:
            try:
                conn.close()
            except OSError:
                pass
        connections.clear()
    # An empty line ends karotzVM; with "end": "eof" stdin is closed instead,
    # as when the controller goes away.
    try:
        if CONFIG.get("end") == "eof":
            proc.stdin.close()
        else:
            proc.stdin.write(b"\n")
            proc.stdin.flush()
    except OSError:
        record.append("== karotzVM gone before the end")
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        pass
    alive = proc.poll() is None
    if alive:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
    ended.wait(5)
    collect("end")
    record.append(f"== karotzVM exit status: {'still running' if alive else proc.returncode}")
    stdout.close()
    record.append("== stdout")
    record.extend(normalise(open(CONFIG["stdout"], "rb").read().decode(errors="replace").splitlines()))
    json.dump(record, open(CONFIG["record"], "w"), indent=0)


if __name__ == "__main__":
    main()
