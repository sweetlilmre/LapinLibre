# SPDX-License-Identifier: MIT
"""controller: the Karotz's central brain (status, platform, daemons' signals).

There is no network, so the platform connection fails and the Karotz is
never authenticated: the daemons' signals are then mostly just logged, and
the controller acts locally only where it does so without the platform.

The other daemons are played by a helper process (below) that owns their
bus names. That is needed for their signals to reach the controller at all:
dbus-glib only delivers a name proxy's signals from the name's owner, so a
plain dbus-send would be ignored. Owning the names also brings the
controller's calls to the daemons (earsMove, ledPulse, ...) to the helper,
which records them with their arguments and answers TRUE (with an empty
string for the get_* lists). The helper's own signals show up in the signal
record too.

The scenario covers start-up (configuration and language from the sysroot,
the "registered" signal, the network assumed online and the failed platform
connection), the three methods with valid and invalid ids (with no
platform the Karotz never becomes available, so take_control is refused;
release_control("") is refused too, "" being no session's id: it used to
match the empty "no application" id and reset the Karotz, which made it
available; the D-Bus session itself is in controller_empty_interactive_id),
every daemon signal the controller listens to, the button's four-click
network report (two ifconfig commands), the sleep schedule (with the
update check, which fails), a triple click while asleep, and finally the
wake-up schedule, after which the controller exits.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time

from harness import introspection
from scenarios import conf_fixture

DAEMON = "controller"


def sandbox_setup(workdir, sysroot):
    """The scenario's own karotz.conf (servers that don't resolve), whatever the sysroot has."""
    return conf_fixture.sandbox_setup(workdir, sysroot, karotz=conf_fixture.KAROTZ_CONF)
DEVICES = []
SERVICE = "com.mindscape.karotz.Controller"
PATH = "/com/mindscape/karotz/Controller"
IFACE = "com.mindscape.karotz.KarotzInterface"

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

# The fake daemons: own the names, record and answer calls, emit signals on request.
HELPER = r'''
import json, os, sys
import dbus, dbus.lowlevel, dbus.mainloop.glib
from gi.repository import GLib

dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
bus = dbus.SystemBus()
SHORT = ["Led", "Ears", "Voice", "Rfid", "Button", "Multimedia", "Choreography_manager", "Webcam",
         "Scheduler", "K2k", "Serial", "Dock"]
NAMES = {"com.mindscape.karotz." + s for s in SHORT}
log = open(sys.argv[1], "a")

def show(value):
    if isinstance(value, dbus.Boolean):
        return "true" if value else "false"
    if isinstance(value, (dbus.String, str)):
        return json.dumps(str(value))
    return str(int(value))

def on_message(conn, msg):
    if msg.get_type() != dbus.lowlevel.MESSAGE_TYPE_METHOD_CALL or msg.get_destination() not in NAMES:
        return dbus.lowlevel.HANDLER_RESULT_NOT_YET_HANDLED
    args = ", ".join(show(a) for a in msg.get_args_list())
    log.write("call %s %s(%s)\n" % (msg.get_destination().rsplit(".", 1)[1], msg.get_member(), args))
    log.flush()
    reply = dbus.lowlevel.MethodReturnMessage(msg)
    if msg.get_member().startswith("get"):
        reply.append("", True, signature="sb")
    elif msg.get_member() == "launchScript" and os.environ.get("FAKE_LAUNCHSCRIPT_ECHO"):
        # tests/aggressive: the choreography manager's script path is the
        # file asked for, so a test chooses it (then the result, as for get*).
        reply.append(str(msg.get_args_list()[0]), True, signature="sb")
    else:
        reply.append(True, signature="b")
    conn.send_message(reply)
    return dbus.lowlevel.HANDLER_RESULT_HANDLED

bus.add_message_filter(on_message)
for name in sorted(NAMES):
    bus.request_name(name)

def on_command(source, condition):
    line = sys.stdin.readline()
    if not line:
        loop.quit()
        return False
    short, member, signature, args = json.loads(line)
    signal = dbus.lowlevel.SignalMessage("/com/mindscape/karotz/" + short, "com.mindscape.karotz.KarotzInterface",
                                         member)
    if signature:
        signal.append(*args, signature=signature)
    bus.get_connection().send_message(signal)
    bus.flush()
    print("sent", flush=True)
    return True

GLib.io_add_watch(sys.stdin, GLib.IO_IN, on_command)
print("ready", flush=True)
loop = GLib.MainLoop()
loop.run()
'''


class FakeDaemons:
    def __init__(self, bus):
        fd, self.log = tempfile.mkstemp(prefix="karotz-controller-calls-")
        os.close(fd)
        self.read = 0
        self.proc = subprocess.Popen([sys.executable, "-c", HELPER, self.log], env=bus.env(),
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        assert self.proc.stdout.readline().strip() == "ready"

    def emit(self, short, member, signature="", *args):
        self.proc.stdin.write(json.dumps([short, member, signature, list(args)]) + "\n")
        self.proc.stdin.flush()
        self.proc.stdout.readline()

    def calls(self):
        """The calls received since the last time."""
        with open(self.log) as f:
            f.seek(self.read)
            text = f.read()
            self.read = f.tell()
        return [UUID.sub("<uuid>", line) for line in text.splitlines()]

    def close(self):
        self.proc.stdin.close()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        os.unlink(self.log)


def call(bus, method, *args):
    out = bus.send(f"--dest={SERVICE}", PATH, f"{IFACE}.{method}", *args)
    lines = [l.strip() for l in out.splitlines() if not l.startswith("method return")]
    return UUID.sub("<uuid>", f"{method}{args}: {' | '.join(lines)}")


def interactive_id(bus):
    out = bus.send(f"--dest={SERVICE}", PATH, f"{IFACE}.take_control")
    m = UUID.search(out)
    return m.group(0) if m else None, UUID.sub("<uuid>", " | ".join(
        l.strip() for l in out.splitlines() if not l.startswith("method return")))


def drive(d, bus):
    record = []
    record.append(f"== registered: {bus.wait_for_name(SERVICE)}")
    started = time.time()
    record.append("== introspection")
    record.extend(introspection(bus, SERVICE, PATH))
    # The platform connection is retried at about 0, 1, 2, 12, 42 s, then
    # every minute. The steps run between the fourth and fifth attempts,
    # and the wake-up after the fifth, so the system() lines always come in
    # the same order.
    time.sleep(max(0, started + 16 - time.time()))

    fake = FakeDaemons(bus)
    try:
        def step(title, wait=1.0):
            time.sleep(wait)
            record.append(f"== {title}")
            record.extend(fake.calls())

        record.append(call(bus, "get_status"))
        record.append(call(bus, "take_control"))
        record.append(call(bus, "release_control", "string:not-the-id"))
        step("methods while not available")

        fake.emit("Button", "click")
        fake.emit("Button", "dclick")
        fake.emit("Button", "lclick_start")
        fake.emit("Button", "lclick_stop")
        fake.emit("Button", "mclick", "i", 3)
        fake.emit("Button", "mclick", "i", 4)
        step("button")

        fake.emit("Ears", "event", "si", "ears-uuid", 1)
        fake.emit("Ears", "move_left_start")
        fake.emit("Ears", "move_right_start")
        fake.emit("Ears", "move_left_stop", "ii", 3, 5)
        fake.emit("Ears", "move_right_stop", "ii", 1, 2)
        fake.emit("Ears", "move_left_blocked")
        fake.emit("Ears", "move_right_blocked")
        fake.emit("Ears", "reset_finished", "sii", "reset-uuid", 4, 6)
        fake.emit("Ears", "status", "ii", 1, 2)
        fake.emit("Led", "event", "si", "led-uuid", 1)
        fake.emit("Dock", "event", "si", "dock-uuid", 1)
        fake.emit("Dock", "status", "ii", 1, 2)
        step("ears, LED and dock")

        fake.emit("Multimedia", "multi_playfinished", "si", "play-uuid", 4)
        fake.emit("Multimedia", "multi_recordfinished", "si", "record-uuid", 4)
        fake.emit("Multimedia", "multi_callback", "i", 1)
        fake.emit("Multimedia", "multi_callback", "i", 6)
        fake.emit("Rfid", "tag_in", "ssiiii", "e0040100c0ffee42", "content", 3, 1, 4, 2)
        fake.emit("Rfid", "tag_out", "s", "e0040100c0ffee42")
        fake.emit("Serial", "readline", "ss", "/dev/ttyUSB0", "hello")
        fake.emit("Webcam", "event", "si", "webcam-uuid", 1)
        fake.emit("K2k", "beginning_call", "s", "sip:friend")
        fake.emit("K2k", "finished_call", "s", "sip:friend")
        step("multimedia, RFID, serial, webcam and k2k")

        fake.emit("Voice", "tts_event", "si", "tts-uuid", 4)
        fake.emit("Voice", "asr_event", "sssi", "asr-uuid", "<nomatch>", "<app>x</app>", 50)
        fake.emit("Scheduler", "schedule_event", "si", "some-schedule", 0)
        step("voice and an unknown schedule")

        # "" is no session's id: releasing it changes nothing (no reset).
        record.append(call(bus, "release_control", "string:"))
        step("release_control('') -> refused, no reset", 2.0)
        record.append(call(bus, "get_status"))
        uuid, reply = interactive_id(bus)
        record.append(f"take_control(): {reply}")
        step("take_control while not available (refused)")
        record.append(call(bus, "take_control"))
        record.append(call(bus, "release_control", "string:wrong-id"))
        record.append(call(bus, "get_status"))
        step("still not interactive")

        fake.emit("Voice", "asr_event", "sssi", "asr-uuid", "hello", "<app>x</app>", 50)
        fake.emit("Button", "lclick_start")
        step("signals while not interactive")

        fake.emit("Scheduler", "schedule_event", "si", "E_STATUS_KAROTZ_SLEEP", 1)
        step("sleep schedule", 2.5)
        record.append(call(bus, "get_status"))
        record.append(call(bus, "release_control", "string:" + (uuid or "")))
        step("release_control (take_control gave no id) while asleep")

        fake.emit("Button", "tclick")
        step("triple click while asleep (reset, wake up)", 4.5)
        record.append(call(bus, "get_status"))

        time.sleep(max(0, started + 50 - time.time()))
        fake.emit("Scheduler", "schedule_event", "si", "E_STATUS_KAROTZ_WAKEUP", 1)
        step("wake-up schedule (the controller exits)", 5.0)
    finally:
        fake.close()
    return record
