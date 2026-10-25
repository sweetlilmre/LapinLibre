# SPDX-License-Identifier: MIT
"""karotzVM: errors in an application don't take the VM down.

- Listeners that throw (button, ears, multimedia, RFID, k2k): the exception
  is reported in karotzVM's log, the listener stays registered (it gets the
  next event too) and the application goes on.
- A reply callback that throws (led.light's): reported, the application
  goes on.
- http.get and http.post with wrong arguments, social.twitter.sign,
  social.facebook.get and social.soundcloud.post before social.init: each
  throws into the application, which catches it.

What must hold: the application reaches its end, every listener was called
twice (once per event, so it was kept after throwing), every listener's
exception was reported, and every call above was caught as an exception.
ARM only, like the karotzVM scenario.
"""

import re

from scenarios import karotzVM as kvm

DAEMON = "karotzVM"
DEVICES = []

LISTENERS = ["button", "ears", "multimedia", "rfid", "k2k"]
CALLS = {
    "http.get": "http.get(42)",
    "http.get()": "http.get()",
    "http.post": "http.post(42, {})",
    "http.post(url)": 'http.post("http://127.0.0.1:8088/x")',
    "twitter.sign": 'social.twitter.sign("base")',
    "facebook.get": 'social.facebook.get("https://graph.facebook.com/me")',
    "soundcloud.post": 'social.soundcloud.post("https://api.soundcloud.com/x", {})',
}

THROWS_JS = kvm.PRELUDE + r"""
var calls = {};
function wait(ms) { setTimeout(ms, function () { next(); return false; }); }
function thrower(name) {
    calls[name] = 0;
    return function () { calls[name]++; log("listener " + name + " #" + calls[name]); throw new Error(name + " listener failed"); };
}
add(function () {
""" + "\n".join(f'    karotz.{n}.addListener(thrower("{n}"));' for n in LISTENERS) + r"""
    wait(500);
});
""" + "\n".join(f'add(function () {{ log("CMD:{n}"); wait(1500); }});\nadd(function () {{ log("CMD:{n}"); wait(1500); }});'
                for n in LISTENERS) + r"""
add(function () {
    calls.ledcb = 0;
    karotz.led.light("00FF00", function (code) {
        calls.ledcb++;
        log("led callback " + code);
        throw new Error("led callback failed");
    });
    wait(1500);
});
add(function () {
""" + "\n".join(f'    try {{ {call}; log("{name}: no exception"); }} catch (e) {{ log("{name}: caught " + e); }}'
                for name, call in CALLS.items()) + r"""
    wait(500);
});
add(function () {
    var counts = [];
    for (var n in calls) counts.push(n + "=" + calls[n]);
    log("counts " + counts.join(" "));
    log("throws done");
    exit();
});
start();
"""

TOPLEVEL_JS = r"""
try { log("top get() -> " + http.get()); } catch (e) { log("top get() threw " + e); }
log("top survived");
exit();
"""

PROBES = {
    "t1": 'log("t1 start"); log("t1 end"); exit();',
    "t2": 'log("t2 start"); try { karotz.led.light(); } catch (e) { log("t2 caught " + e); } log("t2 end"); exit();',
    "t3": 'log("t3 start"); try { http.get(); } catch (e) { log("t3 caught " + e); } log("t3 end"); exit();',
    "t4": 'log("t4 start"); try { throw new Error("js"); } catch (e) { log("t4 caught " + e); } log("t4 end"); exit();',
    "t5": 'function f() { karotz.led.light(); } log("t5 start"); try { f(); } catch (e) { log("t5 caught " + e); } '
          'log("t5 end"); exit();',
    "t6": 'log("t6 start"); try { throw new Error("js"); } catch (e) { log("t6 caught " + e); } '
          'setTimeout(10, function () { log("t6 end"); exit(); return false; });',
    "t7": 'log("t7 start"); try { log("t7 in try"); } catch (e) { } log("t7 end"); exit();',
    "t8": 'log("t8 start"); try { throw new Error("js"); } catch (e) { log("t8 caught " + e); } log("t8 end");',
    "t9": 'for (var i = 0; i < 1; i++) {} log("t9 start"); try { throw new Error("js"); } catch (e) { log("t9 caught " + e); } '
          'log("t9 end"); exit();',
}

APPS = {"throws": {"accesses": kvm.ALL_ACCESSES, "main": THROWS_JS},
        "toplevel": {"accesses": ["http"], "main": TOPLEVEL_JS},
        **{name: {"accesses": kvm.ALL_ACCESSES, "main": js} for name, js in PROBES.items()}}
LAUNCHES = [("throws manual", 180), ("toplevel", 60), ("t1", 60), ("t2", 60), ("t3", 60), ("t4", 60), ("t5", 60), ("t6", 60), ("t7", 60), ("t8", 60), ("t9", 60)]


def drive(d, bus):
    saved = (kvm.APPS, kvm.LAUNCHES, kvm.PARALLEL, kvm.SPINNERS)
    kvm.APPS, kvm.LAUNCHES, kvm.PARALLEL, kvm.SPINNERS = APPS, LAUNCHES, [], []
    try:
        return kvm.drive(d, bus)
    finally:
        kvm.APPS, kvm.LAUNCHES, kvm.PARALLEL, kvm.SPINNERS = saved


def check(record):
    problems = []
    text = "\n".join(record)
    if "JSLOG: throws done" not in text:
        problems.append("the application didn't finish (the VM died?)")
    counts = dict(re.findall(r"(\w+)=(\d+)", (re.findall(r"JSLOG: counts (.*)", text) or [""])[-1]))
    for name in LISTENERS:
        if counts.get(name) != "2":
            problems.append(f"{name} listener called {counts.get(name, 'no')} time(s), expected 2 (kept after throwing)")
        if f"{name} listener failed" not in text.replace("JSLOG:", ""):
            problems.append(f"{name} listener's exception not reported")
    if counts.get("ledcb", "0") == "0":
        problems.append("the led.light callback was never called")
    if "led callback failed" not in text:
        problems.append("the led.light callback's exception not reported")
    for name in CALLS:
        if not re.search(rf"JSLOG: {re.escape(name)}: caught ", text):
            problems.append(f"{name}: not caught as an exception")
    if "JSLOG: top get() threw" not in text or "JSLOG: top survived" not in text:
        problems.append("http.get() at the top level of main.js: not caught (the VM died?)")
    return problems
