# SPDX-License-Identifier: MIT
"""Run the USB key's real autorun against a simulated Karotz.

The key is built (image/tools/image.py, with test network settings) into a
temporary directory. Its scripts are copied into a sandbox with every
absolute path (/mnt/usbkey, /usr, /dev/mtd1, /proc/mtd, ...) rewritten to
point inside it. Flashing and rebooting are stubbed to record what they
would have done, so each test can check what ends up in the "partitions"
and on /usr.

Needs the system, v8, kernel and userland stages' results, and bash, gzip,
tar, unzip, md5sum and cmp on PATH.
"""

import contextlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "image" / "tools"))
sys.path.insert(0, str(ROOT / "scripts"))
import image  # noqa: E402
from lapin import load_config, out_dir, work_dir  # noqa: E402

TOP_DIRS = "mnt|karotz|bin|sbin|tmp|usr|etc|dev|proc|sys"
STOCK_INITTAB = "::once:/karotz/scripts/karotz_init.sh\n"
BUILD_ID_FILE = "usr/etc/conf/lapinlibre_build_id"
MiB = 1 << 20
# Test values only: the key's network script is never run here.
# What a user types into the key's lapinlibre.txt; Rabbit() writes it by default.
TEST_SETTINGS = "WIFI_SSID=testssid\nWIFI_PASSWORD=testpassword\n"
TEST_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestKeyNotARealOne0000000000000000000 test"


def stub(path: Path, body: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/bash\n" + body + "\n", encoding="utf-8", newline="\n")
    path.chmod(0o755)


class Rabbit:
    """A sandboxed Karotz with the USB key at <sb>/mnt/usbkey."""

    def __init__(self, key: Path, sb: Path, rootfs_version="12.07.19.00", build_id=None,
                 mtd_sizes=(3 * MiB, 5 * MiB), ramdisk=5 * MiB, settings=TEST_SETTINGS, wpa="kept\n",
                 mem_free_kb=45 * 1024):
        self.sb = sb
        prefix = str(sb)

        usbkey = sb / "mnt" / "usbkey"
        shutil.copytree(key, usbkey)
        if settings is not None:  # bytes as they are (a Windows editor's), or text
            (usbkey / "lapinlibre.txt").write_bytes(settings if isinstance(settings, bytes) else settings.encode())
        for script in [usbkey / "autorun", *usbkey.rglob("*.sh")]:
            text = script.read_text(encoding="utf-8")
            head, _, rest = text.partition("\n")
            rest = re.sub(rf"(?<![\w.$/])/({TOP_DIRS})(?=[/\s\"');$]|$)", lambda m: prefix + m.group(0), rest, flags=re.M)
            # The sound loop runs an ARM binary forever; nothing to test there.
            rest = rest.replace("sound_loop & LOOP_PID=$!", "LOOP_PID=  # sound loop disabled")
            # The install's madplay is ARM: a stub records which sound would play.
            rest = rest.replace(f"{prefix}/tmp/madplay -Q", f"{prefix}/bin/madplay-log")
            script.write_text(head + "\n" + rest, encoding="utf-8", newline="\n")
            # A FAT key shows every file as executable.
            script.chmod(0o755)

        log = sb / "actions.log"
        stub(sb / "karotz/bin/led", "exit 0")
        stub(sb / "bin/killall", "exit 0")
        for tool in ("md5sum", "chmod", "mkdir", "unzip", "gzip"):
            stub(sb / "bin" / tool, f'exec {tool} "$@"')
        stub(sb / "sbin/flash_eraseall", f'echo "erase $1" >> {log}; : > "$1"')
        stub(sb / "sbin/nandwrite", f'echo "write $2 $3" >> {log}; cp "$3" "$2"')
        stub(sb / "sbin/reboot", f'echo "reboot $*" >> {log}')
        stub(sb / "bin/madplay-log", f'echo "play $(basename "$1")" >> {log}')

        (sb / "dev").mkdir()
        for mtd in ("mtd1", "mtd2"):
            (sb / "dev" / mtd).write_bytes(b"old " + mtd.encode())
        (sb / "proc").mkdir()
        (sb / "proc/mtd").write_text(
            'dev:    size   erasesize  name\n'
            f'mtd1: {mtd_sizes[0]:08x} 00020000 "kernel"\n'
            f'mtd2: {mtd_sizes[1]:08x} 00020000 "rootfs"\n',
            encoding="utf-8",
            newline="\n",
        )
        (sb / "proc/sys/vm").mkdir(parents=True)
        (sb / "proc/sys/vm/drop_caches").write_text("")
        (sb / "proc/meminfo").write_text(f"MemTotal:        59884 kB\nMemFree:  {mem_free_kb:>13} kB\n", newline="\n")
        if ramdisk:
            (sb / "sys/block/ram0").mkdir(parents=True)
            (sb / "sys/block/ram0/size").write_text(f"{ramdisk // 512}\n", newline="\n")
        (sb / "tmp").mkdir()
        (sb / "karotz/etc").mkdir(parents=True)
        (sb / "karotz/etc/rootfs_version").write_text(rootfs_version + "\n", newline="\n")
        (sb / "karotz/etc/inittab").write_text(STOCK_INITTAB, newline="\n")

        # /usr as an installed rabbit has it: config to keep, the rest to wipe.
        usr = sb / "usr"
        (usr / "etc/conf").mkdir(parents=True)
        if wpa is not None:
            (usr / "etc/conf/wpa.conf").write_text(wpa, newline="\n")
        (usr / "etc/inittab").write_text(STOCK_INITTAB, newline="\n")
        (usr / "etc/passwd").write_text("old passwd\n", newline="\n")
        (usr / "karotz/apps").mkdir(parents=True)
        (usr / "karotz/apps/myapp").write_text("kept\n", newline="\n")
        (usr / "karotz/stale.conf").write_text("wiped\n", newline="\n")
        (usr / "karotz/uuid.conf").write_text("0e1f2a3b-4c5d-6e7f-8091-a2b3c4d5e6f7\n", newline="\n")
        (usr / "scripts").mkdir()
        (usr / "scripts/old.sh").write_text("wiped\n", newline="\n")
        if build_id:
            (sb / BUILD_ID_FILE).write_text(build_id + "\n", newline="\n")

    def boot(self):
        """Run autorun the way linuxrc does."""
        return subprocess.run(["bash", str(self.sb / "mnt/usbkey/autorun")], cwd=self.sb,
                              capture_output=True, text=True, timeout=600)

    def actions(self):
        f = self.sb / "actions.log"
        return f.read_text().splitlines() if f.exists() else []

    def setuplog(self):
        return (self.sb / "mnt/usbkey/install-log.txt").read_text(encoding="utf-8", errors="replace")


def missing_inputs():
    """Why the key can't be built here, or None."""
    if not (out_dir() / "system-kernel" / "zImage").is_file():
        return "no kernel (./build.sh kernel)"
    if not (work_dir() / "system" / "out" / "target" / "lib").is_dir():
        return "no system build (./build.sh system)"
    if not (work_dir() / "userland" / "system" / "usr" / "controller").is_file():
        return "no userland (./build.sh userland)"
    return None


class UsbKeyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        why = missing_inputs()
        if why:
            raise unittest.SkipTest(why)
        cls.tmp = Path(tempfile.mkdtemp(prefix="lapinlibre-usbkey-test-"))
        cls.key = cls.tmp / "usbkey"
        with contextlib.redirect_stdout(io.StringIO()):
            image.build(cls.key)
        cls.build_id = (cls.key / "build_id").read_text().strip()

    @classmethod
    def tearDownClass(cls):
        if os.environ.get("LAPIN_TEST_KEEP"):
            print(f"sandboxes kept in {cls.tmp}")
        else:
            shutil.rmtree(cls.tmp, ignore_errors=True)

    def rabbit(self, name, **kw):
        return Rabbit(self.key, self.tmp / name, **kw)

    def test_key_has_system_kernel_only(self):
        # The key's kernel is the system build's Linux 2.6.32, and the old override that chose a
        # kernel is refused.
        self.assertEqual((self.key / "zImage").read_bytes(), (out_dir() / "system-kernel" / "zImage").read_bytes())
        os.environ["LAPIN_KERNEL"] = "system"
        try:
            with self.assertRaises(image.ImageError):
                image.build(self.tmp / "refused")
        finally:
            del os.environ["LAPIN_KERNEL"]

    def assert_flashed(self, r, ssid="testssid"):
        sb = r.sb
        self.assertEqual((sb / "dev/mtd1").read_bytes(), (self.key / "zImage").read_bytes())
        self.assertEqual((sb / "dev/mtd2").read_bytes(), (self.key / "rootfs.img.gz").read_bytes())
        self.assertEqual((sb / BUILD_ID_FILE).read_text().strip(), self.build_id)
        self.assertTrue((sb / "usr/etc/init.d/S40lapinlibre").is_file(), "usr.tar.gz has no S40lapinlibre")
        if ssid is not None:
            self.assertIn(f'ssid="{ssid}"', (sb / "usr/etc/conf/wpa.conf").read_text(), "lapinlibre.txt not applied")
        self.assertTrue((sb / "usr/karotz/res/lang/en-GB").is_file(), "usr.tar.gz not unpacked")
        self.assertTrue((sb / "usr/karotz/bin/controller").is_file(), "LapinLibre's programs not installed")
        self.assertFalse((sb / "usr/scripts/old.sh").exists(), "the install should wipe /usr")
        self.assertFalse((sb / "usr/install").exists())
        self.assertEqual((sb / "usr/karotz/apps/myapp").read_text(), "kept\n")
        self.assertFalse((sb / "usr/karotz/stale.conf").exists(), "the rest of /usr/karotz should be wiped")
        # The rabbit's UUID outlives an install; the core's web page and stick hook are in.
        self.assertEqual((sb / "usr/karotz/uuid.conf").read_text(), "0e1f2a3b-4c5d-6e7f-8091-a2b3c4d5e6f7\n")
        self.assertTrue((sb / "usr/www/index.html").is_file())
        self.assertTrue(os.access(sb / "usr/etc/usbkey.d/50mpd", os.X_OK))
        self.assertIn("reboot -f", r.actions())

    def assert_untouched(self, r):
        sb = r.sb
        self.assertEqual((sb / "dev/mtd1").read_bytes(), b"old mtd1")
        self.assertEqual((sb / "dev/mtd2").read_bytes(), b"old mtd2")
        self.assertEqual((sb / "usr/scripts/old.sh").read_text(), "wiped\n")
        self.assertEqual((sb / "usr/etc/inittab").read_text(), STOCK_INITTAB)
        self.assertNotIn("reboot -f", r.actions())

    def test_factory_rabbit_flashes_in_one_pass(self):
        r = self.rabbit("factory", rootfs_version="11.03.10.00")
        r.boot()
        self.assert_flashed(r)
        # No reboot-only inittab: linuxrc recreates it from the new rootfs.
        self.assertFalse((r.sb / "usr/etc/inittab").exists())

    def test_base_rabbit_flashes_new_build(self):
        r = self.rabbit("base", build_id=None)
        r.boot()
        self.assert_flashed(r)

    def test_older_build_is_replaced(self):
        r = self.rabbit("older", build_id="0123456789abcdef")
        r.boot()
        self.assert_flashed(r)

    def test_lapinlibre_rabbit_erases_with_flash_erase(self):
        # A rabbit running LapinLibre has current mtd-utils: the installer erases with flash_erase, whole
        # device (offset 0, count 0), and never falls back to flash_eraseall.
        r = self.rabbit("lapinlibre", build_id="0123456789abcdef")
        log = r.sb / "actions.log"
        stub(r.sb / "sbin/flash_erase", f'echo "flash_erase $*" >> {log}; : > "$1"')
        stub(r.sb / "sbin/flash_eraseall", f'echo "flash_eraseall used" >> {log}; exit 1')
        r.boot()
        self.assert_flashed(r)
        self.assertIn("flash_erase /dev/mtd1 0 0", [a.replace(str(r.sb), "") for a in r.actions()])
        self.assertIn("flash_erase /dev/mtd2 0 0", [a.replace(str(r.sb), "") for a in r.actions()])
        self.assertNotIn("flash_eraseall used", r.actions())

    def test_same_build_only_refreshes_network(self):
        r = self.rabbit("same", build_id=self.build_id)
        r.boot()
        self.assertEqual((r.sb / "dev/mtd1").read_bytes(), b"old mtd1")
        self.assertEqual((r.sb / "dev/mtd2").read_bytes(), b"old mtd2")
        self.assertNotIn("reboot -f", r.actions())
        self.assertIn('ssid="testssid"', (r.sb / "usr/etc/conf/wpa.conf").read_text(), "lapinlibre.txt not applied")
        self.assertEqual((r.sb / "usr/karotz/apps/myapp").read_text(), "kept\n")

    def test_settings_from_a_windows_editor(self):
        # Lapinlibre.txt as Notepad saves it: a byte-order mark, CRLF line ends,
        # quoted values; a fixed address, an SSH key and telnet. All of it lands in /usr/etc/conf.
        settings = ("\ufeff# my rabbit\r\nWIFI_SSID = \"my net\"\r\nWIFI_PASSWORD='pa ss word'\r\nIP=192.168.1.20\r\n"
                    "NETMASK=255.255.255.0\r\nGATEWAY=192.168.1.1\r\nDNS=192.168.1.1\r\n"
                    f"SSH_PUBKEY={TEST_KEY}\r\nTELNET=yes\r\n").encode("utf-8")
        r = self.rabbit("windows", rootfs_version="11.03.10.00", settings=settings)
        r.boot()
        self.assert_flashed(r, ssid="my net")
        conf = r.sb / "usr/etc/conf"
        wpa = (conf / "wpa.conf").read_text()
        self.assertIn('ssid="my net"\npsk="pa ss word"', wpa)
        self.assertEqual(oct((conf / "wpa.conf").stat().st_mode & 0o777), "0o600")
        self.assertEqual((conf / "network.conf").read_text(),
                         'IP="192.168.1.20"\nNETMASK="255.255.255.0"\nGATEWAY="192.168.1.1"\nDNS="192.168.1.1"\n')
        self.assertEqual((conf / "authorized_keys").read_text(), TEST_KEY + "\n")
        self.assertEqual((conf / "telnet").read_text(), "yes\n")
        self.assertNotIn("pa ss word", r.setuplog(), "the Wi-Fi password must not be logged")

    def test_blank_settings_keep_the_rabbits(self):
        # An update with an empty lapinlibre.txt keeps the rabbit's Wi-Fi, address,
        # SSH key and telnet choice (all in /usr/etc/conf, which installs keep).
        r = self.rabbit("blank", build_id="0123456789abcdef", settings="WIFI_SSID=\nSSH_PUBKEY=\nTELNET=\n")
        conf = r.sb / "usr/etc/conf"
        (conf / "network.conf").write_text('IP="10.1.1.5"\n', newline="\n")
        (conf / "authorized_keys").write_text(TEST_KEY + "\n", newline="\n")
        (conf / "telnet").write_text("no\n", newline="\n")
        r.boot()
        self.assert_flashed(r, ssid=None)
        self.assertEqual((conf / "wpa.conf").read_text(), "kept\n")
        self.assertEqual((conf / "network.conf").read_text(), 'IP="10.1.1.5"\n')
        self.assertEqual((conf / "authorized_keys").read_text(), TEST_KEY + "\n")
        self.assertEqual((conf / "telnet").read_text(), "no\n")

    def test_app_server_and_language(self):
        # SERVER points the controller (karotz.conf) and voice-daemon (voice.conf) at
        # the app server, ASR_SERVER moves speech recognition elsewhere, LANGUAGE sets the language.
        # Keys the settings don't name stay as the rabbit had them.
        r = self.rabbit("server", rootfs_version="11.03.10.00",
                        settings=TEST_SETTINGS + "SERVER=192.168.1.10\nASR_SERVER=10.0.0.5:9000\nLANGUAGE=fr-FR\nHOSTNAME=kitchen\n")
        conf = r.sb / "usr/etc/conf"
        (conf / "karotz.conf").write_text("[karotz]\nip_server=old.example\nasr_threshold=40\n[k2k]\nsipDomain=sip.example\n",
                                          newline="\n")
        r.boot()
        self.assert_flashed(r)
        karotz = (conf / "karotz.conf").read_text()
        for line in ("ip_server=192.168.1.10", "port_server=9123", "url_app=http://192.168.1.10:8080",
                     "domain_app=192.168.1.10", "url_update=http://127.0.0.1:1", "lang=fr-FR", "asr_threshold=40",
                     "sipDomain=sip.example"):
            self.assertIn(line + "\n", karotz)
        self.assertNotIn("old.example", karotz)
        voice = (conf / "voice.conf").read_text()
        for line in ("en=http://192.168.1.10:8081/en", "fr=http://192.168.1.10:8081/fr", "nbserver=1", "server0=10.0.0.5",
                     "port=9000"):
            self.assertIn(line + "\n", voice)
        self.assertEqual((conf / "account.conf").read_text(), "[account]\nid=lapinlibre\npwd=lapinlibre\n")
        self.assertEqual((conf / "hostname").read_text(), "kitchen\n")

    def test_no_server_keeps_the_rabbits(self):
        # The install keeps the rabbit's own karotz.conf and voice.conf when
        # lapinlibre.txt names no server; a rabbit with none gets the image's, which name no one's
        # servers.
        r = self.rabbit("keep-server", rootfs_version="11.03.10.00")
        conf = r.sb / "usr/etc/conf"
        (conf / "karotz.conf").write_text("[karotz]\nip_server=192.168.7.7\n", newline="\n")
        r.boot()
        self.assert_flashed(r)
        self.assertEqual((conf / "karotz.conf").read_text(), "[karotz]\nip_server=192.168.7.7\n")
        voice = (conf / "voice.conf").read_text()
        self.assertIn("server0=127.0.0.1\n", voice)
        self.assertNotIn("188.165", voice)
        # The controller's account is written without a server too (an older install's voos.conf
        # isn't read).
        self.assertEqual((conf / "account.conf").read_text(), "[account]\nid=lapinlibre\npwd=lapinlibre\n")

    def test_bad_settings_flash_nothing(self):
        # A bad value stops the install before anything is erased, and says why.
        for name, settings, why in (
                ("bad-key", TEST_SETTINGS + "SSH_PUBKEY=not a key\n", "SSH_PUBKEY"),
                ("bad-ip", TEST_SETTINGS + "IP=192.168.1\n", "IP must be an address"),
                ("short-password", "WIFI_SSID=net\nWIFI_PASSWORD=short\n", "8 to 63"),
                ("unknown", TEST_SETTINGS + "WIFI_PASWORD=typo\n", "unknown setting WIFI_PASWORD"),
                ("bad-server", TEST_SETTINGS + "SERVER=http://pc\n", "SERVER must be a host name"),
                ("bad-language", TEST_SETTINGS + "LANGUAGE=french\n", "LANGUAGE must look like"),
                ("bad-name", TEST_SETTINGS + "HOSTNAME=My_Rabbit\n", "HOSTNAME must be"),
                ("short-web-password", TEST_SETTINGS + "WEB_PASSWORD=short\n", "WEB_PASSWORD must be"),
                ("long-web-password", TEST_SETTINGS + "WEB_PASSWORD=" + "x" * 65 + "\n", "WEB_PASSWORD must be"),
                ("web-password-colon", TEST_SETTINGS + "WEB_PASSWORD=pass:word1\n", "WEB_PASSWORD must be"),
                ("web-password-quote", TEST_SETTINGS + 'WEB_PASSWORD=pass"word1\n', "WEB_PASSWORD must be"),
                ("web-password-backslash", TEST_SETTINGS + "WEB_PASSWORD=pass\\word1\n", "WEB_PASSWORD must be"),
                ("web-password-space", TEST_SETTINGS + "WEB_PASSWORD=pass word1\n", "WEB_PASSWORD must be"),
                ("web-password-utf8", TEST_SETTINGS + "WEB_PASSWORD=passwörd1\n", "WEB_PASSWORD must be")):
            with self.subTest(name):
                r = self.rabbit(name, build_id="0123456789abcdef", settings=settings)
                result = r.boot()
                self.assertNotEqual(result.returncode, 0)
                self.assert_untouched(r)
                self.assertIn(why, r.setuplog())

    def test_web_password(self):
        # WEB_PASSWORD lands in /usr/etc/conf/web_password (0600), which the rabbit
        # hashes at its next start; never logged. Empty (or no line) means no password: any earlier
        # one goes.
        password = "S3cret!pa$$#~"
        r = self.rabbit("web-password", build_id=self.build_id, settings=TEST_SETTINGS + f"WEB_PASSWORD={password}\n")
        r.boot()
        conf = r.sb / "usr/etc/conf"
        self.assertEqual((conf / "web_password").read_text(), password + "\n")
        self.assertEqual(oct((conf / "web_password").stat().st_mode & 0o777), "0o600")
        self.assertIn("web password set", r.setuplog())
        self.assertNotIn(password, r.setuplog())
        for name, line in (("web-password-empty", "WEB_PASSWORD=\n"), ("web-password-none", "")):
            with self.subTest(name):
                r = self.rabbit(name, build_id=self.build_id, settings=TEST_SETTINGS + line)
                (r.sb / "usr/etc/conf/web_password.hash").write_text("$6$salt$" + "x" * 86 + "\n", newline="\n")
                r.boot()
                self.assertFalse((r.sb / "usr/etc/conf/web_password").exists())
                self.assertFalse((r.sb / "usr/etc/conf/web_password.hash").exists())
                self.assertIn("no web password", r.setuplog())

    def test_no_wifi_anywhere_flashes_nothing(self):
        # A first install with no Wi-Fi in lapinlibre.txt and none on the rabbit.
        r = self.rabbit("no-wifi", rootfs_version="11.03.10.00", settings="WIFI_SSID=\n", wpa=None)
        r.boot()
        self.assert_untouched(r)
        self.assertIn("no Wi-Fi settings", r.setuplog())

    def test_key_has_no_personal_data(self):
        # The key is anyone's. Its lapinlibre.txt is the blank template, the network
        # script is the image's own (no settings in it), the SSH key is a link to /usr/etc/conf, and
        # the image's inetd.conf has no telnet.
        import fstree
        import gzip
        self.assertEqual((self.key / "lapinlibre.txt").read_bytes(), (ROOT / "image" / "usbkey" / "lapinlibre.txt").read_bytes())
        self.assertFalse((self.key / "network.sh").exists())
        usr = {e.path: e for e in fstree.TarPart(gzip.decompress((self.key / "usr.tar.gz").read_bytes()), 0).entries()}
        rootfs = {e.path: e for e in fstree.CramfsPart(gzip.decompress((self.key / "rootfs.img.gz").read_bytes())).entries()}
        self.assertEqual((usr["karotz/.ssh/authorized_keys"].kind, usr["karotz/.ssh/authorized_keys"].target),
                         ("l", "/usr/etc/conf/authorized_keys"))
        self.assertNotIn("etc/conf/wpa.conf", usr)
        self.assertNotIn(b"ssid=", rootfs["karotz/scripts/network.sh"].data)

    def test_kernel_too_big_for_partition_flashes_nothing(self):
        r = self.rabbit("small-mtd1", build_id="0123456789abcdef", mtd_sizes=(1 * MiB, 5 * MiB))
        result = r.boot()
        self.assertNotEqual(result.returncode, 0)
        self.assert_untouched(r)
        self.assertIn("zImage is", r.setuplog())

    def test_rootfs_too_big_for_ramdisk_flashes_nothing(self):
        r = self.rabbit("small-ram", rootfs_version="11.03.10.00", ramdisk=2 * MiB)
        r.boot()
        self.assert_untouched(r)
        self.assertIn("RAM disk holds", r.setuplog())

    def rootfs_file(self, path):
        import fstree
        import gzip
        part = fstree.CramfsPart(gzip.decompress((self.key / "rootfs.img.gz").read_bytes()))
        return next(e.data for e in part.entries() if e.path == path and e.kind == "f")

    def test_no_telnet_or_ftp(self):
        # Both ran as root for anyone on the network. The core's inetd.conf serves
        # only the web page.
        core = self.rootfs_file("karotz/etc/inetd.conf").decode()
        self.assertNotIn("telnetd", core)
        self.assertNotIn("ftpd", core)
        self.assertIn("80 stream tcp nowait.60 root /bin/busybox httpd -i -c /var/run/httpd.conf -h /usr/www", core)
        self.assertNotIn("/karotz/etc/httpd.conf", core)
        # The fixed part alone refuses everyone; inetd.sh makes the configuration with the password.
        self.assertIn("D:*", self.rootfs_file("karotz/etc/httpd.conf").decode())
        self.assertNotIn("A:*", self.rootfs_file("karotz/etc/httpd.conf").decode())

    def test_key_carries_the_committed_keyring(self):
        # The root filesystem's Karotz keyring is image/core's, byte for byte, at the MD5s
        # config.toml [key.md5] pins; nothing else is in /karotz/etc/gpg.
        import hashlib
        pins = {name.partition(":")[2]: want for name, want in load_config()["key"]["md5"].items()
                if name.startswith("rootfs:karotz/etc/gpg/")}
        self.assertEqual(sorted(pins), ["karotz/etc/gpg/pubring.gpg", "karotz/etc/gpg/trustdb.gpg"])
        for path, want in pins.items():
            data = self.rootfs_file(path)
            self.assertEqual(data, (ROOT / "image/core/rootfs" / path).read_bytes(), path)
            self.assertEqual(hashlib.md5(data).hexdigest(), want, path)
        import fstree
        import gzip
        root = fstree.CramfsPart(gzip.decompress((self.key / "rootfs.img.gz").read_bytes()))
        self.assertEqual(sorted(e.path for e in root.entries() if e.path.startswith("karotz/etc/gpg/")), sorted(pins))

    def test_a_changed_keyring_is_refused(self):
        # A keyring file that isn't the pinned one stops the build.
        config = load_config()
        config["key"]["md5"]["rootfs:karotz/etc/gpg/pubring.gpg"] = "0" * 32
        rootfs = image.fstree.CramfsPart.empty()
        for name in ("pubring.gpg", "trustdb.gpg"):
            rootfs.put_file(f"karotz/etc/gpg/{name}", (ROOT / "image/core/rootfs/karotz/etc/gpg" / name).read_bytes())
        with self.assertRaises(image.ImageError):
            image.check_pinned(config, {"rootfs": rootfs, "usr": image.fstree.TarPart.empty(0)})
        image.check_pinned(load_config(), {"rootfs": rootfs, "usr": image.fstree.TarPart.empty(0)})

    def test_corrupt_image_flashes_nothing(self):
        r = self.rabbit("corrupt", build_id="0123456789abcdef")
        with open(r.sb / "mnt/usbkey/usr.tar.gz", "r+b") as f:
            f.seek(1000)
            f.write(b"corrupt")
        r.boot()
        self.assert_untouched(r)
        self.assertIn("MD5 checksum error", r.setuplog())

    def test_every_program_finds_its_libraries(self):
        # What the root's programs need must be in the root: the boot runs them
        # before /usr is mounted, and a USB-key install wipes /usr while they run. /usr's
        # programs find a library in their RPATH/RUNPATH, the directories of ld.so.conf (the
        # root's /karotz/etc/ld.so.conf, linked to /etc by linuxrc, which runs ldconfig when
        # /etc/ld.so.cache is missing, as after an install), then /lib and /usr/lib.
        import fstree
        import gzip
        root = list(fstree.CramfsPart(gzip.decompress((self.key / "rootfs.img.gz").read_bytes())).entries())
        usr = [fstree.Entry("usr/" + e.path, e.kind, e.mode, e.uid, e.gid, e.target, e.rdev, e.data)
               for e in fstree.TarPart(gzip.decompress((self.key / "usr.tar.gz").read_bytes()), 0).entries()]
        names = {}  # directory -> the names (files and links) in it
        for e in root + usr:
            if e.kind in "fl":
                names.setdefault(os.path.dirname(e.path), set()).add(os.path.basename(e.path))
        conf = next(e.data for e in root if e.path == "karotz/etc/ld.so.conf" and e.kind == "f")
        conf_dirs = [d.strip().lstrip("/") for d in conf.decode().splitlines() if d.strip() and not d.startswith("#")]
        missing = []
        for e in root + usr:
            if e.kind != "f" or not e.data.startswith(b"\x7fELF"):
                continue
            needed, paths = elf_needed(e.data)
            dirs = (["lib"] if not e.path.startswith("usr/")
                    else [p.lstrip("/") for p in paths] + conf_dirs + ["lib", "usr/lib"])
            for lib in needed:
                if not any(lib in names.get(d, ()) for d in dirs):
                    missing.append(f"{e.path}: {lib} (looked in {', '.join('/' + d for d in dirs)})")
        self.maxDiff = None
        self.assertEqual(missing, [])

    def test_every_own_build_is_in_the_key(self):
        # Every program and library LapinLibre builds is in the key, the programs
        # executable, karotz-player among them.
        import fstree
        import gzip
        import system
        root = fstree.CramfsPart(gzip.decompress((self.key / "rootfs.img.gz").read_bytes()))
        usr = fstree.TarPart(gzip.decompress((self.key / "usr.tar.gz").read_bytes()), 0)
        files = {label: {e.path: e for e in part.entries()} for label, part in (("rootfs", root), ("usr", usr))}
        own = system.own_builds(work_dir() / "userland" / "system", work_dir() / "v8" / "libv8.so")
        bad = [f"{label}:{path}" + ("" if path in files[label] else " missing")
               for label, path in sorted(own)
               if path not in files[label]
               or (".so" not in path and not files[label][path].mode & 0o111)]
        self.assertIn(("usr", "bin/karotz-player"), own)
        self.assertEqual(bad, [])
        # They are stripped: no debug information, and no build folder in them.
        unstripped = [f"{label}:{path}" for label, path in sorted(own)
                      if path in files[label] and (b".debug_info" in files[label][path].data
                                                   or b"/home/" in files[label][path].data)]
        self.assertEqual(unstripped, [])

    def test_installed_files_match_the_record(self):
        # The key's layout (every entry's path, kind, mode, owner and link target, not
        # the data) is tests/expected/key-files.txt's.
        # After a deliberate change: tests/image/keydiff.py --list KEY > tests/expected/key-files.txt
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import keydiff
        record = (ROOT / "tests" / "expected" / "key-files.txt").read_text(encoding="utf-8").splitlines()
        now = keydiff.layout(self.key)
        gone, new = sorted(set(record) - set(now)), sorted(set(now) - set(record))
        self.assertEqual((gone[:20], new[:20]), ([], []), f"{len(gone)} lines gone, {len(new)} new")

    def test_key_has_the_install_tools(self):
        # Install-tools.tar, holding madplay and ears only, both statically linked
        # (no loader, so they run on any system the install meets), and the key's install-lib.sh checks this archive's md5 (install.conf's)
        # before unpacking it.
        import hashlib
        import tarfile
        data = (self.key / "install-tools.tar").read_bytes()
        with tarfile.open(fileobj=io.BytesIO(data)) as t:
            members = {m.name: t.extractfile(m).read() for m in t.getmembers() if m.isfile()}
        self.assertEqual(sorted(members), ["ears", "madplay"])
        for name, elf in members.items():
            self.assertEqual(elf[:4], b"\x7fELF", name)
            self.assertNotIn(b"ld-uClibc", elf, f"{name} asks for a loader")
        self.assertIn(f"INSTALL_TOOLS_MD5={hashlib.md5(data).hexdigest()}\n", (self.key / "install.conf").read_text())
        lib = (self.key / "install-lib.sh").read_text()
        self.assertIn("md5sum /mnt/usbkey/install-tools.tar", lib)
        self.assertIn('"${sum}" = "${INSTALL_TOOLS_MD5}"', lib)
        self.assertNotIn("LD_LIBRARY_PATH", lib)
        # A wrong archive stops the install before anything is touched.
        r = self.rabbit("bad-tools", build_id="0123456789abcdef")
        with open(r.sb / "mnt/usbkey/install-tools.tar", "r+b") as f:
            f.seek(100)
            f.write(b"corrupt")
        self.assertNotEqual(r.boot().returncode, 0)
        self.assert_untouched(r)
        self.assertIn("MD5 checksum error: install-tools.tar", r.setuplog())

    def test_key_layout(self):
        # The key carries our own install (autorun, install-lib.sh, applysettings.sh)
        # and a generated install.conf of plain assignments, and nothing else at its top.
        files = ["applysettings.sh", "autorun", "autorun.sig", "build_id", "install-lib.sh", "install-tools.tar",
                 "install.conf", "lapinlibre.txt", "rootfs.img.gz", "usr.tar.gz", "zImage"]
        for name in files:
            self.assertTrue((self.key / name).is_file(), name)
        self.assertEqual(sorted(p.name for p in self.key.iterdir()), sorted(files + ["sound"]))
        conf = (self.key / "install.conf").read_text()
        self.assertEqual(sorted(line.split("=")[0] for line in conf.splitlines()),
                         ["BUILD_ID", "BUILD_VERSION", "INSTALL_TOOLS_MD5", "KERNEL_MD5", "ROOTFS_MD5", "USR_MD5"])
        self.assertIn(f"BUILD_ID={self.build_id}\n", conf)
        self.assertNotIn("/", conf)

    def test_core_sounds_installed(self):
        # The core's own spoken sounds are installed as the repo has them.
        r = self.rabbit("sounds", build_id="0123456789abcdef")
        r.boot()
        self.assert_flashed(r)
        sounds = sorted((ROOT / "image/core/usr/karotz/sounds").glob("*.mp3"))
        self.assertEqual(len(sounds), 14)
        for f in sounds:
            self.assertEqual((r.sb / "usr/karotz/sounds" / f.name).read_bytes(), f.read_bytes(), f.name)

    def test_earlier_addon_record_removed(self):
        # A full install wipes an earlier key's Free Rabbits add-on, and its record.
        r = self.rabbit("old-addon", build_id="0123456789abcdef")
        (r.sb / "usr/etc/conf/freerabbits_build_id").write_text("7bfe2c5ade59609d\n", newline="\n")
        (r.sb / "usr/freerabbits/gpg").mkdir(parents=True)
        r.boot()
        self.assert_flashed(r)
        self.assertFalse((r.sb / "usr/etc/conf/freerabbits_build_id").exists())
        self.assertFalse((r.sb / "usr/freerabbits").exists())

    def test_stick_without_a_build_installs_nothing(self):
        # A stick with the install but no build flashes nothing and lets the boot carry on.
        stick = self.tmp / "no-build-stick"
        shutil.copytree(self.key, stick)
        for name in ("build_id", "zImage", "rootfs.img.gz", "usr.tar.gz"):
            (stick / name).unlink()
        r = Rabbit(stick, self.tmp / "no-build", build_id=self.build_id, settings=None)
        r.boot()
        self.assert_untouched(r)
        self.assertIn("carries no build", r.setuplog())

    def test_the_install_announces_its_checks(self):
        # The checks are announced before they start; passing ones before the install starts, a
        # failing one with the error tones then in words, once (not again by applysettings.sh).
        def sounds(r):
            return [l.split()[1] for l in r.actions() if l.startswith("play ")]
        ok = self.rabbit("announce-ok", build_id="0123456789abcdef")
        ok.boot()
        self.assertEqual(sounds(ok), ["key-found.mp3", "checks-start.mp3", "checks-passed.mp3", "install-start.mp3",
                                      "install-done.mp3"])
        for name, settings in (("announce-bad-md5", TEST_SETTINGS), ("announce-bad-settings", TEST_SETTINGS + "IP=1.2\n")):
            with self.subTest(name):
                bad = self.rabbit(name, build_id="0123456789abcdef", settings=settings)
                if name == "announce-bad-md5":
                    with open(bad.sb / "mnt/usbkey/usr.tar.gz", "r+b") as f:
                        f.seek(1000)
                        f.write(b"corrupt")
                bad.boot()
                self.assert_untouched(bad)
                self.assertEqual(sounds(bad), ["key-found.mp3", "checks-start.mp3", "error.mp3", "checks-failed.mp3"])
        # The same build with bad settings: the error tones once, nothing flashed.
        same = self.rabbit("announce-same-bad", build_id=self.build_id, settings=TEST_SETTINGS + "IP=1.2\n")
        same.boot()
        self.assertEqual((same.sb / "dev/mtd1").read_bytes(), b"old mtd1")
        self.assertEqual(sounds(same), ["key-found.mp3", "error.mp3"])

    def test_usr_archive_in_ram_when_it_fits(self):
        # With room in RAM the install checks and unpacks one copy of usr.tar.gz there; without, it
        # unpacks from the key. Both install the same files.
        for name, mem, where in (("usr-ram", 45 * 1024, "copied into RAM"), ("usr-key", 12 * 1024, "stays on the key")):
            with self.subTest(name):
                r = self.rabbit(name, build_id="0123456789abcdef", mem_free_kb=mem)
                r.boot()
                self.assert_flashed(r)
                self.assertIn(where, r.setuplog())
                self.assertFalse((r.sb / "tmp/usr.tar.gz").exists())

    def test_failed_unpack_from_the_key_is_tried_again(self):
        # A key that reads back wrong once: the unpack from the key is tried once more on a clean
        # /usr; from RAM the same bytes would fail again, so that stops.
        # Only the first unpack into /usr fails (the key's PATH is the host's, so the unpack is pointed at
        # this stub).
        flaky = ('case "$*" in *usr*) n=$(cat "$0.count" 2> /dev/null || echo 0); echo $((n + 1)) > "$0.count"; '
                 '[ "$n" = 0 ] && { cat > /dev/null; exit 2; } ;; esac; exec /bin/tar "$@"')
        def use_flaky_tar(r):
            stub(r.sb / "bin/tar", flaky)
            lib = r.sb / "mnt/usbkey/install-lib.sh"
            text = lib.read_text(encoding="utf-8")
            self.assertEqual(text.count("| tar -xf - -C"), 1)
            lib.write_text(text.replace("| tar -xf - -C", f"| {r.sb}/bin/tar -xf - -C"), encoding="utf-8", newline="\n")
        r = self.rabbit("unpack-retry", build_id="0123456789abcdef", mem_free_kb=12 * 1024)
        use_flaky_tar(r)
        r.boot()
        self.assert_flashed(r)
        self.assertIn("trying once more", r.setuplog())
        ram = self.rabbit("unpack-ram-fails", build_id="0123456789abcdef")
        use_flaky_tar(ram)
        result = ram.boot()
        self.assertNotEqual(result.returncode, 0)
        self.assertRegex(ram.setuplog(), r"ERROR: unpacking usr\.tar\.gz into \S*/usr failed\n")
        self.assertNotIn("trying once more", ram.setuplog())

    def test_full_flash_sets_the_system_version(self):
        # After a full flash the rabbit's sys_version is LapinLibre's build version.
        conf = dict(line.split("=", 1) for line in (self.key / "install.conf").read_text().splitlines())
        r = self.rabbit("sys-version", build_id="0123456789abcdef")
        r.boot()
        self.assert_flashed(r)
        self.assertEqual((r.sb / "usr/etc/conf/sys_version").read_text(), conf["BUILD_VERSION"] + "\n")

    def test_cleaning_usr_keeps_settings_apps_and_messages(self):
        # The install empties /usr but for /usr/etc/conf (all of it), /usr/karotz/apps,
        # /usr/karotz/messages and /usr/lost+found; names with spaces go too.
        r = self.rabbit("clean-usr", build_id="0123456789abcdef")
        usr = r.sb / "usr"
        kept = ["etc/conf/deep/dir/file", "etc/conf/with space", "karotz/messages/msg 1.txt", "karotz/apps/other app",
                "lost+found/x"]
        gone = ["etc/old.conf", "etc/dir with space/f", "karotz/old dir/f", "karotz/.hidden", "top file", ".hidden-top/f",
                "lib/libold.so", "yaffs-leftover", ".install_yaffs_start", "install/x"]
        for rel in kept + gone:
            (usr / rel).parent.mkdir(parents=True, exist_ok=True)
            (usr / rel).write_text(rel + "\n", newline="\n")
        r.boot()
        self.assert_flashed(r)
        for rel in kept:
            self.assertEqual((usr / rel).read_text(), rel + "\n", rel)
        for rel in gone:
            self.assertFalse((usr / rel).exists(), rel)
        for rel in ("etc/old.conf", "etc/dir with space", "karotz/old dir", "karotz/.hidden", ".hidden-top"):
            self.assertFalse((usr / rel).exists(), rel)

    def test_key_has_the_generated_timezone_list(self):
        # /usr/etc/timezone.list is the one generated from tzdata (tests/image/test_tzlist.py
        # checks its contents).
        import fstree
        import gzip
        usr = fstree.TarPart(gzip.decompress((self.key / "usr.tar.gz").read_bytes()), 0)
        entry = next(e for e in usr.entries() if e.path == "etc/timezone.list")
        lines = entry.data.decode("ascii").splitlines()
        self.assertGreater(len(lines), 410)
        self.assertIn("Europe/Kyiv;EET-2EEST,M3.5.0/3,M10.5.0/4", lines)
        self.assertIn("Europe/Istanbul;<+03>-3", lines)

    def test_every_program_can_be_executed(self):
        # The kernel won't start a program that isn't executable, nor one whose
        # interpreter (PT_INTERP, the loader) isn't. qemu-user doesn't check the interpreter's mode,
        # so only the rabbit showed it: a LapinLibre key whose loader was 0644 booted to a red LED.
        import fstree
        import gzip
        import struct
        root = list(fstree.CramfsPart(gzip.decompress((self.key / "rootfs.img.gz").read_bytes())).entries())
        usr = [fstree.Entry("usr/" + e.path, e.kind, e.mode, e.uid, e.gid, e.target, e.rdev, e.data)
               for e in fstree.TarPart(gzip.decompress((self.key / "usr.tar.gz").read_bytes()), 0).entries()]
        by_path = {e.path: e for e in root + usr}

        def resolve(path):
            for _ in range(10):
                e = by_path.get(path)
                if e is None or e.kind != "l":
                    return e
                t = e.target
                path = os.path.normpath(t.lstrip("/") if t.startswith("/") else os.path.join(os.path.dirname(path), t))
            return None

        bad = []
        for e in root + usr:
            d = e.data
            if e.kind != "f" or not d.startswith(b"\x7fELF") or d[4] != 1 or d[5] != 1:
                continue
            e_type, = struct.unpack_from("<H", d, 16)
            phoff, = struct.unpack_from("<I", d, 28)
            phentsize, phnum = struct.unpack_from("<HH", d, 42)
            interp = None
            for i in range(phnum):
                p_type, p_offset, _, _, p_filesz = struct.unpack_from("<IIIII", d, phoff + i * phentsize)
                if p_type == 3:  # PT_INTERP
                    interp = d[p_offset:p_offset + p_filesz].rstrip(b"\0").decode()
            if interp is None and e_type != 2:  # a shared library without an interpreter: not run
                continue
            if not e.mode & 0o111:
                bad.append(f"{e.path}: mode {e.mode & 0o7777:04o}, not executable")
            if interp:
                target = resolve(os.path.normpath(interp).lstrip("/"))
                if target is None or target.kind != "f":
                    bad.append(f"{e.path}: interpreter {interp} missing")
                elif not target.mode & 0o111:
                    bad.append(f"{e.path}: interpreter {interp} is mode {target.mode & 0o7777:04o}")
        self.maxDiff = None
        self.assertEqual(sorted(set(bad))[:20], [])


def elf_needed(data):
    """An ELF32 little-endian file's DT_NEEDED names and its RPATH/RUNPATH directories."""
    import struct
    if data[4] != 1 or data[5] != 1:
        return [], []
    phoff, = struct.unpack_from("<I", data, 28)
    phentsize, phnum = struct.unpack_from("<HH", data, 42)
    loads, dynamic = [], None
    for i in range(phnum):
        p_type, p_offset, p_vaddr, _, p_filesz = struct.unpack_from("<IIIII", data, phoff + i * phentsize)
        if p_type == 1:
            loads.append((p_vaddr, p_offset, p_filesz))
        elif p_type == 2:
            dynamic = (p_offset, p_filesz)
    if dynamic is None:
        return [], []
    tags = [struct.unpack_from("<iI", data, dynamic[0] + i) for i in range(0, dynamic[1], 8)]
    strtab = next((v for t, v in tags if t == 5), None)
    off = next(o + strtab - va for va, o, sz in loads if va <= strtab < va + sz)

    def string(i):
        return data[off + i:data.index(b"\0", off + i)].decode()
    needed = [string(v) for t, v in tags if t == 1]
    paths = [d for t, v in tags if t in (15, 29) for d in string(v).split(":") if d]
    return needed, paths


if __name__ == "__main__":
    unittest.main()
