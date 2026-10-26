<!-- SPDX-License-Identifier: MIT -->
# Changelog

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
