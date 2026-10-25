# SPDX-License-Identifier: MIT
"""controller: an update check with the installed rootfs_version missing.

Both version files download, but /etc/conf/rootfs_version is absent, so the
check stops before comparing anything. The check opens the four version
files; whichever opened must be closed again. A controller makes one check
per process (when it goes to sleep; on waking up it exits and immortaldog
starts a new one), so the controller's open descriptors are counted, from
outside the sandbox, just before going to sleep and just after the check.
What must hold: the count doesn't grow, no reboot, the controller keeps
running.
"""

import os
from pathlib import Path

from aggressive import _update as u

DAEMON, DEVICES, normalise_record = u.DAEMON, u.DEVICES, u.normalise_record
_setup = u.make_sandbox_setup({"rootfs_version": u.published("13.01.01.00"),
                               "yaffs_version": u.published("13.01.01.00")})


def sandbox_setup(workdir, sysroot):
    setup = _setup(workdir, sysroot)
    (Path(workdir) / "conf-usr" / "rootfs_version").unlink()
    return setup


def open_files(root_pid):
    """The descriptor count of the controller: the descendant of root_pid
    (outside the sandbox's PID namespace) whose command line names it."""
    parents, cmdlines = {}, {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            stat = open(f"/proc/{entry}/stat").read()
            parents[int(entry)] = int(stat[stat.rindex(")") + 2:].split()[1])
            cmdlines[int(entry)] = open(f"/proc/{entry}/cmdline", "rb").read().replace(b"\0", b" ").decode(errors="replace")
        except (OSError, ValueError):
            pass

    def under(pid):
        while pid in parents and pid > 1:
            if pid == root_pid:
                return True
            pid = parents[pid]
        return False
    # The sandbox's shell wrapper has the same command line; the program is
    # the innermost match.
    pids = [p for p, c in cmdlines.items() if under(p) and c.rstrip().endswith("/controller")]
    pids = [p for p in pids if not any(parents.get(q) == p for q in pids)]
    return len(os.listdir(f"/proc/{pids[0]}/fd")) if len(pids) == 1 else -1


def drive(d, bus):
    def trigger(platform, p, fake, step):
        platform.send(p, 'id: "p-stop" interactiveId: "ia-1" interactiveMode { action: STOP }')
        step("interactive mode stopped (available)", 'correlationId: "p-stop"', settle=3.0)
        step(f"open files before the check: {open_files(d.proc.pid)}", settle=0.5)
        fake.emit("Scheduler", "schedule_event", "si", "E_STATUS_KAROTZ_SLEEP", 0)
        step("time to sleep: the update check", "update/", settle=5.0, timeout=30)
        step(f"open files after the check: {open_files(d.proc.pid)}", settle=0.5)
    return u.base.drive(d, bus, trigger=trigger, when="interactive")


def check(record):
    problems = u.common_problems(record)
    if u.rebooted(record):
        problems.append("rebooted although the installed rootfs_version was missing")
    counts = {}
    for line in record:
        for when in ("before", "after"):
            prefix = f"== open files {when} the check: "
            if line.startswith(prefix):
                counts[when] = int(line[len(prefix):])
    if counts.get("before", -1) < 0 or counts.get("after", -1) < 0:
        problems.append(f"the controller's descriptors couldn't be counted ({counts})")
    elif counts["after"] > counts["before"]:
        problems.append(f"the check left {counts['after'] - counts['before']} more descriptors open "
                        f"({counts['before']} before, {counts['after']} after)")
    return problems
