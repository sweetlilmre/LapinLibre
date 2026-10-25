# SPDX-License-Identifier: MIT
"""controller: a karotzMessageQuery from the platform.

The controller forwards the query and keeps running. The set-up is
controller_platform's, up to interactive mode.
"""

from scenarios import controller_platform as base

DAEMON = "controller"
DEVICES = []
sandbox_setup = base.sandbox_setup
normalise_record = base.normalise_record


def trigger(platform, p, fake, step):
    platform.send(p, 'id: "p-crash" karotzMessageQuery { action: LIST }')
    step("a karotzMessageQuery", "karotzMessageQuery", timeout=20)


def drive(d, bus):
    return base.drive(d, bus, trigger=trigger, when="interactive")
