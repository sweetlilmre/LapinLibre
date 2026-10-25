# SPDX-License-Identifier: MIT
"""karotzVM: reply handlers freed after their request's last reply, and when
they age out.

karotzVM keeps a reply handler, with the application's callback, until its
request's last reply where that reply can be told
(NamedFunction::end, from what the controller and the daemons send), or
for ten hours otherwise; it frees aged-out handlers with their data, and copes with a handler aged out while
it is being called.

The fake controller sends each Response and its Event as messages of their
own, as the real one does (CMD:split), and every message twice (CMD:repeat),
so a handler freed too late shows as a callback called again, and one freed
too early as a callback missing a call. Then memory: karotzVM caps V8's
heap (10 MB of old space, of which about 8.4 MB holds an application's
arrays: about 530 callbacks of 16 KB), so what isn't freed ends karotzVM
with V8's fatal out-of-memory error (an abort; the controller starts a new
VM). Ten rounds of requests whose callbacks each hold 56 KB, about 22 MB in
all or 2.7 times what fits, with karotzVM's clock moved on by more than ten
hours each round (tests/fakes/timeshift-arm.c), must all finish; a control
application keeping 250 such callbacks (play's, about 14 MB) must run out.
ARM only, like the karotzVM scenario.
"""

import re

from scenarios import karotzVM as kvm

DAEMON = "karotzVM"
DEVICES = []

# Each kind of request: (tag, JavaScript call with the callback cb), and when
# its handler ends: "end" after the Event or a Response ERROR, "event" after
# the Event only, "first" after one reply, "kept" never (until it ages out).
KINDS = [
    ("fade", 'karotz.led.fade("00FF00", 100, cb)', "end"),
    ("pulse", 'karotz.led.pulse("00FF00", 100, 1000, cb)', "end"),
    ("light", 'karotz.led.light("00FF00", cb)', "event"),
    ("move1", "karotz.ears.move(3, null, cb)", "end"),
    ("move2", "karotz.ears.move(3, 4, cb)", "kept"),
    ("reset", "karotz.ears.reset(cb)", "end"),
    ("tts", 'karotz.tts.start("bonjour", "fr-FR", cb)', "end"),
    ("ttsstop", "karotz.tts.stop(cb)", "first"),
    ("play", 'karotz.multimedia.play("http://127.0.0.1:8088/a.mp3", cb)', "kept"),
    ("pause", "karotz.multimedia.pause(cb)", "first"),
    ("record", "karotz.multimedia.record(cb)", "end"),
    ("wfile", 'karotz.webcam.photo("file", cb)', "event"),
    ("wpost", 'karotz.webcam.photo("http://127.0.0.1:8088/up", cb)', "event"),
    ("wvideo", 'karotz.webcam.photo("video", cb)', "kept"),
    ("dock", 'karotz.dock.light("00FF00", cb)', "first"),
    ("ping", "karotz.ping(cb)", "first"),
]

# Calls each callback gets from Response, Response, Event, Event.
EXPECTED = {
    "ok": {"end": 3, "event": 3, "first": 1, "kept": 4},
    "err": {"end": 1, "event": 3, "first": 1, "kept": 4},
}

ISSUE = "\n".join(f'    issue("{tag}", function (cb) {{ {call}; }});' for tag, call, _ in KINDS)

ENDS_JS = kvm.PRELUDE + r"""
var counts = {};
var mode = "";
function counter(tag) {
    tag = mode + ":" + tag;
    counts[tag] = 0;
    return function () { counts[tag]++; log("n " + tag + " #" + counts[tag]); };
}
function issue(tag, f) { f(counter(tag)); }
function wait(ms) { setTimeout(ms, function () { next(); return false; }); }
function issueAll() {
""" + ISSUE + r"""
}
add(function () { log("CMD:split"); log("CMD:repeat"); log("CMD:code=OK/TERMINATED"); wait(500); });
add(function () { mode = "ok"; issueAll(); wait(4000); });
add(function () { log("CMD:code=ERROR/CANCELLED"); wait(500); });
add(function () { mode = "err"; issueAll(); wait(4000); });
add(function () { log("ends done"); exit(); });
start();
"""

ROUNDS = 10
CONTROL = 250  # kept callbacks for the control application
PER_KIND = 2

# 56 KB of array per callback.
HOLDER_JS = r"""
function big() { var a = new Array(14000); a[0] = 1; return a; }
function holder() { var data = big(); return function () { return data.length; }; }
"""

MEMORY_JS = kvm.PRELUDE + HOLDER_JS + r"""
var round = 0;
var heldCalls = 0;
function wait(ms) { setTimeout(ms, function () { next(); return false; }); }
function issueAll() {
    for (var k = 0; k < """ + str(PER_KIND) + r"""; k++) {
""" + "\n".join(f"        (function () {{ var cb = holder(); {call}; }})();" for _, call, _ in KINDS) + r"""
    }
}
// Unanswered requests (CMD:hold), left to age out. The first one's callback
// makes a request of its own when its late reply comes, after the clock has
// moved on: adding that handler ages out every older one, the one being
// called included.
function issueHeld() {
    var data = big();
    karotz.tts.start("held", "fr-FR", function () {
        heldCalls++;
        log("held reply, round " + round + ", " + data.length);
        karotz.led.light("0000FF", holder());
    });
    for (var k = 0; k < """ + str(PER_KIND) + r"""; k++) {
        karotz.asr.string("oui|non", "fr-FR", holder());
        social.init(holder());
        karotz.led.fade("FF0000", 100, holder());
    }
}
// The steps are all queued first, so each round's number is its own.
function oneRound(r) {
    add(function () { round = r; log("CMD:age=" + (r * 40000)); wait(500); });
    add(function () { log("CMD:flush"); wait(1500); });
    add(function () { issueAll(); wait(3000); });
    add(function () { log("CMD:hold"); wait(500); });
    add(function () { issueHeld(); wait(1500); });
    add(function () { log("round " + round + " done"); wait(500); });
}
add(function () { log("CMD:split"); wait(500); });
for (var r = 1; r <= """ + str(ROUNDS) + r"""; r++) oneRound(r);
add(function () { log("CMD:flush"); wait(1500); });
add(function () { log("held calls " + heldCalls); log("memory done"); exit(); });
start();
"""

# Kept callbacks (play's, with the clock left alone) run out of memory: the
# heap is small enough for what isn't freed to show.
CONTROL_JS = kvm.PRELUDE + HOLDER_JS + r"""
add(function () {
    for (var k = 1; k <= """ + str(CONTROL) + r"""; k++) {
        karotz.multimedia.play("http://127.0.0.1:8088/a.mp3", holder());
        if (k % 50 == 0) log("control " + k);
    }
    log("control done");
    exit();
});
start();
"""

APPS = {
    "ends": {"accesses": kvm.ALL_ACCESSES, "main": ENDS_JS},
    "memory": {"accesses": kvm.ALL_ACCESSES, "main": MEMORY_JS},
    "control": {"accesses": kvm.ALL_ACCESSES, "main": CONTROL_JS},
}
LAUNCHES = [("ends manual", 120), ("memory manual", 900), ("control manual", 120)]


def drive(d, bus):
    saved = (kvm.APPS, kvm.LAUNCHES, kvm.PARALLEL, kvm.SPINNERS, kvm.TIMESHIFT)
    kvm.APPS, kvm.LAUNCHES, kvm.PARALLEL, kvm.SPINNERS, kvm.TIMESHIFT = APPS, LAUNCHES, [], [], True
    try:
        return kvm.drive(d, bus)
    finally:
        kvm.APPS, kvm.LAUNCHES, kvm.PARALLEL, kvm.SPINNERS, kvm.TIMESHIFT = saved


# V8's fatal out-of-memory error, which ends karotzVM.
OUT_OF_MEMORY = "Allocation failed - process out of memory"


def launch_text(text, title):
    """One launch's stderr from the record."""
    return text.split(f"== {title}: stderr", 1)[-1].split(f"== {title}: messages sent", 1)[0]


def summary(record):
    text = "\n".join(record)
    rounds = re.findall(r"JSLOG: round (\d+) done", launch_text(text, "memory manual"))
    control = launch_text(text, "control manual")
    kept = re.findall(r"JSLOG: control (\d+)", control)
    end = "ran out of memory" if OUT_OF_MEMORY in control else "did not run out"
    return (f"memory: {len(rounds)} of {ROUNDS} rounds done; "
            f"control: {end} after {kept[-1] if kept else 'under 50'} kept callbacks")


def check(record):
    problems = []
    text = "\n".join(record)
    for mode, ends in EXPECTED.items():
        for tag, _, end in KINDS:
            calls = [int(n) for n in re.findall(rf"JSLOG: n {mode}:{tag} #(\d+)$", text, re.M)]
            got = max(calls, default=0)
            if got != ends[end]:
                problems.append(f"{mode}:{tag} ({end}): callback called {got} time(s), expected {ends[end]}")
    memory = launch_text(text, "memory manual")
    if OUT_OF_MEMORY in memory:
        done = re.findall(r"JSLOG: round (\d+) done", memory)
        problems.append(f"memory: ran out of memory after {len(done)} of {ROUNDS} rounds")
    # The control must run out, or the rounds proved nothing.
    control = launch_text(text, "control manual")
    if "JSLOG: interactive" not in control:
        problems.append("control: never started")
    elif OUT_OF_MEMORY not in control or "JSLOG: control done" in control:
        problems.append(f"control: {CONTROL} kept callbacks didn't run out of memory")
    # Each round's late replies (rounds 2 on): only the first one's callback
    # runs, for its Response; the rest, its Event included, aged out with it
    # while it ran. The last round's, flushed without moving the clock, get
    # both their Response and Event.
    if f"JSLOG: held calls {ROUNDS + 1}" not in memory:
        problems.append("late replies: " + (re.findall(r"JSLOG: held calls \d+", memory) or ["no count"])[0])
    for expected in ("ends done", "memory done"):
        if f"JSLOG: {expected}" not in text:
            problems.append(f"missing: {expected}")
    # The control's out-of-memory error is an abort; any other is a crash.
    for title in ("ends manual", "memory manual"):
        if re.search(r"signal (11|6)|Segmentation|Aborted|core dumped", launch_text(text, title)):
            problems.append(f"{title}: a crash")
    if re.search(r"signal 11|Segmentation", control):
        problems.append("control manual: a crash")
    return problems
