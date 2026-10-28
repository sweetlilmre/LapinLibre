# SPDX-License-Identifier: MIT
"""multimedia-daemon: a record when /dev/audio can't be set up.

/dev/audio opens, but its OSS ioctls fail (they aren't faked here, and a
regular file has none), so the recording format and rate can't be set.
What must hold: the record is refused (false), no recording is started or
reported, and the daemon keeps running.
"""

import os
import re
import time
from pathlib import Path

from scenarios import multimedia_daemon as md

DAEMON = md.DAEMON
DEVICES = md.DEVICES
REGULAR_FILE_DEVICES = md.REGULAR_FILE_DEVICES


EXPECTED_WARNINGS = md.EXPECTED_WARNINGS + [
    r"^Unable to reset OSS driver\.$",
    r"^Record Go: /dev/audio setup failed$",
]


def drive(d, bus):
    stubs = Path(os.path.dirname(d.log.name)) / "stub-bin"
    (stubs / "mpd").write_text("#!/bin/true\n")
    (stubs / "mpd").chmod(0o755)
    record = [f"== registered: {bus.wait_for_name(md.SERVICE)}"]
    out = bus.send("--reply-timeout=20000", f"--dest={md.SERVICE}", md.PATH, f"{md.IFACE}.record",
                   "string:r1", "string:/tmp/r1.flac", "int32:0")
    record.append(f"record(string:r1) -> {md.reply(out)}")
    time.sleep(2)
    return record


def check(record):
    text = "\n".join(record)
    problems = []
    if "record(string:r1) -> boolean false" not in text:
        problems.append("the record wasn't refused")
    if re.search(r"multi_recordfinished", text):
        problems.append("a recording was reported")
    if re.search(r"system flac", text):
        problems.append("the encoder was started")
    if "== daemon still running: True" not in text:
        problems.append("the daemon died")
    return problems
