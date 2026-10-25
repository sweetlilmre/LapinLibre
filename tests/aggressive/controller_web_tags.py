# SPDX-License-Identifier: MIT
"""controller: a tag the web API knows gets no "not recognised" sound.

The web API keeps its tags in /usr/etc/conf/web/tags (one line per tag: its
id, a tab, then the tag's fields), and its own tag listener acts on them. A
tag laid on the nose that no application owns makes the controller play the
language's not-recognised sound, unless the tag is in that store. What
must hold: the store's tag plays nothing, a tag in neither plays the sound,
a tag whose id only starts a stored id is still unknown, and the controller
keeps running.
"""

from pathlib import Path

from scenarios import controller_platform as base

DAEMON = "controller"
DEVICES = []
normalise_record = base.normalise_record

KNOWN = "E0040100A1B2C3D4"
UNKNOWN = "E004010012345678"
# The language's not-recognised sound (the scenario's platform sets French).
SOUND = "_tag_unknown.mp3"


def sandbox_setup(workdir, sysroot):
    setup = base.sandbox_setup(workdir, sysroot)
    store = Path(workdir) / "conf-usr" / "web"
    store.mkdir(parents=True, exist_ok=True)
    (store / "tags").write_text(f"# the web API's tags\n{KNOWN}\t1\t4\t5\t-1\tdoor\tleds\tcolor=FF0000\t\n"
                                f"{UNKNOWN[:12]}\t1\t4\t0\t-1\tshort\t\t\t\n", encoding="utf-8")
    return setup


def trigger(platform, p, fake, step):
    platform.send(p, 'id: "p-stop" interactiveId: "ia-1" interactiveMode { action: STOP }')
    step("interactive mode stopped (available)", 'correlationId: "p-stop"', settle=3.0)
    fake.emit("Rfid", "tag_in", "ssiiii", KNOWN, "content", 3, 1, 4, 2)
    # Nothing is expected, so the step only waits for the controller to be quiet.
    step("the web API's tag", settle=4.0, timeout=20)
    fake.emit("Rfid", "tag_in", "ssiiii", UNKNOWN, "content", 3, 1, 4, 2)
    step("unknown tag", "tag_unknown", settle=2.0, timeout=20)


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
    plays = [l for l in section(record, "the web API's tag") if l.startswith("call Multimedia play(")]
    if any(SOUND in l for l in plays):
        problems.append(f"the web API's tag: the not-recognised sound was played ({plays})")
    plays = [l for l in section(record, "unknown tag") if l.startswith("call Multimedia play(")]
    if not any(SOUND in l for l in plays):
        problems.append(f"unknown tag: {SOUND} not played (plays: {plays})")
    return problems
