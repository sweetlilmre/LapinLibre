# SPDX-License-Identifier: MIT
"""multimedia-daemon: a play that ends while a recording runs.

When an karotz-player play ends, the daemon's SIGCHLD handler reaps children. If
it waited for every child, it would also wait for the recording's encoder,
which only ends when the recording is stopped from the main loop: the
daemon would hang. Here flac is a stub that reads until its input closes
(like the real encoder), and karotz-player the multimedia scenario's stub (a URL
holding "quick" plays for 1 s). A recording is started, a quick play
started and left to end, then the recording is stopped.

What must hold: the stop is answered (true), the recording is reported
done once (recordFinished DONE), a later play is answered, and the daemon
is still running at the end.
"""

import re
import time
from pathlib import Path

from scenarios import multimedia_daemon as md

DAEMON = md.DAEMON
DEVICES = md.DEVICES
REGULAR_FILE_DEVICES = md.REGULAR_FILE_DEVICES
FAKE_IOCTL_TYPES = md.FAKE_IOCTL_TYPES

DONE = 4


def call(bus, record, method, *args, settle=1.5):
    out = bus.send("--reply-timeout=15000", f"--dest={md.SERVICE}", md.PATH, f"{md.IFACE}.{method}", *args)
    record.append(f"{method}({', '.join(args)}) -> {md.reply(out)}")
    time.sleep(settle)


def drive(d, bus):
    md.prepare(d)
    stubs = Path(d.log.name).parent / "stub-bin"
    (stubs / "flac").write_text("#!/bin/sh\nexec /bin/cat > /dev/null\n")
    (stubs / "flac").chmod(0o755)

    record = [f"== registered: {bus.wait_for_name(md.SERVICE)}"]
    time.sleep(3)
    call(bus, record, "record", "string:r1", "string:/tmp/r1.flac", "int32:0")
    call(bus, record, "play", "string:p1", "string:/tmp/quick.mp3", "int32:0", settle=4)  # plays 1 s and ends
    call(bus, record, "stop", "string:r1", "int32:0")
    call(bus, record, "play", "string:p2", "string:/tmp/quick-again.mp3", "int32:0", settle=3)
    return record


def check(record):
    text = "\n".join(record)
    problems = []
    if "stop(string:r1, int32:0) -> boolean true" not in text:
        problems.append("the recording's stop wasn't answered true (the daemon hung in its SIGCHLD handler?)")
    finished = re.findall(r'multi_recordfinished\(string "r1", int32 (\d+)\)', text)
    if finished != [str(DONE)]:
        problems.append(f"recordFinished for r1: {finished or 'none'}, expected once with {DONE}")
    if "play(string:p2, string:/tmp/quick-again.mp3, int32:0) -> boolean true" not in text:
        problems.append("the play after the recording wasn't answered true")
    if "== daemon still running: True" not in text:
        problems.append("the daemon died")
    return problems
