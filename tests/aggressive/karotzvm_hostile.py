# SPDX-License-Identifier: MIT
"""karotzVM: a misbehaving controller and careless applications.

Reply handlers that free their data, against every reply sent twice and a
reply to no request; an ASR result without recognition, serial listeners,
an empty <access/>; a post without parameters; message.new's checks;
dock.move's callback and a bad direction; messages from the controller
that don't parse (dropped, not given to a listener; a malformed reply to a
waiting request gives its callback ERROR); stdin closed, as when the
controller goes away. The karotzVM scenario's machinery runs two
applications of its own, then closes stdin. ARM only, like the karotzVM
scenario.

What must hold: every callback is called once (twice would mean a handler
running again on a repeat, on data already freed), nothing crashes, the
checks throw, and karotzVM exits when its input closes.
"""

import re

from scenarios import karotzVM as kvm

DAEMON = "karotzVM"
DEVICES = []

HOSTILE_JS = kvm.PRELUDE + r"""
var counts = {};
function once(tag) {
    counts[tag] = 0;
    return function () { counts[tag]++; log(tag + " #" + counts[tag]); };
}
function wait(ms) { setTimeout(ms, function () { next(); return false; }); }
add(function () {
    social.init(once("social"));
    karotz.contact.get(once("contacts"));
    karotz.info(once("info"));
    karotz.message.list(once("messages"));
    karotz.multimedia.artist(once("artist"));
    karotz.asr.string("oui|non", "fr-FR", once("asr"));
    wait(3000);
});
add(function () { log("CMD:repeat-off"); log("CMD:stray"); log("CMD:asr-norec"); wait(500); });
add(function () { karotz.asr.string("oui|non", "fr-FR", once("asr-norec")); wait(2000); });
add(function () { attempt("post {}", function () { return typeof http.post("http://127.0.0.1:8088/empty", {}); }); next(); });
add(function () {
    attempt("get endenc", function () { return http.get("http://127.0.0.1:8088/endenc"); });
    // Character codes, not the text: log() passes strings out byte-wise,
    // so a converted U+00E9 and a raw 0xE9 would look alike in the record.
    attempt("get accents", function () {
        var t = http.get("http://127.0.0.1:8088/accents");
        return t.length + " chars, first code " + t.charCodeAt(0);
    });
    next();
});
add(function () {
    attempt("message.new no objects", function () { return karotz.message["new"]({uuid: "u"}, "x"); });
    attempt("message.new objects string", function () { return karotz.message["new"]({uuid: "u", objects: "o1"}, "x"); });
    attempt("message.new text number", function () { return karotz.message["new"]({uuid: "u", objects: []}, 3); });
    next();
});
add(function () {
    attempt("dock.move FORWARD", function () { return karotz.dock.move("FORWARD"); });
    karotz.dock.move("UP", 500, once("dockmove"));
    wait(1500);
});
// Two messages that don't parse, then a good button event: only that one
// reaches the listener.
add(function () {
    karotz.button.addListener(function (type) { log("button seen " + type); return true; });
    log("CMD:malformed");
    log("CMD:button");
    wait(1500);
});
// A request whose reply doesn't parse: its callback gets ERROR in its place.
add(function () { log("CMD:hold"); wait(500); });
add(function () { karotz.led.light("00FF00", function (code) { log("held light " + code); }); wait(500); });
add(function () { log("CMD:malformed-replies"); wait(1500); });
add(function () {
    attempt("serial.addListener__(5)", function () { return karotz.serial.addListener__(5); });
    attempt("serial.addListener__({})", function () { return karotz.serial.addListener__({}); });
    log("CMD:serial");
    wait(1000);
});
add(function () { log("counts " + str(counts)); log("hostile done"); exit(); });
// Every reply twice from here, karotz.start's included.
log("CMD:repeat");
start();
"""

NOACCESS_JS = kvm.PRELUDE + r"""
add(function () { karotz.led.light("FF0000", function (code) { log("light " + code); }); setTimeout(800, function () { next(); return false; }); });
add(function () { log("noaccess done"); exit(); });
start();
"""

APPS = {
    "hostile": {"accesses": kvm.ALL_ACCESSES, "main": HOSTILE_JS},
    # Empty and non-text <access> elements before a real one.
    "noaccess": {"descriptor": '<?xml version="1.0"?>\n<descriptor><version>1</version><accesses>'
                               '<access/><access><x/></access><access>led</access></accesses></descriptor>\n',
                 "accesses": ["led"], "main": NOACCESS_JS},
    # A config.bin that can't be read: no parameters, not what wasn't read.
    "dirconfig": {"accesses": [], "config_dir": True,
                  "main": 'log("dirconfig params=" + JSON.stringify(params)); exit();\n'},
}
LAUNCHES = [("hostile manual", 120), ("noaccess manual", 60), ("dirconfig manual", 60)]


def drive(d, bus):
    saved = (kvm.APPS, kvm.LAUNCHES, kvm.PARALLEL, kvm.SPINNERS, kvm.END)
    kvm.APPS, kvm.LAUNCHES, kvm.PARALLEL, kvm.SPINNERS, kvm.END = APPS, LAUNCHES, [], [], "eof"
    try:
        return kvm.drive(d, bus)
    finally:
        kvm.APPS, kvm.LAUNCHES, kvm.PARALLEL, kvm.SPINNERS, kvm.END = saved


def check(record):
    problems = []
    text = "\n".join(record)
    # Reply handlers that free their data: one call each. dock.move's
    # callback is event-style, called for the Response and for the Event.
    calls = {"social": 1, "contacts": 1, "info": 1, "messages": 1, "artist": 1, "asr": 1, "asr-norec": 1,
             "dockmove": 2}
    for tag, n in calls.items():
        if f"JSLOG: {tag} #1" not in text:
            problems.append(f"{tag}: callback never called")
        if f"JSLOG: {tag} #{n + 1}" in text:
            problems.append(f"{tag}: callback called more than {n} time(s): a repeated reply ran it again")
    if len(re.findall(r'JSLOG: interactive, data \{"app":"hostile"\}', text)) != 1:
        problems.append("karotz.start's callback not called exactly once")
    # A7: the charset word at the very end is left alone; an all-accents
    # Latin-1 body comes through converted (20 x U+00E9).
    for expected in ('get endenc -> "tail encoding"', 'get accents -> "20 chars, first code 233"', "post {} -> ", "message.new no objects threw Arg[0].objects Must be array",
                     "message.new objects string threw Arg[0].objects Must be array",
                     "message.new text number threw Arg[1] Must be string",
                     "dock.move FORWARD threw Wrong 1st arguments",
                     "serial.addListener__(5) threw Wrong arguments",
                     "hostile done", "light OK", "noaccess done",
                     "no params loaded: short read of", "JSLOG: dirconfig params={}"):
        if expected not in text:
            problems.append(f"missing: {expected}")
    # Messages that don't parse are dropped, with a warning naming them.
    seen = re.findall(r"JSLOG: button seen (\w+)", text)
    if seen != ["DOUBLE"]:
        problems.append(f"button listener calls {seen}, expected one DOUBLE (malformed messages must not reach it)")
    if len(re.findall(r"WARNING: malformed message \(\d+ bytes\) not handled: .*fields: buttonCallback", text)) != 2:
        problems.append("the two malformed button messages weren't each logged as malformed")
    if "missing: buttonCallback.type" not in text:
        problems.append("the malformed message's warning doesn't name the missing field")
    if "JSLOG: held light ERROR" not in text:
        problems.append("a request whose reply didn't parse: its callback didn't get ERROR")
    if re.search(r"signal (11|6)|Segmentation|Aborted|core dumped", text):
        problems.append("a crash")
    if "== karotzVM exit status: still running" in record:
        problems.append("karotzVM kept running after its input closed")
    return problems
