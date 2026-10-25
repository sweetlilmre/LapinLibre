# SPDX-License-Identifier: MIT
"""multimedia-daemon: failures around its child processes and the recorder.

The daemon starts mpd (every second while it can't connect), karotz-player (for a
play) and flac (for a recording, through popen()). Here mpd and karotz-player are
missing from PATH, and flac is a stub that takes its input and exits 0, or
exits 3 at once while the file flac-fail exists in the run's directory.

What must hold:
 - a child whose exec failed ends: only one multimedia-daemon process is left
   running, after several mpd starts and an karotz-player play;
 - a recording started with an empty id is stopped like any other and
   reported (recordFinished "" DONE), and the next one is reported once;
 - a recording whose encoder fails is reported as failed (recordFinished
   ERROR), once;
 - recordings made after a play has ended get flac's exit status: a good
   one is reported done, one whose encoder fails after taking its input is
   reported as failed (each once);
 - 30 plays in a row whose karotz-player ends at once (each child's end arriving
   while the daemon is busy) are all answered, and the daemon survives;
 - a record with /dev/audio missing is refused (false) and reported never;
 - the daemon is still running at the end.
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

DONE, ERROR = 4, 2


def call(bus, record, method, *args, settle=1.5):
    out = bus.send("--reply-timeout=20000", f"--dest={md.SERVICE}", md.PATH, f"{md.IFACE}.{method}", *args)
    record.append(f"{method}({', '.join(args)}) -> {md.reply(out)}")
    time.sleep(settle)


def daemon_processes(root_pid):
    """The running (not zombie) processes under root_pid whose command line
    names the daemon: under qemu-arm a forked child is another qemu process
    with the same command line."""
    children = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            stat = Path(f"/proc/{entry}/stat").read_text()
            cmdline = Path(f"/proc/{entry}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            continue
        # pid (comm) state ppid ...: comm may hold spaces, so split after it.
        state, ppid = stat.rsplit(")", 1)[1].split()[:2]
        children.setdefault(int(ppid), []).append((int(entry), state, cmdline))
    found, todo = [], [root_pid]
    while todo:
        for pid, state, cmdline in children.get(todo.pop(), []):
            todo.append(pid)
            # The program itself, not the sandbox's sh -c wrapper that names it.
            program = os.path.basename(cmdline.split(" ", 1)[0])
            if state != "Z" and program in ("qemu-arm", DAEMON) and DAEMON in cmdline:
                found.append(f"{state} {cmdline[:150]}")
    return found


def drive(d, bus):
    workdir = Path(d.workdir)
    stubs = Path(os.path.dirname(d.log.name)) / "stub-bin"
    for name in ("mpd", "karotz-player"):
        if (stubs / name).exists():
            (stubs / name).unlink()
    flac = stubs / "flac"
    flac.write_text(f"#!/bin/sh\n[ -e {workdir / 'flac-fail'} ] && exit 3\n"
                    f"[ -e {workdir / 'flac-fail-late'} ] && {{ /bin/cat > /dev/null; exit 3; }}\n"
                    "exec /bin/cat > /dev/null\n")
    flac.chmod(0o755)
    if os.path.lexists(md.PLAYER_CTRL):
        os.unlink(md.PLAYER_CTRL)
    open(md.PLAYER_CTRL, "w").close()  # a regular file: the daemon's fopen(..., "w") doesn't block
    d.write("audio", b"\x00\x80" * 512 + bytes(range(256)) * 4)

    record = [f"== registered: {bus.wait_for_name(md.SERVICE)}"]
    time.sleep(6)  # the daemon tries to start mpd every second
    found = daemon_processes(d.proc.pid)
    record.append(f"== daemon processes after mpd starts: {len(found)}")
    record.extend("  " + p for p in found)

    record.append("== recordings")
    call(bus, record, "record", "string:", "string:/tmp/r0.flac", "int32:0")
    call(bus, record, "stop", "string:", "int32:0")
    call(bus, record, "record", "string:r1", "string:/tmp/r1.flac", "int32:0")
    call(bus, record, "stop", "string:r1", "int32:0")
    (workdir / "flac-fail").touch()
    call(bus, record, "record", "string:r2", "string:/tmp/r2.flac", "int32:0", settle=3)
    call(bus, record, "stop", "string:r2", "int32:0")
    (workdir / "flac-fail").unlink()

    record.append("== karotz-player missing")
    call(bus, record, "play", "string:p1", "string:/tmp/none.mp3", "int32:0", settle=3)
    found = daemon_processes(d.proc.pid)
    record.append(f"== daemon processes after the play: {len(found)}")
    record.extend("  " + p for p in found)

    # After a play, flac's exit status still reaches pclose(): a good
    # recording is done, and one whose encoder takes everything and then
    # fails is an error (when SIGCHLD was left ignored after a play, the
    # kernel reaped flac, its status was lost and r5 was reported done).
    record.append("== recordings after a play")
    call(bus, record, "record", "string:r4", "string:/tmp/r4.flac", "int32:0")
    call(bus, record, "stop", "string:r4", "int32:0")
    (workdir / "flac-fail-late").touch()
    call(bus, record, "record", "string:r5", "string:/tmp/r5.flac", "int32:0")
    call(bus, record, "stop", "string:r5", "int32:0")
    (workdir / "flac-fail-late").unlink()

    # Plays in a row whose karotz-player ends at once: each child's end arrives
    # while the daemon is busy (logging, answering on D-Bus). Each must be
    # answered, and the daemon must survive.
    record.append("== plays in a row, karotz-player missing")
    for i in range(30):
        call(bus, record, "play", f"string:q{i}", "string:/tmp/none.mp3", "int32:0", settle=0.1)
    time.sleep(2)

    os.unlink(d.fifos["audio"])
    call(bus, record, "record", "string:r3", "string:/tmp/r3.flac", "int32:0")
    return record


def check(record):
    text = "\n".join(record)
    problems = []
    for line in record:
        m = re.match(r"== daemon processes (.*): (\d+)$", line)
        if m and m.group(2) != "1":
            problems.append(f"{m.group(2)} daemon processes {m.group(1)} (a failed exec's child kept running?)")
    finished = re.findall(r'multi_recordfinished\(string "([^"]*)", int32 (\d+)\)', text)
    expect = {"": DONE, "r1": DONE, "r2": ERROR, "r4": DONE, "r5": ERROR}
    for ident, code in expect.items():
        got = [int(c) for i, c in finished if i == ident]
        if got != [code]:
            problems.append(f'recordFinished for "{ident}": {got or "none"}, expected once with {code}')
    if any(i == "r3" for i, _ in finished):
        problems.append("recordFinished for r3, which was recorded with /dev/audio missing")
    if "record(string:r3, string:/tmp/r3.flac, int32:0) -> boolean false" not in text:
        problems.append("record with /dev/audio missing wasn't refused")
    unanswered = [l for l in record if re.match(r"play\(string:q\d+, ", l) and "-> boolean" not in l]
    if unanswered:
        problems.append(f"{len(unanswered)} of the plays in a row got no answer, first: {unanswered[0]}")
    if "== daemon still running: True" not in text:
        problems.append("the daemon died")
    return problems
