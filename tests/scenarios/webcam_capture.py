# SPDX-License-Identifier: MIT
"""webcam-daemon: snapshots that capture, and the event each one signals.

The camera is a regular file here and the driver's VGA ioctl (type 0xf9)
is faked, so a snapshot gets past the camera set-up: its frame reads
return nothing (end of file), which counts as a frame, and the picture is
the empty buffer, encoded. A snapshot to "file" writes /tmp/picture.jpg
(the sysroot's /tmp is this run's var/tmp); an upload goes to a closed
local port and fails. Either way the daemon then signals
event(correlation id, 4), the id being the part of "interactiveid:
correlationid" after the ':' (empty when there is none).
"""

import shutil
import time
from pathlib import Path

DAEMON = "webcam-daemon"
DEVICES = ["camera"]
REGULAR_FILE_DEVICES = ["camera"]
FAKE_IOCTL_TYPES = [0xF9]
SERVICE = "com.mindscape.karotz.Webcam"
PATH = "/com/mindscape/karotz/Webcam"
IFACE = "com.mindscape.karotz.KarotzInterface"

CALLS = [
    ("ia-file:corr-file", "file", 0),
    ("ia-up:corr-up", "http://127.0.0.1:9/upload", 0),
    ("noseparator", "http://127.0.0.1:9/upload", 0),
]


# Syslog warnings the run is known to log (harness.py): uploads to a server that refuses them.
EXPECTED_WARNINGS = [
    r"^output_httppost_sendFrame curl perform ERROR$",
    r"^lib_takeSnapshot: output_httppost_sendFile error$",
]


def sandbox_setup(workdir, sysroot):
    """Give the sysroot an empty, private var (its /tmp is var/tmp)."""
    var = Path(workdir) / "var"
    (var / "tmp").mkdir(parents=True)
    # qemu -L maps a path to the sysroot only when it exists there.
    (var / "tmp" / "picture.jpg").write_bytes(b"")
    # By full path: the sandbox's PATH has only no-op stubs.
    mount = shutil.which("mount") or shutil.which("mount", path="/usr/sbin:/sbin")
    return f"{mount} --bind {var} {Path(sysroot).resolve() / 'var'}; "


def drive(d, bus):
    record = [f"== registered: {bus.wait_for_name(SERVICE)}"]
    record.append("== takeSnapshot")
    for ident, url, behaviour in CALLS:
        out = bus.send(f"--dest={SERVICE}", PATH, f"{IFACE}.takeSnapshot",
                       f"string:{ident}", f"string:{url}", f"int32:{behaviour}")
        record.append(f"takeSnapshot({ident!r}, {url!r}, {behaviour}) -> {out.splitlines()[-1].strip() if out else ''}")
    time.sleep(0.5)
    picture = Path(d.workdir) / "var" / "tmp" / "picture.jpg"
    record.append(f"== /tmp/picture.jpg is a JPEG: {picture.is_file() and picture.read_bytes()[:2] == bytes([0xFF, 0xD8])}")
    return record
