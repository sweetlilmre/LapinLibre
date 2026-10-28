# SPDX-License-Identifier: MIT
"""multimedia-daemon: a command for a player that no longer reads its FIFO.

karotz-player holds its command FIFO (/tmp/karotz-player.ctrl) open while it
runs and exits after its last item; the daemon learns of the exit from a
GLib child watch, in the main loop. A play while the player is still thought
to be running is sent to it as a loadfile through the FIFO. If the player has
let go of the FIFO by then, an open for writing that waits for a reader waits
forever: the watch that would reap the player can't run, and the daemon stops
answering D-Bus (on the rabbit: a zombie player, and a controller whose calls
hang).

Here the FIFO is a real one and karotz-player a stub. For a URL holding
"slow-exit" it holds the FIFO for 1 s, lets go of it and exits 2 s later; for
"deaf" it never opens the FIFO and sleeps; otherwise it holds the FIFO and
sleeps. Steps: a slow-exit play, and 1.5 s later a second play (the player
alive, the FIFO without a reader, then the player gone); a third play, a
loadfile to the new player; a stop; then a deaf player and a play sent to it.

What must hold: every play and stop is answered true; the second play within
6 s, by a new player (the daemon's log says the last one had ended before its
command); the play sent to the deaf player within 6 s (the daemon gives up
on the FIFO after 3 s); and the daemon keeps running.
"""

import os
import re
import time
from pathlib import Path

from scenarios import multimedia_daemon as md

DAEMON = md.DAEMON
DEVICES = md.DEVICES
REGULAR_FILE_DEVICES = md.REGULAR_FILE_DEVICES
FAKE_IOCTL_TYPES = md.FAKE_IOCTL_TYPES

PLAYER = """#!/bin/sh
case "$*" in
*slow-exit*) exec 3<>/tmp/karotz-player.ctrl; /bin/sleep 1; exec 3>&-; exec /bin/sleep 2;;
*deaf*) exec /bin/sleep 300;;
*) exec 3<>/tmp/karotz-player.ctrl; exec /bin/sleep 300;;
esac
"""


EXPECTED_WARNINGS = md.EXPECTED_WARNINGS + [
    r"^Multimedia: player \d+ had ended before its command$",
    r"^Multimedia: send_player_cmd - no player reads /tmp/karotz-player\.ctrl: ",
]


def call(bus, record, method, *args, settle=0.5):
    started = time.time()
    out = bus.send("--reply-timeout=15000", f"--dest={md.SERVICE}", md.PATH, f"{md.IFACE}.{method}", *args)
    took = time.time() - started
    record.append(f"{method}({', '.join(args)}) -> {md.reply(out)}")
    record.append(f"   within 6 s: {took < 6}")
    time.sleep(settle)


def drive(d, bus):
    md.prepare(d)
    stubs = Path(d.log.name).parent / "stub-bin"
    (stubs / "karotz-player").write_text(PLAYER)
    (stubs / "karotz-player").chmod(0o755)
    os.unlink(md.PLAYER_CTRL)
    os.mkfifo(md.PLAYER_CTRL, 0o666)

    record = [f"== registered: {bus.wait_for_name(md.SERVICE)}"]
    try:
        time.sleep(3)
        call(bus, record, "play", "string:p1", "string:/tmp/slow-exit.mp3", "int32:0", settle=1.5)
        call(bus, record, "play", "string:p2", "string:/tmp/next.mp3", "int32:0")
        call(bus, record, "play", "string:p3", "string:/tmp/again.mp3", "int32:0")
        call(bus, record, "stop", "string:p3", "int32:0", settle=1)
        call(bus, record, "play", "string:p4", "string:/tmp/deaf.mp3", "int32:0", settle=1)
        call(bus, record, "play", "string:p5", "string:/tmp/after-deaf.mp3", "int32:0")
        call(bus, record, "stop", "string:p5", "int32:0", settle=1)
        record.append(f"== running before the stop: {d.proc.poll() is None}")
        # The daemon's output reaches daemon.log once its buffer is flushed: stop it before reading.
        d.stop()
    finally:
        # Other scenarios read the control file: leave a regular one, as prepare() does.
        if os.path.lexists(md.PLAYER_CTRL):
            os.unlink(md.PLAYER_CTRL)
        open(md.PLAYER_CTRL, "w").close()
    log = Path(d.workdir) / "daemon.log"
    record.append("== the daemon's log: player lines")
    if log.exists():
        for line in log.read_text(errors="replace").splitlines():
            if re.search(r"had ended before its command", line):
                record.append("log " + line.strip()[:200])
    return record


def check(record):
    text = "\n".join(record)
    problems = []
    for line in (l for l in record if " -> " in l):
        if not line.endswith("-> boolean true"):
            problems.append(f"not answered true: {line}")
    for i, line in enumerate(record):
        if line.startswith("   within 6 s: False"):
            problems.append(f"answered late: {record[i - 1]}")
    if "== running before the stop: True" not in text:
        problems.append("the daemon died")
    if "had ended before its command" not in text:
        problems.append("the second play didn't find the ended player (no 'had ended before its command')")
    return problems
