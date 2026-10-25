# SPDX-License-Identifier: MIT
"""voice-daemon: speech recognition when the microphone gives nothing.

voice_daemon's /dev/audio is a regular file, so every read() returns 0:
each audio chunk the daemon streams to the ASR server is unread audio,
which must be silence, not whatever memory held. The fake ASR server logs,
per chunk, whether it is all zeros.
What must hold: chunks were streamed, and every one is silent.
"""

import os

from scenarios import voice_daemon as vd

DAEMON = vd.DAEMON
DEVICES = vd.DEVICES
REGULAR_FILE_DEVICES = vd.REGULAR_FILE_DEVICES
FAKE_IOCTL_TYPES = getattr(vd, "FAKE_IOCTL_TYPES", ())
for _name in ("sandbox_setup", "normalise_record"):
    if hasattr(vd, _name):
        globals()[_name] = getattr(vd, _name)


def drive(d, bus):
    os.environ["KAROTZ_ASR_LOG_SILENCE"] = "1"
    try:
        return vd.drive(d, bus)
    finally:
        os.environ.pop("KAROTZ_ASR_LOG_SILENCE", None)


def check(record):
    problems = []
    silent = sum("asr: audio chunk silent" in l for l in record)
    noisy = sum("asr: audio chunk NOT silent" in l for l in record)
    if silent == 0:
        problems.append("no audio chunk reached the ASR server")
    if noisy:
        problems.append(f"{noisy} audio chunk(s) carried stale memory instead of silence")
    return problems
