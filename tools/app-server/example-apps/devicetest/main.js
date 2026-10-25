// SPDX-License-Identifier: MIT
// Device test: goes through the rabbit's paths one step at a time and
// reports each result to the app server (GET <server>/log?msg=...), where it
// is printed on the server's console. The server's address comes from the
// instance's "server" parameter (settings.json: "apps" -> "devicetest" ->
// "instances" -> "params"), e.g. "http://192.168.1.10:8080".
//
// Each step waits for its callback's last call, or for a time limit, so a
// path that never answers shows as "timed out" instead of stopping the test.

var server = (params[instanceName] && params[instanceName].server) || "http://127.0.0.1:8080";
var steps = [];
var current = null;
var results = [];

function report(text) {
    try { http.get(server + "/log?msg=" + encodeURIComponent("devicetest: " + text)); } catch (e) { log("report failed: " + e); }
    log(text);
}
function str(v) { try { return JSON.stringify(v); } catch (e) { return "" + v; } }

// step(name, limit_ms, fn): fn(done) starts the step and calls done(result)
// when it is over.
function step(name, limit, fn) { steps.push({name: name, limit: limit, fn: fn}); }
function next() {
    var s = steps.shift();
    if (!s) { finish(); return; }
    current = s;
    var over = false;
    function done(result) {
        if (over) return;
        over = true;
        results.push(s.name + ": " + result);
        report(s.name + ": " + result);
        setTimeout(300, function () { next(); return false; });
    }
    setTimeout(s.limit, function () { done("timed out after " + s.limit + " ms"); return false; });
    try { s.fn(done); } catch (e) { done("threw " + e); }
}
function finish() {
    report("all done: " + results.length + " steps");
    exit();
}

// A callback that ends the step on its last call: an event other than "OK"
// (TERMINATED, CANCELLED, ERROR...).
function untilEnd(done) {
    var seen = [];
    return function (event) {
        var type = (event && event.type) ? event.type : event;
        seen.push(type);
        if (type != "OK") done(seen.join(", "));
    };
}

step("led light", 5000, function (done) { karotz.led.light("00FF00", untilEnd(done)); });
step("led pulse", 5000, function (done) { karotz.led.pulse("FFFF00", 500, 2000, untilEnd(done)); });
step("led fade", 5000, function (done) { karotz.led.fade("FF0000", 1000, untilEnd(done)); });
step("ears move", 15000, function (done) { karotz.ears.move(5, 5, untilEnd(done)); });
step("ears reset", 20000, function (done) { karotz.ears.reset(untilEnd(done)); });
step("tts", 30000, function (done) { karotz.tts.start("Device test", "en", untilEnd(done)); });
step("play a sound", 20000, function (done) { karotz.multimedia.play("/usr/karotz/sounds/acknowledge.mp3", untilEnd(done)); });
step("http.get", 10000, function (done) { done(str(http.get(server + "/text"))); });
step("message.list", 10000, function (done) {
    karotz.message.list(function (messages) {
        var ids = [];
        for (var i = 0; i < messages.length; i++) ids.push(messages[i].header.id);
        done(messages.length + " message(s): " + ids.join(", "));
    });
});
step("message.get", 10000, function (done) {
    karotz.message.list(function (messages) {
        if (messages.length == 0) { done("no messages"); return; }
        karotz.message.get({a: messages[0].header.id}, function (got) {
            done(got.length ? got[0].content.type + " " + str(got[0].content.content) : "nothing");
        });
    });
});
step("message.markAsRead", 10000, function (done) {
    karotz.message.list(function (messages) {
        if (messages.length == 0) { done("no messages"); return; }
        karotz.message.markAsRead({a: messages[0].header.id}, function (got) {
            done(got.length ? "status " + got[0].header.status : "nothing");
        });
    });
});
step("message.new (check with appctl.py messages)", 5000, function (done) {
    // Sent without a reply handler (karotzVM keeps none for it).
    karotz.message.new({uuid: "u-ann", objects: []}, "hello from the device test");
    setTimeout(2000, function () { done("sent"); return false; });
});
step("contact.get", 10000, function (done) {
    karotz.contact.get(function (contacts) {
        var names = [];
        for (var i = 0; i < contacts.length; i++) names.push(contacts[i].firstname || contacts[i].alias);
        done(contacts.length + " contact(s): " + names.join(", "));
    });
});
step("social.init", 10000, function (done) {
    social.init(function () {
        done("twitter " + typeof social.twitter + ", facebook " + typeof social.facebook);
    });
});
step("karotz.info", 10000, function (done) { karotz.info(function (info) { done(str(info)); }); });
step("webcam.photo (uploaded to the server)", 30000, function (done) {
    // A Response ("taken"), then an Event ("sent") once the picture is
    // posted; both can be OK, so the step ends on the second call (or on an
    // error).
    var seen = [];
    karotz.webcam.photo(server + "/upload/photo", function (event) {
        seen.push(event.type + (event.description ? " (" + event.description + ")" : ""));
        if (seen.length == 2 || event.type != "OK") done(seen.join(", "));
    });
});
step("record 3 s and upload", 30000, function (done) {
    karotz.multimedia.record(function (event) {
        if (event.type == "OK") {
            setTimeout(3000, function () { karotz.multimedia.stop(); return false; });
            return;
        }
        var answer = "";
        try { answer = str(http.post(server + "/upload/record", {flacFile: event.data}, {}, true)); }
        catch (e) { answer = "upload threw " + e; }
        done(event.type + ", upload: " + answer);
    });
});
step("asr.string (needs voice.conf [asr] at the server)", 20000, function (done) {
    karotz.asr.string("hello rabbit|good night", "en-US", function (result) { done(str(result)); });
});
step("multimedia.artist (needs a USB key)", 10000, function (done) {
    karotz.multimedia.artist(function (artists) { done(str(artists)); });
});
step("ping", 5000, function (done) { karotz.ping(function (r) { done(str(r)); }); });
step("button (press it within 15 s)", 15000, function (done) {
    karotz.button.addListener(function (type) { done("pressed: " + type); return true; });
});

karotz.connect("127.0.0.1", 9123);
karotz.start(function () {
    report("interactive; server " + server);
    next();
}, {});
