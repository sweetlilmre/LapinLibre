# SPDX-License-Identifier: MIT
"""controller: a hostile platform's configuration, schedules, tags and calls.

After controller_platform's set-up
(logged in, configured, in interactive mode) the platform sends:
  - a configuration whose application keys hold shell syntax, "..", spaces,
    300 characters and a 300-character k2k key, an instance name with a
    newline, and a 200-character language name;
  - schedules whose time zone matches many lines of /etc/timezone.list, none,
    or is 300 characters of shell syntax;
then the fake daemons send an RFID tag whose content has spaces and a
newline, a tag for the instance with the newline, and an incoming k2k call.
What must hold: the controller keeps running and answers a ping; no unsafe
key reaches a download, an unzip or a VM command; every VM command line is
one well-formed line.
"""

import re

from scenarios import controller_platform as base

DAEMON = "controller"
DEVICES = []
sandbox_setup = base.sandbox_setup
normalise_record = base.normalise_record

UNSAFE_KEYS = ["x;touch /tmp/pwned", "../../etc", "a b", "q" * 300, "$(touch /tmp/pwned2)"]
LONG_K2K = "k2k" + "b" * 300


def _quote(text):
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def trigger(platform, p, fake, step):
    apps = "".join(f'configapp {{ apiKey: "{_quote(k)}" version: "1" name: "Bad" deployment: "hosted" }} '
                   for k in UNSAFE_KEYS)
    apps += f'configapp {{ apiKey: "{LONG_K2K}" version: "1" name: "K2k" deployment: "hosted" }} '
    apps += ('configapp { apiKey: "app-ok" version: "1" name: "Ok" deployment: "hosted" '
             '  configInstance { name: "inst1" uuid: "cfg-1" rfid: "e0040100c0ffee42" } } ')
    apps += ('configapp { apiKey: "app-nl" version: "1" name: "Nl" deployment: "hosted" '
             '  configInstance { name: "i\\nevil-app --x" uuid: "cfg-nl" rfid: "d002000000000001" } } ')
    platform.send(p, f'id: "p-hostile-config" config {{ {apps} lang: "{"L" * 200}" playsounds: false }}')
    step("hostile configuration", "vm start", settle=3.0, timeout=60)

    for i, zone in enumerate(["Europe", "a", "", "$(touch /tmp/pwned3)" + "z" * 300, "No/Such_Zone"]):
        platform.send(p, f'id: "p-tz{i}" scheduleConfig {{ time: 1700000000000 gmt: "{_quote(zone)}" }}')
        step(f"time zone {zone[:20]!r}", f'correlationId: "p-tz{i}"', timeout=20)

    # Tags start applications only when the rabbit is available, not in
    # interactive mode (where they go to the platform).
    platform.send(p, 'id: "p-stop" interactiveId: "ia-1" interactiveMode { action: STOP }')
    step("interactive mode stopped (available)", 'correlationId: "p-stop"', settle=3.0)
    fake.emit("Rfid", "tag_in", "ssiiii", "e0040100c0ffee42", "con tent\nwith newline", 3, 1, 4, 2)
    step("a tag whose content has spaces and a newline", settle=3.0)
    fake.emit("Rfid", "tag_in", "ssiiii", "d002000000000001", "c", 3, 1, 4, 2)
    step("a tag for the instance with a newline", settle=3.0)
    fake.emit("K2k", "incoming_call", "s", "sip:bob@example")
    step("an incoming k2k call (300-character key)", settle=3.0)
    platform.send(p, 'id: "p-ping-after" ping { }')
    step("still answering", 'correlationId: "p-ping-after"', timeout=20)


def drive(d, bus):
    return base.drive(d, bus, trigger=trigger, when="interactive")


def check(record):
    problems = []
    text = "\n".join(record)
    if "== daemon still running: True" not in record:
        problems.append("the controller didn't survive")
    if "== the sandbox ended with the controller" in record:
        problems.append("the controller's sandbox ended")
    if 'correlationId: "p-ping-after"' not in text:
        problems.append("no answer to the ping after the hostile input")
    for key in UNSAFE_KEYS:
        for line in record:
            if line.startswith(("http GET", "system", "vm command")) and key in line:
                problems.append(f"unsafe key reached: {line[:120]}")
    for line in record:
        if line.startswith("vm command"):
            if "evil-app" in line or "\n" in line:
                problems.append(f"injected VM command: {line[:120]}")
            if LONG_K2K in line:
                problems.append("the 300-character k2k key reached the VM")
        if line.startswith("system") and "pwned" in line:
            problems.append(f"shell syntax reached a command: {line[:120]}")
    if not any(re.match(r"^vm command app-ok RFID;e0040100c0ffee42;.*con_tent_with_newline", line) for line in record):
        problems.append("the tag with spaces didn't start app-ok with a sanitised trigger")
    return problems
