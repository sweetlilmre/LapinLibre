# SPDX-License-Identifier: MIT
"""controller: an update server without yaffs_version (an HTTP 404).

The controller counts the 404 as a failed download and skips the check.
What must hold: no reboot, and the controller keeps running.
"""

from aggressive import _update as u

DAEMON, DEVICES, normalise_record, drive = u.DAEMON, u.DEVICES, u.normalise_record, u.drive
sandbox_setup = u.make_sandbox_setup({"rootfs_version": u.published(u.INSTALLED_ROOTFS), "yaffs_version": None})


def check(record):
    problems = u.common_problems(record)
    if u.rebooted(record):
        problems.append("rebooted although yaffs_version was missing")
    return problems
