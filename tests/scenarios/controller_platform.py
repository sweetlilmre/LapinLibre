# SPDX-License-Identifier: MIT
"""controller against a fake platform: everything it does once logged in.

The controller connects out to its platform (ip_server:port_server) and
also listens on the same port for interactive applications, so the fake
platform (fake_platform.py) runs on another "host": its own network
namespace inside the sandbox, joined to the controller's by a veth pair
(controller 10.9.0.2, platform 10.9.0.1). Inside the sandbox's mount
namespace only, copies are bind-mounted over the sysroot's configuration
(karotz.conf pointing at the fake platform, an account.conf account), /var
(holding /tmp) and the applications folder, and /usr/bin/unzip is replaced by /bin/true. The
sysroot itself is never written.

qemu (and devtrace for the native run) take an absolute path from the
sysroot only when it already exists there; new files go to the host. So the
files the controller writes are created beforehand in the copies: the
account (/tmp/account.conf: the controller copies account.conf there with cp,
which is a stub here), /tmp/apps.abnf, and the applications' config.bin.
(The sysroot's /tmp is a link to its empty var/tmp, so a private var is
mounted there.)

The scenario logs the controller in (md5 of password and token), asks for
its information, pushes a configuration with two applications (one
downloaded from the fake platform's HTTP server, one already installed at
the configured version) and an application folder that is no longer
configured, schedules, contacts and the social keys, the pending-message
flag, then interactive mode (a refused configId, a start) and every request
an application can make, signals while interactive, an application's own
connection to the controller's server, the stop, RFID and button events
while available, an interactive ttl that runs out, the remote system
commands, and finally the platform dropping the connection (the controller
reconnects).

Recorded: the messages the controller sends (protoc text format; UUIDs
numbered in order of appearance; pings, whose timing varies, left out), the
calls it makes to the daemons, what it tells the (fake) karotzVM, the HTTP
requests and the files it wrote.
"""

import json
import os
import re
import shutil
import socket
import sys
import time
import zipfile
from pathlib import Path

from scenarios import conf_fixture
from scenarios.controller import SERVICE, FakeDaemons, call

DAEMON = "controller"
DEVICES = []

HERE = Path(__file__).resolve().parent
PROTO_DIR = HERE.parent.parent / "userland" / "proto"
PLATFORM_ADDRESS = "10.9.0.1"
CONTROLLER_ADDRESS = "10.9.0.2"
PORT = 9123
HTTP_PORT = 8080
URL_APP = f"http://{PLATFORM_ADDRESS}:{HTTP_PORT}"

ACCOUNT = "[account]\nid=rabbit-id\npwd=secret\nlang=fr-FR\n"
TOKEN = "t0k3n"

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


# Syslog warnings the run is known to log (harness.py): the platform's telnet and log commands,
# which LapinLibre ignores, and the fake platform closing its connection at the end.
EXPECTED_WARNINGS = [
    r"^(Telnet|Log) (on|off) from the platform ignored: ",
    r"^socket \d+: the other side closed the connection$",
    r"^socket error on socket \d+$",
    r"^Platform - Offline$",
    # The platform closing at the end resets the controller, which moves the ears after the
    # test's fake daemons may have stopped.
    r"^earsMove returned FALSE$",
]


def tool(name):
    path = shutil.which(name) or shutil.which(name, path="/usr/sbin:/sbin")
    if path is None:
        raise RuntimeError(f"controller_platform needs {name}")
    return path


def copy_dir(source, target):
    shutil.copytree(Path(source).resolve(), target, symlinks=True)


def descriptor(version):
    return f'<?xml version="1.0"?>\n<descriptor><version>{version}</version><name>x</name></descriptor>\n'


def sandbox_setup(workdir, sysroot, platform_command=None):
    """Prepare the copies and return the shell commands that mount them and start the platform.

    platform_command(workdir, control_socket) can give another platform to
    run (tests/scenarios/app_server.py runs tools/app-server); it must
    listen on PLATFORM_ADDRESS and create the control socket once it is up.
    """
    sysroot = Path(sysroot).resolve()
    w = Path(workdir)

    # The controller's karotz.conf is /usr/etc/conf's: the scenarios' own, pointed at the fake platform.
    copy_dir(sysroot / "usr" / "etc" / "conf", w / "conf-usr")
    conf = conf_fixture.KAROTZ_CONF
    for key, value in (("ip_server", PLATFORM_ADDRESS), ("url_app", URL_APP), ("domain_app", PLATFORM_ADDRESS)):
        conf, n = re.subn(rf"^{key}=.*$", f"{key}={value}", conf, flags=re.M)
        assert n == 1, key
    (w / "conf-usr" / "karotz.conf").unlink(missing_ok=True)
    (w / "conf-usr" / "karotz.conf").write_text(conf)
    (w / "conf-usr" / "account.conf").write_text(ACCOUNT)

    # The sysroot's /tmp is a link to var/tmp, and its var is empty.
    assert os.readlink(sysroot / "tmp") == "var/tmp" and not os.listdir(sysroot / "var")
    (w / "var" / "tmp").mkdir(parents=True)
    (w / "var" / "tmp" / "account.conf").write_text(ACCOUNT)
    (w / "var" / "tmp" / "apps.abnf").write_text("")
    # What the info query's shell pipelines would leave on the rabbit. The
    # controller reads them from the sysroot's /tmp under qemu, and from the
    # host's /tmp natively (glibc's fopen escapes devtrace's mapping), so the
    # sandbox gets a private host /tmp too, with the workdir mounted back in
    # it. (Under qemu the pipelines run, on the host, and write to the
    # private host /tmp; natively they are only logged.)
    host_tmp = w / "host-tmp"
    (host_tmp / w.name).mkdir(parents=True)
    for d in (w / "var" / "tmp", host_tmp):
        (d / "mac_addr").write_text("00:0f:3d:12:34:56\n")
        (d / "ip_addr").write_text(f"{CONTROLLER_ADDRESS}\n")

    # The time zone the controller writes when a platform sends one, over the
    # sysroot's (empty) /etc/TZ.
    (w / "TZ").write_text("")

    copy_dir(sysroot / "usr" / "karotz" / "apps", w / "apps")
    for name in ("app-new", "app-same", "app-gone"):
        (w / "apps" / name).mkdir()
    (w / "apps" / "app-new" / "config.bin").write_text("")
    (w / "apps" / "app-same" / "config.bin").write_text("")
    (w / "apps" / "app-same" / "descriptor.xml").write_text(descriptor("2"))

    archive = w / "http" / "file" / "app-new" / "1" / "get"
    archive.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("descriptor.xml", descriptor("1"))
        z.writestr("main.js", 'log("hello");\n')

    control = w / "control.sock"
    config = w / "platform.json"
    config.write_text(json.dumps({"address": PLATFORM_ADDRESS, "port": PORT, "http_port": HTTP_PORT,
                                  "http_dir": str(w / "http"), "control": str(control),
                                  "protoc": tool("protoc"), "proto_dir": str(PROTO_DIR)}))

    ip, sleep, mount, readlink = tool("ip"), tool("sleep"), tool("mount"), tool("readlink")

    # The fake karotzVM: the controller execs /usr/karotz/bin/karotzVM itself
    # (qemu doesn't map an exec's path to the sysroot), so the host's /usr is
    # replaced, in the sandbox, by one made of bind mounts of its entries plus
    # karotz/bin/karotzVM. The VM logs the commands it is sent. On the rabbit
    # a reset's "killall karotzVM" stops it, so the killall stub here does
    # too, waiting until it has ended (a zombie, for the controller's wait()).
    # Without that the reset's wait() would block, and a VM that ended by
    # itself would interrupt the controller's select() with SIGCHLD at a
    # time that depends on the run (that case is tested once, at the end).
    usr = w / "usr"
    usr.mkdir()
    for entry in sorted(os.listdir("/usr")):
        source = Path("/usr") / entry
        if source.is_symlink():
            (usr / entry).symlink_to(os.readlink(source))
        elif source.is_dir():
            (usr / entry).mkdir()
    (usr / "karotz" / "bin").mkdir(parents=True)
    # The same applications folder from both views: libxml2, for one, opens
    # descriptor.xml without devtrace's mapping to the sysroot.
    (usr / "karotz" / "apps").mkdir()
    vm_log, vm_pids = w / "vm.log", w / "vm.pids"
    vm = usr / "karotz" / "bin" / "karotzVM"
    # The native run's devtrace (inherited through LD_PRELOAD) would map the
    # script's own path to the sysroot's ARM karotzVM when sh opens it.
    vm.write_text(f'#!{tool("env")} -S -u LD_PRELOAD /bin/sh\necho $$ >> {vm_pids}\necho "start $*" >> {vm_log}\n'
                  f'while read -r line; do echo "command $line" >> {vm_log}; done\n')
    killall = w / "killall"
    killall.write_text(f'#!/bin/sh\n[ "$1" = karotzVM ] || exit 0\necho killall >> {vm_log}\n'
                       f'[ -f {vm_pids} ] || exit 0\n'
                       'while read -r pid; do\n'
                       # (no 2>/dev/null: in the native sandbox that redirect
                       # fails, and dash then skips the command)
                       '    kill $pid || :\n'
                       '    while [ -e /proc/$pid ] && read -r a b state rest < /proc/$pid/stat && [ "$state" != Z ]; do\n'
                       f'        {sleep} 0.01\n'
                       '    done\n'
                       f'done < {vm_pids}\n: > {vm_pids}\n')
    for script in (vm, killall):
        script.chmod(0o755)
    vm_log.write_text("")

    usr_binds = "".join(f"{mount} --rbind /usr/{e} {usr / e}; " for e in sorted(os.listdir(usr))
                        if e != "karotz" and not (usr / e).is_symlink())
    usr_binds += f"{mount} --bind {w / 'apps'} {usr / 'karotz' / 'apps'}; "
    mounts = [(w / "conf-usr", sysroot / "usr" / "etc" / "conf"), (w / "TZ", sysroot / "usr" / "etc" / "TZ"),
              (w / "var", sysroot / "var"),
              (w / "apps", sysroot / "usr" / "karotz" / "apps"), (killall, w / "stub-bin" / "killall")]
    unzip = shutil.which("unzip")
    if unzip:
        mounts.append((tool("true"), Path(unzip).resolve()))
    if platform_command is None:
        command = f"{sys.executable} -u {HERE / 'fake_platform.py'} {config}"
    else:
        command = platform_command(w, control)
    # The veth's far end goes to the platform's namespace by its pid, so only
    # once unshare has made that namespace: before then, "netns $!" is this
    # one, vk1 stays here and the platform waits for it for ever (7% of tries).
    platform = (f"while ! {ip} link show vk1 >/dev/null 2>&1; do {sleep} 0.05; done; "
                f"{ip} addr add {PLATFORM_ADDRESS}/24 dev vk1; {ip} link set vk1 up; {ip} link set lo up; "
                f"exec {command}")
    return (usr_binds + f"{mount} --rbind {usr} /usr; " +
            "".join(f"{mount} --bind {source} {target}; " for source, target in mounts) +
            f"{mount} --rbind {w} {host_tmp / w.name}; {mount} --rbind {host_tmp} /tmp; "
            "export KAROTZ_RUN_STUBS=killall; " +
            f"{tool('unshare')} -n /bin/sh -c '{platform}' >{w / 'platform.log'} 2>&1 & "
            f"platform=$!; while [ \"$({readlink} /proc/$platform/ns/net)\" = \"$({readlink} /proc/self/ns/net)\" ]; "
            f"do {sleep} 0.01; done; "
            f"{ip} link add vk0 type veth peer name vk1 netns $platform; "
            f"{ip} addr add {CONTROLLER_ADDRESS}/24 dev vk0; {ip} link set vk0 up; "
            f"while [ ! -S {control} ]; do {sleep} 0.05; done; ")


class Platform:
    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(str(path))
        self.stream = self.sock.makefile("rw")

    def request(self, op, **args):
        self.stream.write(json.dumps({"op": op, **args}) + "\n")
        self.stream.flush()
        answer = json.loads(self.stream.readline())
        if "error" in answer:
            raise RuntimeError(f"fake platform: {answer['error']}")
        return answer

    def send(self, conn, text, truncate=0):
        """Send a PlatformMessage in text format; truncate drops that many bytes from its end."""
        self.request("send", conn=conn, text=text, truncate=truncate)


def is_ping(text):
    return re.search(r"^ping \{", text, re.M) is not None and "correlationId" not in text


def schedule_day():
    """The weekday of the scenario's schedules: never today, so no wake-up or sleep now."""
    return (time.gmtime().tm_wday + 3) % 7


# The scenario's weekly schedule and wake-up/sleep entries (7:30, 7:00, 22:00).
SCHEDULE_ENTRY = re.compile(r'(add_weekly_entry\("[^"]*", )(\d)(, (?:7, 30|7, 0|22, 0), 0\))')


def normalise_record(record):
    """Mask what changes from run to run: the archive's temporary name is
    random, and the schedules' weekday moves with the date (it is masked only
    when it is the day the scenario asked for, so a wrong day still shows)."""
    day = str(schedule_day())
    lines = [re.sub(r"/tmp/file[0-9A-Za-z]{6}\b", "/tmp/file<random>", line) for line in record]
    return [SCHEDULE_ENTRY.sub(lambda m: m.group(1) + ("<day>" if m.group(2) == day else m.group(2)) + m.group(3),
                               line) for line in lines]


REQUESTS = [
    'ping { }',
    'ears { left: 5 right: 7 }', 'ears { left: 2 right: -3 relative: true }', 'ears { left: 4 }',
    'ears { right: 3 relative: true }', 'ears { reset: true }', 'ears { reset: true left: 1 }',
    'led { light { color: "FF0000" } }', 'led { fade { color: "00FF00" period: 300 } }',
    'led { pulse { color: "0000FF" period: 700 pulse: 5000 } }',
    'tts { action: START text: "bonjour" }', 'tts { action: START text: "hello" lang: "en-US" }',
    'tts { action: STOP }',
    'asr { dictionary: "oui" dictionary: "non" lang: "fr-FR" }', 'asr { file: "/tmp/g.abnf" }',
    'multimedia { play { url: "http://x/a.mp3" } }', 'multimedia { pause { } }', 'multimedia { resume { } }',
    'multimedia { stop { } }', 'multimedia { record { url: "x" } }', 'multimedia { next { } }',
    'multimedia { previous { } }', 'multimedia { getartist { } getsong { } }',
    'k2k { call { address: "sip:bob" } }', 'k2k { answer { accept: true } }', 'k2k { hangup { } }',
    'k2k { account { sipId: "me" sipDomain: "d" sipPassword: "p" stunServer: "s" } }',
    f'webcam {{ photo: "{URL_APP}/upload" }}',
    'choreography { start_file: "/tmp/dance.chor" }',
    'serial { open { port: "/dev/ttyUSB0" baud: 9600 } }',
    'serial { write { port: "/dev/ttyUSB0" data: "hi" base: "a" } }',
    'serial { close { port: "/dev/ttyUSB0" } }',
    'dock { led { light { color: "FFFFFF" } } }', 'dock { led { pulse { color: "FF0000" period: 1 pulse: 2 } } }',
    'dock { move { cmd: LEFT time: 3 } }',
    # Left out: webcam "direct" and karotzMessageQuery, which have scenarios
    # of their own (controller_webcam_direct, controller_message_query).
]


def end_vm(root_pid):
    """Kill the fake karotzVM from outside the sandbox (its pid there means
    nothing here): the descendant of the daemon's process running it."""
    import signal
    parents, cmdlines = {}, {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            stat = open(f"/proc/{entry}/stat").read()
            parents[int(entry)] = int(stat[stat.rindex(")") + 2:].split()[1])
            cmdlines[int(entry)] = open(f"/proc/{entry}/cmdline", "rb").read().replace(b"\0", b" ").decode(errors="replace")
        except (OSError, ValueError):
            pass

    def under(pid):
        while pid in parents and pid > 1:
            if pid == root_pid:
                return True
            pid = parents[pid]
        return False

    ended = 0
    for pid, cmdline in cmdlines.items():
        if "karotz/bin/karotzVM" in cmdline and under(pid):
            try:
                os.kill(pid, signal.SIGTERM)
                ended += 1
            except OSError:
                pass
    return ended


def ask_tag(bus, uid, content="content", app=3, type_=1, pict=4, color=2):
    """What the tag listener asks the controller for a tag laid on the nose: has_tag_app, then
    start_tag_app when an application has it. Their answers, as one line."""
    owned = call(bus, "has_tag_app", f"string:{uid}")
    if "boolean true" not in owned:
        return owned
    return owned + " ; " + call(bus, "start_tag_app", f"string:{uid}", f"string:{content}", f"int32:{app}",
                                f"int32:{type_}", f"int32:{pict}", f"int32:{color}")


def drive(d, bus, trigger=None, when="interactive"):
    """The scenario. With a trigger (see controller_bad_descriptor.py and
    the like), run only up to `when` ("connected": the platform's
    connection; "interactive": logged in, configured and interactive), then
    trigger(platform, p, fake, step), and stop."""
    record = []
    w = Path(d.workdir)
    record.append(f"== registered: {bus.wait_for_name(SERVICE)}")
    fake = FakeDaemons(bus)
    platform = Platform(w / "control.sock")
    uuids = {}
    vm_read = [0]

    def masked(text, numbered=True):
        def number(m):
            return uuids.setdefault(m.group(0), f"<u{len(uuids) + 1}>")
        text = UUID.sub(number, text) if numbered else UUID.sub("<u>", text)
        # Disk usage of the filesystem holding /usr changes between runs.
        return re.sub(r'(key: "set_hard_drive_(?:size|used)"\n\s*value: )"\d+"', r'\1"<n>"', text)

    def collect():
        lines = []
        for conn, text in platform.request("messages", settle=0)["messages"]:
            if is_ping(text):
                continue
            # An unsolicited message's id is never referred to again, so it
            # takes no number: a callback that comes or not depending on
            # timing doesn't renumber everything after it.
            text = masked(text, numbered="correlationId" in text)
            lines.append(f"conn {conn}: " + text.strip().replace("\n", " | "))
        lines.extend(fake.calls())
        lines.extend(f"http {r}" for r in platform.request("http")["requests"])
        with open(w / "vm.log") as f:
            f.seek(vm_read[0])
            lines.extend(f"vm {line}" for line in f.read().splitlines())
            vm_read[0] = f.tell()
        return lines

    def step(title, *until, settle=1.5, timeout=60):
        """Record what happens until every `until` text has been seen and then
        nothing more for `settle` seconds (qemu is much slower than the
        native run, so steps end on what was expected, not on time)."""
        record.append(f"== {title}")
        seen = []
        deadline = time.time() + timeout
        last = time.time()
        while True:
            new = collect()
            if new:
                seen.extend(new)
                last = time.time()
            if all(any(u in line for line in seen) for u in until) and time.time() - last >= settle:
                break
            if time.time() > deadline:
                missing = [u for u in until if not any(u in line for line in seen)]
                seen.append(f"(timed out waiting for {missing})")
                break
            time.sleep(0.1)
        # Grouped by kind, each in its order: a call and the message it led
        # to can be collected in either order.
        kinds = ("conn ", "call ", "http ", "vm ")
        record.extend(sorted(seen, key=lambda line: next((i for i, k in enumerate(kinds) if line.startswith(k)), 4)))

    try:
        p = platform.request("wait_connection", timeout=60)["conn"]
        record.append(f"== platform connection: {p}")
        if trigger and when == "connected":
            try:
                trigger(platform, p, fake, step)
                time.sleep(3)
                platform.request("messages", settle=0)
            except (OSError, ValueError):  # the fake platform went with the controller's sandbox
                record.append("== the sandbox ended with the controller")
            return record

        platform.send(p, f'id: "p-auth" authenticationToken {{ token: "{TOKEN}" }}')
        step("authentication", 'correlationId: "p-auth"')
        platform.send(p, 'id: "p-info" karotzinfo_query { }')
        step("information query (answered twice)", 'correlationId: "p-info"', "karotzinfo_response")

        day = schedule_day()
        platform.send(p, 'id: "p-config" config { '
                         'configapp { apiKey: "app-new" version: "1" name: "New" deployment: "hosted" '
                         '  configInstance { name: "inst1" uuid: "cfg-1" rfid: "e0040100c0ffee42" } } '
                         'configapp { apiKey: "app-same" version: "2" name: "Same" deployment: "hosted" '
                         '  configInstance { name: "inst2" uuid: "cfg-2" } } '
                         'lang: "fr-FR" playsounds: true '
                         'social { twitterConsumerKey: "ck" facebookToken: "fb" } }')
        step("configuration (download, install, available)", "karotzinfo_response", "vm start", settle=3.0)
        platform.send(p, 'id: "p-schedule" scheduleConfig { '
                         'scheduleApp { apiKey: "app-new" instance: "inst1" '
                         f'  schedule {{ scheduleWeekly {{ dayWeek: {day} hour: 7 minute: 30 }} }} }} '
                         f'scheduleWakeup {{ dayweek: {day} wakeuphour: 7 wakeupminute: 0 sleephour: 22 sleepminute: 0 }} '
                         'time: 1700000000000 gmt: "Europe/Paris" }')
        step("schedules", 'correlationId: "p-schedule"')
        platform.send(p, 'id: "p-contacts" contactList { contacts { firstname: "Ann" uuid: "u-ann" } }')
        platform.send(p, 'id: "p-getcontacts" getcontactList { }')
        platform.send(p, 'id: "p-social" socialQuery { }')
        step("contacts and social", 'correlationId: "p-getcontacts"', 'correlationId: "p-social"')
        platform.send(p, 'id: "p-pending1" pendingmessage: true')
        platform.send(p, 'id: "p-pending2" pendingmessage: false')
        step("pending messages", '"0000FF", 1600', '"000000", 1600', settle=2.0)

        platform.send(p, 'id: "p-start-bad" interactiveMode { action: START interactiveId: "ia-1" configId: "nope" }')
        step("interactive start with an unknown configId", 'correlationId: "p-start-bad"')
        platform.send(p, 'id: "p-start" interactiveMode { action: START interactiveId: "ia-1" configId: "cfg-1" ttl: 600 }')
        step("interactive start", 'correlationId: "p-start"')
        if trigger and when == "interactive":
            try:
                trigger(platform, p, fake, step)
                time.sleep(3)
                platform.request("messages", settle=0)
            except (OSError, ValueError):  # the fake platform went with the controller's sandbox
                record.append("== the sandbox ended with the controller")
            return record

        for i, body in enumerate(REQUESTS):
            platform.send(p, f'id: "p-req{i}" interactiveId: "ia-1" {body}')
            step(f"request: {body}", f'correlationId: "p-req{i}"', settle=0.5)
        platform.send(p, 'id: "p-readline" interactiveId: "ia-1" '
                         'serialCallback { readline { port: "/dev/ttyUSB0" data: "" base: "a" } }')
        step("serial read-line mode (no answer)", "readlinemode")
        platform.send(p, 'id: "p-wrong" interactiveId: "ia-x" ears { left: 1 }')
        step("a request with another interactive id", 'correlationId: "p-wrong"')

        fake.emit("Button", "click")
        fake.emit("Button", "dclick")
        fake.emit("Button", "lclick_start")
        fake.emit("Button", "lclick_stop")
        fake.emit("Ears", "move_left_start")
        fake.emit("Ears", "move_left_stop", "ii", 3, 5)
        fake.emit("Ears", "move_right_blocked")
        fake.emit("Rfid", "tag_in", "ssiiii", "e0040100c0ffee42", "content", 3, 1, 4, 2)
        fake.emit("Rfid", "tag_out", "s", "e0040100c0ffee42")
        fake.emit("Multimedia", "multi_callback", "i", 1)
        fake.emit("Voice", "asr_event", "sssi", "asr-uuid", "oui", "<app>x</app>", 50)
        fake.emit("Voice", "tts_event", "si", "tts-uuid", 4)
        fake.emit("K2k", "beginning_call", "s", "sip:friend")
        fake.emit("Serial", "readline", "ss", "/dev/ttyUSB0", "hello")
        fake.emit("Dock", "status", "ii", 1, 2)
        step("signals while interactive", "serialCallback", settle=2.0)

        app = platform.request("connect", address=CONTROLLER_ADDRESS, port=PORT)["conn"]
        record.append(f"== application connection: {app}")
        platform.send(app, 'id: "a-ping" interactiveId: "ia-1" ping { }')
        step("an application's ping on its own connection", 'correlationId: "a-ping"')
        platform.send(app, 'id: "a-stop" interactiveId: "ia-1" interactiveMode { action: STOP }')
        step("the application stops interactive mode (available, reset)", 'correlationId: "a-stop"', "vm start",
             settle=2.0)
        platform.send(app, 'id: "a-ping2" interactiveId: "ia-1" ping { }')
        step("a ping once stopped", 'correlationId: "a-ping2"')

        # A tag laid on the nose: the signal (the controller only passes it on, here to the
        # platform unless a hosted application has it) and the tag listener's questions. Starting
        # the application blocks RFID for a second (a g_timeout_add_seconds, so up to 2 s, later
        # under load): the second start is refused. Leave it well past that.
        fake.emit("Rfid", "tag_in", "ssiiii", "e0040100c0ffee42", "content", 3, 1, 4, 2)
        record.append("== the tag listener asks: " + ask_tag(bus, "e0040100c0ffee42"))
        record.append("== at once again: " + ask_tag(bus, "e0040100c0ffee42"))
        step("the configured RFID tag while available", "vm command", settle=5.0)
        fake.emit("Rfid", "tag_in", "ssiiii", "d002ffffffffffff", "content", 3, 1, 4, 2)
        record.append("== the tag listener asks: " + ask_tag(bus, "d002ffffffffffff"))
        record.append("== start_tag_app for it anyway: " + call(bus, "start_tag_app", "string:d002ffffffffffff",
                                                                 "string:c", "int32:0", "int32:0", "int32:0", "int32:0"))
        step("an unknown RFID tag while available", "rfidCallback", settle=2.5)
        fake.emit("Button", "click")
        fake.emit("Button", "dclick")
        step("button while available", "type: DOUBLE", settle=2.5)

        platform.send(p, 'id: "p-start2" interactiveMode { action: START interactiveId: "ia-2" ttl: 1 }')
        step("interactive start with a 1 s ttl", 'correlationId: "p-start2"')
        step("the ttl runs out (checked every 10 s)", "action: STOP", "vm start", settle=2.0)

        for command in ("TELNET_ON", "TELNET_OFF", "LOG_ON", "LOG_OFF", "LOG_REMOTE_ON", "LOG_REMOTE_OFF",
                        "SYSTEM_REBOOT"):
            platform.send(p, f'id: "p-{command.lower()}" system {{ command: {command} }}')
        platform.send(p, 'id: "p-after-system" karotzinfo_query { }')
        step("system commands (no answers)", 'correlationId: "p-after-system"', "karotzinfo_response")

        platform.request("close", conn=p)
        step("the platform drops the connection", "<closed>", "vm start", settle=2.0)
        again = platform.request("wait_connection", timeout=90)["conn"]
        record.append(f"== reconnected: {again is not None}")
        platform.send(again, f'id: "p-auth2" authenticationToken {{ token: "{TOKEN}2" }}')
        step("authentication again", 'correlationId: "p-auth2"')

        # The time zone by name: an old name, the current one, and a zone whose rules
        # changed after 2012 (Sao Paulo dropped its summer time); then the first one's again. /etc/TZ after each.
        for i, zone in enumerate(("Europe/Kiev", "Europe/Kyiv", "America/Sao_Paulo", "Europe/Paris")):
            platform.send(again, f'id: "p-zone{i}" scheduleConfig {{ time: 1700000000000 gmt: "{zone}" }}')
            step(f"time zone {zone}", f'correlationId: "p-zone{i}"')
            record.append(f"/etc/TZ: {(w / 'TZ').read_bytes()!r}")

        record.append("== files")
        record.append("apps: " + " ".join(sorted(os.listdir(w / "apps"))))
        for name in sorted(os.listdir(w / "apps")):
            config_bin = w / "apps" / name / "config.bin"
            if config_bin.exists():
                record.append(f"{name}/config.bin: {config_bin.read_bytes().hex()}")
        record.append("apps.abnf: " + (w / "var" / "tmp" / "apps.abnf").read_text())

        # Last: karotzVM ending by itself. Its SIGCHLD interrupts the select()
        # of socket_idle(), which takes it as nothing read yet: the
        # controller keeps running.
        record.append(f"== the VM ends by itself: {end_vm(d.proc.pid)} ended")
        time.sleep(3)
    finally:
        fake.close()
    return record

