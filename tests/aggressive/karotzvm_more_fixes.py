# SPDX-License-Identifier: MIT
"""karotzVM: the fixes that writing the documentation turned up.

One application ("more") checks:

- karotz.message.new(to, text, callback) calls the callback with the reply's
  code ("OK"), once;
- karotz.dock.addListener(listener) is there, and the listener hears the
  dock's status message (status 1, change 2);
- karotz.webcam.photo("direct", callback): the controller's JPEG reaches the
  application: the callback's object has type "OK" and data, and posting
  that data sends the picture's bytes;
- http.get from a server that sends its headers and then stalls fails within
  the stall limit (30 s), and the application goes on.

multimedia.play("./a") under the application's own folder is in
karotzvm_api_fixes. ARM only, like the karotzVM scenario.
"""

import re

from scenarios import karotzVM as kvm

DAEMON = "karotzVM"
DEVICES = []

STALL_LIMIT_S = 45  # the stall limit (30 s) and a margin for qemu

MORE_JS = kvm.PRELUDE + r"""
var base = "http://127.0.0.1:8088";
function wait(ms) { setTimeout(ms, function () { next(); return false; }); }
add(function () {
    karotz.message.new({uuid: "u-ann", objects: ["o1"]}, "hello", show("msgnew"));
    karotz.message.new({uuid: "u-ann", objects: ["o1"]}, "no callback");
    wait(1500);
});
add(function () {
    karotz.dock.addListener(function (status, change) { log("dock listener " + status + " " + change); return true; });
    log("CMD:dock");
    wait(1500);
});
add(function () {
    karotz.webcam.photo("direct", function (r) {
        log("direct type " + r.type + " data " + (r.data ? "yes" : "no"));
        if (r.data) attempt("post picture", function () { return http.post(base + "/picture", {f: r.data}); });
    });
    wait(3000);
});
add(function () {
    var started = new Date().getTime();
    attempt("stall", function () { return http.get(base + "/stall"); });
    log("stall took " + Math.round((new Date().getTime() - started) / 1000) + " s");
    next();
});
add(function () { log("more done"); exit(); });
start();
"""

APPS = {"more": {"accesses": kvm.ALL_ACCESSES, "main": MORE_JS}}
LAUNCHES = [("more manual", 180)]


def drive(d, bus):
    saved = (kvm.APPS, kvm.LAUNCHES, kvm.PARALLEL, kvm.SPINNERS)
    kvm.APPS, kvm.LAUNCHES, kvm.PARALLEL, kvm.SPINNERS = APPS, LAUNCHES, [], []
    try:
        return kvm.drive(d, bus)
    finally:
        kvm.APPS, kvm.LAUNCHES, kvm.PARALLEL, kvm.SPINNERS = saved


def check(record):
    text = "\n".join(record)
    problems = []
    if "JSLOG: more done" not in text:
        problems.append("the application didn't finish")
    calls = re.findall(r'msgnew\(([^)]*)\)', text)
    if calls != ['"OK"']:
        problems.append(f"message.new's callback: {calls or 'never called'}, expected once with \"OK\"")
    if "dock listener 1 2" not in text:
        problems.append("the dock listener didn't hear the dock's status (1, 2)")
    if "direct type OK data yes" not in text:
        problems.append("photo(\"direct\") didn't give the picture's data")
    if not re.search(r"POST /picture .*b'\\xff\\xd8JPEG\\x00bytes\\xff\\xd9'", text):
        problems.append("posting the direct picture didn't send its bytes")
    m = re.search(r"stall took (\d+) s", text)
    if not m:
        problems.append("http.get from a stalled server never returned")
    elif int(m.group(1)) > STALL_LIMIT_S:
        problems.append(f"http.get from a stalled server took {m.group(1)} s (limit {STALL_LIMIT_S})")
    return problems


def summary(record):
    m = re.search(r"stall took (\d+) s", "\n".join(record))
    return f"stalled http.get returned after {m.group(1) if m else '?'} s"
