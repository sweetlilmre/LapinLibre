# SPDX-License-Identifier: MIT
"""controller: a direct webcam snapshot with no camera.

The capture fails, the controller answers ERROR and keeps running. The
set-up is controller_platform's, up to interactive mode.
"""

from scenarios import controller_platform as base

DAEMON = "controller"
DEVICES = []
sandbox_setup = base.sandbox_setup
normalise_record = base.normalise_record


# Syslog warnings the run is known to log (harness.py): there is no camera.
EXPECTED_WARNINGS = [
    r"^input_cam_init: open\(V4L2_DEV_NODE\) failed$",
    r"^lib_Webcam: takeSnapshot: error init_input\. abort$",
]


def trigger(platform, p, fake, step):
    platform.send(p, 'id: "p-crash" interactiveId: "ia-1" webcam { photo: "direct" }')
    step("webcam direct with no camera", 'correlationId: "p-crash"', timeout=20)


def drive(d, bus):
    return base.drive(d, bus, trigger=trigger, when="interactive")
