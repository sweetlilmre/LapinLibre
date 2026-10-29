# SPDX-License-Identifier: MIT
"""voice-daemon: a speech server that never answers a connection.

voice.conf (the scenario's own) puts the French speech server on
192.0.2.65. Here, inside the daemon's network namespace, that address leads
nowhere (voice_unanswered's silent servers: a dummy device that drops every
packet, or a full accept queue), as for a speech server whose host is down
or behind a firewall that drops packets. A refused connection would fail at
once.

A tts_speak in French is sent. What must hold: it is accepted, the fetch is
given up after the connect timeout (3 s; at most ALLOWED_S under qemu) and
tts_event reports the failure (code 2), not an end of speech (4); then the
daemon takes a new text (not "already speaking"), which fails the same way.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from aggressive import voice_unanswered as vu  # noqa: E402
from scenarios import voice_daemon as vd  # noqa: E402

DAEMON = vd.DAEMON
DEVICES = vd.DEVICES
REGULAR_FILE_DEVICES = vd.REGULAR_FILE_DEVICES
FAKE_IOCTL_TYPES = vd.FAKE_IOCTL_TYPES
sandbox_setup = vd.sandbox_setup

# The connect timeout is 3 s; qemu-user adds the child's start-up.
MIN_S = 2.5
ALLOWED_S = 10

EXPECTED_WARNINGS = vd.EXPECTED_WARNINGS + [
    r"^TTS: fetching the speech failed: .* \(curl error 28\)$",
]


def speak(bus, uid, count):
    """tts_speak in French; (its answer, the seconds until signal number `count`, the last signal)."""
    t = time.time()
    answer = vd.call(bus, "tts_speak", f"string:{uid}", "string:bonjour", "string:fr-FR", "int32:0")
    deadline = t + 30
    while time.time() < deadline and len(bus.signals()) < count:
        time.sleep(0.05)
    seconds = time.time() - t
    signals = bus.signals()
    return answer, seconds, signals[-1] if len(signals) >= count else "none"


def drive(d, bus):
    record = [f"== registered: {bus.wait_for_name(vd.SERVICE)}"]
    vd.wait_signals(bus, 1)
    ctrl = tempfile.mkdtemp(prefix="karotz-voice-silent-")
    proc = subprocess.Popen(["nsenter", "-t", str(d.proc.pid), "-U", "-n", "--preserve-credentials", "--",
                             sys.executable, "-I", os.path.abspath(vu.__file__), "serve-silent", ctrl],
                            stdout=subprocess.DEVNULL, stderr=open(os.path.join(ctrl, "err"), "w"))
    try:
        deadline = time.time() + 30
        while time.time() < deadline and not os.path.exists(os.path.join(ctrl, "ready")):
            time.sleep(0.1)
        ready = open(os.path.join(ctrl, "ready")).read().strip() if os.path.exists(os.path.join(ctrl, "ready")) else "not ready"
        record.append(f"== the speech server: {ready}")
        for n, uid in ((2, "t-silent"), (3, "t-again")):
            answer, seconds, signal = speak(bus, uid, n)
            record.append(f"{uid}: {answer}")
            within = MIN_S <= seconds < ALLOWED_S
            record.append(f"{uid}: ended in {MIN_S} to {ALLOWED_S} s: {within} ({seconds:.1f} s), signal {signal}")
    finally:
        proc.kill()
        proc.wait()
        shutil.rmtree(ctrl, ignore_errors=True)
    return record


def summary(record):
    return "; ".join(l for l in record if l.startswith("== the speech server:") or " ended " in l)


def check(record):
    text = "\n".join(record)
    problems = []
    if "== the speech server: silent: True" not in text:
        problems.append("the test's speech server wasn't silent (connections were answered or refused)")
    for uid in ("t-silent", "t-again"):
        if not any(l.startswith(f"{uid}: tts_speak") and l.endswith("boolean true") for l in record):
            problems.append(f"{uid}: tts_speak wasn't accepted")
        line = next((l for l in record if l.startswith(f"{uid}: ended")), "")
        if ": True" not in line:
            problems.append(f"{uid}: not given up within the time: {line or 'no line'}")
        if f'tts_event(string "{uid}", int32 2)' not in line:
            problems.append(f"{uid}: no tts_event with the failure code 2: {line or 'no line'}")
    if "== daemon still running: True" not in text:
        problems.append("the daemon died")
    return problems
