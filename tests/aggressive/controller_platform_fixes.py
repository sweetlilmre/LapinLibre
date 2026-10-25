# SPDX-License-Identifier: MIT
"""controller: fixes to the platform requests and status changes.

The set-up is controller_platform's, up to interactive mode (ia-1); the
fake choreography manager answers launchScript with the file asked for
(FAKE_LAUNCHSCRIPT_ECHO), so the test chooses the script path. Then:

- a tts request without its (required) action (item 21): it doesn't
  parse, so it is answered ERROR and nothing is spoken (see also
  controller_malformed);
- a k2k request with nothing in it (item 22): answered ERROR;
- choreography with a script path holding '/' (answered OK, and the VM is
  told to run it), with one without '/', and with one the VM command
  refuses (a space in the name) (item 23): both answered ERROR, nothing
  told to the VM;
- a daemon event with an empty id (a request nobody waits for) is not
  forwarded, one with an id is;
- the platform dropping the connection (item 25): the controller's log
  must not show GLib's g_io_channel_unref assertion.
"""

import os
import re
from pathlib import Path

from scenarios import controller_platform as base

DAEMON = "controller"
DEVICES = []
sandbox_setup = base.sandbox_setup
normalise_record = base.normalise_record

CHOREOGRAPHIES = (("slash", "/tmp/dance.js"), ("noslash", "dance.js"), ("unsafe", "/tmp/da nce.js"))


def trigger(platform, p, fake, step):
    platform.send(p, 'id: "p-tts-noact" interactiveId: "ia-1" tts { text: "no action" }')
    step("tts without an action", 'correlationId: "p-tts-noact"', timeout=20)
    platform.send(p, 'id: "p-k2k-empty" interactiveId: "ia-1" k2k { }')
    step("an empty k2k request", 'correlationId: "p-k2k-empty"', timeout=20)
    for label, path in CHOREOGRAPHIES:
        platform.send(p, f'id: "p-chor-{label}" interactiveId: "ia-1" choreography {{ start_file: "{path}" }}')
        step(f"choreography {label}: {path}", f'correlationId: "p-chor-{label}"', timeout=20)
    # A daemon event for a request without an id (nobody waits) isn't
    # forwarded; one with an id is.
    fake.emit("Led", "event", "si", "", 1)
    fake.emit("Led", "event", "si", "led-x", 1)
    step("daemon events with an empty id and with one", 'correlationId: "led-x"', timeout=20)
    platform.request("close", conn=p)
    step("the platform drops the connection", "<closed>", "vm start", settle=2.0, timeout=60)


def drive(d, bus):
    os.environ["FAKE_LAUNCHSCRIPT_ECHO"] = "1"
    try:
        record = base.drive(d, bus, trigger=trigger, when="interactive")
    finally:
        os.environ.pop("FAKE_LAUNCHSCRIPT_ECHO", None)
    # The controller's output reaches daemon.log only when its buffer is
    # flushed, at the latest when it ends: stop it before reading.
    d.stop()
    log = Path(d.workdir) / "daemon.log"
    record.append("== the controller's log: GLib assertions and platform connection lines")
    if log.exists():
        for line in log.read_text(errors="replace").splitlines():
            if "assertion" in line or "controller_platform_connect" in line:
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


def reply_code(lines, cid):
    for line in lines:
        if f'correlationId: "{cid}"' in line:
            m = re.search(r"code: (\w+)", line)
            return m.group(1) if m else "no code"
    return "no reply"


def summary(record):
    s = sections(record)
    tts = s.get("tts without an action", [])
    calls = [l for l in tts if l.startswith("call Voice")]
    return f"tts without an action: {reply_code(tts, 'p-tts-noact')}, daemon calls {calls}"


def check(record):
    problems = []
    s = sections(record)
    tts = s.get("tts without an action", [])
    code = reply_code(tts, "p-tts-noact")
    calls = [l for l in tts if l.startswith("call Voice")]
    if code != "ERROR" or calls:
        problems.append(f"tts without an action: answered {code}, daemon calls {calls}; expected ERROR and none")
    code = reply_code(s.get("an empty k2k request", []), "p-k2k-empty")
    if code != "ERROR":
        problems.append(f"an empty k2k request was answered {code}, expected ERROR")
    for label, path in CHOREOGRAPHIES:
        lines = s.get(f"choreography {label}: {path}", [])
        code = reply_code(lines, f"p-chor-{label}")
        told = [l for l in lines if l.startswith("vm ") and "appli_choreography" in l]
        if label == "slash":
            if code != "OK" or not told:
                problems.append(f"choreography {path}: answered {code}, VM told {told}; expected OK and the VM told")
        elif code != "ERROR" or told:
            problems.append(f"choreography {path}: answered {code}, VM told {told}; expected ERROR and nothing told")
    events = [l for l in s.get("daemon events with an empty id and with one", []) if "event {" in l]
    if not any('correlationId: "led-x"' in l for l in events):
        problems.append(f"the event with an id wasn't forwarded: {events}")
    if any('correlationId: ""' in l for l in events):
        problems.append(f"an event with an empty id was forwarded: {events}")
    unref = [l for l in s.get("the controller's log: GLib assertions and platform connection lines", [])
             if "g_io_channel_unref" in l]
    if unref:
        problems.append(f"GLib complained when the platform went offline: {unref[0]}")
    if "<closed>" not in "\n".join(s.get("the platform drops the connection", [])):
        problems.append("the platform's connection wasn't dropped (nothing to check for the unref)")
    return problems
