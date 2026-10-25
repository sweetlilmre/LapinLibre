# SPDX-License-Identifier: MIT
"""controller: no platform server set.

Without SERVER= in lapinlibre.txt, karotz.conf's ip_server is 127.0.0.1, on
port_server 9123: the port where the controller's own server listens for
applications. Connecting there would connect the controller to itself.
What must hold: the controller makes no platform connection (its own
server sees none in 15 s), logs once that no platform server is set, and
keeps running.
"""

import re
import time
from pathlib import Path

from scenarios import conf_fixture

DAEMON = "controller"
DEVICES = []


def sandbox_setup(workdir, sysroot):
    conf, n = re.subn(r"^ip_server=.*$", "ip_server=127.0.0.1", conf_fixture.KAROTZ_CONF, flags=re.M)
    assert n == 1
    return conf_fixture.sandbox_setup(workdir, sysroot, karotz=conf)


def drive(d, bus):
    record = [f"== registered: {bus.wait_for_name('com.mindscape.karotz.Controller', timeout=60)}"]
    time.sleep(15)
    record.append(f"== running before the stop: {d.proc.poll() is None}")
    # The controller's output reaches daemon.log once its buffer is flushed: stop it before reading.
    d.stop()
    log = Path(d.workdir) / "daemon.log"
    record.append("== the controller's log: platform lines")
    if log.exists():
        for line in log.read_text(errors="replace").splitlines():
            if re.search(r"Platform - |New Connection from", line):
                record.append("log " + line.strip()[:200])
    return record


def check(record):
    problems = []
    if "== running before the stop: True" not in record:
        problems.append("the controller didn't survive")
    log = [l for l in record if l.startswith("log ")]
    if not any("no platform server" in l for l in log):
        problems.append(f"no 'no platform server' line in the log: {log}")
    if sum("no platform server" in l and "set SERVER=" in l for l in log) < 1:
        problems.append("the log doesn't say how to set the server")
    bad = [l for l in log if "successfully connected" in l or "New Connection from" in l]
    if bad:
        problems.append(f"the controller connected to itself: {bad}")
    return problems
