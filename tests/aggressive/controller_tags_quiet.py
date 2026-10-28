# SPDX-License-Identifier: MIT
"""controller: a tag on the nose gets no feedback from the controller.

The tag listener gives every tag's light and sound and asks the controller
(has_tag_app, start_tag_app) only for an application's tag. Logged in and
available, the controller is sent a tag that the web API's store has
(/usr/etc/conf/web/tags), one in neither, and the configured application's.
What must hold: for none of them does the controller play a sound, set the
LED or move the ears; the unknown tags go to the platform as rfidCallback,
the application's doesn't and its start comes only from start_tag_app; and
the controller keeps running.
"""

from pathlib import Path

from scenarios import controller_platform as base

DAEMON = "controller"
DEVICES = []
normalise_record = base.normalise_record

KNOWN = "E0040100A1B2C3D4"
UNKNOWN = "E004010012345678"
CONFIGURED = "e0040100c0ffee42"


def sandbox_setup(workdir, sysroot):
    setup = base.sandbox_setup(workdir, sysroot)
    store = Path(workdir) / "conf-usr" / "web"
    store.mkdir(parents=True, exist_ok=True)
    (store / "tags").write_text(f"# the web API's tags\n{KNOWN}\t1\t4\t5\t-1\tdoor\tleds\tcolor=FF0000\t\n",
                                encoding="utf-8")
    return setup


def trigger(platform, p, fake, step):
    platform.send(p, 'id: "p-stop" interactiveId: "ia-1" interactiveMode { action: STOP }')
    step("interactive mode stopped (available)", 'correlationId: "p-stop"', settle=3.0)
    for title, uid in (("the web API's tag", KNOWN), ("unknown tag", UNKNOWN)):
        fake.emit("Rfid", "tag_in", "ssiiii", uid, "content", 3, 1, 4, 2)
        step(title, "rfidCallback", settle=3.0, timeout=20)
    fake.emit("Rfid", "tag_in", "ssiiii", CONFIGURED, "content", 3, 1, 4, 2)
    # Nothing is expected from the signal alone, so the step only waits for the controller to
    # be quiet.
    step("the application's tag, signal only", settle=4.0, timeout=20)
    fake.emit("Rfid", "tag_in", "ssiiii", CONFIGURED, "content", 3, 1, 4, 2)
    base.ask_tag(fake.bus, CONFIGURED)
    step("the application's tag, asked", "vm command", settle=3.0, timeout=30)


def drive(d, bus):
    return base.drive(d, bus, trigger=trigger, when="interactive")


def section(record, title):
    """The lines of step `title` (up to the next step)."""
    lines, inside = [], False
    for line in record:
        if line.startswith("== "):
            inside = line == f"== {title}"
            continue
        if inside:
            lines.append(line)
    return lines


def check(record):
    problems = []
    if "== daemon still running: True" not in record:
        problems.append("the controller didn't survive")
    titles = ("the web API's tag", "unknown tag", "the application's tag, signal only", "the application's tag, asked")
    for title in titles:
        lines = section(record, title)
        feedback = [l for l in lines if l.startswith(("call Multimedia play(", "call Led ", "call Ears "))]
        if feedback:
            problems.append(f"{title}: the controller gave feedback ({feedback})")
    for title in titles[:2]:
        if not any("rfidCallback" in l for l in section(record, title)):
            problems.append(f"{title}: no rfidCallback to the platform")
    for title in titles[2:]:
        if any("rfidCallback" in l for l in section(record, title)):
            problems.append(f"{title}: an rfidCallback for a hosted application's tag")
    if any(l.startswith("vm command") for l in section(record, titles[2])):
        problems.append("the tag's signal alone started the application")
    if not any(l.startswith("vm command") for l in section(record, titles[3])):
        problems.append("start_tag_app didn't start the application")
    return problems
