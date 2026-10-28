# SPDX-License-Identifier: MIT
"""controller: requests that assume a configuration, before there is one.

Right after the platform's connection and authentication, before any
configuration: interactive mode starts with a configId (there is no
configuration to look it up in), one without, and a choreography with a
very long interactive id. What must
hold: the controller keeps running, answers the configId start with ERROR,
and answers a ping afterwards.
"""

from scenarios import controller_platform as base

DAEMON = "controller"
DEVICES = []
sandbox_setup = base.sandbox_setup
normalise_record = base.normalise_record
EXPECTED_WARNINGS = base.EXPECTED_WARNINGS


def trigger(platform, p, fake, step):
    platform.send(p, f'id: "p-auth" authenticationToken {{ token: "{base.TOKEN}" }}')
    step("authentication", 'correlationId: "p-auth"')
    platform.send(p, 'id: "p-start-cfg" interactiveMode { action: START interactiveId: "ia-early" configId: "cfg-1" }')
    step("interactive start with a configId, no configuration", 'correlationId: "p-start-cfg"', timeout=20)
    long_id = "i" * 400
    platform.send(p, f'id: "p-start-long" interactiveMode {{ action: START interactiveId: "{long_id}" }}')
    step("interactive start with a 400-character id", 'correlationId: "p-start-long"', timeout=20)
    platform.send(p, f'id: "p-choreo" interactiveId: "{long_id}" choreography {{ start_file: "/tmp/x/a.js" }}')
    step("choreography under the long id", settle=3.0)
    platform.send(p, 'id: "p-ping-after" ping { }')
    step("still answering", 'correlationId: "p-ping-after"', timeout=20)


def drive(d, bus):
    return base.drive(d, bus, trigger=trigger, when="connected")


def check(record):
    problems = []
    text = "\n".join(record)
    if "== daemon still running: True" not in record:
        problems.append("the controller didn't survive")
    start = [l for l in record if 'correlationId: "p-start-cfg"' in l]
    if not start or "code: ERROR" not in start[0]:
        problems.append(f"the configId start wasn't answered ERROR: {start[:1]}")
    if 'correlationId: "p-ping-after"' not in text:
        problems.append("no answer to the ping afterwards")
    if any(l.startswith("vm command") and "i" * 300 in l for l in record):
        problems.append("the 400-character interactive id reached the VM")
    return problems
