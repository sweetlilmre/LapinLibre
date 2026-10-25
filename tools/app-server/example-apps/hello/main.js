// SPDX-License-Identifier: MIT
// A minimal Karotz application: say hello, wiggle the ears, then exit.
// The tts callback runs once with "OK" (request accepted) and again when the
// speech ends ("TERMINATED", or "CANCELLED" / "ERROR"): exit only then, since
// exiting resets the rabbit and stops the speech.
// The ears' callback runs the same way ("OK", then how the move ended): speak
// once the move has ended, whichever way. A move of both ears blocked at once
// ends twice (CANCELLED for each ear), so only the first end speaks.
var done = function (event) {
    if (event != "OK") exit();
};
var spoken = false;

karotz.connect("127.0.0.1", 9123);
karotz.start(function (data) {
    karotz.led.light("00FF00");
    karotz.ears.move(5, 5, function (event) {
        if (event == "OK" || spoken) return;
        spoken = true;
        karotz.tts.start("Hello from " + instanceName, "en-US", done);
    });
}, {});
