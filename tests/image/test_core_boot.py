# SPDX-License-Identifier: MIT
"""The core image's generated files and its telnet switch: what the build
makes, and how inetd.sh adds telnet when the key's install set it; the core scripts usbkey.sh,
network.sh and the head button's watcher, run in sandboxes. system/tests/core-boot.sh tests the
boot itself under qemu.

Needs the network (or the download cache) for the pinned RT73 firmware.
"""

import contextlib
import io
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "image" / "tools"))
sys.path.insert(0, str(ROOT / "scripts"))
import image  # noqa: E402


def generated():
    with contextlib.redirect_stderr(io.StringIO()):
        return image.core_generated([])


class CoreGeneratedTest(unittest.TestCase):
    def test_core_inetd_serves_the_page_only(self):
        # The core's inetd.conf serves the web page over IPv4, at most 60 connections
        # a minute, with the configuration inetd.sh makes in RAM; telnet is the install's choice
        # (inetd.sh).
        conf = generated()[("rootfs", "karotz/etc/inetd.conf")]
        lines = [line for line in conf.decode().splitlines() if line and not line.startswith("#")]
        self.assertEqual(lines, ["80 stream tcp nowait.60 root /bin/busybox httpd -i -c /var/run/httpd.conf -h /usr/www"])
        for service in (b"telnetd", b"ftpd", b"/karotz/etc/httpd.conf"):
            self.assertNotIn(service, conf)

    def test_fixed_httpd_conf_refuses_everyone(self):
        # Read directly (an inetd.conf from before the generated configuration), the fixed part
        # refuses every client: no allow rule, a deny-all.
        fixed = (ROOT / "image/core/rootfs/karotz/etc/httpd.conf").read_text(encoding="utf-8")
        rules = [line for line in fixed.splitlines() if line and not line.startswith("#")]
        self.assertIn("D:*", rules)
        self.assertFalse([r for r in rules if r.startswith(("A:", "P:", "/"))], rules)

    def test_firmware_and_sounds(self):
        files = generated()
        self.assertEqual(len(files[("rootfs", "lib/firmware/rt73.bin")]), 2048)
        self.assertIn(b"Ralink", files[("rootfs", "lib/firmware/LICENCE.ralink-firmware.txt")])
        for name in image.SOUNDS:
            wav = files[("usr", f"karotz/sounds/{name}")]
            self.assertEqual((wav[:4], wav[8:12]), (b"RIFF", b"WAVE"), name)
        for name in image.EFFECTS:
            sound = files[("usr", f"karotz/sounds/{name}")]
            # An MPEG audio frame from the first byte (no tag).
            self.assertEqual((sound[0], sound[1] & 0xE0), (0xFF, 0xE0), name)


def arm_busybox():
    """The image's own BusyBox when this machine can run it (qemu-user through binfmt), or None."""
    from lapin import work_dir
    path = work_dir() / "system" / "out" / "build" / "busybox-1.37.0" / "busybox"
    if not path.is_file():
        return None
    try:
        r = subprocess.run([str(path), "true"], capture_output=True, timeout=60)
    except OSError:
        return None
    return path if r.returncode == 0 else None


# A stand-in for BusyBox's cryptpw where the image's BusyBox can't run: a hash of the same form.
FAKE_CRYPTPW = ('[ "$1" = cryptpw ] || exit 1\n'
                'h=$(sha512sum | cut -c1-86)\n'
                'printf \'$6$fakesalt$%s\\n\' "$h"')

INETD_PATHS = ("/var/run/", "/etc/inetd.conf", "/usr/etc/conf/", "/sbin/inetd", "/karotz/etc/httpd.conf")
LAN_RULES = ["A:127.0.0.1", "A:10.0.0.0/8", "A:172.16.0.0/12", "A:192.168.0.0/16", "A:169.254.0.0/16", "D:*"]


class InetdTest(unittest.TestCase):
    """image/core's inetd.sh, run here with its paths in a sandbox, a stub inetd that prints the
    configuration it was given, a stub logger, and BusyBox for cryptpw: the image's own under qemu
    when this machine runs it, else a stand-in that makes a hash of the same form."""

    busybox = None

    @classmethod
    def setUpClass(cls):
        cls.busybox = arm_busybox()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        sb = self.sb = Path(self.tmp.name)
        for d in ("etc", "usr/etc/conf", "var/run", "sbin", "bin", "karotz/etc"):
            (sb / d).mkdir(parents=True)
        shutil.copy(ROOT / "image/core/rootfs/karotz/etc/httpd.conf", sb / "karotz/etc/httpd.conf")
        stub(sb / "sbin/inetd", 'echo "inetd $1"; cat "$2"')
        stub(sb / "bin/logger", f'echo "$*" >> {sb}/syslog.log')
        if self.busybox:
            stub(sb / "bin/busybox", f'exec {self.busybox} "$@"')
        else:
            stub(sb / "bin/busybox", FAKE_CRYPTPW)
        self.conf = sb / "usr/etc/conf"
        self.httpd_conf = sb / "var/run/httpd.conf"

    def tearDown(self):
        for d in (self.sb / "var/run",):
            d.chmod(0o755)
        self.tmp.cleanup()

    def run_inetd(self, inetd_conf, telnet=None):
        sb = self.sb
        script = (ROOT / "image/core/rootfs/karotz/scripts/inetd.sh").read_text(encoding="utf-8")
        (sb / "etc/inetd.conf").write_text(inetd_conf, newline="\n")
        if telnet is not None:
            (self.conf / "telnet").write_text(telnet + "\n", newline="\n")
        for path in INETD_PATHS:
            script = script.replace(path, f"{sb}{path}")
        script = script.replace("exec sleep 2147483647", "echo waits; exit 0")
        return subprocess.run(["sh", "-c", script], env={"PATH": f"{sb}/bin:/usr/bin:/bin"},
                              capture_output=True, text=True, timeout=120).stdout

    def syslog(self):
        path = self.sb / "syslog.log"
        return path.read_text() if path.exists() else ""

    def rules(self):
        return [line for line in self.httpd_conf.read_text().splitlines() if line and not line.startswith("#")]

    def mode(self, path):
        return oct(path.stat().st_mode & 0o777)

    def test_telnet_only_when_the_install_set_it(self):
        page = "# comment\n80 stream tcp nowait root /bin/busybox httpd -i\n"
        on = self.run_inetd(page, "yes")
        off = self.run_inetd(page, "no")
        unset = self.run_inetd(page, None)
        self.assertIn("telnetd -i -l /bin/sh", on)
        self.assertIn("httpd -i", on)
        for out in (off, unset):
            self.assertNotIn("telnetd", out)
            self.assertIn("httpd -i", out)
        self.assertNotIn("# comment", on)

    def test_no_service_waits(self):
        # An inetd.conf with no service and telnet off: nothing to serve, so no inetd (init would restart it
        # without end); with telnet on, inetd runs for telnet alone.
        self.assertEqual(self.run_inetd("# nothing\n", "no").strip(), "waits")
        self.assertIn("telnetd", self.run_inetd("# nothing\n", "yes"))

    # --- the web server's configuration ---

    PAGE = "80 stream tcp nowait.60 root /bin/busybox httpd -i -c /var/run/httpd.conf -h /usr/www\n"

    def test_no_password_no_login(self):
        # No WEB_PASSWORD (the default): the local network only, no login, and the boot says so.
        out = self.run_inetd(self.PAGE)
        self.assertIn("httpd -i -c /var/run/httpd.conf", out)
        rules = self.rules()
        self.assertEqual(rules[:6], ["A:127.0.0.1", "A:10.0.0.0/8", "A:172.16.0.0/12", "A:192.168.0.0/16",
                                     "A:169.254.0.0/16", "D:*"])
        self.assertFalse([r for r in rules if r.startswith("/")])
        self.assertEqual(self.mode(self.httpd_conf), "0o600")
        self.assertIn("no web password is set", self.syslog())

    def test_new_password_hashed_once_and_removed(self):
        plain = self.conf / "web_password"
        plain.write_text("secretpass1\n", newline="\n")
        plain.chmod(0o600)
        self.run_inetd(self.PAGE)
        self.assertFalse(plain.exists())
        hash_file = self.conf / "web_password.hash"
        self.assertEqual(self.mode(hash_file), "0o600")
        digest = hash_file.read_text().strip()
        self.assertRegex(digest, r"^\$6\$[./0-9A-Za-z]{1,16}\$[./0-9A-Za-z]{86}$")
        # The local network, then deny-all, the fixed part's index, and the password for every path.
        self.assertEqual(self.rules(), LAN_RULES + ["I:index.html", f"/:admin:{digest}"])
        self.assertEqual(self.mode(self.httpd_conf), "0o600")
        for text in (self.httpd_conf.read_text(), self.syslog(), hash_file.read_text()):
            self.assertNotIn("secretpass1", text)
        if self.busybox:
            # The image's own cryptpw gives the same hash for the password with the stored salt.
            salt = digest[:digest.rindex("$")]
            again = subprocess.run([str(self.busybox), "cryptpw", "-m", "sha512", "-S", salt.split("$")[2], "secretpass1"],
                                   capture_output=True, text=True, timeout=60).stdout.strip()
            self.assertEqual(again, digest)
        # The next start uses the hash as it is: the flash isn't written again.
        stamp = hash_file.stat().st_mtime_ns
        time.sleep(0.05)
        self.run_inetd(self.PAGE)
        self.assertEqual(hash_file.stat().st_mtime_ns, stamp)
        self.assertEqual(self.rules()[-1], f"/:admin:{digest}")

    def test_new_password_replaces_the_old_hash(self):
        (self.conf / "web_password.hash").write_text("$6$oldsalt$" + "A" * 86 + "\n", newline="\n")
        (self.conf / "web_password").write_text("anotherpass\n", newline="\n")
        self.run_inetd(self.PAGE)
        digest = (self.conf / "web_password.hash").read_text().strip()
        self.assertNotIn("oldsalt", digest)
        self.assertEqual(self.rules()[-1], f"/:admin:{digest}")

    def test_hash_only_is_used_as_it_is(self):
        digest = "$6$abcdefgh$" + "x" * 86
        (self.conf / "web_password.hash").write_text(digest + "\n", newline="\n")
        self.run_inetd(self.PAGE)
        self.assertEqual(self.rules()[-1], f"/:admin:{digest}")

    def test_bad_hash_refuses_everything(self):
        for bad in ("plaintext", "$6$salt$has:colon", "$6$salt$has#hash", "$1$md5$" + "x" * 22 + " x", "$6$" + "x" * 130):
            with self.subTest(bad):
                (self.conf / "web_password.hash").write_text(bad + "\n", newline="\n")
                self.run_inetd(self.PAGE)
                self.assertEqual(self.rules(), ["D:*"])

    def test_hashing_failure_refuses_everything_and_keeps_the_password(self):
        stub(self.sb / "bin/busybox", "exit 1")
        (self.conf / "web_password.hash").write_text("$6$oldsalt$" + "A" * 86 + "\n", newline="\n")
        (self.conf / "web_password").write_text("secretpass1\n", newline="\n")
        self.run_inetd(self.PAGE)
        self.assertEqual(self.rules(), ["D:*"])
        # Tried again at the next start; the old password no longer opens the page.
        self.assertTrue((self.conf / "web_password").exists())
        self.assertFalse((self.conf / "web_password.hash").exists())
        self.assertIn("couldn't be hashed", self.syslog())

    def test_fixed_part_gives_only_its_index_and_types(self):
        (self.sb / "karotz/etc/httpd.conf").write_text(
            "A:*\nP:/x:http://example.com/\n/cgi-bin:me:open\nI:index.html\nE404:/404.html\n.txt:text/plain\nH:/tmp\n",
            newline="\n")
        (self.conf / "web_password.hash").write_text("$6$abcdefgh$" + "x" * 86 + "\n", newline="\n")
        self.run_inetd(self.PAGE)
        rules = self.rules()
        self.assertEqual(rules[:6], LAN_RULES)
        self.assertEqual(rules[6:9], ["I:index.html", "E404:/404.html", ".txt:text/plain"])
        self.assertEqual(len(rules), 10)
        for bad in ("A:*", "P:/x:http://example.com/", "/cgi-bin:me:open", "H:/tmp"):
            self.assertNotIn(bad, rules)

    def test_write_failure_refuses_everything(self):
        # The new file can't be made: what httpd reads then refuses everyone.
        (self.conf / "web_password.hash").write_text("$6$abcdefgh$" + "x" * 86 + "\n", newline="\n")
        (self.sb / "var/run/httpd.conf.new").mkdir()
        self.run_inetd(self.PAGE)
        self.assertEqual(self.rules(), ["D:*"])
        self.assertIn("couldn't be written", self.syslog())
        # Nothing can be written there at all: no configuration, so httpd can't start.
        (self.sb / "var/run/httpd.conf.new").rmdir()
        self.httpd_conf.unlink()
        (self.sb / "var/run").chmod(0o555)
        self.run_inetd(self.PAGE)
        self.assertFalse(self.httpd_conf.exists())


def stub(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body + "\n", newline="\n")
    path.chmod(0o755)


def sandboxed(name, sb, paths, extra=()):
    """image/core's script NAME (under rootfs/karotz/scripts) with PATH and the given absolute paths
    moved into the sandbox sb, plus (old, new) replacements."""
    script = (ROOT / "image/core/rootfs/karotz/scripts" / name).read_text(encoding="utf-8")
    script = script.replace("PATH=/bin:/sbin:/usr/bin:/usr/sbin\n", f"PATH={sb}/bin:$PATH\n")
    for path in paths:
        script = script.replace(path, f"{sb}{path}")
    for old, new in extra:
        script = script.replace(old, new)
    return script


class UsbKeyTest(unittest.TestCase):
    """image/core's usbkey.sh (mdev's stick rule), with stub mount and umount that keep a sandbox
    /proc/mounts."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        sb = self.sb = Path(self.tmp.name)
        for d in ("mnt/usbkey", "proc", "dev", "sys/block/uba", "bin"):
            (sb / d).mkdir(parents=True)
        (sb / "proc/mounts").write_text("rootfs / rootfs rw 0 0\n", newline="\n")
        self.log = sb / "actions.log"
        stub(sb / "bin/mount", f'echo "mount $*" >> {self.log}; echo "$3 $4 vfat rw 0 0" >> {sb}/proc/mounts')
        stub(sb / "bin/umount", f'echo "umount $*" >> {self.log}; grep -v " $1 " {sb}/proc/mounts > {sb}/m; '
                                f'cat {sb}/m > {sb}/proc/mounts')
        stub(sb / "bin/logger", "exit 0")
        self.script = sandboxed("usbkey.sh", sb, ("/mnt/usbkey", "/usr/etc/usbkey.d", "/proc/mounts", "/sys/block/"),
                                (('"/dev/$', f'"{sb}/dev/$'), ("[ -b ", "[ -e ")))

    def tearDown(self):
        self.tmp.cleanup()

    def event(self, mdev, action="add"):
        env = {"PATH": "/usr/bin:/bin", "MDEV": mdev}
        if action is not None:
            env["ACTION"] = action
        subprocess.run(["sh", "-c", self.script], env=env, capture_output=True, text=True, timeout=60)

    def actions(self):
        text = self.log.read_text() if self.log.exists() else ""
        return text.replace(str(self.sb), "").splitlines()

    def test_whole_disk_without_partitions(self):
        (self.sb / "dev/uba").touch()
        self.event("uba", action=None)  # mdev -s sets no ACTION here: add
        self.assertEqual(self.actions(), ["mount -t vfat /dev/uba /mnt/usbkey"])

    def test_first_partition_preferred_and_mounted_once(self):
        (self.sb / "sys/block/uba/uba1").mkdir()
        (self.sb / "dev/uba").touch()
        (self.sb / "dev/uba1").touch()
        self.event("uba")
        self.event("uba1")
        self.assertEqual(self.actions(), ["mount -t vfat /dev/uba1 /mnt/usbkey"])

    def test_partition_without_its_node_waits_for_its_event(self):
        (self.sb / "sys/block/uba/uba1").mkdir()
        self.event("uba")
        self.assertEqual(self.actions(), [])

    def test_remove_only_the_mounted_device(self):
        (self.sb / "dev/uba").touch()
        self.event("uba")
        self.event("ubb", "remove")
        self.assertEqual(len(self.actions()), 1)
        self.event("uba", "remove")
        self.assertEqual(self.actions()[1:], ["umount /mnt/usbkey"])
        self.assertNotIn("/mnt/usbkey", (self.sb / "proc/mounts").read_text())

    def test_hooks_in_order_around_mount_and_unmount(self):
        hooks = self.sb / "usr/etc/usbkey.d"
        for name in ("20-second", "10-first"):
            stub(hooks / name, f'echo "{name} $1" >> {self.log}; exit 1')  # failures are ignored
        (hooks / "30-not-executable").write_text("#!/bin/sh\necho never >> {self.log}\n", newline="\n")
        (self.sb / "dev/uba").touch()
        self.event("uba")
        self.event("uba", "remove")
        self.assertEqual(self.actions(), [
            "mount -t vfat /dev/uba /mnt/usbkey", "10-first add", "20-second add",
            "10-first remove", "20-second remove", "umount /mnt/usbkey", "10-first removed", "20-second removed"])


class NetworkTest(unittest.TestCase):
    """image/core's network.sh with stub network tools: ifconfig and iwconfig report what the test
    sets, the daemons record that they were started, and a sandbox /proc lists what runs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        sb = self.sb = Path(self.tmp.name)
        for d in ("usr/etc/conf", "etc", "proc", "bin"):
            (sb / d).mkdir(parents=True)
        self.log = sb / "actions.log"
        for tool in ("wpa_supplicant", "udhcpc", "route"):
            stub(sb / "bin" / tool, f'echo "{tool} $*" >> {self.log}')
        stub(sb / "bin/ifconfig", f'[ $# -gt 1 ] && echo "ifconfig $*" >> {self.log}; '
                                  f'[ "$*" = wlan0 ] && cat {sb}/address 2> /dev/null; exit 0')
        stub(sb / "bin/iwconfig", f'cat {sb}/iw 2> /dev/null')
        stub(sb / "bin/logger", "exit 0")
        self.script = sandboxed("network.sh", sb, ("/usr/etc/conf", "/etc/resolv.conf", "/proc/"))

    def tearDown(self):
        self.tmp.cleanup()

    def settings(self, wpa="network={\n}\n", network=None):
        if wpa is not None:
            (self.sb / "usr/etc/conf/wpa.conf").write_text(wpa, newline="\n")
        if network is not None:
            (self.sb / "usr/etc/conf/network.conf").write_text(network, newline="\n")

    def up(self, associated=True, address=True):
        (self.sb / "iw").write_text("wlan0  Access Point: " + ("00:11:22:33:44:55\n" if associated else "Not-Associated\n"))
        (self.sb / "address").write_text("          inet addr:192.168.1.20  Mask:255.255.255.0\n" if address else "")

    def running(self, *args):
        d = self.sb / "proc" / str(100 + len(list((self.sb / "proc").iterdir())))
        d.mkdir()
        (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in args) + b"\0")

    def run_network(self, limit="2"):
        r = subprocess.run(["sh", "-c", self.script, "network.sh", limit], env={"PATH": "/usr/bin:/bin"},
                           capture_output=True, text=True, timeout=60)
        text = self.log.read_text() if self.log.exists() else ""
        return r.returncode, text.replace(str(self.sb), "").splitlines()

    def test_no_wifi_settings_fails_at_once(self):
        self.settings(wpa=None)
        rc, actions = self.run_network()
        self.assertEqual((rc, actions), (1, []))

    def test_dhcp(self):
        self.settings()
        self.up()
        rc, actions = self.run_network()
        self.assertEqual(rc, 0)
        self.assertIn("wpa_supplicant -iwlan0 -Dwext -B -c/usr/etc/conf/wpa.conf", actions)
        self.assertIn("udhcpc -b -i wlan0 -s /karotz/scripts/udhcpc_script.sh", actions)

    def test_static_address(self):
        self.settings(network='IP="192.168.1.20"\nNETMASK=""\nGATEWAY="192.168.1.1"\nDNS="192.168.1.2"\n')
        self.up()
        rc, actions = self.run_network()
        self.assertEqual(rc, 0)
        self.assertIn("ifconfig wlan0 192.168.1.20 netmask 255.255.255.0 up", actions)
        self.assertIn("route add default gw 192.168.1.1 dev wlan0", actions)
        self.assertFalse([a for a in actions if a.startswith("udhcpc")])
        self.assertEqual((self.sb / "etc/resolv.conf").read_text(), "nameserver 192.168.1.2\n")

    def test_runs_again_without_second_daemons(self):
        self.settings()
        self.up()
        self.running("wpa_supplicant", "-iwlan0", "-Dwext", "-B", "-c/usr/etc/conf/wpa.conf")
        self.running("udhcpc", "-b", "-i", "wlan0", "-s", "/karotz/scripts/udhcpc_script.sh")
        rc, actions = self.run_network()
        self.assertEqual(rc, 0)
        self.assertFalse([a for a in actions if a.startswith(("wpa_supplicant", "udhcpc"))])

    def test_address_without_association_times_out(self):
        self.settings(network='IP="192.168.1.20"\n')
        self.up(associated=False)
        rc, _ = self.run_network(limit="1")
        self.assertEqual(rc, 1)
        self.up(address=False)
        rc, _ = self.run_network(limit="1")
        self.assertEqual(rc, 1)


BUTTON = "/com/mindscape/karotz/Button"
KAROTZ = "com.mindscape.karotz.KarotzInterface"
WATCHER = ROOT / "image/core/usr/scripts/dbus_watcher"
TONES = ("button-click.wav", "button-double.wav", "button-triple.wav", "button-long.wav")


def signal(member, path=BUTTON, interface=KAROTZ, kind="signal", arg=None):
    """A message as dbus-monitor prints it: a header line and one indented line per argument."""
    text = (f"{kind} time=1760000000.123456 sender=:1.4 -> destination=(null destination) serial=7 "
            f"path={path}; interface={interface}; member={member}\n")
    return text + (f"   {arg}\n" if arg else "")


NAME_ACQUIRED = ("signal time=1760000000.000001 sender=org.freedesktop.DBus -> destination=:1.9 serial=2 "
                 "path=/org/freedesktop/DBus; interface=org.freedesktop.DBus; member=NameAcquired\n"
                 '   string ":1.9"\n')


def pause(seconds):
    """A pause in the stub monitor's output."""
    return f"SLEEP {seconds}\n"


class ButtonWatcherTest(unittest.TestCase):
    """image/core's dbus_watcher with its paths in a sandbox: a stub dbus-monitor that records its
    arguments and prints canned signals, a stub karotz-player and hooks that append to a log, a stub
    logger, and stubs for the commands that must never run."""

    def setUp(self):
        # A hook's leftover child may still write when the test ends.
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        sb = self.sb = Path(self.tmp.name)
        for d in ("bin", "etc/conf", "usr/karotz/sounds", "usr/bin", "var/run/karotz"):
            (sb / d).mkdir(parents=True)
        for name in TONES:
            (sb / "usr/karotz/sounds" / name).write_bytes(b"RIFF")
        self.log = sb / "actions.log"
        self.syslog = sb / "syslog.log"
        stub(sb / "bin/dbus-monitor",
             f'for a; do echo "$a"; done > {sb}/monitor.args; echo $$ > {sb}/monitor.pid\n'
             f'[ -f {sb}/signals ] && while IFS= read -r l; do\n'
             '    case $l in "SLEEP "*) sleep "${l#SLEEP }" ;; *) printf "%s\\n" "$l" ;; esac\n'
             f'done < {sb}/signals\n'
             f'[ -e {sb}/hold ] && exec sleep 30\n'
             'exit 0')
        stub(sb / "usr/bin/karotz-player",
             f'echo "tone ${{1##*/}}" >> {self.log}; echo $$ >> {sb}/player.pid; date +%s.%N >> {sb}/tone.times\n'
             f'[ -e {sb}/player-sleeps ] && exec sleep 30\nexit 0')
        stub(sb / "bin/logger",
             'pri=user.notice; msg=""\n'
             'while [ $# -gt 0 ]; do case $1 in -t) shift 2 ;; -p) pri=$2; shift 2 ;; *) msg="$msg$1"; shift ;; esac; done\n'
             f'if [ -n "$msg" ]; then echo "$pri $msg" >> {self.syslog}\n'
             f'else while IFS= read -r l; do echo "$pri out $l" >> {self.syslog}; done; fi')
        for name in ("reboot", "halt", "poweroff", "shutdown", "killall"):
            stub(sb / "bin" / name, f'echo "FORBIDDEN {name} $*" >> {self.log}')
        script = WATCHER.read_text(encoding="utf-8")
        script = script.replace("PATH=/bin:/sbin:/usr/bin:/usr/sbin\n", f"PATH={sb}/bin:$PATH\n")
        for path in ("/etc/button.d", "/etc/conf/playsounds", "/usr/karotz/sounds", "/usr/bin/karotz-player",
                     "/var/run/karotz"):
            script = script.replace(path, f"{sb}{path}")
        self.script = script
        self.hooks = sb / "etc/button.d"

    def tearDown(self):
        # No signal may ever reboot, halt or kill everything.
        self.assertNotIn("FORBIDDEN", self.log.read_text() if self.log.exists() else "")
        for name in ("player.pid", "hook.pids"):
            path = self.sb / name
            for pid in (path.read_text().split() if path.exists() else ()):
                subprocess.run(["kill", pid], capture_output=True)
        time.sleep(0.3)  # the stubs' last writes
        self.tmp.cleanup()

    def signals(self, *messages):
        # The stub monitor stays a moment after its last signal, as a bus does: its end ends the tone.
        (self.sb / "signals").write_text("".join(messages) + pause(0.5), newline="\n")

    def sounds_on(self):
        (self.sb / "etc/conf/playsounds").write_text("1\n", newline="\n")

    def hook(self, name, body=""):
        stub(self.hooks / name, f'echo "hook {name} $1 $2 $BUTTON_GESTURE $BUTTON_CLICKS" >> {self.log}\n{body}')

    def start(self):
        return subprocess.Popen(["sh", "-c", self.script, "dbus_watcher"], env={"PATH": "/usr/bin:/bin"},
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def run_watcher(self, settle=1.0):
        """Runs the watcher until the stub monitor's output ends, then leaves its tones and hooks
        settle; returns its exit status, its console output and the actions."""
        p = self.start()
        out, err = p.communicate(timeout=60)
        time.sleep(settle)
        return p.returncode, out + err, self.actions()

    def assertGestures(self, actions, gestures):
        """actions is each gesture's in turn; within one gesture the tone and the hooks start
        together, so their lines may come in either order."""
        got, i = [], 0
        for gesture in gestures:
            got.append(sorted(actions[i:i + len(gesture)]))
            i += len(gesture)
        self.assertEqual((got, actions[i:]), ([sorted(g) for g in gestures], []), actions)

    def actions(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def syslog_lines(self):
        return self.syslog.read_text().splitlines() if self.syslog.exists() else []

    def test_monitor_rule_is_the_buttons_only(self):
        rc, console, _ = self.run_watcher(settle=0)
        args = (self.sb / "monitor.args").read_text().splitlines()
        self.assertEqual(args[0], "--system")
        self.assertEqual(len(args), 2, args)
        rule = dict(term.split("=", 1) for term in args[1].split(","))
        self.assertEqual(rule, {"type": "'signal'", "path": f"'{BUTTON}'", "interface": f"'{KAROTZ}'"})
        self.assertNotEqual(rc, 0)
        self.assertEqual(console, b"")
        self.assertTrue([line for line in self.syslog_lines() if "started" in line and args[1] in line])

    def test_each_gesture_its_tone_and_hook_arguments(self):
        self.sounds_on()
        self.hook("50-test")
        self.signals(signal("click"), pause(0.3), signal("dclick"), pause(0.3), signal("tclick"), pause(0.3),
                     signal("mclick", arg="int32 5"), pause(0.3), signal("lclick_start"), pause(0.3),
                     signal("lclick_stop"))
        _, _, actions = self.run_watcher()
        self.assertGestures(actions, [
            ["tone button-click.wav", "hook 50-test click 1 click 1"],
            ["tone button-double.wav", "hook 50-test dclick 2 dclick 2"],
            ["tone button-triple.wav", "hook 50-test tclick 3 tclick 3"],
            ["hook 50-test mclick 5 mclick 5"],
            ["tone button-long.wav", "hook 50-test lclick_start 0 lclick_start 0"],
            ["hook 50-test lclick_stop 0 lclick_stop 0"]])

    def test_near_misses_give_nothing(self):
        self.sounds_on()
        self.hook("50-test")
        self.signals(NAME_ACQUIRED, signal("lclick_stopped"), signal("clicked"), signal("Click"),
                     signal("xdclick"), signal("click", path=BUTTON + "s"), signal("click", path="/com/mindscape/karotz/Led"),
                     signal("click", interface="com.mindscape.karotz.Other"), signal("click", kind="method call"),
                     signal("event"), signal("registered"), signal("lclick_stop_x", arg="int32 5"),
                     signal("mclick"), signal("dclick", path="/com/mindscape/karotz/Button/x"))
        _, _, actions = self.run_watcher()
        self.assertEqual(actions, [])

    def test_sound_switch(self):
        self.hook("50-test")
        self.signals(signal("click"))
        _, _, actions = self.run_watcher()
        self.assertEqual(actions, ["hook 50-test click 1 click 1"])
        self.log.unlink()
        self.sounds_on()
        _, _, actions = self.run_watcher()
        self.assertGestures(actions, [["tone button-click.wav", "hook 50-test click 1 click 1"]])

    def test_missing_tone_file_skips_the_tone_only(self):
        self.sounds_on()
        self.hook("50-test")
        (self.sb / "usr/karotz/sounds/button-click.wav").unlink()
        self.signals(signal("click"), pause(0.3), signal("dclick"))
        _, _, actions = self.run_watcher()
        self.assertGestures(actions, [["hook 50-test click 1 click 1"],
                                      ["tone button-double.wav", "hook 50-test dclick 2 dclick 2"]])
        warnings = [line for line in self.syslog_lines() if line.startswith("user.warning")]
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("button-click.wav", warnings[0])

    def test_hooks_order_and_skips(self):
        self.hook("20-second")
        self.hook("10-first", "exit 3")  # a failing hook doesn't stop the next
        self.hook("30-third", "echo hello from a hook")
        self.hook(".hidden")
        self.hook("40-backup~")
        (self.hooks / "15-not-executable").write_text(f"#!/bin/sh\necho never >> {self.log}\n", newline="\n")
        (self.hooks / "25-directory").mkdir(mode=0o755)
        self.signals(signal("click"))
        _, _, actions = self.run_watcher()
        self.assertEqual(actions, ["hook 10-first click 1 click 1", "hook 20-second click 1 click 1",
                                   "hook 30-third click 1 click 1"])
        syslog = self.syslog_lines()
        self.assertIn("user.info out hello from a hook", syslog)
        self.assertTrue([line for line in syslog if "10-first" in line and "status 3" in line], syslog)

    def test_no_hooks_directory_or_an_empty_one(self):
        self.sounds_on()
        self.signals(signal("click"))
        rc, console, actions = self.run_watcher()
        self.assertEqual((rc, console, actions), (1, b"", ["tone button-click.wav"]))
        self.log.unlink()
        self.hooks.mkdir()
        rc, console, actions = self.run_watcher()
        self.assertEqual((rc, console, actions), (1, b"", ["tone button-click.wav"]))

    def test_hanging_hook_is_killed_and_does_not_delay_the_next_signal(self):
        self.sounds_on()
        self.hook("10-sleeps", f'echo $$ >> {self.sb}/hook.pids; sleep 30; echo "woke $1" >> {self.log}')
        self.signals(signal("click"), pause(0.5), signal("dclick"))
        start = time.monotonic()
        p = self.start()
        p.communicate(timeout=60)
        # The double click's tone and hook came while the click's hook still slept.
        self.assertLess(time.monotonic() - start, 5)
        time.sleep(0.5)
        self.assertGestures(self.actions(), [["tone button-click.wav", "hook 10-sleeps click 1 click 1"],
                                             ["tone button-double.wav", "hook 10-sleeps dclick 2 dclick 2"]])
        pids = (self.sb / "hook.pids").read_text().split()
        self.assertEqual(len(pids), 2)
        self.assertTrue(all(self.alive(pid) for pid in pids))
        # Each hook gets at most 10 s.
        time.sleep(max(0.0, start + 12 - time.monotonic()))
        self.assertFalse([pid for pid in pids if self.alive(pid)])
        self.assertFalse([a for a in self.actions() if a.startswith("woke")])

    def pids(self, name):
        return (self.sb / name).read_text().split()

    @staticmethod
    def alive(pid):
        """Whether the process runs (a zombie has ended)."""
        try:
            return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
        except OSError:
            return False

    def test_at_most_four_hook_runners(self):
        self.hook("10-slow", f'echo $$ >> {self.sb}/hook.pids; sleep 3')
        self.signals(*[signal("click") + pause(0.1) for _ in range(5)], pause(4), signal("dclick"))
        _, _, actions = self.run_watcher(settle=3.5)
        hooks = [a for a in actions if a.startswith("hook")]
        self.assertEqual(hooks, ["hook 10-slow click 1 click 1"] * 4 + ["hook 10-slow dclick 2 dclick 2"])
        warnings = [line for line in self.syslog_lines() if line.startswith("user.warning")]
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("no hooks for click 1", warnings[0])

    def test_tone_before_hooks_and_one_tone_at_a_time(self):
        self.sounds_on()
        (self.sb / "player-sleeps").touch()
        self.hook("50-test", f"date +%s.%N >> {self.sb}/hook.times")
        self.signals(signal("click"), pause(0.5), signal("lclick_start"))
        _, _, actions = self.run_watcher(settle=0.5)
        self.assertGestures(actions, [["tone button-click.wav", "hook 50-test click 1 click 1"],
                                      ["tone button-long.wav", "hook 50-test lclick_start 0 lclick_start 0"]])
        # The tone is started first: the hooks' runner, started just after it, never gets far ahead.
        for tone, hook in zip(self.pids("tone.times"), self.pids("hook.times")):
            self.assertLess(float(tone), float(hook) + 0.2)
        # The long press's tone ended the click's; the watcher's exit ended the long press's.
        players = self.pids("player.pid")
        self.assertEqual(len(players), 2)
        self.assertFalse([pid for pid in players if self.alive(pid)])

    def test_exits_non_zero_when_the_monitor_ends(self):
        start = time.monotonic()
        rc, console, _ = self.run_watcher(settle=0)
        self.assertLess(time.monotonic() - start, 2)
        self.assertNotEqual(rc, 0)
        self.assertEqual(console, b"")
        self.assertTrue([line for line in self.syslog_lines() if "monitor ended" in line])
        self.assertEqual(list((self.sb / "var/run/karotz").iterdir()), [])

    def test_sigterm_ends_the_monitor_and_the_tone(self):
        self.sounds_on()
        (self.sb / "hold").touch()
        (self.sb / "player-sleeps").touch()
        self.signals(signal("click"))
        p = self.start()
        deadline = time.monotonic() + 10
        while not (self.sb / "player.pid").exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        time.sleep(0.3)
        p.terminate()
        p.communicate(timeout=5)
        self.assertEqual(p.returncode, 0)
        time.sleep(0.5)
        for name in ("monitor.pid", "player.pid"):
            self.assertFalse([pid for pid in self.pids(name) if self.alive(pid)], name)
        self.assertEqual(list((self.sb / "var/run/karotz").iterdir()), [])

    def test_shell_syntax(self):
        r = subprocess.run(["sh", "-n", str(WATCHER)], capture_output=True, text=True, timeout=30)
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        if shutil.which("shellcheck"):
            r = subprocess.run(["shellcheck", "-s", "sh", str(WATCHER)], capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stdout)


S40 = ROOT / "image/core/usr/etc/init.d/S40lapinlibre"
LISTENER = ROOT / "image/core/usr/scripts/tag_listener"


class S40Test(unittest.TestCase):
    """image/core's S40lapinlibre with its paths in a sandbox: a stub immortaldog that records what it
    starts and writes the pid file of a live stand-in process, and stubs for the bus and the rest."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        sb = self.sb = Path(self.tmp.name)
        for d in ("bin", "usr/karotz/bin", "usr/etc/conf", "usr/scripts", "usr/sbin", "var/run/karotz"):
            (sb / d).mkdir(parents=True)
        self.log = sb / "actions.log"
        stub(sb / "usr/karotz/bin/immortaldog",
             f'echo "immortaldog ${{2##*/}}" >> {self.log}\nsleep 30 > /dev/null 2>&1 &\necho $! > "$1"')
        stub(sb / "bin/dbus-send", 'echo "   boolean true"')
        stub(sb / "bin/uuidgen", "echo 1234")
        stub(sb / "bin/start-stop-daemon", f'echo "start-stop-daemon $*" >> {self.log}')
        stub(sb / "bin/logger", f'echo "logger $*" >> {self.log}')
        for name in ("dbus_watcher", "tag_listener"):
            stub(sb / "usr/scripts" / name, "exit 0")
        script = S40.read_text(encoding="utf-8")
        for path in ("/usr/karotz/uuid.conf", "/usr/etc/conf/playsounds", "/usr/karotz/bin", "/var/run/karotz",
                     "/usr/sbin/mdnsd", "/var/run/mdnsd.pid", "/usr/scripts/"):
            script = script.replace(path, f"{sb}{path}")
        self.script = sb / "S40lapinlibre"
        self.script.write_text(script, newline="\n")

    def tearDown(self):
        for pid_file in (self.sb / "var/run/karotz").glob("*.pid"):
            subprocess.run(["kill", pid_file.read_text().strip()], capture_output=True)
        self.tmp.cleanup()

    def run_s40(self, action):
        r = subprocess.run(["sh", str(self.script), action], env={"PATH": f"{self.sb}/bin:/usr/bin:/bin"},
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)

    def started(self):
        return [line.split()[1] for line in self.log.read_text().splitlines() if line.startswith("immortaldog")]

    def test_listener_started_once_after_the_daemons(self):
        self.run_s40("start")
        started = self.started()
        self.assertEqual(started.count("tag_listener"), 1)
        self.assertLess(started.index("rfid-daemon"), started.index("tag_listener"))
        self.assertLess(started.index("dbus_watcher"), started.index("tag_listener"))
        self.assertLess(started.index("tag_listener"), started.index("controller"))
        self.assertTrue((self.sb / "var/run/karotz/tag_listener.pid").is_file())
        # Started again: not a second listener or watcher while the first live.
        self.log.unlink()
        self.run_s40("start")
        self.assertNotIn("tag_listener", self.started())
        self.assertNotIn("dbus_watcher", self.started())
        # A pid file of a process that has ended: started again.
        (self.sb / "var/run/karotz/tag_listener.pid").write_text("999999\n")
        self.log.unlink()
        self.run_s40("start")
        self.assertEqual(self.started().count("tag_listener"), 1)

    def test_stop_ends_the_listener(self):
        self.run_s40("start")
        pid = (self.sb / "var/run/karotz/tag_listener.pid").read_text().strip()
        self.run_s40("stop")
        time.sleep(0.3)
        r = subprocess.run(["sh", "-c", f'kill -0 {pid} 2> /dev/null'], capture_output=True, timeout=10)
        self.assertNotEqual(r.returncode, 0)

    def test_shell_syntax(self):
        for path in (S40, LISTENER):
            r = subprocess.run(["sh", "-n", str(path)], capture_output=True, text=True, timeout=30)
            self.assertEqual((r.returncode, r.stderr), (0, ""), path)


if __name__ == "__main__":
    unittest.main()
