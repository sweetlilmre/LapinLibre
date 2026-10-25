# SPDX-License-Identifier: MIT
"""led-daemon: animation ids of 64 characters or more.

The daemon keeps the running animation's id in a 64-byte field and sends it back in the
"event" signal when the animation ends or is replaced. An id that didn't fit was copied
without its NUL (strncpy), so the signal read past the field; now it is cut to 63
characters (g_strlcpy). Here:

- a fade with a 100-character id, left to finish: its "done" event carries the first 63;
- a pulse with an id of exactly 64 characters, replaced by a light: its "aborted" event
  carries the first 63;
- a fade with a 63-character id: its event carries it whole.

The set-up is led_daemon's.
"""

import re
import time

from scenarios import led_daemon as led

DAEMON = led.DAEMON
DEVICES = led.DEVICES
FAKE_IOCTL_TYPES = led.FAKE_IOCTL_TYPES

LONG = "L" * 100
SIXTY_FOUR = "P" * 64
FITS = "F" * 63


def drive(d, bus):
    record = [f"== registered: {led.wait_registered(d, bus)}"]
    record.append(led.call(bus, "fade", f"string:{LONG}", "string:00FF00", "int32:10"))
    time.sleep(1)
    record.append(led.call(bus, "pulse", f"string:{SIXTY_FOUR}", "string:FF0000", "string:0000FF",
                           "int32:100", "int32:-1"))
    time.sleep(0.5)
    record.append(led.call(bus, "light", "string:l1", "string:000000"))
    time.sleep(0.5)
    record.append(led.call(bus, "fade", f"string:{FITS}", "string:FFFFFF", "int32:10"))
    time.sleep(1)
    return record


def event_ids(record):
    return [m.group(1) for m in (re.search(r'KarotzInterface\.event\(string "([^"]*)"', l) for l in record) if m]


def check(record):
    problems = []
    if record[0] != "== registered: True":
        return ["led-daemon didn't register"]
    ids = event_ids(record)
    for name, sent in (("100-character fade", LONG), ("64-character pulse", SIXTY_FOUR), ("63-character fade", FITS)):
        expected = sent[:63]
        if expected not in ids:
            got = [i[:12] + "..." + f"({len(i)})" for i in ids if i[:1] == sent[:1]]
            problems.append(f"{name}: no event with the id cut to 63 characters (got {got})")
    for i in ids:
        if len(i) > 63:
            problems.append(f"an event id of {len(i)} characters: {i[:20]}...")
    return problems


def summary(record):
    return f"event ids: {[f'{i[:1]}x{len(i)}' for i in event_ids(record)]}"
