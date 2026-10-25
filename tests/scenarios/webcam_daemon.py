# SPDX-License-Identifier: MIT
"""webcam-daemon: takeSnapshot opens /dev/camera, switches it to VGA and uploads a JPEG.

The camera is a FIFO here, so the driver's private VGA ioctl fails (ENOTTY)
and every snapshot stops there: the camera is closed, no LED/sound/upload
happens, no event signal is emitted and the reply is false. A successful
capture would need a fake camera device that answers the ioctl and serves
640x480 RGB565 frames.
"""

import time

DAEMON = "webcam-daemon"
DEVICES = ["camera"]
SERVICE = "com.mindscape.karotz.Webcam"
PATH = "/com/mindscape/karotz/Webcam"
IFACE = "com.mindscape.karotz.KarotzInterface"

# A closed local port (fails fast), the "save to /tmp/picture.jpg" mode, an
# id without a correlation part, and empty strings.
CALLS = [
    ("interactive:correlation", "http://127.0.0.1:9/x", 0),
    ("interactive:correlation", "file", 1),
    ("noseparator", "http://127.0.0.1:9/upload", -1),
    ("", "", 0),
    ("a:b:c", "file", 7),
]


def drive(d, bus):
    from harness import introspection

    record = [f"== registered: {bus.wait_for_name(SERVICE)}"]
    record.append("== introspection")
    record.extend(introspection(bus, SERVICE, PATH))
    record.append("== takeSnapshot")
    for ident, url, behaviour in CALLS:
        out = bus.send(f"--dest={SERVICE}", PATH, f"{IFACE}.takeSnapshot",
                       f"string:{ident}", f"string:{url}", f"int32:{behaviour}")
        record.append(f"takeSnapshot({ident!r}, {url!r}, {behaviour}) -> {out.splitlines()[-1].strip() if out else ''}")
    time.sleep(0.5)
    return record
