<!-- SPDX-License-Identifier: MIT -->
# Tests

`./build.sh test` (or `bash tests/run.sh`) runs everything and prints a PASS, FAIL or SKIP line per test, then a summary; the exit status is non-zero if anything failed. It needs the system, v8, kernel and userland stages; it makes the test preloads and the test sysroot itself, in the work location. The test sysroot (`$LL_WORK/sysroot-core`, made by `image/tools/sysroot.py core`) is LapinLibre's core as the key installs it, built by `image/tools/image.py`'s `make_core` with no key, with the `ld.so.cache` its own `ldconfig` makes and an empty `/var`. Run it on Linux or WSL (qemu-user, dbus-daemon, unshare with user namespaces, nsenter, python3-dbus). `bash tests/run.sh SCENARIO...` runs only those scenarios. The image tests build a USB key with test network settings in a temporary directory.

In order:

- **Image tests** (`image/`, every `test_*.py` there): the cramfs and tar codecs and overlays (`test_fstree.py`), the key's real autorun run against a simulated Karotz (`test_usbkey.py`), the core's boot files and the configuration `inetd.sh` makes for httpd (`test_core_boot.py`), the generated time-zone list (`test_tzlist.py`), the web API's programs (`test_web_api.py`) and karotzVM's JavaScript API (`test_karotzvm_api.py`: every function karotzVM gives applications, read from its source, must be called by a test application, or be in its exceptions with the reason; `python3 -I -B tests/image/test_karotzvm_api.py --list` lists them); `python3 -I tests/image/run.py` runs them alone.
- **App server tests** (`app_server/`): `tools/app-server` against a fake controller (the login, the commands, the messages, the HTTP endpoints, speech recognition and the REST API for external applications); `python3 -I tests/app_server/test_app_server.py <protoc>` runs them alone (the system build's `protoc-2.3.0`).
- **Behaviour scenarios** (`scenarios/`, run by `harness.py`): each starts one program on a private D-Bus bus with fake devices, drives it (D-Bus calls, device input, fake servers), and records its replies, signals, device I/O and what the fake servers saw. They run under qemu-user against the test sysroot, in user, mount, network and PID namespaces with only no-op stubs on PATH. The record must equal `expected/<scenario>.txt`. The program's syslog messages (its `g_log` messages) go to the run's `syslog` file, through the preload `fakes/syslogfake-arm.c` (`devtrace.c` for host builds); a warning or worse that the module's `EXPECTED_WARNINGS` doesn't match fails the run, in the scenarios and the aggressive tests alike. karotzVM's log is in its record itself.
- **Aggressive tests** (`aggressive/`): hostile and broken input (bad configuration and update files, short reads, a misbehaving controller, memory over a long run); each module's `check(record)` lists what went wrong.
- **The system build's library and program tests** (`system/tests`): libmpd's retry on EINTR, and mpd-karotz's playback, tags and seek against host decodes.

`system/check-image.sh` tests a built USB key's own files: the same scenarios on its sysroot (`image/tools/sysroot.py sysroot-image`), and smoke tests of its programs. `system/tests/core-boot.sh` checks a key's boot files under qemu-user.

## Recording

`bash tests/run.sh --record` runs the scenarios and rewrites the expected records instead of comparing (`tests/expected/README.md` says what they record). Record only on purpose, when behaviour has changed, and review the diff of `tests/expected/` before committing it. The harness masks what changes from run to run or machine to machine (the work location, the repository, temporary names, source paths in log messages); a scenario's `normalise_record` masks the rest.

One scenario by hand (from the repository, after `tests/run.sh` has built the preloads):

```
. scripts/common.sh
python3 -I tests/harness.py --sysroot $LL_WORK/sysroot-core --build $LL_WORK/userland/system \
    --fakes $LL_WORK/tests led_daemon
```

A failing scenario keeps its working directory (logs and `record.txt`) and prints a diff against the expected record.

## Adding a test

- A scenario is a module in `scenarios/` with `DAEMON`, `DEVICES` and `drive(daemon, bus)` returning the first part of the record (the harness adds the signals, the device I/O and whether the program is still running); see the docstring of `harness.py` for the optional hooks. Add its name to `SCENARIOS` in `run.sh`, then record it with `--record` and check the new expected file by reading it.
- An aggressive test is a module in `aggressive/` with the same parts plus `check(record)`; `run.sh` runs every module there.
- `scenarios/button_replay.py` replays a new recording of button reads made on a rabbit (`KAROTZ_BUTTON_REPLAY`), so `run.sh` doesn't run it. The recordings in `data/button/` are replayed and checked by the aggressive test `button_bounce`.

## Device tests

`device/` holds tests that need a real rabbit (or a native build), so `run.sh` doesn't run them. Each is a POSIX shell script that also runs under busybox; its header says how to run it, and it prints a PASS or FAIL line per check.

- `device/immortaldog.sh`: immortaldog's launching, relaunching, SIGTERM and pid-file handling. qemu-user can't judge it (it runs `vfork()` as `fork()`), so it runs on the rabbit, with a build copied to `/dev/shm`, or natively.
- `device/web-api.sh`: the web page and API from RAM (a second httpd on 127.0.0.1:8099 with a test password), before the files are installed: authentication, each endpoint's reply, and the refusals. With `hardware` it also drives the LED (red, green and yellow only), the ears (within one turn), a tone, speech, the camera, and sleep and wake-up, starting and ending awake so it can be run again; run it with someone at the rabbit. With `tag` (someone at the rabbit with one RFID tag) it also learns a tag and checks the tag listener's action for it, and forgets the tag at the end. With `reboot` it also checks the reboot endpoint: the rabbit restarts about 10 s after the check.

## Tested only on the rabbit

These paths need the rabbit's hardware or its whole system, which the scenarios fake or stub, so they are tested by hand with the device test application, `tools/app-server/example-apps/devicetest`. Run the app server and the speech server on a computer (see `tools/app-server/README.md`), give the application an instance whose `server` parameter is that computer's app server (`http://<computer>:8080`), then `appctl.py run devicetest`. Each step's result is printed on the app server's console, and every step must end without "timed out" (the button's needs a press within 15 s). Run it twice: the first run's `message.new` leaves a message on the server, which the second run's `message.list`, `message.get` and `message.markAsRead` find. Like any application, the device test is installed in `/usr/karotz/apps` on the rabbit's flash; its picture goes to `/tmp/picture.jpg` and its recording to `/tmp`.

| Path | What the scenarios do instead | The device test's step |
|---|---|---|
| karotzVM running a real application against the app server, end to end | karotzVM against a fake controller (`karotzvm_controller.py`), the controller against a fake karotzVM (`app_server.py`) | every step |
| multimedia-daemon: karotz-player's playback of a file | a karotz-player stub that sleeps | `play a sound`: OK, then TERMINATED |
| multimedia-daemon: MPD's database on the USB key | an `mpd` stub that exits | `multimedia.artist` lists the key's artists |
| multimedia-daemon: a recording through flac, uploaded | flac not on PATH, so the recording fails | `record 3 s and upload`: TERMINATED, upload `"OK"` |
| voice-daemon: speech played to the end | a `madplay` stub | `tts`: OK, then TERMINATED |
| voice-daemon: speech recognition with the microphone | a fake ASR server and a regular file for `/dev/audio` | `asr.string` gives the server's answer (`--asr-answer`) |
| webcam-daemon: a real capture and its upload | a regular file for `/dev/camera`, whose frames are empty | `webcam.photo (uploaded to the server)`: OK, then TERMINATED |
| controller: the video stream and a direct snapshot | `controller_webcam_direct`, with no camera | `webcam.photo direct`: OK; `webcam video for 5 s, then videoStop`: frames, none failed, at most one after the stop |
| controller: messages relayed between the platform and an application | `controller_message_query` forwards a query | `message.new`, then on the second run `message.list`, `message.get` (TEXT) and `message.markAsRead` (status 1) |
| ears-daemon and led-daemon on the real devices | the device files are regular files, the ioctls faked | `led light`, `led pulse`, `led fade`, `ears move`, `ears reset`: OK, then TERMINATED |

What no test reaches yet:

- k2k-daemon calling another k2k-daemon: the aggressive test `k2k_sip_peer` calls a SIP peer on the loopback interface and is called by it, and checks the answered call's audio both ways (a tone from `/dev/dsp` in the RTP the peer receives, the peer's tone in what the daemon writes to `/dev/dsp`), but no two daemons have called each other, and no call between two rabbits has been made.
- That the rabbit comes back after `power`'s reboot: the aggressive test `power_reads` follows `power` through its reboots (one a second while the button reads `'0'`, none after `'1'`, a failing reboot reported and tried again) with a stub for `reboot`.
- Specific log messages as the only evidence of a behaviour: the log is checked for unexpected warnings, not for messages that must appear.

`image/test_web_api.py` runs the web API's programs on the host (with the image's BusyBox under qemu when it can, including its httpd), and `image/test_core_boot.py` the configuration `inetd.sh` makes for httpd.
