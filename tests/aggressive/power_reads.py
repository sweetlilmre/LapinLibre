# SPDX-License-Identifier: MIT
"""power: only a '0' actually read from the power button driver reboots, and what follows a reboot.

/dev/rabbit_power is a regular file, empty at first: every read() returns
nothing (end of file) until the test appends a byte. power must not reboot
on a read that gave nothing (before the first byte its state is "not
pressed"), must go on reading after such reads (the stream's end-of-file
flag is cleared), must not reboot on '1', and must reboot on '0'. A reboot
is power's system("reboot"), which runs the test's reboot stub; the stub
counts its calls.

After its reboot power goes back to its loop with its state still '0', so
while no byte says '1' it runs reboot again every second (on the rabbit the
system is going down by then). The test checks that behaviour as it is: at
least two reboots in the 3.5 s after '0', none once a '1' is read, and with
a reboot that fails (the reboot stub exits 1 once the flag file exists)
"power: reboot failed" on stderr and the reboots going on.
"""

import shutil
import time

DAEMON = "power"
DEVICES = ["rabbit_power"]
REGULAR_FILE_DEVICES = ["rabbit_power"]


def sandbox_setup(workdir, sysroot):
    """A reboot stub that fails once $workdir/reboot-fails exists, run by devtrace's system() too."""
    mount = shutil.which("mount") or shutil.which("mount", path="/usr/sbin:/sbin")
    stub = workdir / "reboot"
    stub.write_text(f'#!/bin/sh\necho reboot >> {workdir / "reboot-calls"}\n'
                    f'[ -f {workdir / "reboot-fails"} ] && exit 1\nexit 0\n')
    stub.chmod(0o755)
    return f"{mount} --bind {stub} {workdir / 'stub-bin' / 'reboot'}; export KAROTZ_RUN_STUBS=reboot; "


def reboots(d):
    """The reboots so far, as the stub counts them: qemu's syscall log may not have them all yet."""
    calls = d.workdir / "reboot-calls"
    return len(calls.read_text().splitlines()) if calls.exists() else 0


def drive(d, bus):
    record = []
    time.sleep(4)  # empty device: each one-second read gives nothing
    record.append(f"== reboots after 4 s of empty reads: {reboots(d)}")
    d.write("rabbit_power", b"1")
    time.sleep(3)
    record.append(f"== reboots after '1': {reboots(d)}")
    d.write("rabbit_power", b"0")
    time.sleep(3.5)
    after_zero = reboots(d)
    record.append(f"== reboots after '0': {after_zero}")
    d.write("rabbit_power", b"1")
    time.sleep(1.5)  # the reboot already under way, if any, and the read of '1'
    settled = reboots(d)
    time.sleep(3)
    record.append(f"== reboots in 3 s after '1' again: {reboots(d) - settled}")
    (d.workdir / "reboot-fails").touch()
    before = reboots(d)
    d.write("rabbit_power", b"0")
    time.sleep(3.5)
    record.append(f"== failing reboots after '0': {reboots(d) - before}")
    d.log.flush()
    log = (d.workdir / "daemon.log").read_text(errors="replace")
    record.append(f"== 'reboot failed' reported: {'power: reboot failed' in log}")
    return record


def value(text, label):
    for line in text.splitlines():
        if line.startswith(f"== {label}: "):
            rest = line.split(": ", 1)[1]
            return int(rest) if rest.isdigit() else rest
    return None


def check(record):
    text = "\n".join(record)
    problems = []
    for label in ("reboots after 4 s of empty reads", "reboots after '1'"):
        if value(text, label) != 0:
            problems.append(f"{label}: {value(text, label)} (expected none)")
    zero = value(text, "reboots after '0'")
    if not isinstance(zero, int) or zero < 2:
        problems.append(f"reboots in 3.5 s after '0': {zero} (expected one a second, at least 2)")
    again = value(text, "reboots in 3 s after '1' again")
    if again != 0:
        problems.append(f"reboots after '1' again: {again} (expected none)")
    failing = value(text, "failing reboots after '0'")
    if not isinstance(failing, int) or failing < 2:
        problems.append(f"failing reboots in 3.5 s after '0': {failing} (expected to go on, at least 2)")
    if value(text, "'reboot failed' reported") != "True":
        problems.append("a failing reboot wasn't reported on stderr")
    return problems
