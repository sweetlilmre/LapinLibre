# SPDX-License-Identifier: MIT
"""controller: a platform that stops answering, then the reconnection.

The set-up is controller_platform's, up to interactive mode (ia-1). Then the
platform answers nothing: the controller's pings (20 s after the connection,
then every 60 s) find it silent for more than 75 s twice, and the controller
closes the connection itself and connects again at once. Its watch on the old
connection must go with it: left behind, it polls the descriptor number the
new connection gets, and the two watches split the new connection's messages
between them ("socket error", messages lost or read from the middle).

What must hold: the controller pings, times out and connects again; after
logging in again, each of three requests on the new connection gets exactly
one answer; its log shows no socket error after the reconnection; and it
keeps running.
"""

import re
from pathlib import Path

from scenarios import controller_platform as base

DAEMON = "controller"
DEVICES = []
sandbox_setup = base.sandbox_setup
normalise_record = base.normalise_record

TOKEN = "token-reconnect"
QUERIES = ("p-info-1", "p-info-2", "p-info-3")


def trigger(platform, p, fake, step):
    # Silence: nothing is sent; the controller's pings stay unanswered.
    again = platform.request("wait_connection", timeout=240)["conn"]
    step(f"the controller connects again: {again is not None}", settle=1.0, timeout=5)
    if again is None:
        return
    platform.send(again, f'id: "p-auth-again" authenticationToken {{ token: "{TOKEN}" }}')
    step("authentication on the new connection", 'correlationId: "p-auth-again"', timeout=30)
    for q in QUERIES:
        platform.send(again, f'id: "{q}" karotzinfo_query {{ }}')
    step("three requests on the new connection", *(f'correlationId: "{q}"' for q in QUERIES), settle=3.0, timeout=60)


def drive(d, bus):
    record = base.drive(d, bus, trigger=trigger, when="interactive")
    record.append(f"== running before the stop: {d.proc.poll() is None}")
    # The controller's output reaches daemon.log once its buffer is flushed: stop it before reading.
    d.stop()
    log = Path(d.workdir) / "daemon.log"
    record.append("== the controller's log: ping, connection and socket lines")
    if log.exists():
        for line in log.read_text(errors="replace").splitlines():
            if re.search(r"ping timeout|successfully connected|socket error|closed the connection|Failed to receive",
                         line):
                record.append("log " + line.strip()[:200])
    return record


def sections(record):
    out, title = {}, None
    for line in record:
        if line.startswith("== "):
            title = line[3:]
            out[title] = []
        elif title is not None:
            out[title].append(line)
    return out


def check(record):
    problems = []
    s = sections(record)
    if "== running before the stop: True" not in record:
        problems.append("the controller didn't survive")
    log = s.get("the controller's log: ping, connection and socket lines", [])
    if not any("Platform - ping timeout" in l and "first_fail" not in l for l in log):
        problems.append(f"no ping timeout in the controller's log: {log}")
    connects = [i for i, l in enumerate(log) if "successfully connected" in l]
    if len(connects) < 2:
        problems.append(f"the controller didn't connect again: {log}")
    else:
        after = [l for l in log[connects[1]:] if "socket error" in l or "Failed to receive" in l]
        if after:
            problems.append(f"socket errors after the reconnection: {after}")
    lines = s.get("three requests on the new connection", [])
    for q in QUERIES:
        answers = [l for l in lines if f'correlationId: "{q}"' in l]
        if len(answers) != 1:
            problems.append(f"{q}: {len(answers)} answers on the new connection, expected 1")
    return problems
