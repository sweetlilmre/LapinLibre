# SPDX-License-Identifier: MIT
"""ears-daemon: D-Bus requests queue ear moves/resets; a 60 ms tick polls /dev/rabbit_ears.

The device is a regular file (read() returns 0 when no event is queued, like
the real driver) and the 'E' ioctls succeed, returning zeros for status and
positions. With zero status a move finishes on the tick after it starts, and
with positions that never change the ears are never seen moving, so the
start/stop/blocked detection cannot fire; everything else is exercised.

Every step waits well beyond a few ticks so the daemon is idle before the
next one, which keeps the device I/O sequence independent of timing.
"""

import time

import harness

DAEMON = "ears-daemon"
DEVICES = ["rabbit_ears"]
REGULAR_FILE_DEVICES = ["rabbit_ears"]
FAKE_IOCTL_TYPES = [0x45]
SERVICE = "com.mindscape.karotz.Ears"
PATH = "/com/mindscape/karotz/Ears"
IFACE = "com.mindscape.karotz.KarotzInterface"

SET_POS = "ioctl /dev/rabbit_ears 0x40084503"


def raw_device_filter(lines):
    """Take the SET_POS ioctls (written only in the resets) out of the
    device I/O before repeats are collapsed, and record how many there were.
    The values passed aren't traced, so only their number and the rest of
    the order are compared."""
    kept = [l for l in lines if l != SET_POS]
    return kept, [f"== SET_POS calls: {len(lines) - len(kept)}"]

SETTLE = 1.0  # many ticks: the daemon has finished reacting
LEFT = 0x80   # bit 7: event from the left ear; low 7 bits: encoder value


def normalise(out):
    """Keep the reply's values, or just the error name."""
    lines = out.splitlines()
    if not lines:
        return "(empty)"
    if lines[0].startswith("Error "):
        return lines[0].split(":")[0]
    return " | ".join(l.strip() for l in lines[1:]) or "(no values)"


def call(bus, record, method, *args):
    out = bus.send("--reply-timeout=5000", f"--dest={SERVICE}", PATH, f"{IFACE}.{method}", *args)
    record.append(f"{method}({', '.join(args)}) -> {normalise(out)}")
    time.sleep(SETTLE)


def events(d, data):
    """Feed driver events one at a time, letting each be consumed."""
    for b in data:
        d.write("rabbit_ears", bytes([b]))
        time.sleep(0.5)
    time.sleep(SETTLE)


def drive(d, bus):
    record = [f"== registered: {bus.wait_for_name(SERVICE)}"]
    time.sleep(2 * SETTLE)  # the start-up reset of both ears has started

    # Finish it: each ear's first event is ignored, then a value above 24
    # means "at the stop"; a value of 24 or less is not enough.
    events(d, [LEFT | 5, LEFT | 24, LEFT | 30, 5, 30])

    # Explicit resets: left, right, both ("B" and anything not L/R).
    call(bus, record, "reset", "string:idL", "string:L")
    events(d, [5, LEFT | 5, LEFT | 30])  # right events are ignored by a left reset
    call(bus, record, "reset", "string:idR", "string:R")
    events(d, [5, 30])
    call(bus, record, "reset", "string:idB", "string:B")
    events(d, [LEFT | 5, 5, LEFT | 30, 30])
    call(bus, record, "reset", "string:idE", "string:")
    events(d, [LEFT | 5, LEFT | 30, 5, 30])

    # Moves, absolute and relative, each ear and both: each finishes (event 4).
    call(bus, record, "move", "string:m1", "int32:5", "int32:100", "int32:-3", "int32:120")
    call(bus, record, "move_step", "string:m2", "int32:4", "int32:100", "int32:20", "int32:120")
    call(bus, record, "move_left", "string:m3", "int32:16", "int32:200")
    call(bus, record, "move_right", "string:m4", "int32:9", "int32:200")
    call(bus, record, "move_left_step", "string:m5", "int32:-7", "int32:50")
    call(bus, record, "move_right_step", "string:m6", "int32:-40", "int32:50")
    # move_right_step tags the right channel with its id, so m6's events
    # carry m6 and the next move gets its own.
    call(bus, record, "move_right", "string:m7", "int32:1", "int32:10")

    # Cancellation: a request on an ear interrupts a running reset (event 5).
    call(bus, record, "reset", "string:c1", "string:L")
    call(bus, record, "move_left", "string:c2", "int32:3", "int32:30")
    call(bus, record, "reset", "string:c3", "string:R")
    call(bus, record, "move", "string:c4", "int32:3", "int32:30", "int32:4", "int32:40")
    call(bus, record, "reset", "string:c5", "string:B")
    call(bus, record, "move_right_step", "string:c6", "int32:2", "int32:20")
    call(bus, record, "reset", "string:c7", "string:B")
    call(bus, record, "reset", "string:c8", "string:L")
    call(bus, record, "reset", "string:c9", "string:R")
    events(d, [5, 30])
    call(bus, record, "reset", "string:c10", "string:L")
    events(d, [LEFT | 5, LEFT | 30])

    # Bad argument types are rejected by dbus-glib.
    call(bus, record, "move_left", "string:bad", "string:x", "int32:1")
    record.append("== introspection")
    record.extend(harness.introspection(bus, SERVICE, PATH))
    time.sleep(SETTLE)
    return record
