# SPDX-License-Identifier: MIT
"""button-daemon: replay presses recorded on the real rabbit, with their timing.

KAROTZ_BUTTON_REPLAY names a file of the daemon's reads from /dev/rabbit_button
as strace -tt recorded them on the rabbit: lines like
'1533  12:47:01.123456 read(6, "1", 1) = 1'. The daemon reads from a GLib
watch, so each read's time is (within a millisecond or so) when the driver
delivered the byte. The bytes are written with the same spacing, so real
presses, including the contact bounce of the rabbit's button (repeats about
7-28 ms apart), are classified as they would be on the rabbit.

tests/data/button/ holds recordings made on the rabbit, which
the aggressive test button_bounce replays and checks. This scenario is for
a new recording: its record depends on the replay file, so tests/run.sh
doesn't run it; run it by hand with an expected directory of its own:

  KAROTZ_BUTTON_REPLAY=reads.txt python3 -I -B tests/harness.py --record --expected /tmp/replay ...
"""

import os
import re
import time

DAEMON = "button-daemon"
DEVICES = ["rabbit_button"]
SERVICE = "com.mindscape.karotz.Button"
PATH = "/com/mindscape/karotz/Button"


def load(path):
    events = []
    for line in open(path):
        m = re.search(r'(\d+):(\d+):(\d+\.\d+) read\(\d+, "([01])"', line)
        if m:
            h, mi, s, value = m.groups()
            events.append((int(h) * 3600 + int(mi) * 60 + float(s), value.encode()))
    return events


def replay(d, events):
    """Write the events' bytes with their recorded spacing."""
    start, t0 = time.monotonic(), events[0][0]
    for t, value in events:
        delay = (t - t0) - (time.monotonic() - start)
        if delay > 0:
            time.sleep(delay)
        d.write("rabbit_button", value)


def wait_registered(d, bus):
    deadline = time.time() + 30
    # The daemon reads and discards one byte at start-up, before it joins the bus.
    while not d.write("rabbit_button", b"0") and time.time() < deadline:
        time.sleep(0.2)
    return f"== registered: {bus.wait_for_name(SERVICE)}"


def drive(d, bus):
    record = [wait_registered(d, bus)]
    events = load(os.environ["KAROTZ_BUTTON_REPLAY"])
    record.append(f"== replaying {len(events)} driver events")
    replay(d, events)
    time.sleep(1.5)  # every click window has closed
    return record
