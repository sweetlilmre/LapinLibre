# SPDX-License-Identifier: MIT
"""Shared by controller_update_*: the update check the controller makes when
it goes to sleep, against version files served by the fake platform's HTTP
server (url_update points at it). /sbin/reboot is a no-op stub in the
sandbox (harness.SBIN_STUBS) and the device I/O records it as
"system /sbin/reboot", so the record shows whether the controller decided
to reboot."""

import re
from pathlib import Path

from scenarios import controller_platform as base

DAEMON = "controller"
DEVICES = []
normalise_record = base.normalise_record

INSTALLED_ROOTFS = "12.07.19.00"
INSTALLED_SYS = "11.01.01.00"


EXPECTED_WARNINGS = base.EXPECTED_WARNINGS


def published(version):
    """A published version file: the version on its fourth line."""
    return f"Karotz\nversion file\n--\n{version}\n"


def make_sandbox_setup(served):
    """served: {"rootfs_version": text or None, "yaffs_version": text or None}."""
    def sandbox_setup(workdir, sysroot):
        setup = base.sandbox_setup(workdir, sysroot)
        w = Path(workdir)
        conf = w / "conf-usr" / "karotz.conf"
        text, n = re.subn(r"^url_update=.*$", f"url_update={base.URL_APP}/update", conf.read_text(), flags=re.M)
        assert n == 1
        conf.write_text(text)
        (w / "conf-usr" / "rootfs_version").write_text(INSTALLED_ROOTFS + "\n")
        (w / "conf-usr" / "sys_version").write_text(INSTALLED_SYS + "\n")
        (w / "http" / "update").mkdir(parents=True, exist_ok=True)
        for name, content in served.items():
            if content is not None:
                (w / "http" / "update" / name).write_text(content)
        return setup
    return sandbox_setup


def trigger(platform, p, fake, step):
    platform.send(p, 'id: "p-stop" interactiveId: "ia-1" interactiveMode { action: STOP }')
    step("interactive mode stopped (available)", 'correlationId: "p-stop"', settle=3.0)
    fake.emit("Scheduler", "schedule_event", "si", "E_STATUS_KAROTZ_SLEEP", 0)
    step("time to sleep: the update check", "update/", settle=5.0, timeout=30)


def drive(d, bus):
    return base.drive(d, bus, trigger=trigger, when="interactive")


def common_problems(record):
    problems = []
    if "== daemon still running: True" not in record:
        problems.append("the controller didn't survive")
    if not any(l.startswith("http GET /update/rootfs_version") for l in record):
        problems.append("the update check didn't run (no GET /update/rootfs_version)")
    return problems


def rebooted(record):
    return any(l.strip() == "system /sbin/reboot" for l in record)
