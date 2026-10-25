# SPDX-License-Identifier: MIT
"""karotzVM: the JavaScript application runner, driven as the controller drives it.

ARM builds only. The harness starts karotzVM with no arguments and its own
stdin (a launch needs a line there), so that run only shows start-up. The
real test runs a second karotzVM (under qemu-arm against the sysroot's
libraries, same command line as the harness uses plus --app_folder) from a
helper, karotzvm_controller.py, inside an empty
network namespace with loopback: the helper writes one launch line at a time
on karotzVM's stdin, plays the controller on 127.0.0.1:9123 (PlatformMessage protobufs,
decoded and encoded with the toolchain's protoc 2.3.0), serves HTTP on
127.0.0.1:8088, and sends unsolicited events when a test application logs
"CMD:<event>". The record holds karotzVM's stderr (its log), every message it
sent, every HTTP request and its stdout, UUIDs numbered and pointers masked.

The test applications, written to /tmp/karotzvm-apps-<harness pid>:

- basic: the global environment (appName, cmdline, launchType, params from
  config.bin, uuid, timestamp, include, file.read/write, setTimeout, log,
  exit), argument errors, connect errors, an uncaught exception in a timer;
- launchrfid / launchasr: the RFID and voice launch types (with the lazily
  parsed launchType.semantic);
- throws / syntax: an exception and a syntax error in main.js (reported,
  then the 10 s autokill as the application never goes interactive);
- badxml / missing: an unparsable and a missing descriptor (silent exit);
- refused: the controller refuses interactive mode (exit 1);
- devices: led, ears, tts, multimedia, webcam, dock with their replies;
- data: contact, message, k2k, info, ping, asr, serial;
- listeners: button, ears, rfid, multimedia, k2k, serial listeners with
  controller events, a reply nobody waits for, a message without id;
- web: http.get/get2/post/post2 against the local server (charset
  conversion, errors), signHMACSHA1, social.init, twitter signing;
- errors: argument checks of every API object, before and after interactive
  mode, then karotz.stop();
- codes / codes2: every call that takes a callback answered with each
  ReturnCode, as a Response alone or followed by an Event (CMD:code=...);
  crashrounds: the same mix repeated ten times (reply callbacks run in the
  application's own context, however many there have been);
- stopping: requests left unanswered across karotz.stop() and answered
  later, the controller's STOP, exit() from a reply callback;
  latestart / slowstart: karotz.start() answered after the 10 s autokill,
  and just in time;
- together (multi1..multi4): three applications at once, a fourth refused,
  one event reaching every listener, ending out of order; compared as
  sorted lists, since the children's output interleaves.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DAEMON = "karotzVM"
DEVICES = []

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
PROTO_DIR = REPO / "userland" / "proto"
sys.path.insert(0, str(REPO / "scripts"))
from lapin import work_dir  # noqa: E402

# The toolchain's protoc 2.3.0, the version the userland's protobuf code is generated with.
PROTOC = work_dir() / "system" / "out" / "host" / "bin" / "protoc-2.3.0"  # the system build's protobuf 2.3.0 (host)

ALL_ACCESSES = ["led", "ears", "tts", "webcam", "asr", "multimedia", "button", "rfid", "k2k", "message",
                "contact", "serial", "dock", "http", "twitter", "facebook", "soundcloud"]

# Shared by the interactive applications: connect, go interactive, then run
# the steps one after the other (each step calls next() when its last
# callback has fired).
PRELUDE = r"""
var steps = [];
function add(f) { steps.push(f); }
function next() {
    var f = steps.shift();
    if (f) setTimeout(1, function () { f(); return false; });
}
function str(v) { try { return JSON.stringify(v); } catch (e) { return "" + v; } }
function show(tag) {
    return function () {
        var a = [];
        for (var i = 0; i < arguments.length; i++) a.push(str(arguments[i]));
        log(tag + "(" + a.join(",") + ") self=" + (this === arguments.callee));
    };
}
// event_reply callbacks: "OK" from the response, then the event's code.
function done(tag) {
    return function (code) {
        log(tag + ": " + code + " self=" + (this === arguments.callee));
        if (code != "OK") next();
    };
}
function attempt(tag, f) {
    try { log(tag + " -> " + str(f())); } catch (e) { log(tag + " threw " + e); }
}
function start() {
    karotz.connect("127.0.0.1", 9123);
    karotz.start(function (data) {
        log("interactive, data " + str(data) + " self=" + (this === arguments.callee));
        next();
    }, {app: appName});
}
"""

CODES_JS = r"""
// Callbacks only log; each step moves on after a fixed wait, so a callback
// that is called twice, or never, shows in the record.
function cb(tag) { return show(tag); }
function wait(ms) { setTimeout(ms, function () { next(); return false; }); }
function round(code) {
    add(function () { log("CMD:code=" + code); wait(600); });
    add(function () {
        log("-- " + code);
        karotz.led.light("FF0000", cb("light"));
        karotz.ears.move(1, 2, cb("move"));
        karotz.ears.reset(cb("reset"));
        karotz.tts.start("x", "fr", cb("tts"));
        karotz.multimedia.play("http://h/a.mp3", cb("play"));
        karotz.multimedia.artist(cb("artist"));
        karotz.multimedia.record(cb("record"));
        karotz.webcam.photo("http://127.0.0.1:8088/up", cb("photo"));
        karotz.dock.light("00FF00", cb("dock"));
        karotz.ping(cb("ping"));
        karotz.info(cb("info"));
        karotz.contact.get(cb("contacts"));
        karotz.message.list(cb("messages"));
        karotz.asr.string("oui|non", "fr-FR", cb("asr"));
        wait(2500);
    });
}
"""

APPS = {
    "basic": {
        "accesses": ["led", "http"],
        "config": True,
        "files": {"lib.js": 'log("lib.js included, appName " + appName); var fromLib = 42;\n'},
        "main": r"""
log("appName=" + appName + " instanceName=" + instanceName + " cmdline=" + cmdline);
log("launchType=" + JSON.stringify(launchType));
log("params=" + JSON.stringify(params));
log("objects: karotz.led " + typeof karotz.led + ", karotz.ears " + typeof karotz.ears + ", http " + typeof http +
    ", social " + typeof social + ", file " + typeof file + ", karotz.http " + typeof karotz.http);
var keys = []; for (var k in karotz) keys.push(k); log("karotz has " + keys.join(","));
log("uuid " + uuid().length + " " + /^[0-9a-f-]+$/.test(uuid()));
log("timestamp " + (Math.abs(parseInt(timestamp()) - new Date().getTime() / 1000) < 100));
log("random " + (typeof karotz.random()));
log("sign " + signHMACSHA1("hello", "key") + " " + signHMACSHA1("", "") + " " + signHMACSHA1("a long text to sign", "k3y"));
try { signHMACSHA1("x"); } catch (e) { log("sign: " + e); }
try { log(1, 2); } catch (e) { log("log: " + e); }
try { log(); } catch (e) { log("log(): " + e); }
log(undefined); log(null); log({a: 1}); log("café");
try { exit(1); } catch (e) { log("exit: " + e); }
try { setTimeout("x"); } catch (e) { log("setTimeout: " + e); }
try { setTimeout(10, 20); } catch (e) { log("setTimeout: " + e); }
include("lib.js");
log("fromLib " + fromLib);
try { include("../basic/lib.js"); } catch (e) { log("include: " + e); }
try { include("descriptor.xml"); } catch (e) { log("include: " + e); }
try { include("a b"); } catch (e) { log("include: " + e); }
try { include(5); } catch (e) { log("include: " + e); }
try { include("x", "y"); } catch (e) { log("include: " + e); }
file.write("data.txt", "hello file");
var r = file.read("data.txt");
log("read " + r.text + " / " + r.text);
try { file.read("nofile").text; } catch (e) { log("read: " + e); }
try { file.read(1); } catch (e) { log("read: " + e); }
try { file.read("../x"); } catch (e) { log("read: " + e); }
try { file.write("y"); } catch (e) { log("write: " + e); }
try { karotz.start(function () {}, {}); } catch (e) { log("start: " + e); }
try { karotz.start(); } catch (e) { log("start(): " + e); }
try { karotz.start(1, 2); } catch (e) { log("start(1,2): " + e); }
try { karotz.start(function () {}, 2); } catch (e) { log("start(f,2): " + e); }
try { karotz.stop(); } catch (e) { log("stop: " + e); }
try { karotz.stop(1); } catch (e) { log("stop(1): " + e); }
try { karotz.led.light("FF0000"); } catch (e) { log("led: " + e); }
try { karotz.connect("127.0.0.1"); } catch (e) { log("connect: " + e); }
try { karotz.connect(1, 2); } catch (e) { log("connect: " + e); }
try { karotz.connect("127.0.0.1", "x"); } catch (e) { log("connect: " + e); }
try { karotz.connect("127.0.0.1", 9124, {}); } catch (e) { log("connect 3 args: " + e); }
try { karotz.connect("127.0.0.1", 9124); } catch (e) { log("connect refused: " + e); }
try { karotz.connect("no.such.host.invalid", 9123); } catch (e) { log("connect unknown: " + e); }
var n = 0;
setTimeout(100, function () {
    n++;
    log("tick " + n + " self=" + (this === arguments.callee));
    return n < 3;
});
// Well after the three ticks (about 300 ms), with room for a slow qemu.
setTimeout(1650, function () { log("not a boolean"); return 1; });
setTimeout(1900, function () { log("bye"); exit(); return true; });
log("end of main.js");
""",
    },
    "launchrfid": {
        "accesses": [],
        "main": r"""
log("launchType=" + JSON.stringify(launchType) + " instanceName=" + instanceName);
exit();
""",
    },
    "launchasr": {
        "accesses": [],
        "main": r"""
log("name " + launchType.name + " text " + launchType.text + " raw " + launchType.rawSemantic +
    " confident " + launchType.confident);
log("semantic " + JSON.stringify(launchType.semantic));
launchType.semantic = 5;
log("semantic again " + JSON.stringify(launchType.semantic));
exit();
""",
    },
    "throws": {
        "accesses": [],
        "main": 'log("before");\nfunction f() { return g(); }\nfunction g() { return missing.property; }\nf();\nlog("after");\n',
    },
    "syntax": {
        "accesses": [],
        "main": 'log("never");\nvar x = (1 +;\n',
    },
    "badxml": {
        "descriptor": "<descriptor><accesses><access>led</access></accesses",
        "main": 'log("never");\n',
    },
    "refused": {
        "accesses": ["led"],
        "options": {"refuse": True},
        "main": PRELUDE + 'start();\nadd(function () { log("never"); });\n',
    },
    "devices": {
        "accesses": ALL_ACCESSES,
        "main": PRELUDE + r"""
add(function () { karotz.led.light("FF0000", done("light")); });
add(function () { karotz.led.fade("00FF00", 500, done("fade")); });
add(function () { karotz.led.pulse("0000FF", 300, 1000, done("pulse")); });
add(function () { karotz.led.light("123456"); karotz.led.fade("abcdef", -1); karotz.led.pulse("1", 2, 3); next(); });
add(function () { karotz.ears.move(5, 7, done("move")); });
add(function () { karotz.ears.moveRelative(1, -2, done("moveRelative")); });
add(function () { karotz.ears.move(null, 3); karotz.ears.move(2.7, "4"); karotz.ears.moveRelative(1, null); next(); });
add(function () { karotz.ears.reset(function (c, l, r) { show("reset").apply(this, arguments); next(); }); });
add(function () { karotz.ears.resetLeft(); karotz.ears.resetRight(); karotz.ears.reset(); next(); });
add(function () { karotz.tts.start("hello", "fr", done("tts")); });
add(function () { karotz.tts.start("bonjour", done("tts2")); });
add(function () { karotz.tts.stop(done("ttsstop")); });
add(function () { karotz.tts.start("café"); karotz.tts.start("a", "b", 3); karotz.tts.stop(); next(); });
add(function () { karotz.multimedia.play("./sound.mp3", done("play")); });
add(function () { karotz.multimedia.play("http://host/a.mp3"); karotz.multimedia.play("./x");
                  karotz.multimedia.play("/abs/b.mp3"); next(); });
add(function () { karotz.multimedia.pause(done("pause")); });
add(function () { karotz.multimedia.resume(done("resume")); });
add(function () { karotz.multimedia.stop(done("stop")); });
add(function () { karotz.multimedia.next(done("next")); });
add(function () { karotz.multimedia.previous(done("previous")); });
add(function () { karotz.multimedia.pause(); karotz.multimedia.resume(); karotz.multimedia.stop();
                  karotz.multimedia.next(); karotz.multimedia.previous(); next(); });
add(function () { karotz.multimedia.artist(function (a) { show("artist").apply(this, arguments); next(); }); });
add(function () { karotz.multimedia.folder(function (a) { show("folder").apply(this, arguments); next(); }); });
add(function () { karotz.multimedia.genre(function (a) { show("genre").apply(this, arguments); next(); }); });
add(function () { karotz.multimedia.playlist(function (a) { show("playlist").apply(this, arguments); next(); }); });
add(function () {
    var calls = 0;
    karotz.multimedia.record(function (e) {
        show("record").apply(this, arguments);
        if (++calls == 2) { karotz.multimedia.play(e.data, done("play record")); }
    });
});
add(function () {
    var calls = 0;
    karotz.webcam.photo("http://127.0.0.1:8088/upload", function (e) {
        show("photo").apply(this, arguments);
        if (++calls == 2) { karotz.multimedia.play(e.data); next(); }
    });
});
add(function () { karotz.webcam.photo("http://x/nocallback"); next(); });
add(function () { karotz.dock.light("FF0000", done("docklight")); });
add(function () { karotz.dock.fade("00FF00", 400, done("dockfade")); });
add(function () { karotz.dock.pulse("0000FF", 300, 900, done("dockpulse")); });
// (dock.move(direction) alone sends an uninitialised time: not tested.)
add(function () { karotz.dock.move("LEFT", 2000); karotz.dock.move("STOP", 0); karotz.dock.move("RIGHT", 5);
                  karotz.dock.move("DOWN", 1.5); next(); });
add(function () { karotz.multimedia.song(function (a) { log("song " + a); }); setTimeout(3000, function () { next(); return false; }); });
add(function () { log("devices done"); exit(); });
start();
""",
    },
    "data": {
        "accesses": ALL_ACCESSES,
        "main": PRELUDE + r"""
add(function () { karotz.contact.get(function (c) { show("contacts").apply(this, arguments); next(); }); });
add(function () { karotz.message.list(function (m) { show("list").apply(this, arguments); next(); }); });
add(function () { karotz.message.get({a: "m1", b: "m2"}, function (m) { show("get").apply(this, arguments); next(); }); });
add(function () { karotz.message["delete"]({x: "m3"}, function (m) { show("delete").apply(this, arguments); next(); }); });
add(function () { karotz.message.markAsRead({x: "m4"}, function (m) { show("read").apply(this, arguments); next(); }); });
add(function () { karotz.message["new"]({uuid: "u-to", objects: ["o1", "o2"]}, "hello message"); next(); });
add(function () { karotz.k2k.call("sip:bob@example"); karotz.k2k.answer(1); karotz.k2k.answer(0);
                  karotz.k2k.hangup(); karotz.k2k.account("id", "domain", "pw", "stun"); next(); });
add(function () { karotz.info(function (i) { show("info").apply(this, arguments); next(); }); });
add(function () { karotz.ping(done("karotz.ping")); });
add(function () { ping(done("ping")); });
add(function () { ping(); karotz.ping(); next(); });
add(function () { karotz.asr.string("oui|non", "fr-FR", function (r) { show("asr").apply(this, arguments); next(); }); });
add(function () { attempt("asr.file", function () { return karotz.asr.file("grammar.xml"); });
                  attempt("asr.file cb", function () { return karotz.asr.file("grammar.xml", function () {}); });
                  attempt("asr.file denied", function () { return karotz.asr.file("../descriptor.xml"); });
                  next(); });
add(function () {
    karotz.serial.open__("/dev/ttyUSB0", 115200);
    karotz.serial.open__("/dev/ttyUSB1");
    karotz.serial.open__("/dev/ttyUSB2", 9600, 3);
    karotz.serial.write__("/dev/ttyUSB0", "a", "hello serial");
    karotz.serial.write__("/dev/ttyUSB0");
    attempt("open__ bad baud", function () { return karotz.serial.open__("p", "x"); });
    attempt("open__()", function () { return karotz.serial.open__(); });
    next();
});
add(function () {
    karotz.serial.open("/dev/ttyGS0", 9600);
    karotz.serial.write("plain line");
    karotz.serial.write("0a0d", "h");
    karotz.serial.write("/dev/ttyUSB0", "a", "three arguments");
    attempt("write()", function () { return karotz.serial.write(); });
    // The controller answers in order: its answer to the ping comes once it has every message above.
    karotz.ping(function () { next(); });
});
add(function () { log("data done"); exit(); });
start();
""",
    },
    "listeners": {
        "accesses": ALL_ACCESSES,
        "main": PRELUDE + r"""
function listen(tag, keep) {
    return function () {
        show(tag).apply(this, arguments);
        next();
        return keep;
    };
}
add(function () { attempt("serial.write before open", function () { return karotz.serial.write("x"); });
                  attempt("serial.addListener before open",
                          function () { return karotz.serial.addListener(function () {}); });
                  next(); });
add(function () { karotz.button.addListener(listen("button", true)); log("CMD:button"); });
add(function () { log("CMD:button-long"); });
add(function () { karotz.ears.addListener(listen("earsListener", false)); log("CMD:ears"); });
add(function () { karotz.rfid.addListener(listen("rfidListener", true)); log("CMD:rfid"); });
add(function () { log("CMD:rfid-bare"); });
add(function () { karotz.multimedia.addListener(listen("mmListener", "yes")); log("CMD:multimedia"); });
add(function () { karotz.k2k.addListener(listen("k2kListener", true)); log("CMD:k2k"); });
add(function () {
    karotz.serial.addListener__({onMessage: function (self, port, data) {
        log("serial onMessage " + port + " " + data + " " + (typeof self));
        next();
        return true;
    }});
    log("CMD:serial");
});
add(function () {
    karotz.serial.open("/dev/ttyGS0");
    karotz.serial.addListener(function (data) { log("serial line " + data); });
    log("CMD:serial");
});
add(function () { log("CMD:ears-blocked"); log("CMD:nocorr"); log("CMD:noid"); log("CMD:dock"); log("CMD:button");
                  setTimeout(4000, function () { next(); return false; }); });
add(function () { log("listeners done"); exit(); });
start();
""",
    },
    "web": {
        "accesses": ALL_ACCESSES,
        "main": PRELUDE + r"""
var base = "http://127.0.0.1:8088";
add(function () {
    attempt("get", function () { return http.get(base + "/text"); });
    attempt("get headers map", function () { return http.get(base + "/text?h", {"X-One": "1", "X-Two": "2"}); });
    attempt("get headers array", function () { return http.get(base + "/text?a", [{"A": "1"}, {"B": "2"}]); });
    attempt("get latin1", function () { return http.get(base + "/latin1"); });
    attempt("get meta", function () { return http.get(base + "/meta"); });
    attempt("get missing", function () { return http.get(base + "/missing"); });
    attempt("get refused", function () { return http.get("http://127.0.0.1:8089/"); });
    attempt("get2", function () { return http.get2(base + "/text?2"); });
    attempt("post", function () { return http.post(base + "/form", {a: "1", b: "x y&z"}); });
    attempt("post headers", function () { return http.post(base + "/form?h", {a: "1"}, {"X-H": "v"}); });
    attempt("post empty", function () { return http.post(base + "/empty", {}); });
    attempt("post multipart", function () { return http.post(base + "/multi", {a: "1", b: "2"}, {}, true); });
    attempt("post2", function () { return http.post2(base + "/form2", {k: "v"}); });
    attempt("post2 latin1", function () { return http.post2(base + "/latin1", {k: "v"}); });
    next();
});
add(function () {
    social.init(function (d) {
        show("social").apply(this, arguments);
        log("twitter " + typeof social.twitter + " facebook " + typeof social.facebook +
            " soundcloud " + typeof social.soundcloud);
        attempt("twitter keys", function () { return [social.twitter.oauth_consumer_key, social.twitter.oauth_token]; });
        attempt("twitter sign", function () { return social.twitter.sign("GET&http%3A%2F%2Fx&a%3D1"); });
        attempt("facebook get", function () { return social.facebook.get("http://127.0.0.1:8088/fb"); });
        attempt("soundcloud post", function () { return social.soundcloud.post("http://127.0.0.1:8088/sc", {a: "1"}); });
        next();
    }, {d: 1});
});
add(function () { log("web done"); exit(); });
start();
""",
    },
    "httpcrash": {
        "accesses": ["http"],
        "main": r"""
// After its "Signature error function" exception http.get goes on with the
// undefined it got back (ToObject, Get("content")).
try { log("get() -> " + http.get()); } catch (e) { log("get() threw " + e); }
try { log("post(u) -> " + http.post("http://127.0.0.1:8088/x")); } catch (e) { log("post(u) threw " + e); }
log("survived");
exit();
""",
    },
    "errors": {
        "accesses": ALL_ACCESSES,
        "main": PRELUDE + r"""
function battery(when) {
    var calls = {
        "led.light()": function () { return karotz.led.light(); },
        "led.light(1)": function () { return karotz.led.light(1); },
        "led.fade(c)": function () { return karotz.led.fade("c"); },
        "led.pulse(c,1)": function () { return karotz.led.pulse("c", 1); },
        "ears.move()": function () { return karotz.ears.move(); },
        "ears.moveRelative(1)": function () { return karotz.ears.moveRelative(1); },
        "ears.reset(1)": function () { return karotz.ears.reset(1); },
        "ears.addListener()": function () { return karotz.ears.addListener(); },
        "tts.start()": function () { return karotz.tts.start(); },
        "tts.stop(1)": function () { return karotz.tts.stop(1); },
        "multimedia.play()": function () { return karotz.multimedia.play(); },
        "multimedia.pause(1,2)": function () { return karotz.multimedia.pause(1, 2); },
        "multimedia.artist()": function () { return karotz.multimedia.artist(); },
        "multimedia.record()": function () { return karotz.multimedia.record(); },
        "multimedia.addListener(1)": function () { return karotz.multimedia.addListener(1); },
        "webcam.photo()": function () { return karotz.webcam.photo(); },
        "button.addListener()": function () { return karotz.button.addListener(); },
        "rfid.addListener(1)": function () { return karotz.rfid.addListener(1); },
        "k2k.call()": function () { return karotz.k2k.call(); },
        "k2k.answer()": function () { return karotz.k2k.answer(); },
        "k2k.hangup(1)": function () { return karotz.k2k.hangup(1); },
        "k2k.account(1)": function () { return karotz.k2k.account(1); },
        "message.list()": function () { return karotz.message.list(); },
        "message.get(1,f)": function () { return karotz.message.get(1, function () {}); },
        "message.new(5,t)": function () { return karotz.message["new"](5, "t"); },
        "message.new(o,3)": function () { return karotz.message["new"]({uuid: "u", objects: []}, 3); },
        "message.new(o)": function () { return karotz.message["new"]({}); },
        "contact.get()": function () { return karotz.contact.get(); },
        "info(1)": function () { return karotz.info(1); },
        "ping(1)": function () { return karotz.ping(1); },
        "asr.string(a,b)": function () { return karotz.asr.string("a", "b"); },
        "asr.file()": function () { return karotz.asr.file(); },
        "serial.write__()": function () { return karotz.serial.write__(); },
        "dock.move(1)": function () { return karotz.dock.move(1); },
        "dock.light()": function () { return karotz.dock.light(); },
        "social.init()": function () { return social.init(); }
    };
    for (var name in calls) attempt(when + " " + name, calls[name]);
}
battery("before");
// The controller's answers to the battery's requests (led.light(1)'s, unawaited) arrive before the
// stop: with next()'s 1 ms timer the two raced, and the record's order changed with the build's speed.
add(function () { battery("interactive"); setTimeout(500, function () { next(); return false; }); });
add(function () {
    attempt("stop", function () { return karotz.stop(); });
    attempt("led after stop", function () { return karotz.led.light("FFFFFF"); });
    setTimeout(1000, function () { log("errors done"); exit(); return false; });
});
start();
""",
    },
    # Every call that takes a callback, answered with each kind of failure
    # (CMD:code=Response[/Event], see karotzvm_controller).
    "codes": {
        "accesses": ALL_ACCESSES,
        "main": PRELUDE + CODES_JS + r"""
["ERROR", "TIMEOUT", "BUSY", "BAD_INTERACTIVEID", "NOT_CONNECTED", "CANCELLED"].forEach(round);
add(function () { log("CMD:code-off"); log("codes done"); exit(); });
start();
""",
    },
    # The rest of the codes, from a fresh process.
    "codes2": {
        "accesses": ALL_ACCESSES,
        "main": PRELUDE + CODES_JS + r"""
["OK/ERROR", "OK/CANCELLED", "OK/TIMEOUT", "OK/SCHEDULE_EVENT", "ERROR/TERMINATED"].forEach(round);
add(function () { log("CMD:code-off"); log("codes2 done"); exit(); });
start();
""",
    },
    # Ten rounds of the mix in one application: every reply callback runs
    # in the application's own context, however many there have been
    # (recorded in summary mode).
    "crashrounds": {
        "accesses": ALL_ACCESSES,
        "summary": True,
        "main": PRELUDE + CODES_JS + r"""
["ERROR", "ERROR", "ERROR", "ERROR", "ERROR", "ERROR", "ERROR", "ERROR", "ERROR", "ERROR"].forEach(round);
add(function () { log("CMD:code-off"); log("crashrounds done"); exit(); });
start();
""",
    },
    # Stopping. Requests still unanswered when the
    # application stops interactive mode (their answers come later), the
    # controller ending interactive mode itself, exit() from a callback.
    "stopping": {
        "accesses": ALL_ACCESSES,
        "main": PRELUDE + r"""
function wait(ms) { setTimeout(ms, function () { next(); return false; }); }
add(function () { log("CMD:hold"); wait(600); });
add(function () {
    karotz.led.light("FF0000", show("light"));
    karotz.tts.start("pending", "fr", show("tts"));
    karotz.ears.reset(show("reset"));
    karotz.multimedia.artist(show("artist"));
    attempt("stop", function () { return karotz.stop(); });
    wait(600);
});
add(function () { log("CMD:flush"); wait(1500); });
add(function () {
    attempt("led after stop", function () { return karotz.led.light("00FF00", show("light2")); });
    wait(1000);
});
add(function () { log("CMD:stop"); wait(1000); });
add(function () {
    attempt("led after the controller's stop", function () { return karotz.led.light("0000FF", show("light3")); });
    wait(1000);
});
add(function () {
    karotz.ping(function (code) {
        log("ping " + code + ", exit() from the callback");
        exit();
        log("after exit()");
    });
    setTimeout(3000, function () { log("still running 3 s after exit()"); return false; });
});
start();
""",
    },
    # The controller answers karotz.start() after 12 s: the 10 s autokill of
    # an application that isn't interactive yet comes first.
    "latestart": {
        "accesses": ["led"],
        "options": {"start_delay": 12},
        "main": PRELUDE + r"""
setTimeout(5000, function () { log("5 s, not interactive yet"); return false; });
add(function () { log("never: interactive after the autokill"); exit(); });
start();
""",
    },
    # Answered after 4 s: interactive in time, and the autokill finds it so.
    "slowstart": {
        "accesses": ["led"],
        "options": {"start_delay": 4},
        "main": PRELUDE + r"""
add(function () { karotz.led.light("FF00FF", show("light")); setTimeout(12000, function () { log("12 s, past the autokill"); exit(); return false; }); });
start();
""",
    },
}

# Errors an application must survive or be ended for cleanly: a timer
# callback that throws (reported, the application goes on), include() of a
# file with a syntax error or of a missing one (thrown into the including
# script), no main.js (logged, the 10 s autokill ends the child), a
# file.write() that can't create its file (throws), multimedia.play() with no
# usable url (throws), the controller dropping the connection (the
# application ends). missinginclude, nomain and lostcontroller run last of
# all (karotzVM runs three children at most). All are recorded in summary
# mode.
APPS.update({
    "timerthrow": {"accesses": [], "summary": True, "main": r"""
setTimeout(100, function () { log("about to throw"); return missing.thing; });
setTimeout(600, function () { log("still alive after the throw"); exit(); return false; });
"""},
    "badinclude": {"accesses": ["led"], "summary": True, "files": {"bad.js": "var x = (1 +;\n"}, "main": r"""
// (Quirk: at the top level, try/catch and exit() together kill V8 2.5
// itself while it compiles the script, so this runs from a timer.)
setTimeout(10, function () {
    try { include("bad.js"); } catch (e) { log("include bad.js threw " + e); }
    log("after the include");
    exit();
    return false;
});
"""},
    "missinginclude": {"accesses": ["led"], "summary": True, "main": r"""
// (Quirk: at the top level, try/catch and exit() together kill V8 2.5
// itself while it compiles the script, so this runs from a timer.)
setTimeout(10, function () {
    try { include("nofile.js"); } catch (e) { log("include nofile.js threw " + e); }
    log("after the include");
    exit();
    return false;
});
"""},
    "nomain": {"accesses": [], "summary": True, "main": None},
    "writefail": {"accesses": [], "summary": True, "main": r"""
setTimeout(10, function () {
    try { file.write("nodir/x.txt", "hi"); } catch (e) { log("write threw " + e); }
    log("after the write");
    exit();
    return false;
});
"""},
    "playnourl": {"accesses": ["multimedia"], "summary": True, "main": PRELUDE + r"""
add(function () {
    try { karotz.multimedia.play(5); } catch (e) { log("play(5) threw " + e); }
    log("after play(5)");
    exit();
});
start();
"""},
    "lostcontroller": {"accesses": ["led"], "summary": True, "main": PRELUDE + r"""
add(function () { log("CMD:close"); setTimeout(3000, function () { log("never: still running 3 s later"); return false; }); });
start();
"""},
})
SPINNERS = [("missinginclude", 25), ("nomain", 25), ("lostcontroller manual", 25)]

# Three applications at once (karotzVM's limit), a
# fourth launch while they run (refused), one button event reaching every
# application's listener, and the applications ending in another order
# than they started (3, 1, 2).
MULTI_JS = PRELUDE + r"""
var ends = {multi1: 8000, multi2: 11000, multi3: 6000};
add(function () {
    karotz.button.addListener(function (type) { log(appName + " button " + type); return true; });
    if (appName == "multi1") setTimeout(4000, function () { log("CMD:button-all"); return false; });
    setTimeout(ends[appName], function () { log(appName + " exits"); exit(); return false; });
});
start();
"""
for _name in ("multi1", "multi2", "multi3", "multi4"):
    APPS[_name] = {"accesses": ["button"], "main": MULTI_JS}

# Launches of several lines at once: (title, lines, children expected to end,
# timeout[, freeze]); freeze = (after, for): the karotzVM parent is stopped
# (SIGSTOP) that many seconds after the lines are written, for that long, so
# children ending meanwhile give it a single SIGCHLD (tests/aggressive).
PARALLEL = [
    ("together", ["multi1 a", "multi2 b", "multi3 c", "multi4 d"], 3, 90),
]

# Launch lines, in order: (line, timeout in seconds, app).
# How the helper ends karotzVM: an empty line, or "eof" (stdin closed, as when
# the controller goes away; tests/aggressive).
END = None
# Whether karotzVM runs with tests/fakes/timeshift-arm.c's preload, so that the
# helper's CMD:age=N can move its clock (tests/aggressive).
TIMESHIFT = False

LAUNCHES = [
    ("basic normal inst-1", 90),
    ("launchrfid RFID;e0040100;5;FLAT;3;RED;payload tag-instance", 60),
    ("launchasr ASR;bonjour;<answer>yes</answer><x><y>1</y></x>", 60),
    ("launchasr", 60),
    ("throws", 60),
    ("syntax", 60),
    ("badxml", 60),
    ("missing x y z", 60),
    ("refused", 60),
    ("devices manual", 180),
    ("data manual", 120),
    ("listeners manual", 120),
    ("web manual", 120),
    ("httpcrash", 60),
    ("errors manual", 120),
    ("codes manual", 180),
    ("codes2 manual", 180),
    ("crashrounds manual", 180),
    ("timerthrow", 60),
    ("badinclude", 60),
    ("writefail", 60),
    ("playnourl manual", 60),
    ("stopping manual", 120),
    ("latestart", 60),
    ("slowstart", 60),
]

CONFIG_APP = """apiKey: "key-1" version: "1.0" name: "basic" deployment: "test"
parameters { key: "color" value: "blue" } parameters { key: "size" value: "3" }
configInstance { name: "inst-1" uuid: "uuid-inst-1" accessToken: "token-1"
  params { key: "volume" value: "7" } params { key: "voice" value: "alice" } }
configInstance { name: "inst-2" params { key: "volume" value: "2" } }
"""


def descriptor(accesses):
    items = "".join(f"<access>{a}</access>" for a in accesses)
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<descriptor><version>1</version><accesses>{items}</accesses></descriptor>\n'


def make_apps(folder):
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    for name, app in APPS.items():
        d = folder / name
        d.mkdir()
        (d / "descriptor.xml").write_text(app.get("descriptor") or descriptor(app.get("accesses", [])))
        if app["main"] is not None:
            (d / "main.js").write_text(app["main"])
        for fname, text in app.get("files", {}).items():
            (d / fname).write_text(text)
        if app.get("config_dir"):
            # A config.bin that stat finds but fread can't read (EISDIR).
            (d / "config.bin").mkdir()
        elif app.get("config"):
            r = subprocess.run([str(PROTOC), "--encode=lapinlibre.platform.ConfigApp",
                                f"--proto_path={PROTO_DIR}", str(PROTO_DIR / "platform-message.proto")],
                               input=CONFIG_APP.encode(), capture_output=True, check=True)
            (d / "config.bin").write_bytes(r.stdout)


def drive(d, bus):
    import harness
    from harness import LOOPBACK_UP, SANDBOX, make_stub_dir

    record = []
    folder = Path(f"/tmp/karotzvm-apps-{os.getpid()}")
    make_apps(folder)
    work = Path(tempfile.mkdtemp(prefix="karotzvm-"))
    launches = []
    only = os.environ.get("KAROTZVM_ONLY")  # development aid: a subset of the applications
    for line, timeout in LAUNCHES:
        if only and line.split(" ")[0] not in only.split(","):
            continue
        app = APPS.get(line.split(" ")[0], {})
        full = os.environ.get("KAROTZVM_FULL")  # development aid: no summary mode
        launches.append({"line": line, "timeout": timeout, "options": app.get("options", {}),
                         "summary": app.get("summary", False) and not full})
    for entry in PARALLEL:
        title, lines, ends, timeout = entry[:4]
        if only and title not in only.split(","):
            continue
        launches.append({"line": title, "lines": lines, "ends": ends, "timeout": timeout,
                         "freeze": entry[4] if len(entry) > 4 else None})
    for line, timeout in SPINNERS:
        if only and line.split(" ")[0] not in only.split(","):
            continue
        launches.append({"line": line, "timeout": timeout, "summary": not os.environ.get("KAROTZVM_FULL")})
    extra = []
    if TIMESHIFT:
        extra = ["-E", f"LD_PRELOAD={harness.FAKES / 'timeshift-arm.so'}",
                 "-E", f"KAROTZ_TIMESHIFT_FILE={work / 'timeshift'}"]
    command = harness.qemu_command(d.sysroot, d.build, DAEMON, extra)
    config = {
        "command": command,
        "args": [f"--app_folder={folder}"],
        "launches": launches,
        "protoc": str(PROTOC),
        "proto_dir": str(PROTO_DIR),
        "stdout": str(work / "stdout"),
        "record": str(work / "record.json"),
        "end": END,
        "timeshift": str(work / "timeshift"),
    }
    (work / "config.json").write_text(json.dumps(config))
    env = {**os.environ, "PATH": f"{make_stub_dir(work)}:/usr/bin:/bin", "TZ": "UTC0"}
    helper = HERE / "karotzvm_controller.py"
    script = LOOPBACK_UP + f'exec python3 -I -B {helper} {work / "config.json"}'
    try:
        subprocess.run([*SANDBOX, "/bin/sh", "-c", script], env=env, timeout=1500,
                       stdout=open(work / "helper.log", "w"), stderr=subprocess.STDOUT)
    except subprocess.TimeoutExpired:
        record.append("== helper timed out")
    try:
        record.extend(json.load(open(work / "record.json")))
    except (OSError, ValueError) as e:
        record.append(f"== no record: {e}: {(work / 'helper.log').read_text(errors='replace')[-2000:]}")
    shutil.rmtree(work, ignore_errors=True)
    shutil.rmtree(folder, ignore_errors=True)
    return record

