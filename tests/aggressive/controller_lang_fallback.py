# SPDX-License-Identifier: MIT
"""controller: a language file without the "not recognized" sounds.

The platform sets French (fr-FR), and the sandbox's fr-FR file lacks the
three keys the controller plays when it doesn't understand: an unknown RFID
tag (tag_unknown), a speech result with <nomatch>
(speech_no_match) and one naming no application
(speech_not_understood). A key missing from the language's file is taken from
the default one (en-US). What must hold: each case plays the English sound,
nothing is ever played with an empty path, and the controller keeps
running.
"""

from pathlib import Path

from scenarios import controller_platform as base

DAEMON = "controller"
DEVICES = []
normalise_record = base.normalise_record

DROPPED = ("tag_unknown", "speech_no_match", "speech_not_understood")
ENGLISH = {
    "unknown tag": "/usr/karotz/sounds/en_tag_unknown.mp3",
    "nomatch": "/usr/karotz/sounds/en_speech_not_understood.mp3",
}


def sandbox_setup(workdir, sysroot):
    setup = base.sandbox_setup(workdir, sysroot)
    w = Path(workdir)
    original = Path(sysroot) / "usr" / "karotz" / "res" / "lang" / "fr-FR"
    kept = [line for line in original.read_text(encoding="utf-8").splitlines(keepends=True)
            if line.split("=", 1)[0].strip() not in DROPPED]
    (w / "fr-FR").write_text("".join(kept), encoding="utf-8")
    # Over the sysroot's fr-FR, in the sandbox's mount namespace.
    return f"{base.tool('mount')} --bind {w / 'fr-FR'} {original}; " + setup


def trigger(platform, p, fake, step):
    platform.send(p, 'id: "p-stop" interactiveId: "ia-1" interactiveMode { action: STOP }')
    step("interactive mode stopped (available)", 'correlationId: "p-stop"', settle=3.0)
    fake.emit("Rfid", "tag_in", "ssiiii", "e004010012345678", "content", 3, 1, 4, 2)
    step("unknown tag", "tag_unknown", settle=2.0, timeout=20)
    fake.emit("Voice", "asr_event", "sssi", "asr-1", "<nomatch>", "", 50)
    step("nomatch", "speech_not_understood", settle=2.0, timeout=20)
    fake.emit("Voice", "asr_event", "sssi", "asr-2", "hello", "<par>x</par>", 50)
    step("no application named", "speech_not_understood", settle=2.0, timeout=20)


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
    for title, sound in [("unknown tag", ENGLISH["unknown tag"]), ("nomatch", ENGLISH["nomatch"]),
                         ("no application named", ENGLISH["nomatch"])]:
        plays = [l for l in section(record, title) if l.startswith("call Multimedia play(")]
        if not any(f'"{sound}"' in l for l in plays):
            problems.append(f"{title}: {sound} not played (plays: {plays})")
    empty = [l for l in record if l.startswith('call Multimedia play("", "", ')]
    if empty:
        problems.append(f"played with an empty path: {empty}")
    return problems
