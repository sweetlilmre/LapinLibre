# SPDX-License-Identifier: MIT
"""karotzVM: the handles made while handling an event are freed after it.

karotzVM enters the application's JavaScript from two places in its main
loop: for each message from the controller (reply handlers and listeners)
and for each setTimeout callback. The V8 handles made there (a button
event's type string, a callback's return value, ...) must be freed when the
event is done; without a HandleScope of their own they would stay in the
application's outermost scope until it exits, and its memory would grow
with every event.

The application listens to the button, then the fake controller sends
ROUNDS bursts of BURST button events (CMD:burst), and the application runs
TIMERS setTimeout callbacks one after another; the process's memory
(CMD:rss) is taken after the first burst, after the last and after the
timers. What must hold: every event reached the listener, the timers all
ran, the application finished, and its memory grew by less than GROWTH_KB
from the first measurement to the last. ARM only, like the karotzVM scenario.
"""

import re

from scenarios import karotzVM as kvm

DAEMON = "karotzVM"
DEVICES = []

ROUNDS = 8
BURST = 20000
TIMERS = 5000
GROWTH_KB = 512

HANDLES_JS = kvm.PRELUDE + r"""
var presses = 0;
var target = 0;
function wait(ms) { setTimeout(ms, function () { next(); return false; }); }
function burst() { target = presses + """ + str(BURST) + r"""; log("CMD:burst=button,""" + str(BURST) + r""""); }
add(function () {
    karotz.button.addListener(function (type) {
        presses++;
        if (presses == target) next();
        return true;
    });
    burst();
});
add(function () { log("CMD:rss"); log("first burst done"); wait(500); });
for (var r = 2; r <= """ + str(ROUNDS) + r"""; r++) add(burst);
add(function () { log("CMD:rss"); log("presses " + presses); wait(500); });
add(function () {
    var k = 0;
    setTimeout(1, function () {
        k++;
        if (k < """ + str(TIMERS) + r""") return true;
        log("timers " + k);
        next();
        return false;
    });
});
add(function () { log("CMD:rss"); wait(500); });
add(function () { log("handles done"); exit(); });
start();
"""

APPS = {"handles": {"accesses": kvm.ALL_ACCESSES, "main": HANDLES_JS}}
LAUNCHES = [("handles manual", 600)]


def drive(d, bus):
    saved = (kvm.APPS, kvm.LAUNCHES, kvm.PARALLEL, kvm.SPINNERS)
    kvm.APPS, kvm.LAUNCHES, kvm.PARALLEL, kvm.SPINNERS = APPS, LAUNCHES, [], []
    try:
        return kvm.drive(d, bus)
    finally:
        kvm.APPS, kvm.LAUNCHES, kvm.PARALLEL, kvm.SPINNERS = saved


def summary(record):
    text = "\n".join(record)
    rss = [int(n) for n in re.findall(r"MEM: rss (-?\d+)", text)]
    return f"rss kB after the first burst, the last, the timers: {rss}"


def check(record):
    problems = []
    text = "\n".join(record)
    if "JSLOG: handles done" not in text:
        problems.append("the application didn't finish")
    presses = re.findall(r"JSLOG: presses (\d+)", text)
    if not presses or int(presses[-1]) != ROUNDS * BURST:
        problems.append(f"the listener got {presses[-1] if presses else 'no'} presses, expected {ROUNDS * BURST}")
    if f"JSLOG: timers {TIMERS}" not in text:
        problems.append(f"the {TIMERS} timers didn't all run")
    rss = [int(n) for n in re.findall(r"MEM: rss (-?\d+)", text)]
    if len(rss) != 3 or min(rss) < 0:
        problems.append(f"memory not measured ({rss})")
    elif rss[-1] - rss[0] >= GROWTH_KB:
        problems.append(f"memory grew by {rss[-1] - rss[0]} kB over {(ROUNDS - 1) * BURST} more events and "
                        f"{TIMERS} timers (from {rss[0]} to {rss[-1]} kB; allowed under {GROWTH_KB})")
    return problems
