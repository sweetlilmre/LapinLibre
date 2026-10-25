# SPDX-License-Identifier: MIT
"""led-daemon: light / fade / pulse calls become SPI writes to the nose LED and "event" signals."""

import time

import harness

DAEMON = "led-daemon"
DEVICES = ["spidev0.1"]
# SPI setup ioctls (type 0x6b) fail on a FIFO and the daemon aborts; let them succeed.
FAKE_IOCTL_TYPES = [0x6B]
SERVICE = "com.mindscape.karotz.Led"
PATH = "/com/mindscape/karotz/Led"
IFACE = "com.mindscape.karotz.KarotzInterface"

# A bad colour in light or fade replies normally with result FALSE; light
# replacing a running animation reports it aborted (5), as fade and pulse do.


def call(bus, method, *args):
    out = bus.send(f"--dest={SERVICE}", PATH, f"{IFACE}.{method}", *args)
    # Keep the reply body (or error), drop the run-specific header line.
    lines = [l.strip() for l in out.splitlines() if not l.startswith("method return")]
    return f"{method}{args} -> {' | '.join(lines)}"


def wait_registered(d, bus, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not d.alive():
            return False
        if "boolean true" in bus.send("--dest=org.freedesktop.DBus", "/org/freedesktop/DBus",
                                      "org.freedesktop.DBus.NameHasOwner", f"string:{SERVICE}"):
            return True
        time.sleep(0.2)
    return False


def drive(d, bus):
    record = []
    registered = wait_registered(d, bus)
    record.append(f"== registered: {registered}")
    if not registered:
        # The SPI mode ioctls fail on a FIFO (ENOTTY) and the daemon aborts
        # ("can't set spi mode") in its instance init, before it joins the
        # bus. Making SPI_IOC_* ioctls succeed in the harness lets the rest
        # of the scenario run.
        time.sleep(0.5)
        return record
    record.append("== interface")
    record.extend(harness.introspection(bus, SERVICE, PATH))
    record.append("== calls")
    # light: valid, empty (black), 'n...' (rejected), malformed, short.
    for color in ["FF0000", "", "none", "zz0000", "12"]:
        record.append(call(bus, "light", "string:l1", f"string:{color}"))
        time.sleep(0.2)
    # fade to completion (period clamped to 25 ms), then a longer one.
    record.append(call(bus, "fade", "string:f1", "string:00FF00", "int32:10"))
    time.sleep(0.5)
    record.append(call(bus, "fade", "string:f2", "string:0000FF", "int32:300"))
    time.sleep(1.0)
    # bad colour: no event, FALSE reply.
    record.append(call(bus, "fade", "string:f3", "string:nope", "int32:300"))
    time.sleep(0.5)
    # fade replaced by another fade (event 5), which finishes (event 4).
    record.append(call(bus, "fade", "string:f4", "string:FFFFFF", "int32:2000"))
    time.sleep(0.3)
    record.append(call(bus, "fade", "string:f5", "string:101010", "int32:200"))
    time.sleep(1.0)
    # finite pulse runs to the end.
    record.append(call(bus, "pulse", "string:p1", "string:FF0000", "string:0000FF", "int32:100", "int32:400"))
    time.sleep(1.0)
    # pulse with bad colours (current colour used), endless, cut by a fade.
    record.append(call(bus, "pulse", "string:p2", "string:xx", "string:00FF00", "int32:5", "int32:-1"))
    time.sleep(0.5)
    record.append(call(bus, "fade", "string:f6", "string:000000", "int32:100"))
    time.sleep(0.8)
    # endless pulse replaced by another pulse, then cut by light (flag still set: event 5).
    record.append(call(bus, "pulse", "string:p3", "string:FF00FF", "string:00FFFF", "int32:200", "int32:-1"))
    time.sleep(0.4)
    record.append(call(bus, "pulse", "string:p4", "string:FFFF00", "string:", "int32:200", "int32:-1"))
    time.sleep(0.4)
    record.append(call(bus, "light", "string:l2", "string:808080"))
    time.sleep(0.3)
    # A pulse that runs to its end, then a fade cut by light: the fade is
    # reported aborted (event 5).
    record.append(call(bus, "pulse", "string:p5", "string:010203", "string:A0B0C0", "int32:100", "int32:300"))
    time.sleep(1.0)
    record.append(call(bus, "fade", "string:f7", "string:FF8000", "int32:3000"))
    time.sleep(0.3)
    record.append(call(bus, "light", "string:l3", "string:000000"))
    time.sleep(0.5)
    return record
