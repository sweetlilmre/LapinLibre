# SPDX-License-Identifier: MIT
"""voice-daemon: ASR servers that never answer a connection.

voice.conf (the scenario's own) names two ASR servers, 192.0.2.64/65 port
443. Here, inside the daemon's network namespace, both addresses are routed
to a dummy device, which drops every packet, as a host or firewall that
drops packets does (a refused connection would fail at once); without a
dummy device they lead to a listener whose accept queue is full, which
Linux mostly leaves unanswered too. The ASR connection is made from the
daemon's main loop, and the asr_start_* call is answered once it has failed.

An asr_start_string is sent, and 1 s later a tts_stop. What must hold: the
start is answered false and the asr_event reports the failure, within the
connect timeout per server (5 s each, two servers; 30 s allowed under qemu),
and the tts_stop is answered meanwhile, within PROMPT_S. Without a timeout
each server took the kernel's retries, about 189 s; with the connection made
in the method, the daemon answered nothing until it was over.

Then a second asr_start_string, and 1 s later an asr_stop: the stop is
answered within PROMPT_S, the start then false, and that session signals no
asr_event (asr_stop drops a session silently).
"""

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

# Also when run as the silent server (python -I, so tests/ isn't on the path).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scenarios import voice_daemon as vd  # noqa: E402

DAEMON = vd.DAEMON
DEVICES = vd.DEVICES
REGULAR_FILE_DEVICES = vd.REGULAR_FILE_DEVICES
FAKE_IOCTL_TYPES = vd.FAKE_IOCTL_TYPES
sandbox_setup = vd.sandbox_setup

ALLOWED_S = 30
# For a call answered while a session connects (qemu-user is slow).
PROMPT_S = 3


def ip(*args):
    """Run ip(8) with args; True if it succeeded."""
    for path in ("/usr/sbin/ip", "/sbin/ip", "ip"):
        try:
            if subprocess.run([path, *args], capture_output=True).returncode == 0:
                return True
        except FileNotFoundError:
            continue
    return False


def answered(timeout=1.0):
    """True if a connection to the first ASR address is answered (or refused) within timeout."""
    c = socket.socket()
    c.settimeout(timeout)
    try:
        c.connect((vd.SERVER_ADDRESSES[0], 443))
        return True
    except socket.timeout:
        return False
    except OSError:
        return True
    finally:
        c.close()


def serve_silent(ctrl):
    """In the daemon's network namespace: the ASR addresses lead nowhere, so
    connections get no answer. They are routed to a dummy device, which
    drops every packet; without one, they are on lo with a listener on port
    443 whose accept queue is full (whether Linux then drops a connection
    request or still answers it depends on timing, so it is the fallback)."""
    if ip("link", "add", "asrdrop", "type", "dummy") and ip("link", "set", "asrdrop", "up") and \
            all(ip("route", "add", f"{addr}/32", "dev", "asrdrop") for addr in vd.SERVER_ADDRESSES):
        silent = not answered()
        with open(os.path.join(ctrl, "ready"), "w") as f:
            f.write(f"silent: {silent} (a dummy device)\n")
        while True:
            time.sleep(3600)
    for addr in vd.SERVER_ADDRESSES:
        ip("addr", "add", f"{addr}/32", "dev", "lo")
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("0.0.0.0", 443))
    listener.listen(0)
    fillers = []
    # Fill the queue (never accepted) until a connection gets no answer.
    silent = False
    for _ in range(32):
        c = socket.socket()
        c.settimeout(1.0)
        try:
            c.connect((vd.SERVER_ADDRESSES[0], 443))
            fillers.append(c)
        except socket.timeout:
            silent = True
            c.close()
            break
    with open(os.path.join(ctrl, "ready"), "w") as f:
        f.write(f"silent: {silent} (a full accept queue)\n")
    while True:
        time.sleep(3600)


def drive(d, bus):
    record = [f"== registered: {bus.wait_for_name(vd.SERVICE)}"]
    vd.wait_signals(bus, 1)
    ctrl = tempfile.mkdtemp(prefix="karotz-voice-silent-")
    proc = subprocess.Popen(["nsenter", "-t", str(d.proc.pid), "-U", "-n", "--preserve-credentials", "--",
                             sys.executable, "-I", os.path.abspath(__file__), "serve-silent", ctrl],
                            stdout=subprocess.DEVNULL, stderr=open(os.path.join(ctrl, "err"), "w"))
    try:
        deadline = time.time() + 30
        while time.time() < deadline and not os.path.exists(os.path.join(ctrl, "ready")):
            time.sleep(0.1)
        ready = open(os.path.join(ctrl, "ready")).read().strip() if os.path.exists(os.path.join(ctrl, "ready")) else "not ready"
        record.append(f"== the ASR servers: {ready}")

        answers = {}

        def timed(name, *args):
            t = time.time()
            try:
                out = bus.send(f"--dest={vd.SERVICE}", vd.PATH, f"{vd.IFACE}.{name}", *args)
            except subprocess.TimeoutExpired:  # the harness gives up after 30 s
                answers[name] = (None, "no answer")
                return
            answers[name] = (time.time() - t, " | ".join(l.strip() for l in out.splitlines()
                                                         if not l.startswith("method return")))
        def session(uid, then, name):
            start = threading.Thread(target=timed, args=("asr_start_string", f"string:{uid}",
                                                         "string:oui | non", "string:fr-FR", "int32:0"))
            start.start()
            time.sleep(1)
            timed(then, "string:nothing", "int32:0")
            start.join(60)
            answers[name] = answers.pop("asr_start_string", (None, "no answer"))
            answers[name + " " + then] = answers.pop(then)

        session("asr-silent", "tts_stop", "start")
        vd.wait_signals(bus, 2, timeout=5)
        session("asr-stopped", "asr_stop", "stopped start")
        time.sleep(2)
        for name, limit in (("start", ALLOWED_S), ("start tts_stop", PROMPT_S), ("stopped start", PROMPT_S),
                            ("stopped start asr_stop", PROMPT_S)):
            seconds, reply = answers.get(name, (None, "no answer"))
            within = seconds is not None and seconds < limit
            record.append(f"{name}: {reply}, within {limit} s: {within} ({seconds:.1f} s)" if seconds is not None
                          else f"{name}: no answer within 30 s")
    finally:
        proc.kill()
        proc.wait()
        shutil.rmtree(ctrl, ignore_errors=True)
    return record


def summary(record):
    return "; ".join(l for l in record if l.startswith(("== the ASR servers:", "start", "stopped")))


def check(record):
    text = "\n".join(record)
    problems = []
    if "== the ASR servers: silent: True" not in text:
        problems.append("the test's ASR servers weren't silent (connections were answered or refused)")
    for name, reply in (("start", "boolean false"), ("start tts_stop", "boolean true"),
                        ("stopped start", "boolean false"), ("stopped start asr_stop", "boolean true")):
        line = next((l for l in record if l.startswith(name + ":")), "")
        if not line.startswith(f"{name}: {reply},") or "s: True" not in line:
            problems.append(f"{name} wasn't answered {reply} in time: {line[:160] or 'no answer'}")
    if 'asr_event(string "asr-silent"' not in text:
        problems.append("no asr_event for the failed session")
    if 'asr_event(string "asr-stopped"' in text:
        problems.append("an asr_event for the session asr_stop dropped")
    if "== daemon still running: True" not in text:
        problems.append("the daemon died")
    return problems


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "serve-silent":
    serve_silent(sys.argv[2])
