# SPDX-License-Identifier: MIT
"""ears-daemon: moves whose ears are blocked.

The set-up is ears_daemon's (the device a regular file, the 'E' ioctls
faked); once the start-up reset is done, the faked status ioctl reports
motors as busy (KAROTZ_FAKE_IOCTL_DATA_FILE, ioctlfake-arm.c) while the
positions never change, so a running move's ears are seen not moving and,
after BLOCKED_MS (1.4 s), blocked. Then:

- a move of both ears, both motors busy: both are blocked. The move must be
  stopped once and reported interrupted once, with one "blocked" signal per
  ear;
- a move of the right ear alone, its motor busy: blocked once, interrupted
  once, and no left "blocked" signal.

ARM only (the data file is read by the ARM ioctl fake).
"""

import re
import time
from pathlib import Path

from scenarios import ears_daemon as ears

DAEMON = ears.DAEMON
DEVICES = ears.DEVICES
REGULAR_FILE_DEVICES = ears.REGULAR_FILE_DEVICES
FAKE_IOCTL_TYPES = ears.FAKE_IOCTL_TYPES
raw_device_filter = ears.raw_device_filter

GET_STATUS = "80044504"  # EARS_IOC_GET_STATUS, _IOR('E', 4, int)
INTERRUPTED = 5          # the "event" code of an interrupted request
BUSY_BOTH, BUSY_RIGHT = "03000000", "02000000"  # STATUS_LEFT_BUSY 1, STATUS_RIGHT_BUSY 2


def drive(d, bus):
    record = [f"== registered: {bus.wait_for_name(ears.SERVICE)}"]
    time.sleep(2 * ears.SETTLE)
    ears.events(d, [ears.LEFT | 5, ears.LEFT | 30, 5, 30])  # end the start-up reset
    data = Path(d.workdir) / "ioctl-data"

    data.write_text(f"{GET_STATUS} {BUSY_BOTH}\n")
    record.append("== both ears blocked")
    ears.call(bus, record, "move", "string:mb", "int32:5", "int32:100", "int32:7", "int32:100")
    time.sleep(3)
    data.write_text("")  # motors idle again
    time.sleep(ears.SETTLE)

    data.write_text(f"{GET_STATUS} {BUSY_RIGHT}\n")
    record.append("== the right ear blocked")
    ears.call(bus, record, "move_right", "string:mr", "int32:9", "int32:100")
    time.sleep(3)
    data.write_text("")
    time.sleep(ears.SETTLE)
    return record


def signals(record, member):
    return [l for l in record if f"KarotzInterface.{member}(" in l]


def check(record):
    problems = []
    for ident in ("mb", "mr"):
        interrupted = [l for l in signals(record, "event")
                       if f'string "{ident}"' in l and f"int32 {INTERRUPTED}" in l]
        if len(interrupted) != 1:
            problems.append(f"move {ident}: {len(interrupted)} interrupted events, expected 1")
    # Blocked signals, in the order they came: two for mb, then one for mr.
    blocked = [m.group(1) for m in (re.search(r"KarotzInterface\.move_(right|left)_blocked\(", l) for l in record)
               if m]
    if sorted(blocked[:2]) != ["left", "right"] or blocked[2:] != ["right"]:
        problems.append(f"blocked signals {blocked}, expected right and left for mb, then right for mr")
    return problems
