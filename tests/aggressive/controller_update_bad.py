# SPDX-License-Identifier: MIT
"""controller: an update server whose yaffs_version comes back empty.

An empty version file has no version on its fourth line, so there is
nothing to compare: what must hold is no reboot (one here would come at
every check), and the controller keeps running. See
controller_update_missing (a 404) and controller_update_newer (a real
update must still reboot).
"""

from aggressive import _update as u

DAEMON, DEVICES, normalise_record, drive = u.DAEMON, u.DEVICES, u.normalise_record, u.drive
sandbox_setup = u.make_sandbox_setup({"rootfs_version": u.published(u.INSTALLED_ROOTFS), "yaffs_version": ""})


def check(record):
    problems = u.common_problems(record)
    if u.rebooted(record):
        problems.append("rebooted although yaffs_version was empty")
    return problems
