# SPDX-License-Identifier: MIT
"""button-daemon: a bouncing button gives the clicks the presser meant.

The head button's contacts bounce, and the driver queues every edge, so one
press can reach the daemon as 1 0 1 0 1 a few ms apart. First the presses
recorded on the rabbit (tests/data/button/) are replayed with their timing:
singles (one of them held 541 ms, a long press), doubles, triples and long
presses. Then clean clicks with synthetic bounce bursts of several widths on
every press and release edge, with gaps up to the 28 ms the recordings show.
Each must be classified as the clean presses it stands for.
"""

import re
import time
from pathlib import Path

from scenarios import button_replay

DAEMON = button_replay.DAEMON
DEVICES = button_replay.DEVICES

DATA = Path(__file__).resolve().parent.parent / "data" / "button"
LONG = ["lclick_start", "lclick_stop"]
RECORDINGS = [
    ("single", ["click"] + LONG + ["click"] * 4),
    ("double", ["dclick"] * 5),
    ("triple", ["tclick"] * 3),
    ("long", LONG * 3),
]
# (bounces per edge, ms between them). The recordings' bursts last up to
# about 55 ms (8+8+29+8); a burst delays the press or release it ends in, so
# much longer ones would also stretch the gap between clicks past the
# 300 ms multi-click window, as a slower click would.
BURSTS = [(1, 5), (3, 7), (5, 10), (2, 14), (1, 28)]
GESTURES = ["click", "dclick", "tclick"] + LONG


def edge(d, level, bounces, gap):
    """Reach LEVEL through BOUNCES flips back to the other level, GAP ms apart."""
    other = b"0" if level == b"1" else b"1"
    for _ in range(bounces):
        d.write("rabbit_button", level)
        time.sleep(gap / 1000)
        d.write("rabbit_button", other)
        time.sleep(gap / 1000)
    d.write("rabbit_button", level)


def bounced_press(d, hold, bounces, gap):
    edge(d, b"1", bounces, gap)
    time.sleep(hold)
    edge(d, b"0", bounces, gap)


def bounced_clicks(d, n, bounces, gap):
    for _ in range(n):
        bounced_press(d, 0.15, bounces, gap)
        time.sleep(0.15)
    time.sleep(0.8)  # the multi-click window closes


def drive(d, bus):
    record = [button_replay.wait_registered(d, bus)]
    for name, _ in RECORDINGS:
        events = button_replay.load(DATA / f"{name}.txt")
        record.append(f"== replaying {name}: {len(events)} driver events")
        button_replay.replay(d, events)
        time.sleep(1.5)
    for bounces, gap in BURSTS:
        record.append(f"== {bounces} bounces {gap} ms apart on each edge")
        for n in (1, 2, 3):
            bounced_clicks(d, n, bounces, gap)
        bounced_press(d, 1.0, bounces, gap)
        time.sleep(0.8)
    return record


def check(record):
    seen = [m.group(1) for m in (re.search(r"KarotzInterface\.(\w+\(.*\))", l) for l in record) if m]
    seen = [s for s in seen if s != "registered()"]
    expected = [s + "()" for _, signals in RECORDINGS for s in signals] + [s + "()" for s in GESTURES] * len(BURSTS)
    if seen != expected:
        return [f"button signals {seen}, expected {expected}"]
    return []
