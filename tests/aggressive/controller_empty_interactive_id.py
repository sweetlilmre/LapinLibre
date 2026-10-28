# SPDX-License-Identifier: MIT
"""controller: interactive mode asked for with an empty interactive id.

The controller's "no application is interactive" state is an empty id, so
an id "" used to match it: a start under "" was answered OK without the
rabbit being handed over, and a stop under "" reset an idle rabbit. "" is not a valid id (the platform and
clients use UUIDs): a start under it is refused with BAD_INTERACTIVEID and
nothing else under it matches a session.

On the real controller (controller_platform's set-up, up to interactive
mode, then that session stopped so the controller is available): for the
id "" and then for "u-1", an application connection asks for interactive
mode, a button is pressed, and the application stops.

What is looked at: the reply to the start; whether the controller stops the
multimedia and TTS (as it does when it hands the rabbit to an
application); which connection the button event goes to (the
application's, or the platform's); the reply to the stop. "" must be
refused (BAD_INTERACTIVEID, nothing stopped, the button to the platform,
the stop ERROR); "u-1" gets the normal behaviour. Then the D-Bus session:
take_control gives one; release_control("") and a wrong id leave it
interactive; its own id ends it (the Karotz available again).
"""

import re

from scenarios import controller as ctl
from scenarios import controller_platform as base

DAEMON = "controller"
DEVICES = []
sandbox_setup = base.sandbox_setup
normalise_record = base.normalise_record

IDS = (("empty", ""), ("uuid", "u-1"))


EXPECTED_WARNINGS = base.EXPECTED_WARNINGS + [
    r"^interactive mode start refused: empty interactiveId$",
    r"^controller_interactive_mode_stop failed$",
]


def trigger(platform, p, fake, step):
    platform.send(p, 'id: "p-stop" interactiveId: "ia-1" interactiveMode { action: STOP }')
    step("stop ia-1: the controller is available", 'correlationId: "p-stop"', "vm start", settle=2.0)
    for label, ident in IDS:
        app = platform.request("connect", address=base.CONTROLLER_ADDRESS, port=base.PORT)["conn"]
        step(f"{label}: application connection {app}", settle=0.5)
        platform.send(app, f'id: "a-start-{label}" interactiveMode {{ action: START interactiveId: "{ident}" }}')
        step(f"{label}: start", f'correlationId: "a-start-{label}"', settle=2.0)
        fake.emit("Button", "dclick")
        step(f"{label}: button", "type: DOUBLE", settle=2.0, timeout=20)
        platform.send(app, f'id: "a-stop-{label}" interactiveId: "{ident}" interactiveMode {{ action: STOP }}')
        step(f"{label}: stop", f'correlationId: "a-stop-{label}"', settle=3.0, timeout=20)
    # The D-Bus session (take_control / release_control), now the Karotz
    # is available again: "" and a wrong id leave it alone, its own id ends it.
    sid, reply = ctl.interactive_id(BUS[0])
    DBUS.append(f"take_control(): {reply}")
    DBUS.append(ctl.call(BUS[0], "get_status"))
    for label, ident in (("empty", ""), ("wrong", "wrong-id")):
        DBUS.append(ctl.call(BUS[0], "release_control", "string:" + ident))
        step(f"dbus: release_control {label}", settle=2.0)
        DBUS.append(f"after {label}: " + ctl.call(BUS[0], "get_status"))
    DBUS.append(ctl.call(BUS[0], "release_control", "string:" + (sid or "")))
    step("dbus: release_control with its id", "vm start", settle=3.0, timeout=30)
    DBUS.append("after its id: " + ctl.call(BUS[0], "get_status"))


BUS, DBUS = [], []


def drive(d, bus):
    BUS[:], DBUS[:] = [bus], []
    record = base.drive(d, bus, trigger=trigger, when="interactive")
    record.append("== the D-Bus session")
    record.extend(DBUS)
    return record


def sections(record):
    """{title: [lines]} for the steps of the record."""
    out, title = {}, None
    for line in record:
        if line.startswith("== "):
            title = line[3:]
            out[title] = []
        elif title is not None:
            out[title].append(line)
    return out


def observe(record, label):
    s = sections(record)
    conn = next((t.rsplit(" ", 1)[1] for t in s if t.startswith(f"{label}: application connection ")), None)
    start = s.get(f"{label}: start", [])
    button = s.get(f"{label}: button", [])
    stop = s.get(f"{label}: stop", [])

    def code(lines, cid):
        for line in lines:
            if f'correlationId: "{cid}"' in line:
                m = re.search(r"code: (\w+)", line)
                return m.group(1) if m else "no code"
        return "no reply"

    to = [line.split(":", 1)[0] for line in button if "buttonCallback" in line]
    return {
        "start reply": code(start, f"a-start-{label}"),
        "multimedia and tts stopped at start": any("multimedia" in l.lower() and "stop" in l.lower() for l in start)
                                              and any("tts" in l.lower() and "stop" in l.lower() for l in start),
        "button event to": ", ".join(to) or "nowhere",
        "button to the application": f"conn {conn}" in to,
        "stop reply": code(stop, f"a-stop-{label}"),
    }


def summary(record):
    return "; ".join([f"{label}: {observe(record, label)}" for label, _ in IDS] +
                     sections(record).get("the D-Bus session", []))


# What must hold, the platform's connection being conn 1.
EXPECTED = {
    "empty": {"start reply": "BAD_INTERACTIVEID", "multimedia and tts stopped at start": False,
              "button to the application": False, "button event to": "conn 1", "stop reply": "ERROR"},
    "uuid": {"start reply": "OK", "multimedia and tts stopped at start": True, "button to the application": True,
             "stop reply": "OK"},
}


def check(record):
    problems = []
    for label, expected in EXPECTED.items():
        seen = observe(record, label)
        for key, value in expected.items():
            if seen[key] != value:
                problems.append(f'{label} id: {key} is {seen[key]!r}, expected {value!r}')
    # The D-Bus session: 21 is interactive, 16 available.
    dbus = sections(record).get("the D-Bus session", [])

    def status(prefix):
        line = next((l for l in dbus if l.startswith(prefix)), "")
        m = re.search(r"get_status\(\): int32 (\d+)", line)
        return m.group(1) if m else None
    if not any(l.startswith("take_control(): boolean true") for l in dbus):
        problems.append(f"D-Bus take_control while available didn't give a session: {dbus}")
    for prefix, expected in (("get_status", "21"), ("after empty", "21"), ("after wrong", "21"), ("after its id", "16")):
        if status(prefix) != expected:
            problems.append(f"D-Bus session, {prefix}: status {status(prefix)}, expected {expected}")
    return problems
