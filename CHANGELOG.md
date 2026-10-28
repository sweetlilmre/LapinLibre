<!-- SPDX-License-Identifier: MIT -->
# Changelog

## Unreleased

### Fixed

- A tag whose action is an `https://` URL now works. The tag listener's curl checks certificates, but the system had no CA certificates, so every such request failed.
- A tag laid on the nose gets the same feedback whether or not the app server is reachable, and only once. Before, the light, sound and ear move came from the controller, which reacts only once logged in to its app server: without it, a tag with an action got only a short green flash before its action, and an unknown tag got nothing at all; with it, a tag from the web page got the controller's reaction and the tag listener's flash at the same time.
- Stopping a sound no longer logs the warning "player not exited properly: bug". The multimedia daemon stops the player by ending it, and it took that end for a fault.
- Learning a new tag from the web page no longer makes the rabbit say it doesn't recognise the object.
- The voice daemon answers other requests while it connects to a speech recognition server. It used to wait for the connection inside the call that started recognition, so a server that didn't answer held up speech and every other call for up to 5 s per server. Stopping recognition during the connection now ends the session.

### Added

- The tag listener gives every tag's feedback, in the rabbit's language. The nose turns red as soon as a tag is read (about 0.2 to 0.4 s after it on a rabbit; a speech action then starts about 2.5 to 3 s after the tag). A tag with an action, or an application's tag, then plays a tone before its action or application. A tag learned on the web page but without an action plays the error sound and "I know this object, but it hasn't been given an action yet". An unknown tag plays the error sound and "I don't recognise this object". Nothing shows or plays while the rabbit sleeps or an application has control. The new phrase is in English, French, German and Spanish; other languages use the English one.
- The device test application also takes a photo directly and streams video for 5 s, then stops it. The device test procedure in `docs/app-server.md` ends by removing the application from the rabbit.

### Changed

- The controller no longer reacts to a tag itself: no orange and green pulses, no `notify.mp3`, no "tag unknown" sound and no ear move. It still passes the tag to an interactive application and to the app server. The tag listener starts an application's tag through two new controller methods, `has_tag_app` and `start_tag_app`.
- One error sound for the whole system: the install's error sound is now `error.mp3`, on the stick and in `/usr/karotz/sounds`.
- The notification tone and the camera's shutter are WAV files: `notify.wav` (a tag about to run its action or application, a tag learned, the app server gone) and `shutter.wav`. The player is done with a WAV about half a second sooner than with an MP3, so what follows the sound comes sooner. `notify.mp3` and `shutter.mp3` are gone: an application that plays `notify.mp3` by its path should play `notify.wav`.
- Applications' HTTPS requests (`http` in karotzVM, and the `social` posts) now check the server's certificate against the CA certificates in `/etc/ssl/certs`, from the ca-certificates package. A request to a server whose certificate is self-signed, expired or for another name gets `"curl error"`, and the log says why. The check adds about 0.2 s of CPU per request on the rabbit; a whole request to a site such as example.com or github.com takes 0.7 to 2.6 s. An HTTPS request made before `ntpd` has set the clock after a boot waits for it, for up to 60 s. The checks can't be turned off.

## v0.1.1 (2026-10-26)

### Fixed

- The web page and the API no longer wait for the controller. The controller answers no one while it waits for its app server (up to 5 s at a time), so every status request and every action on the rabbit could stall for seconds. The tag listener now follows the controller's state from its signals and keeps it in RAM; a request asks the controller itself only when that state isn't known yet, and waits 1 s at most.
- `libpython2.4.so.1.0` is now stripped like the rest of the system. Python installed it read-only, so Buildroot's strip step skipped it silently.

### Changed

- A faster web page. On a rabbit, for a request from a computer on the network: `status` 1.09 s → 0.70 s, `lapinlibre/status` 0.94 s → 0.53 s, `voice_list` 0.45 s → 0.22 s. Taking a picture reaches the camera in 0.19 s instead of 0.58 s, so the shutter sounds about half a second after the click.
- LapinLibre's own programs are stripped in the image, and their code holds no build paths. The rabbit's `/usr` uses 45.5 MB instead of 58.4 MB, and `usr.tar.gz` is 16.6 MB instead of 21.2 MB.

### Added

- The camera gallery has a bin button on each picture, which deletes that picture and its thumbnail after a confirmation. The new endpoint is `lapinlibre/snapshot_delete?filename=`.

### Known issues

- When a new tag is learned from the web page, the rabbit still says it doesn't recognise the object the first time the tag is laid. The controller checks only the web API's tag store, which doesn't have the new tag yet.

## v0.1.0 (2026-10-25)

The first release.

- The whole system built from source: a GCC 14.3 and uClibc-ng toolchain, Buildroot for the libraries and tools, Linux 2.6.32 for the rabbit's board, V8 for applications, and the rabbit's own programs (the controller, the daemons, karotzVM) in C and C++.
- A USB key that installs the system on the rabbit, with the settings in `lapinlibre.txt` (Wi-Fi, a fixed address or DHCP, the name on the network, the web password, the SSH key, the language, the app server).
- A web page on the rabbit: status, LED, ears, sounds, speech, the camera and its gallery, RFID tags with actions, the USB stick, logs, SSH keys and restart.
- An HTTP API compatible with OpenKarotz's, so existing scripts and home-automation plugins can drive the rabbit, plus LapinLibre's own endpoints.
- An app server and a speech server to run on your own computer, for the rabbit's applications, speech and speech recognition.
- SSH with the key from `lapinlibre.txt`.
- Tests: behaviour scenarios and aggressive tests under qemu, the image tests, the app server tests, and tests for a real rabbit.
