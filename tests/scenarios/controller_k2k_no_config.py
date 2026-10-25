# SPDX-License-Identifier: MIT
"""controller: an incoming k2k call before any configuration.

Looking for the k2k application, the controller finds no configuration and
so no k2k application, and keeps running. The set-up is
controller_platform's, up to the platform's connection.
"""

from scenarios import controller_platform as base

DAEMON = "controller"
DEVICES = []
sandbox_setup = base.sandbox_setup
normalise_record = base.normalise_record


def trigger(platform, p, fake, step):
    fake.emit("K2k", "incoming_call", "s", "sip:bob@example")
    step("an incoming k2k call", settle=3.0, timeout=20)


def drive(d, bus):
    return base.drive(d, bus, trigger=trigger, when="connected")
