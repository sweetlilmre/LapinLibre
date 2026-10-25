# SPDX-License-Identifier: MIT
"""power: only a '0' actually read from the power button driver reboots.

/dev/rabbit_power is a regular file, empty at first: every read() returns
nothing (end of file) until the test appends a byte. power must not reboot
on a read that gave nothing (before the first byte its state is "not
pressed"), must go on reading after such reads (the stream's end-of-file
flag is cleared), must not reboot on '1', and must reboot on '0'. A reboot
is power's system("reboot"), seen in the device I/O as "system reboot".
"""

import time

DAEMON = "power"
DEVICES = ["rabbit_power"]
REGULAR_FILE_DEVICES = ["rabbit_power"]


def reboots(d):
    return sum(1 for line in d.device_io() if line.startswith("system reboot"))


def drive(d, bus):
    record = []
    time.sleep(4)  # empty device: each one-second read gives nothing
    record.append(f"== reboots after 4 s of empty reads: {reboots(d)}")
    d.write("rabbit_power", b"1")
    time.sleep(3)
    record.append(f"== reboots after '1': {reboots(d)}")
    d.write("rabbit_power", b"0")
    time.sleep(3)
    record.append(f"== reboots after '0': {reboots(d)}")
    return record


def check(record):
    text = "\n".join(record)
    problems = []
    for when, want in (("after 4 s of empty reads", 0), ("after '1'", 0)):
        if f"== reboots {when}: {want}" not in text:
            problems.append(f"a reboot {when} (expected none)")
    if "== reboots after '0': 0" in text or "== reboots after '0'" not in text:
        problems.append("no reboot after '0'")
    return problems
