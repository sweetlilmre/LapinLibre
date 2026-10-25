# SPDX-License-Identifier: MIT
"""controller: a genuine update (yaffs_version newer than the installed one).

The other side of controller_update_bad and controller_update_missing: the
check must still reboot when both version files download and one is really
newer.
"""

from aggressive import _update as u

DAEMON, DEVICES, normalise_record, drive = u.DAEMON, u.DEVICES, u.normalise_record, u.drive
sandbox_setup = u.make_sandbox_setup({"rootfs_version": u.published(u.INSTALLED_ROOTFS),
                                      "yaffs_version": u.published("11.02.01.00")})


def check(record):
    problems = u.common_problems(record)
    if not u.rebooted(record):
        problems.append("didn't reboot for a newer yaffs_version")
    return problems
