# SPDX-License-Identifier: MIT
"""button-daemon: reads that return nothing are not button events.

/dev/rabbit_button is a regular file here, not a FIFO: read() returns 0
whenever no byte is queued, and GLib's watch on a regular file fires all
the time, so the daemon's read callback runs over and over with nothing to
read in between the bytes the test appends. A read that returns nothing
must not count as a release (or anything else): one short press must give
exactly one click, and one long press exactly one lclick_start and one
lclick_stop.
"""

import re
import time

from scenarios import button_daemon as button

DAEMON = button.DAEMON
DEVICES = button.DEVICES
REGULAR_FILE_DEVICES = ["rabbit_button"]


def drive(d, bus):
    record = [f"== registered: {bus.wait_for_name(button.SERVICE)}"]
    time.sleep(1)
    record.append("== a short press")
    button.press(d, 0.12)
    time.sleep(1.5)  # the multi-click window closes
    record.append("== a long press")
    button.press(d, 1.5)
    time.sleep(1.5)
    return record


def check(record):
    seen = [m.group(1) for m in (re.search(r"KarotzInterface\.(\w+)\(", l) for l in record) if m]
    events = [s for s in seen if s not in ("registered",)]
    expected = ["click", "lclick_start", "lclick_stop"]
    if events != expected:
        return [f"button signals {events}, expected {expected}"]
    return []
