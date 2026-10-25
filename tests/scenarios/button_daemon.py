# SPDX-License-Identifier: MIT
"""button-daemon: presses and releases on /dev/rabbit_button become click signals."""

import time

DAEMON = "button-daemon"
DEVICES = ["rabbit_button"]
SERVICE = "com.mindscape.karotz.Button"
PATH = "/com/mindscape/karotz/Button"


def press(d, hold):
    d.write("rabbit_button", b"1")
    time.sleep(hold)
    d.write("rabbit_button", b"0")


def clicks(d, n):
    for _ in range(n):
        press(d, 0.12)
        time.sleep(0.15)
    time.sleep(0.8)  # let the 300 ms multi-click window close


def drive(d, bus):
    record = []
    # The daemon reads and discards one byte at start-up, before it joins the bus.
    deadline = time.time() + 30
    while not d.write("rabbit_button", b"0") and time.time() < deadline:
        time.sleep(0.2)
    record.append(f"== registered: {bus.wait_for_name(SERVICE)}")
    # No introspection: the interface declares an "event" signal the daemon
    # never creates, and dbus-glib can abort on Introspect then.
    clicks(d, 1)
    clicks(d, 2)
    clicks(d, 3)
    clicks(d, 5)
    press(d, 1.0)  # long press: lclick_start, then lclick_stop on release
    time.sleep(0.8)
    # Clicks still pending when a long press starts are reported first.
    press(d, 0.12)
    time.sleep(0.15)
    press(d, 0.12)
    time.sleep(0.15)
    press(d, 1.0)
    time.sleep(0.8)
    return record
