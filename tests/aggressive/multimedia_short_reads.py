# SPDX-License-Identifier: MIT
"""multimedia-daemon: recording when the microphone runs dry.

multimedia_daemon's /dev/audio holds 2048 bytes (a block of silence and one
of sound), then read() returns 0: from there on every block the recording
pump sends to flac is unread audio, which must be silence (the unsigned
midpoint, 0x8000, little-endian), not a stale buffer sent again and again.
Here flac is a stub that keeps what it is given. What must hold: the
recording's tail is silence, and so is the padding record_stop() adds after
it (0x00 there would be the most negative sample, ending every recording
with a click).
"""

from pathlib import Path

from scenarios import multimedia_daemon as md

DAEMON = md.DAEMON
DEVICES = md.DEVICES
for _name in ("REGULAR_FILE_DEVICES", "FAKE_IOCTL_TYPES", "normalise_record"):
    if hasattr(md, _name):
        globals()[_name] = getattr(md, _name)

TAIL = 16384  # bytes looked at, well past the file's 2048
PADDING = 32000  # record_stop()'s padding: 32 writes of 1000 bytes
SILENCE = bytes([0x00, 0x80])  # the unsigned midpoint, little-endian


def sandbox_setup(workdir, sysroot):
    base = md.sandbox_setup(workdir, sysroot) if hasattr(md, "sandbox_setup") else ""
    w = Path(workdir)
    capture = w / "flac-capture"
    # The pump spins as fast as it can once reads return 0 (hundreds of MB):
    # keep the last 200 KB of each recording (the last recording's survive).
    capture.write_text(f"#!/bin/sh\nexec /usr/bin/tail -c 200000 > {w / 'flac.in'}\n")
    capture.chmod(0o755)
    return base + f"/bin/cp {capture} {w / 'stub-bin' / 'flac'}; "


def drive(d, bus):
    record = md.drive(d, bus)
    data = (Path(d.workdir) / "flac.in").read_bytes() if (Path(d.workdir) / "flac.in").exists() else b""
    # record_stop() pads with PADDING bytes of silence. The pumped audio is
    # what comes before.
    pumped, padding = data[:-PADDING], data[-PADDING:]
    tail = pumped[-TAIL:]
    record.append(f"== flac received {len(data)} bytes; the last {len(tail)} pumped silent: {is_silence(tail)}")
    record.append(f"== padding of {len(padding)} bytes silent: {is_silence(padding)}")
    return record


def is_silence(data):
    return len(data) >= 2 and data == SILENCE * (len(data) // 2)


def check(record):
    line = next((l for l in record if l.startswith("== flac received")), None)
    if line is None:
        return ["no flac summary"]
    problems = []
    received = int(line.split()[3])
    if received <= 32000 + 2048:
        problems.append(f"flac got only {received} bytes: the pump never ran past the file")
    if not line.endswith("silent: True"):
        problems.append(f"the recording's tail isn't silence: {line}")
    padding = next((l for l in record if l.startswith("== padding")), "")
    if not padding.endswith("silent: True"):
        problems.append(f"the recording's end padding isn't silence: {padding}")
    return problems
