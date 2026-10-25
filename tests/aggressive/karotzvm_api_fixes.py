# SPDX-License-Identifier: MIT
"""karotzVM: fixes to the application API.

One application ("fixes") makes the calls whose handling was fixed, and
three more launches check the rest:

- http.post of a file-like object whose file can't be read throws "file not
  found" and posts nothing; one whose file holds newlines posts it byte for
  byte; a k=v body of more than the first 8 KiB buffer, with a newline and
  characters to escape, arrives whole;
- multimedia.play("./a") (a one-character file name) plays the
  application's file "a", in the application folder karotzVM was given;
- tts.start(text, lang, x) with x not a function keeps the language;
- message.list(5) is refused;
- a list request answered with no list (multimedia.song() here) frees its
  function: SONGS + FIRST calls whose callbacks each hold a SONG_KB string
  of their own, far more in all than V8's 10 MB heap, must all complete
  (leaked, the strings could never be collected and the application would
  run out of memory first). The memory taken after FIRST calls and at the
  end is shown in the summary only: V8 collects lazily, so it grows either
  way;
- three children ending while the karotzVM parent is stopped (one SIGCHLD
  between them) are all counted: the next launch logs "nbChild 0";
- a lost controller connection logs "CRITICAL: ..., the application ends";
- karotz.start(callback), the callback alone, asks for interactive mode
  under a new id, not "" (the controller's "no application" state, under
  which it answers OK without handing the rabbit over; see
  controller_empty_interactive_id).

ARM only, like the karotzVM scenario.
"""

import re

from scenarios import karotzVM as kvm

DAEMON = "karotzVM"
DEVICES = []

SONGS = 60
FIRST = 10  # calls before the first measurement
SONG_KB = 64
BIG = 9000  # the k=v body's value: BIG "x", then "\n&="

FIXES_JS = kvm.PRELUDE + r"""
var base = "http://127.0.0.1:8088";
function wait(ms) { setTimeout(ms, function () { next(); return false; }); }
add(function () {
    attempt("post missing", function () { return http.post(base + "/nofile", {f: file.read("absent.txt")}); });
    attempt("post file", function () { return http.post(base + "/file", {f: file.read("body.txt")}); });
    var big = new Array(""" + str(BIG + 1) + r""").join("x") + "\n&=";
    attempt("post big", function () { return http.post(base + "/big", {big: big}); });
    karotz.multimedia.play("./a", show("play a"));  /* only logs: the step's wait moves on */
    karotz.tts.start("a", "b", 3);
    wait(1500);
});
var songs = 0;
var block = "A";
while (block.length < """ + str(SONG_KB * 1024) + r""") block += block;
function song() {
    // A string of its own (toLowerCase makes a new flat copy), held only by
    // the callback.
    var held = (block + songs).toLowerCase();
    karotz.multimedia.song(function () { log("song called " + held.length); });
    songs++;
    if (songs == """ + str(FIRST) + r""") log("CMD:rss");
    if (songs < """ + str(SONGS + FIRST) + r""") setTimeout(50, function () { song(); return false; });
    else setTimeout(1000, function () { log("CMD:rss"); log("songs " + songs); next(); return false; });
}
add(song);
// Last: where list(5) is accepted, the reply calls 5 as a function.
add(function () { attempt("list 5", function () { return karotz.message.list(5); }); wait(1500); });
add(function () { log("fixes done"); exit(); });
start();
"""

REAP_JS = r"""
setTimeout(4000, function () { log(appName + " exits"); exit(); return false; });
"""

APPS = {
    "fixes": {"accesses": kvm.ALL_ACCESSES, "main": FIXES_JS,
              "files": {"body.txt": "line one\nline two\n", "a": "not really sound\n"}},
    "reap1": {"accesses": [], "main": REAP_JS},
    "reap2": {"accesses": [], "main": REAP_JS},
    "reap3": {"accesses": [], "main": REAP_JS},
    "reapcheck": {"accesses": [], "main": 'log("reapcheck runs"); exit();\n'},
    "lostcontroller": kvm.APPS["lostcontroller"],
    "startalone": {"accesses": ["led"], "main": r"""
karotz.connect("127.0.0.1", 9123);
karotz.start(function () { log("alone interactive"); exit(); });
"""},
}
LAUNCHES = [("fixes manual", 240), ("startalone", 60)]
# The parent is stopped 3 s after the last line, for 9 s: the three exit 4 s
# after they start, all while it is stopped.
PARALLEL = [("reap", ["reap1 a", "reap2 b", "reap3 c"], 3, 90, (3, 9))]
SPINNERS = [("reapcheck", 30), ("lostcontroller manual", 25)]


def drive(d, bus):
    saved = (kvm.APPS, kvm.LAUNCHES, kvm.PARALLEL, kvm.SPINNERS)
    kvm.APPS, kvm.LAUNCHES, kvm.PARALLEL, kvm.SPINNERS = APPS, LAUNCHES, PARALLEL, SPINNERS
    try:
        return kvm.drive(d, bus)
    finally:
        kvm.APPS, kvm.LAUNCHES, kvm.PARALLEL, kvm.SPINNERS = saved


def section(record, title):
    """The record lines of one launch (from its "== title" headers to the next launch's)."""
    out, inside = [], False
    for line in record:
        if line.startswith("== "):
            inside = line.startswith(f"== {title}:")
        if inside or line.startswith(f"{title}| "):
            out.append(line)
    return "\n".join(out)


def summary(record):
    rss = [int(n) for n in re.findall(r"MEM: rss (-?\d+)", "\n".join(record))]
    return f"rss kB after {FIRST} song() calls and after {SONGS} more: {rss}"


def check(record):
    problems = []
    fixes = section(record, "fixes manual")
    if "JSLOG: fixes done" not in fixes:
        problems.append("the fixes application didn't finish")
    if "post missing threw file not found" not in fixes:
        problems.append("http.post of a missing file didn't throw 'file not found'")
    if "POST /nofile" in fixes:
        problems.append("http.post of a missing file still sent a request")
    if "POST /file " not in fixes or "b'line one\\nline two\\n'" not in fixes:
        problems.append("the file with newlines wasn't posted byte for byte")
    escaped = "%0A%26%3D"
    if f"<{4 + BIG + len(escaped)} bytes: b'big=xxx" not in fixes or f"{escaped}'>" not in fixes:
        problems.append(f"the {BIG}-character k=v body didn't arrive whole and escaped")
    if "list 5 threw Wrong arguments" not in fixes:
        problems.append("message.list(5) wasn't refused")
    if not re.search(r'url: "[^"]*/karotzvm-apps-[^"/]*/fixes/a"', fixes):
        problems.append('multimedia.play("./a") didn\'t play the application\'s file "a"')
    if not re.search(r'text: "a"\s+lang: "b"', fixes):
        problems.append('tts.start("a", "b", 3) didn\'t keep the language "b"')
    if "song called" in fixes:
        problems.append("a song() callback was called although there was no list")
    if f"JSLOG: songs {SONGS + FIRST}" not in fixes:
        problems.append(f"the {SONGS + FIRST} unanswered song() calls, {(SONGS + FIRST) * SONG_KB // 1024} MB held "
                        f"in all, didn't complete (their functions not freed, the heap exhausted?)")
    reap = section(record, "reap")
    if "karotzVM parent stopped" not in reap or reap.count("exits") != 3:
        problems.append("the three reap children didn't all exit while the parent was stopped")
    check_line = section(record, "reapcheck")
    m = re.search(r"nbChild (\d+)", check_line)
    if not m or m.group(1) != "0":
        problems.append(f"after three children ended together the next launch saw nbChild "
                        f"{m.group(1) if m else '?'} (expected 0: all three reaped)")
    if "reapcheck runs" not in check_line:
        problems.append("the launch after the three children didn't run")
    alone = section(record, "startalone")
    if "JSLOG: alone interactive" not in alone:
        problems.append("karotz.start(callback) alone didn't go interactive")
    m = re.search(r'interactiveMode \{\s*action: START\s*interactiveId: "([^"]*)"', alone)
    if not m or not m.group(1).startswith("<uuid"):
        problems.append(f"karotz.start(callback) alone asked for the id {m.group(1) if m else '?'!r}, not a new UUID")
    lost = section(record, "lostcontroller manual")
    if not re.search(r"CRITICAL: .*the application ends", lost):
        problems.append("the lost connection wasn't logged as CRITICAL")
    return problems
