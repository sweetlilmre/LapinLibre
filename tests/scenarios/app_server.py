# SPDX-License-Identifier: MIT
"""controller against LapinLibre's platform server (tools/app-server).

The same sandbox as controller_platform (the server in its own network
namespace, a fake karotzVM, private configuration and applications
folder), but the platform is tools/app-server/app_server.py serving
tools/app-server/example-apps (with the protoc codec), driven the way
appctl.py drives it. The server must get the controller from login to a
running application:

- login and the configuration push, the "hello" application downloaded
  and installed (config.bin written), the Karotz available;
- `run hello`: started through a one-shot date schedule about 3 s ahead in
  the settings' time zone, so the controller tells the VM "hello SCHEDULER
  default"; nothing is sent after it (the settings have a wake-up time on
  every day, so every schedule push resets the rabbit, and a push while the
  application starts would kill it);
- interactive mode: take, ears, LED, speech, release;
- the instance's RFID tag starts the application with its instance.
"""

import datetime
import json
import re
import socket
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from scenarios import controller_platform as base
from scenarios.controller import SERVICE, FakeDaemons

DAEMON = "controller"
DEVICES = []

REPO = Path(__file__).resolve().parents[2]
SERVER = REPO / "tools" / "app-server" / "app_server.py"
APPS = REPO / "tools" / "app-server" / "example-apps"
TAG = "E0040100C0FFEE42"  # upper case, as rfid-daemon reports it
ZONE = "Europe/Paris"
# Wake up at 00:00 and sleep at 23:59 every day: whatever the time, the
# controller's nearest-time rule then always picks "wake up", which resets the
# rabbit on every schedule push, as a rabbit with wake-up times does.
SETTINGS = {"lang": "fr-FR", "playsounds": False, "timezone": ZONE,
            "apps": {"hello": {"instances": [{"name": "default"}, {"name": "tagged", "rfid": TAG}]}},
            "wakeup": [{"day": day, "wake": [0, 0], "sleep": [23, 59]} for day in range(7)]}

UUID = base.UUID
DATE_ENTRY = re.compile(r'add_date_entry\("([^"]+)", (\d+), (\d+), (\d+), (\d+), (\d+), (\d+)\)')


def normalise_record(record):
    # Also the schedule's id in the signal the scenario emits for it; the
    # one-shot date is checked in drive() and masked here.
    lines = [UUID.sub("<uuid>", line) for line in base.normalise_record(record)]
    return [DATE_ENTRY.sub(r'add_date_entry("\1", <date>)', line) for line in lines]


def sandbox_setup(workdir, sysroot):
    w = Path(workdir)
    settings = w / "server-settings.json"

    def server(w, control):
        return (f"{sys.executable} -u {SERVER} --apps {APPS} --settings {settings} --listen {base.PLATFORM_ADDRESS} "
                f"--port {base.PORT} --http-port {base.HTTP_PORT} --control-socket {control} "
                f"--codec protoc --protoc {base.tool('protoc')} --hold")

    setup = base.sandbox_setup(workdir, sysroot, platform_command=server)
    settings.write_text(json.dumps(SETTINGS))
    # Where the controller will write the application's config.bin (see
    # controller_platform: new files would go to the host).
    (w / "apps" / "hello").mkdir()
    (w / "apps" / "hello" / "config.bin").write_text("")
    return setup


class Control:
    """appctl.py's protocol over the server's control socket."""

    def __init__(self, path):
        self.path = path

    def __call__(self, cmd, *args):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(60)
        sock.connect(str(self.path))
        with sock:
            stream = sock.makefile("rw")
            stream.write(json.dumps({"cmd": cmd, "args": list(args)}) + "\n")
            stream.flush()
            answer = json.loads(stream.readline())
        return answer["result"] if answer["ok"] else f"error: {answer['error']}"


def drive(d, bus):
    record = []
    w = Path(d.workdir)
    record.append(f"== registered: {bus.wait_for_name(SERVICE)}")
    fake = FakeDaemons(bus)
    control = Control(w / "control.sock")
    vm_read = [0]
    uuids = {}

    def masked(text):
        return UUID.sub(lambda m: uuids.setdefault(m.group(0), f"<u{len(uuids) + 1}>"), str(text))

    def vm_lines():
        with open(w / "vm.log") as f:
            f.seek(vm_read[0])
            lines = f.read().splitlines()
            vm_read[0] = f.tell()
        return [f"vm {line}" for line in lines]

    def step(title, until=None, settle=1.5, timeout=60, sort_calls=False):
        """Record the daemon calls and VM commands until `until` shows up in
        them and then nothing more for `settle` seconds. With sort_calls the
        calls are sorted, for steps where a schedule push's reset runs
        alongside the push's own calls in no fixed order."""
        record.append(f"== {title}")
        seen, last, deadline = [], time.time(), time.time() + timeout
        while True:
            new = fake.calls() + vm_lines()
            if new:
                seen.extend(new)
                last = time.time()
            wanted = [until] if isinstance(until, str) else until or []
            if all(any(u in line for line in seen) for u in wanted) and time.time() - last >= settle:
                break
            if time.time() > deadline:
                seen.append(f"(timed out waiting for {until!r})")
                break
            time.sleep(0.1)
        kinds = ("call ", "vm ")
        if sort_calls:
            seen = sorted(line for line in seen if line.startswith("call ")) + \
                [line for line in seen if not line.startswith("call ")]
        record.extend(sorted(seen, key=lambda line: next((i for i, k in enumerate(kinds) if line.startswith(k)), 2)))

    def reply(title, text):
        # The controller's answer, without the ids it generates.
        text = re.sub(r'(id|correlationId): "[^"]*" ', "", masked(text))
        record.append(f"{title}: {text}")

    try:
        # Log in only now that the fake daemons own their names.
        deadline = time.time() + 90
        while time.time() < deadline and not control("status").get("connected"):
            time.sleep(0.5)
        record.append(f"login: {control('login')}")
        while time.time() < deadline and not control("status").get("logged_in"):
            time.sleep(0.5)
        status = control("status")
        record.append(f"== logged in: {status['logged_in']}, applications {status['apps']}")
        step("configuration (download, install, available)", until="vm start", settle=3.0, sort_calls=True)

        ran_at = time.time()
        record.append(f"run: {control('run', 'hello')}")
        step("the one-shot date schedule (the push resets the rabbit)", until=["add_date_entry", "vm start"],
             sort_calls=True)
        entries = DATE_ENTRY.findall(Path(fake.log).read_text())
        if entries:
            entry_id, *fields = entries[-1]
            # Whole seconds, so 2 to 3 s after the run.
            due = datetime.datetime(*map(int, fields), tzinfo=ZoneInfo(ZONE)).timestamp()
            record.append(f"the date is about 3 s after the run, in {ZONE}"
                          if 1.5 <= due - ran_at <= 4 else
                          f"the date is {due - ran_at:.1f} s after the run, in {ZONE} (expected about 3)")
            # The scheduler here is one of the fake daemons: fire the entry
            # the controller registered, as scheduler-daemon would, as a
            # one-shot (more = FALSE).
            fake.emit("Scheduler", "schedule_event", "si", entry_id, 0)
        # Nothing more comes from the server: no reset kills the application.
        step("the schedule fires (the VM starts the application), nothing else is sent",
             until="vm command", settle=6.0)

        reply("take", control("take", "60"))
        step("interactive")
        reply("ears", control("ears", "5", "7"))
        reply("led", control("led", "00FF00"))
        reply("say", control("say", "bonjour"))
        step("requests")
        reply("release", control("release"))
        step("release (available, reset)", until="vm start", settle=2.0)

        fake.emit("Rfid", "tag_in", "ssiiii", TAG, "content", 3, 1, 4, 2)
        step("the instance's RFID tag", until="vm command", settle=2.5)

        record.append("== files")
        config_bin = w / "apps" / "hello" / "config.bin"
        record.append(f"hello/config.bin: {config_bin.read_bytes().hex()}")
        # The zone the controller set from the settings' timezone (ends with
        # a newline, or uClibc ignores it).
        record.append(f"/etc/TZ: {(w / 'TZ').read_bytes()!r}")
    finally:
        fake.close()
    return record
