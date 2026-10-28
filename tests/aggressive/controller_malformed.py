# SPDX-License-Identifier: MIT
"""controller: messages that don't parse.

The set-up is controller_platform's, up to interactive mode (ia-1). Then,
on the platform's connection:

- a tts request without its (required) action;
- an LED request cut off mid-field (its last byte dropped);
- an LED request without its (required) id;
- a reply (a correlation id) whose Event lacks its required code;
- a good LED request.

Then ia-1 is stopped, an application connection takes interactive mode
(u-1), and the platform sends a Karotz message response (what the
controller relays to the application) whose message lacks its required
header, as the answer to the application's request a-q1.

What must hold: none of the malformed ones is acted on (no call to a
daemon, nothing relayed as it is); each gets a Response ERROR whose
description says what was wrong and names its fields (never their values):
the requests under their id; the one without an id uncorrelated; the reply
under the reply's own id; and for the relayed reply, the application also
gets ERROR under the correlation id it waits for (a-q1). Each is logged as
malformed. The good request after them is handled (the connection stayed
in step).
"""

import re
from pathlib import Path

from scenarios import controller_platform as base

DAEMON = "controller"
DEVICES = []
sandbox_setup = base.sandbox_setup
normalise_record = base.normalise_record

# (title, message, bytes cut off its end, correlation id of the ERROR ("" uncorrelated),
#  what its description must say)
CASES = (
    ("tts without an action", 'id: "p-tts-noact" interactiveId: "ia-1" tts { text: "no action" }', 0,
     "p-tts-noact", "malformed message (missing: tts.action; fields: interactiveId, tts)"),
    ("led cut off", 'id: "p-cut" interactiveId: "ia-1" led { light { color: "FF0000" } }', 1,
     "p-cut", "malformed message (malformed bytes; fields: interactiveId, led)"),
    ("led without an id", 'interactiveId: "ia-1" led { light { color: "00FF00" } }', 0,
     "", "malformed message (missing: id; fields: interactiveId, led)"),
    ("a reply without its code", 'id: "p-badreply" correlationId: "c-x" event { }', 0,
     "p-badreply", "malformed reply (missing: event.code; fields: event)"),
)
RELAYED = 'id: "p-kmr" correlationId: "a-q1" interactiveId: "u-1" karotzMessageResponse { karotzMessageList { } }'


EXPECTED_WARNINGS = base.EXPECTED_WARNINGS + [
    r"^malformed message \(\d+ bytes\) not handled: ",
]


def trigger(platform, p, fake, step):
    for title, text, truncate, cid, _ in CASES:
        platform.send(p, text, truncate=truncate)
        step(title, f'correlationId: "{cid}"' if cid else "malformed message", timeout=20)
    platform.send(p, 'id: "p-good" interactiveId: "ia-1" led { light { color: "0000FF" } }')
    step("a good led request after them", 'correlationId: "p-good"', timeout=20)
    # An application in interactive mode, then a malformed reply the controller would relay to it.
    platform.send(p, 'id: "p-stop" interactiveId: "ia-1" interactiveMode { action: STOP }')
    step("stop ia-1", 'correlationId: "p-stop"', "vm start", settle=2.0)
    app = platform.request("connect", address=base.CONTROLLER_ADDRESS, port=base.PORT)["conn"]
    step(f"application connection {app}", settle=0.5)
    platform.send(app, 'id: "a-start" interactiveMode { action: START interactiveId: "u-1" }')
    step("the application takes interactive mode", 'correlationId: "a-start"', settle=2.0)
    APP[:] = [app]
    platform.send(p, RELAYED)
    step("a malformed message-store answer for the application", 'correlationId: "a-q1"',
         'correlationId: "p-kmr"', timeout=20)


APP = []


def drive(d, bus):
    APP[:] = []
    record = base.drive(d, bus, trigger=trigger, when="interactive")
    record.append(f"== the application's connection: conn {APP[0] if APP else '?'}")
    # The controller's output reaches daemon.log only when its buffer is
    # flushed, at the latest when it ends: stop it before reading.
    d.stop()
    log = Path(d.workdir) / "daemon.log"
    record.append("== the controller's log: malformed messages")
    if log.exists():
        for line in log.read_text(errors="replace").splitlines():
            if "malformed message" in line and "not handled" in line:
                record.append("log " + line.strip()[:300])
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


def errors(lines):
    """The Response ERRORs among a step's messages: [(connection, correlation id or "", line)]."""
    out = []
    for line in lines:
        if line.startswith("conn ") and "code: ERROR" in line:
            m = re.search(r'correlationId: "([^"]*)"', line)
            out.append((line.split(":", 1)[0], m.group(1) if m else "", line))
    return out


def summary(record):
    return "; ".join(l[4:] for l in sections(record).get("the controller's log: malformed messages", []))


def check(record):
    problems = []
    s = sections(record)
    for title, _, _, cid, description in CASES:
        lines = s.get(title, [])
        calls = [l for l in lines if l.startswith("call ")]
        if calls:
            problems.append(f"{title}: acted on: {calls}")
        got = [e for e in errors(lines) if e[1] == cid and e[0] == "conn 1"]
        if not got:
            problems.append(f"{title}: no ERROR {'under ' + cid if cid else 'uncorrelated'} on the platform's "
                            f"connection: {errors(lines)}")
        elif f'description: "{description}"' not in got[0][2]:
            problems.append(f"{title}: description not {description!r}: {got[0][2]}")
    good = s.get("a good led request after them", [])
    if not any('correlationId: "p-good"' in l and "code: OK" in l for l in good) or \
            not any(l.startswith("call Led") and "0000FF" in l for l in good):
        problems.append("the good led request after the malformed ones wasn't handled")
    app = next((t.split("conn ", 1)[1] for t in s if t.startswith("the application's connection: conn ")), "?")
    relayed = s.get("a malformed message-store answer for the application", [])
    if not any(e[0] == f"conn {app}" and e[1] == "a-q1" and
               'description: "malformed reply from the platform (missing: karotzMessageResponse.karotzMessageList[0]'
               '.header; fields: interactiveId, karotzMessageResponse)"' in e[2] for e in errors(relayed)):
        problems.append(f"the application (conn {app}) didn't get ERROR under a-q1: {errors(relayed)}")
    if not any(e[0] == "conn 1" and e[1] == "p-kmr" for e in errors(relayed)):
        problems.append(f"the platform didn't get ERROR under its reply's id p-kmr: {errors(relayed)}")
    if any("karotzMessageResponse {" in l and l.startswith(f"conn {app}") for l in relayed):
        problems.append("the malformed message-store answer was relayed as it is")
    logs = s.get("the controller's log: malformed messages", [])
    if len(logs) != len(CASES) + 1:
        problems.append(f"{len(logs)} malformed-message warnings, expected {len(CASES) + 1}: {logs}")
    text = "\n".join(logs) + "\n" + "\n".join(l for t in s for l in s[t] if "description:" in l)
    if "no action" in text or "FF0000" in text or "00FF00" in text:
        problems.append("a warning or a description shows a field's value")
    return problems
