<!-- SPDX-License-Identifier: MIT -->
# The expected records

`tests/run.sh` compares each behaviour scenario's record with the file of its name here, and `bash tests/run.sh --record` rewrites them. They are records of LapinLibre's build (Buildroot 2026.02, GCC 14.3, uClibc-ng with NPTL threads, libmpd and linphone as system/external's packages) run on the test sysroot, LapinLibre's core as the key installs it (`tests/README.md`). The sections below explain the parts of some records that aren't obvious from the scenario alone.

## voice_daemon.txt and k2k_daemon.txt: the daemons' threads

qemu-user creates uClibc-ng's NPTL threads, so the paths of both daemons that need a thread run under the test as they do on the rabbit:

- **voice_daemon:** the last `tts_speak` (French, with quotes and `&`) forks the speech player, and `g_child_watch_add()` starts a GLib helper thread for it. The call returns `true`, the `tts_event` (2, finished) follows, and the daemon is still running at the end.
- **k2k_daemon:** linphone's SIP transport (an eXosip thread) starts, and each call to `127.0.0.1` (no SIP server there) ends at once with `beginning_outgoing_call` and `finished_call`. So every call is accepted, and there is nothing to hang up or answer (`false`). Echo calibration opens and reads `/dev/dsp` (mediastreamer's thread runs). The daemon is still running at the end.

## multimedia_daemon.txt: karotz-player

multimedia-daemon runs LapinLibre's own player, karotz-player, under its own name, with its FIFO at `/tmp/karotz-player.ctrl`. So the record's section is `-- karotz-player`, the FIFO's lines are `player_ctrl:`, and the daemon's clean-up is `killall karotz-player`. The daemon writes `loadfile`, `loadlist` and `pause` commands to the FIFO.

## karotzVM.txt

karotzVM runs against V8 2.5.9.23 built with GCC 14.3 (`./build.sh v8`), libxml2 2.15 and libcurl 8.20:

- **The top-level try/catch app (`httpcrash`)** catches both errors, logs "survived" and ends normally (its autokill follows, as for any application that doesn't go interactive).
- **An error's caret:** for the `timerthrow` app's `return missing.thing;`, the caret under the source line points at the end of `missing` (the reference that failed). The app carries on.
- **libxml2's descriptor errors:** a truncated `descriptor.xml` prints no parser error of its own, and a missing one reads `failed to load "...": No such file or directory`. karotzVM refuses the application either way.
- **libcurl's multipart POST** sends no `Expect: 100-continue` for a small body, and its random boundary has 24 dashes and 22 letters and digits, so the body is 246 bytes. The boundary itself is masked (`karotzvm_controller.py`, `normalise`).
