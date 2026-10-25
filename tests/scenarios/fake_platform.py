# SPDX-License-Identifier: MIT
"""Fake platform for the controller_platform scenario (run as a script,
inside its own network namespace, joined to the controller's by a veth pair).

    python3 fake_platform.py CONFIG.json

It plays the platform server: it listens for the controller on
ADDRESS:PORT (PlatformMessage protobufs, each preceded by its length as 4
bytes in network order), serves the application archives over HTTP on
ADDRESS:HTTP_PORT, and can also connect to the controller's own server the
way an interactive application does. Messages are decoded and encoded with
protoc and userland/proto/platform-message.proto, in protobuf text format.

The scenario drives it over a Unix socket (CONTROL), one JSON request per
line, one JSON answer per line:

  {"op": "wait_connection", "timeout": s}   -> {"conn": n} (the next connection from the controller)
  {"op": "connect", "address": a, "port": p} -> {"conn": n} (a connection to the controller's server)
  {"op": "send", "conn": n, "text": t}      -> {} (encode t, send it framed)
  {"op": "close", "conn": n}                -> {}
  {"op": "messages", "settle": s}           -> {"messages": [[n, text], ...]} (everything received
                                               since the last call, once nothing new came for s seconds)
  {"op": "http"}                            -> {"requests": [...]} (the HTTP requests since the last call)
"""

import http.server
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time

CONFIG = json.load(open(sys.argv[1]))
TYPE = "lapinlibre.platform.PlatformMessage"

lock = threading.Lock()
changed = threading.Condition(lock)
conns = {}            # number -> socket
accepted = []         # numbers of the controller's connections, in order, not yet waited for
received = []         # [number, decoded text], not yet collected
http_requests = []
last_activity = [time.time()]


def protoc(mode, data):
    proto_dir = CONFIG["proto_dir"]
    r = subprocess.run([CONFIG["protoc"], f"--{mode}={TYPE}", f"--proto_path={proto_dir}",
                        os.path.join(proto_dir, "platform-message.proto")], input=data, capture_output=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="replace"))
    return r.stdout


def recv_exact(sock, n):
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise EOFError
        data += chunk
    return data


def reader(number, sock):
    while True:
        try:
            head = recv_exact(sock, 4)
            payload = recv_exact(sock, struct.unpack(">I", head)[0])
        except (OSError, EOFError):
            text = "<closed>"
        else:
            try:
                text = protoc("decode", payload).decode(errors="replace")
            except RuntimeError as e:
                text = f"<undecodable {payload.hex()}: {e}>"
        with changed:
            received.append([number, text])
            last_activity[0] = time.time()
            changed.notify_all()
        if text == "<closed>":
            return


def add_connection(sock):
    with changed:
        number = len(conns) + 1
        conns[number] = sock
    threading.Thread(target=reader, args=(number, sock), daemon=True).start()
    return number


def accept_loop(server):
    while True:
        sock, _ = server.accept()
        number = add_connection(sock)
        with changed:
            accepted.append(number)
            changed.notify_all()


class HttpHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=CONFIG["http_dir"], **kwargs)

    def do_GET(self):
        with changed:
            http_requests.append(f"GET {self.path} ({self.headers.get('User-Agent')})")
        super().do_GET()

    def log_message(self, *args):
        pass


def handle(request):
    op = request["op"]
    if op == "wait_connection":
        deadline = time.time() + request.get("timeout", 30)
        with changed:
            while not accepted and time.time() < deadline:
                changed.wait(deadline - time.time())
            return {"conn": accepted.pop(0) if accepted else None}
    if op == "connect":
        sock = socket.create_connection((request["address"], request["port"]), timeout=10)
        sock.settimeout(None)
        return {"conn": add_connection(sock)}
    if op == "send":
        payload = protoc("encode", request["text"].encode())
        # A message cut off mid-field, for tests of malformed input.
        payload = payload[:len(payload) - request.get("truncate", 0)]
        conns[request["conn"]].sendall(struct.pack(">I", len(payload)) + payload)
        return {}
    if op == "close":
        conns[request["conn"]].shutdown(socket.SHUT_RDWR)
        conns[request["conn"]].close()
        return {}
    if op == "messages":
        settle = request.get("settle", 1.0)
        deadline = time.time() + request.get("timeout", 60)
        with changed:
            while time.time() < deadline:
                quiet = time.time() - last_activity[0]
                if quiet >= settle:
                    break
                changed.wait(settle - quiet)
            out = list(received)
            received.clear()
        return {"messages": out}
    if op == "http":
        with changed:
            out = list(http_requests)
            http_requests.clear()
        return {"requests": out}
    return {"error": f"unknown op {op}"}


def main():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((CONFIG["address"], CONFIG["port"]))
    server.listen(5)
    threading.Thread(target=accept_loop, args=(server,), daemon=True).start()

    httpd = http.server.ThreadingHTTPServer((CONFIG["address"], CONFIG["http_port"]), HttpHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    control = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    control.bind(CONFIG["control"])  # its appearance tells the harness the servers are up
    control.listen(1)
    while True:
        conn, _ = control.accept()
        stream = conn.makefile("rw")
        for line in stream:
            try:
                answer = handle(json.loads(line))
            except Exception as e:  # reported to the scenario, which fails the run
                answer = {"error": repr(e)}
            stream.write(json.dumps(answer) + "\n")
            stream.flush()
        conn.close()


if __name__ == "__main__":
    main()
