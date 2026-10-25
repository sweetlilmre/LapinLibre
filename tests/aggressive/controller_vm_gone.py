# SPDX-License-Identifier: MIT
"""controller: a karotzVM that ends outside a reset is replaced on the next command.

Logged in, configured and interactive, the application gives interactive mode back (the
controller is available, and a reset starts a VM). Then the VM is killed from outside, as a
crash or a stray kill would end it, and the configured RFID tag is shown, which is a command for
the VM. What must hold: the controller starts a new VM for it (a second "vm start" after the
kill) and the command reaches that VM, rather than being lost until the next reset; the
controller keeps running.
"""

from scenarios import controller_platform as base

DAEMON = "controller"
DEVICES = []
sandbox_setup = base.sandbox_setup
normalise_record = base.normalise_record
KILLED = "== the VM was killed"


def drive(d, bus):
    def trigger(platform, p, fake, step):
        platform.send(p, 'id: "p-stop" interactiveId: "ia-1" interactiveMode { action: STOP }')
        step("interactive mode stopped (available, reset)", 'correlationId: "p-stop"', "vm start", settle=2.0)
        step(f"{KILLED[3:]}: {base.end_vm(d.proc.pid)} ended", settle=2.0)
        fake.emit("Rfid", "tag_in", "ssiiii", "e0040100c0ffee42", "content", 3, 1, 4, 2)
        step("the configured RFID tag", "vm start", "vm command", settle=2.5, timeout=30)
        platform.send(p, 'id: "p-ping-after" ping { }')
        step("still answering", 'correlationId: "p-ping-after"', timeout=20)
    return base.drive(d, bus, trigger=trigger, when="interactive")


def check(record):
    problems = []
    if "== daemon still running: True" not in record:
        problems.append("the controller didn't survive")
    killed = next((i for i, l in enumerate(record) if l.startswith(KILLED)), None)
    if killed is None or record[killed].endswith(" 0 ended"):
        return problems + [f"the VM wasn't killed: {record[killed] if killed is not None else 'no step'}"]
    after = record[killed + 1:]
    if not any(l.startswith("vm start") for l in after):
        problems.append("no new VM started after the kill")
    if not any(l.startswith("vm command") for l in after):
        problems.append("the RFID tag's command didn't reach a VM after the kill")
    if not any('correlationId: "p-ping-after"' in l for l in record):
        problems.append("no answer to the ping afterwards")
    return problems


def summary(record):
    killed = next((i for i, l in enumerate(record) if l.startswith(KILLED)), len(record))
    return (f"after the kill: {sum(l.startswith('vm start') for l in record[killed:])} VM start(s), "
            f"{sum(l.startswith('vm command') for l in record[killed:])} command(s)")
