# SPDX-License-Identifier: MIT
"""Behaviour tests for the Karotz daemons: each scenario's record against the one stored in tests/expected/.

Each run starts a private dbus-daemon standing in for the system bus, then
starts one daemon, drives it through a scenario (bytes written to its device
FIFOs, D-Bus method calls) and records what it does on the bus and on its
devices. An ARM build runs under qemu-arm against the core's libraries
(SYSROOT, made by tests/run.sh), with its /dev/rabbit_* devices replaced
by FIFOs in the sysroot, and its device I/O is read from qemu's syscall log.
A host build runs natively inside a user+mount namespace whose /dev holds
the same FIFOs, with the devtrace preload logging its device I/O in the
same form. Either way the program runs without network access (an empty
network namespace with only loopback up), in its own PID namespace, and
with only no-op stubs on PATH.

The record, after the scenario's normalise_record, must equal
EXPECTED/<scenario>.txt; with --record it is written there instead.

  python3 -I -B tests/harness.py [--record] [--suite scenarios|aggressive] --sysroot SYSROOT
      --build BUILD --fakes FAKES [--expected DIR] [--label LABEL] NAME...

FAKES is the directory holding the test preloads (devtrace.so for host
builds; ioctlfake-arm.so, syslogfake-arm.so and timeshift-arm.so for ARM
builds), built by tests/run.sh. The program's syslog messages go to the run's
syslog file; a warning or worse (a GLib WARNING, CRITICAL or ERROR) that the
module's EXPECTED_WARNINGS doesn't match fails the run in either suite. With --suite aggressive the modules come from tests/aggressive/
and each is judged by its own check(record) instead of an expected record.

A scenario module provides DAEMON, DEVICES and drive(daemon, bus) (which
returns the first part of the record), and optionally:
  FAKE_IOCTL_TYPES      ioctl type bytes (e.g. [0x6b] for SPI) that succeed on its
                        FIFO devices (ioctls that return data return zeros)
  REGULAR_FILE_DEVICES  devices that are regular files: read() returns 0 when
                        nothing is queued, written data is appended
  sandbox_setup(workdir, sysroot)  shell commands run in the program's
                        namespaces just before it starts
  raw_device_filter(lines)  sees the device I/O before repeats are collapsed
  normalise_record(record)  masks what changes from run to run
  EXPECTED_WARNINGS     regular expressions for the syslog warnings the run
                        is known to log (each matched with re.search)

Linux only (WSL): needs qemu-arm, dbus-daemon, dbus-monitor, dbus-send, unshare.
"""

import argparse
import difflib
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# Set by main(): where the test preloads are.
FAKES = None


def is_arm_build(build):
    """True if BUILD holds binaries for the rabbit rather than the host."""
    lib = Path(build) / "usr" / "lib" / "libkarotz-common.so"
    return lib.exists() and int.from_bytes(lib.read_bytes()[18:20], "little") == 40  # e_machine == EM_ARM


BUS_CONFIG = """<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>system</type>
  <listen>unix:path={socket}</listen>
  <auth>EXTERNAL</auth>
  <!-- The program under test runs in a user namespace where it claims uid
       0, which EXTERNAL rejects; ANONYMOUS lets it connect anyway. -->
  <auth>ANONYMOUS</auth>
  <allow_anonymous/>
  <policy context="default">
    <allow user="*"/>
    <allow own="*"/>
    <allow send_type="method_call"/>
    <allow send_type="signal"/>
    <allow send_requested_reply="true" send_type="method_return"/>
    <allow send_requested_reply="true" send_type="error"/>
    <allow receive_type="method_call"/>
    <allow receive_type="method_return"/>
    <allow receive_type="error"/>
    <allow receive_type="signal"/>
    <allow eavesdrop="true"/>
    <allow send_destination="*" eavesdrop="true"/>
  </policy>
</busconfig>
"""


def parse_monitor(text, kind="signal"):
    """dbus-monitor output as 'interface.member(args)' lines for one message
    kind ("signal" or "method call"), bus-daemon messages left out."""
    events, current = [], None
    for line in text.splitlines():
        m = re.match(r"(signal|method call) .*? interface=(\S+); member=(\S+)", line)
        if m:
            if current:
                events.append(current)
            msg_kind, iface, member = m.groups()
            current = None if iface == "org.freedesktop.DBus" or msg_kind != kind else [f"{iface}.{member}", []]
        elif re.match(r"(signal|method call|method return|error) ", line):
            if current:
                events.append(current)
            current = None
        elif current is not None and line.startswith("   "):
            current[1].append(line.strip())
    if current:
        events.append(current)
    return [f"{name}({', '.join(args)})" for name, args in events]


class Bus:
    def __init__(self, workdir):
        self.socket = workdir / "bus.sock"
        config = workdir / "bus.conf"
        config.write_text(BUS_CONFIG.format(socket=self.socket))
        self.address = f"unix:path={self.socket}"
        self.proc = subprocess.Popen(["dbus-daemon", "--nofork", f"--config-file={config}"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            if self.socket.exists():
                break
            time.sleep(0.05)
        self.monitor_log = workdir / "monitor.log"
        self.monitor = subprocess.Popen(["dbus-monitor", "--address", self.address, "type='signal'"],
                                        stdout=open(self.monitor_log, "w"), stderr=subprocess.DEVNULL)
        # Wait until the monitor is listening: it sees its own NameAcquired.
        for _ in range(100):
            if "NameAcquired" in self.monitor_log.read_text(errors="replace"):
                break
            time.sleep(0.05)

    def env(self):
        return {**os.environ, "DBUS_SYSTEM_BUS_ADDRESS": self.address}

    def daemon_env(self, stub_dir):
        """Environment for a program under test: PATH holds only harmless stubs.

        The programs run commands with system() (e.g. power runs "reboot");
        under qemu those reach the host's shell and PATH, so the host's real
        commands must never be on it.
        """
        # TZ: the sysroot's /etc/TZ is empty, so uClibc-ng runs on UTC;
        # give the host's glibc the same zone.
        return {**self.env(), "PATH": str(stub_dir), "TZ": "UTC0"}

    def send(self, *args, reply=True):
        cmd = ["dbus-send", "--system", "--print-reply" if reply else "--type=method_call", *args]
        r = subprocess.run(cmd, env=self.env(), capture_output=True, text=True, timeout=30)
        return (r.stdout + r.stderr).strip()

    def wait_for_name(self, name, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            out = self.send("--dest=org.freedesktop.DBus", "/org/freedesktop/DBus",
                            "org.freedesktop.DBus.NameHasOwner", f"string:{name}")
            if "boolean true" in out:
                return True
            time.sleep(0.2)
        return False

    def signals(self):
        """The signals seen so far, as 'interface.member(args)' lines, bus-daemon noise removed."""
        return parse_monitor(self.monitor_log.read_text(errors="replace"))

    def close(self):
        for p in (self.monitor, self.proc):
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()


def introspection(bus, service, path):
    """The object's interfaces as a sorted list of 'kind interface.member(signature)'."""
    out = bus.send(f"--dest={service}", path, "org.freedesktop.DBus.Introspectable.Introspect")
    m = re.search(r'string "(.*)"', out, re.S)
    if not m:
        return [f"introspection failed: {out}"]
    root = ET.fromstring(m.group(1).replace('\\"', '"'))
    items = []
    for iface in root.iter("interface"):
        name = iface.get("name")
        if name.startswith("org.freedesktop.DBus."):
            continue
        for el in iface:
            if el.tag in ("method", "signal"):
                sig = ",".join(f"{a.get('direction', 'out' if el.tag == 'signal' else 'in')}:{a.get('type')}"
                               for a in el.iter("arg"))
                items.append(f"{el.tag} {name}.{el.get('name')}({sig})")
    return sorted(items)


def program_path(build, name):
    """Program NAME of a build: in usr/, or in rootfs/ (the tools of the root filesystem, e.g. power)."""
    build_dir = Path(build).resolve()
    rootfs = build_dir / "rootfs" / name
    return rootfs if not (build_dir / "usr" / name).exists() and rootfs.exists() else build_dir / "usr" / name


def qemu_command(sysroot, build, name, extra=()):
    """The qemu-arm command line for program NAME of an ARM build (without a syscall log)."""
    build_dir = Path(build).resolve()
    libpath = f"{build_dir / 'usr' / 'lib'}:/usr/karotz/lib:/usr/lib:/lib"
    return [shutil.which("qemu-arm"), "-cpu", "arm926", "-L", str(sysroot), "-E", f"LD_LIBRARY_PATH={libpath}",
            *extra, str(program_path(build, name))]


class Daemon:
    """One daemon run with FIFO devices: under qemu-arm for an ARM build
    (against the sysroot's C library and GLib), natively for a host build."""

    def __init__(self, name, sysroot, build, workdir, devices, bus, fake_ioctl_types=(), regular_devices=(),
                 setup=""):
        self.name, self.bus = name, bus
        self.sysroot, self.build = Path(sysroot).resolve(), Path(build).resolve()
        self.workdir = workdir
        # Where the native run's namespace keeps the host's /dev (e.g. for ptys).
        self.hostdev = workdir / "hostdev"
        self.qemu = is_arm_build(build)
        self.devdir = workdir / "dev"
        self.devdir.mkdir()
        self.log = open(workdir / "daemon.log", "w")
        self.trace = workdir / "daemon.devtrace"
        self.syslog = workdir / "syslog"
        self.fifos = {}
        for dev in devices:
            if self.qemu:
                # qemu -L prefers SYSROOT/dev/<name> when it exists.
                path = self.sysroot / "dev" / dev
                path.parent.mkdir(exist_ok=True)
                if path.exists() or path.is_symlink():
                    path.unlink()
            else:
                path = self.devdir / dev
            if dev in regular_devices:
                path.touch()  # read() returns 0 when nothing is queued, like the ears driver
            else:
                os.mkfifo(path)
            self.fifos[dev] = path
        stubs = make_stub_dir(workdir)
        setup += sbin_stub_mounts(stubs)
        if self.qemu:
            # qemu's syscall log goes to stderr, i.e. into this run's log: a
            # -D log file is buffered and loses its last lines when the program
            # crashes and qemu dumps core.
            self.strace = workdir / "daemon.log"
            extra = ["-d", "strace"]
            preloads = [FAKES / "syslogfake-arm.so"]
            if fake_ioctl_types:
                preloads.append(FAKES / "ioctlfake-arm.so")
            extra += ["-E", "LD_PRELOAD=" + ":".join(map(str, preloads))]
            cmd = qemu_command(self.sysroot, self.build, name, extra)
            # No network: D-Bus is a Unix socket on the filesystem, so it still works.
            cmd = [*SANDBOX, "/bin/sh", "-c", LOOPBACK_UP + setup + '"$@"', "sh", *cmd]
        else:
            binary = program_path(self.build, name)
            libdir = self.build / "usr" / "lib"
            # Mount the FIFOs over /dev, keeping /dev/null and friends.
            script = (
                "set -e; PATH=/usr/bin:/bin; " + LOOPBACK_UP + setup + f"mkdir -p {self.hostdev}; mount --rbind /dev {self.hostdev}; "
                "mount -t tmpfs none /dev; "
                f"for n in null zero urandom random tty; do touch /dev/$n; mount --bind {self.hostdev}/$n /dev/$n; done; "
                f"for f in {self.devdir}/*; do [ -e \"$f\" ] || continue; touch /dev/$(basename $f); "
                "mount --bind $f /dev/$(basename $f); done; "
                f"PATH={stubs}; /usr/bin/env LD_PRELOAD={FAKES / 'devtrace.so'} DEVTRACE={self.trace} "
                f"LD_LIBRARY_PATH={libdir} {binary}"
            )
            cmd = [*SANDBOX, "/bin/sh", "-c", script]
        cmd[0] = shutil.which(cmd[0])
        env = bus.daemon_env(stubs)
        env["KAROTZ_STUB_DIR"] = str(stubs)
        env["KAROTZ_SYSLOG_FILE"] = str(self.syslog)
        if not self.qemu:
            env["KAROTZ_SYSROOT"] = str(self.sysroot)  # same file view as qemu -L
        if fake_ioctl_types:
            env["KAROTZ_FAKE_IOCTL_TYPES"] = ",".join(f"{t:x}" for t in fake_ioctl_types)
            # Data for faked read ioctls, if a test writes it (ioctlfake-arm.c).
            env["KAROTZ_FAKE_IOCTL_DATA_FILE"] = str(Path(workdir) / "ioctl-data")
        self.proc = subprocess.Popen(cmd, env=env, stdout=self.log, stderr=subprocess.STDOUT,
                                     start_new_session=True)

    def write(self, dev, data):
        """Write to a device; returns False if a FIFO isn't open in the daemon yet."""
        if self.fifos[dev].is_file():
            with open(self.fifos[dev], "ab") as f:
                f.write(data)
            return True
        try:
            fd = os.open(self.fifos[dev], os.O_WRONLY | os.O_NONBLOCK)
        except OSError:  # ENXIO: no reader
            return False
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        return True

    def device_io(self, raw_filter=None):
        """Device operations in order, consecutive repeats collapsed. A
        scenario's raw_filter sees the uncollapsed lines first and returns
        (lines to keep, extra record lines): a dropped line no longer
        separates blocks that would otherwise collapse."""
        if self.qemu:
            lines = parse_qemu_strace(self.strace)
        else:
            lines = self.trace.read_text().splitlines() if self.trace.exists() else []
        extra = []
        if raw_filter is not None:
            lines, extra = raw_filter(lines)
        return collapse_repeats(lines) + extra

    def alive(self):
        return self.proc.poll() is None

    def stop(self):
        if self.alive():
            os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                # Time to exit, and so flush its output (a test reads the log after this): under
                # qemu on a busy machine 5 s wasn't always enough, and SIGKILL lost the end.
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
        self.log.close()
        for path in self.fifos.values():
            if path.exists():
                path.unlink()


# Namespaces for every program under test: user (unprivileged "root"), mount,
# network and PID. The PID namespace matters: a program can call
# kill(-1, ...), which from a user namespace alone would still reach every
# process the user owns.
SANDBOX = ["unshare", "-r", "-m", "-n", "-p", "-f", "--mount-proc"]
# The program under test is run as a child of the namespace's shell, never
# exec'd as PID 1: PID 1 ignores signals it has no handler for, so abort()
# and the like would behave differently.

# The empty network namespace starts with loopback down; bring it up so
# localhost works (controller and karotzVM talk over it), nothing else does.
LOOPBACK_UP = "/usr/sbin/ip link set lo up 2>/dev/null || /sbin/ip link set lo up 2>/dev/null || true; "

# Commands the programs are known to run; each stub just exits 0.
STUB_COMMANDS = ["reboot", "halt", "poweroff", "shutdown", "killall", "kill", "madplay", "python", "karotz-player",
                 "rm", "cp", "mv", "mkdir", "touch", "sync", "ifconfig", "route", "udhcpc", "wpa_supplicant",
                 "dbus-send", "sleep", "echo", "mpd", "flac"]


def collapse_repeats(lines, longest=8):
    """Drop a block of up to `longest` lines that repeats the block just before it.

    Periodic polling makes the number of repeats depend on timing; the
    sequence of distinct steps is what's compared.
    """
    out = []
    for line in lines:
        out.append(line)
        for k in range(1, longest + 1):
            if len(out) >= 2 * k and out[-k:] == out[-2 * k:-k]:
                del out[-k:]
                break
    return out


# Commands the programs run from /sbin by full path, which the stubs on PATH
# don't catch: the host's own would run in the sandbox (its reboot is
# systemctl, which can reach the host's systemd through the filesystem's
# D-Bus socket). Each is covered by its stub, in the sandbox's mount
# namespace; a failed mount stops the program from starting.
SBIN_STUBS = ["reboot", "halt", "poweroff", "shutdown", "ifconfig", "route"]


def sbin_stub_mounts(stubs):
    """Shell commands that bind the stubs over the host's /sbin commands."""
    mount = shutil.which("mount") or shutil.which("mount", path="/usr/sbin:/sbin")
    return "".join(f"if [ -e /sbin/{name} ]; then {mount} --bind {stubs / name} /sbin/{name} || exit 1; fi; "
                   for name in SBIN_STUBS)


def make_stub_dir(workdir):
    """A directory of no-op commands to use as the whole PATH of a program under test."""
    stubs = Path(workdir) / "stub-bin"
    stubs.mkdir(exist_ok=True)
    for name in STUB_COMMANDS:
        path = stubs / name
        if not path.exists():
            path.write_text("#!/bin/true" + chr(10))
            path.chmod(0o755)
    return stubs


SKIP_DEVICES = {"/dev/null", "/dev/zero", "/dev/random", "/dev/urandom", "/dev/tty"}


# ioctls qemu's strace prints by name.
IOCTL_NAMES = {"TCGETS": 0x5401, "TCSETS": 0x5402, "TCSETSW": 0x5403, "TCSETSF": 0x5404, "TCSBRK": 0x5409,
               "TCXONC": 0x540A, "TCFLSH": 0x540B, "TIOCMGET": 0x5415, "TIOCMSET": 0x5418, "FIONREAD": 0x541B,
               "FIONBIO": 0x5421,
               # OSS sound (/dev/audio, /dev/dsp, /dev/mixer)
               "SNDCTL_DSP_RESET": 0x5000, "SNDCTL_DSP_SYNC": 0x5001, "SNDCTL_DSP_SPEED": 0xC0045002,
               "SNDCTL_DSP_STEREO": 0xC0045003, "SNDCTL_DSP_GETBLKSIZE": 0xC0045004, "SNDCTL_DSP_SETFMT": 0xC0045005,
               "SNDCTL_DSP_CHANNELS": 0xC0045006, "SNDCTL_DSP_GETFMTS": 0x8004500B, "SNDCTL_DSP_SETFRAGMENT": 0xC004500A,
               "SNDCTL_DSP_GETOSPACE": 0x8010500C, "SNDCTL_DSP_GETISPACE": 0x8010500D, "SNDCTL_DSP_GETCAPS": 0x8004500F,
               "SOUND_MIXER_READ_DEVMASK": 0x80044DFE, "SOUND_MIXER_READ_RECMASK": 0x80044DFD,
               "SOUND_MIXER_READ_STEREODEVS": 0x80044DFB, "SOUND_MIXER_READ_CAPS": 0x80044DFC}


def parse_qemu_strace(path):
    """Turn qemu-arm -d strace output into devtrace lines."""
    if not path.exists():
        return []
    fds, out = {}, []
    num = r"(-?(?:0x[0-9a-fA-F]+|\d+))"
    for line in path.read_text(errors="replace").splitlines():
        m = re.search(r'(?:open|openat)\((?:[^,]*,)?"(/dev/[^"]+)",[^)]*\) = (\d+)', line)
        if m:
            dev, fd = m.group(1), int(m.group(2))
            if dev not in SKIP_DEVICES:
                fds[fd] = dev
                out.append(f"open {dev}")
            continue
        # uClibc's system() is vfork + execve("/bin/sh", {"sh", "-c", cmd});
        # uClibc-ng's (the system build's) passes "/bin/sh" as argv[0].
        # Matched on its argv alone: the child's strace line can be split by
        # the parent's, which qemu writes to the same log (seen as
        # 'execve(<parent output>\n"/bin/sh", = {44\n"sh","-c","madplay -a 1.5 -",NULL})').
        m = re.search(r'"(?:/bin/)?sh","-c","((?:[^"\\]|\\.)*)"', line)
        if m:
            out.append(f"system {m.group(1)}")
            continue
        m = re.search(r"close\((\d+)\)", line)
        if m:
            fds.pop(int(m.group(1)), None)
            continue
        m = re.search(r"ioctl\((\d+),(" + "|".join(sorted(IOCTL_NAMES, key=len, reverse=True)) + r")\b", line)
        if m and int(m.group(1)) in fds:
            out.append(f"ioctl {fds[int(m.group(1))]} {hex(IOCTL_NAMES[m.group(2)])}")
            continue
        m = re.search(r"ioctl\((\d+)," + num, line)
        if m and int(m.group(1)) in fds:
            out.append(f"ioctl {fds[int(m.group(1))]} {hex(int(m.group(2), 0) & 0xFFFFFFFF)}")
            continue
        m = re.search(r"(read|write)\((\d+),[^,]*," + num + r"\)", line)
        if m and int(m.group(2)) in fds:
            out.append(f"{m.group(1)} {fds[int(m.group(2))]} {int(m.group(3), 0)}")
    return out


SOURCE_PATH = re.compile(r"(?:/[\w.+-]+)+/([\w.+-]+\.(?:c|cc|cpp|cxx|h|hh|hpp))\b")
# Files a scenario names after the harness's pid (unique on the host).
PID_NAMED = re.compile(r"(/tmp/(?:karotz|lapinlibre)[\w-]*?-)\d+\b")


def normalise_paths(record, workdir, build, sysroot):
    """The record without anything specific to this machine or run: the
    run's working directory, the build and sysroot, the repository, the work
    location and home directory, temporary names carrying the harness's pid,
    and the directories of source files named in log and assert messages."""
    prefixes = [(str(Path(workdir).resolve()), "<RUN>"), (str(workdir), "<RUN>"),
                (str(Path(build).resolve()), "<BUILD>"), (str(Path(sysroot).resolve()), "<SYSROOT>"),
                (str(HERE.parent), "<ROOT>")]
    location = os.environ.get("LL_LOCATION")
    if location:
        prefixes.append((str(Path(location).resolve()), "<WORK>"))
    prefixes.append((str(Path.home()), "<HOME>"))
    prefixes.sort(key=lambda p: len(p[0]), reverse=True)
    out = []
    for line in record:
        line = SOURCE_PATH.sub(r"\1", line)
        for prefix, mark in prefixes:
            line = line.replace(prefix, mark)
        line = PID_NAMED.sub(r"\1<pid>", line)
        out.append(line)
    return out


def unexpected_warnings(scenario, workdir):
    """The run's syslog warnings and worse that the scenario's EXPECTED_WARNINGS doesn't match."""
    path = Path(workdir) / "syslog"
    if not path.exists():
        return []
    allowed = [re.compile(p) for p in getattr(scenario, "EXPECTED_WARNINGS", ())]
    out = []
    for line in path.read_text(errors="replace").splitlines():
        level, _, message = line.partition(" ")
        if level.isdigit() and int(level) <= 4 and not any(p.search(message) for p in allowed):
            out.append(line)
    return out


def run(scenario, sysroot, build):
    """Run one scenario; returns its record and the run's working directory."""
    workdir = Path(tempfile.mkdtemp(prefix=f"lapinlibre-{scenario.DAEMON}-"))
    bus = Bus(workdir)
    daemon = None
    try:
        setup = scenario.sandbox_setup(workdir, sysroot) if hasattr(scenario, "sandbox_setup") else ""
        daemon = Daemon(scenario.DAEMON, sysroot, build, workdir, scenario.DEVICES, bus,
                        getattr(scenario, "FAKE_IOCTL_TYPES", ()), getattr(scenario, "REGULAR_FILE_DEVICES", ()),
                        setup)
        record = scenario.drive(daemon, bus)
        record.append("== signals")
        record.extend(bus.signals())
        record.append("== device I/O")
        record.extend(daemon.device_io(getattr(scenario, "raw_device_filter", None)))
        record.append(f"== daemon still running: {daemon.alive()}")
        if hasattr(scenario, "normalise_record"):
            record = scenario.normalise_record(record)
        return normalise_paths(record, workdir, build, sysroot), workdir
    finally:
        if daemon:
            daemon.stop()
        bus.close()


def main():
    global FAKES
    p = argparse.ArgumentParser(description="Run behaviour scenarios and compare their records.")
    p.add_argument("--record", action="store_true", help="write the expected records instead of comparing")
    p.add_argument("--suite", choices=["scenarios", "aggressive"], default="scenarios")
    p.add_argument("--sysroot", required=True, type=Path)
    p.add_argument("--build", required=True, type=Path)
    p.add_argument("--fakes", required=True, type=Path)
    p.add_argument("--expected", type=Path, default=HERE / "expected")
    p.add_argument("--expected-build", type=Path,
                   help="this build's own records, where it differs on purpose (e.g. tests/expected/system): "
                        "a scenario's record there is used instead of the one in --expected")
    p.add_argument("--label", default="", help="prefix for the result lines (e.g. arm/)")
    p.add_argument("names", nargs="*")
    args = p.parse_args()
    FAKES = args.fakes.resolve()
    failed = 0
    for name in args.names:
        label = f"{args.label}{name}"
        module = __import__(f"{args.suite}.{name}", fromlist=["drive"])
        started = time.time()
        try:
            record, workdir = run(module, args.sysroot, args.build)
        except Exception as e:  # a broken run is a failure, the others still run
            failed += 1
            print(f"FAIL  {label}: {type(e).__name__}: {e}", flush=True)
            # Where it happened: the last frames, so a broken run can be traced from the log.
            for frame in traceback.extract_tb(e.__traceback__)[-4:]:
                print(f"      at {Path(frame.filename).name}:{frame.lineno} in {frame.name}: {frame.line}", flush=True)
            continue
        took = f"{time.time() - started:.0f} s"
        warnings = unexpected_warnings(module, workdir)
        if warnings:
            failed += 1
            print(f"FAIL  {label}: warnings in its log that EXPECTED_WARNINGS doesn't list (logs in {workdir})")
            for line in warnings[:20]:
                print(f"    {line}")
            if len(warnings) > 20:
                print(f"    ... {len(warnings) - 20} more")
            sys.stdout.flush()
            if not args.record or args.suite == "aggressive":
                (workdir / "record.txt").write_text("\n".join(record) + "\n")
                continue
        if args.suite == "aggressive":
            problems = module.check(record)
            if hasattr(module, "summary"):  # measurements worth seeing either way
                print(f"      {label}: {module.summary(record)}")
            if problems:
                failed += 1
                print(f"FAIL  {label} (logs in {workdir})")
                for problem in problems:
                    print(f"    {problem}")
                (workdir / "record.txt").write_text("\n".join(record) + "\n")
            else:
                print(f"PASS  {label}: {len(record)} lines, all checks hold ({took})")
                shutil.rmtree(workdir, ignore_errors=True)
            sys.stdout.flush()
            continue
        expected_file = args.expected / f"{name}.txt"
        if args.expected_build and (args.expected_build / f"{name}.txt").exists():
            expected_file = args.expected_build / f"{name}.txt"
        text = "\n".join(record) + "\n"
        if args.record:
            expected_file.parent.mkdir(parents=True, exist_ok=True)
            expected_file.write_text(text, encoding="utf-8", newline="\n")
            print(f"RECORDED  {label}: {len(record)} lines -> {expected_file} ({took})", flush=True)
            shutil.rmtree(workdir, ignore_errors=True)
            continue
        if not expected_file.exists():
            failed += 1
            print(f"FAIL  {label}: no expected record {expected_file} (tests/run.sh --record)", flush=True)
            (workdir / "record.txt").write_text(text)
            continue
        expected = expected_file.read_text(encoding="utf-8").splitlines()
        # Compared as written: a record line may itself hold a newline (a
        # value sent with one), which the file stores as two lines.
        record = text.splitlines()
        if record == expected:
            print(f"PASS  {label}: {len(record)} lines match ({took})", flush=True)
            shutil.rmtree(workdir, ignore_errors=True)
        else:
            failed += 1
            (workdir / "record.txt").write_text(text)
            print(f"FAIL  {label}: record differs from {expected_file} (logs and record.txt in {workdir})")
            diff = list(difflib.unified_diff(expected, record, "expected", "this run", lineterm=""))
            for line in diff[:80]:
                print("   ", line)
            if len(diff) > 80:
                print(f"    ... {len(diff) - 80} more diff lines")
            sys.stdout.flush()
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    # Run main() in the module the scenarios import, so they see FAKES.
    import harness
    harness.main()
