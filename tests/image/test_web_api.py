# SPDX-License-Identifier: MIT
"""The web API's programs (image/core/usr/www/cgi-bin, sourcing image/core/usr/share/lapinlibre/
web.sh), run as httpd runs them: CGI environment variables, a sandbox for every
absolute path, and stubs first on PATH for the commands that would touch the rabbit (dbus-send,
reboot, ps, logger, ifconfig, iwgetid, dropbearkey, dmesg, df). The shell and its tools are the
image's own BusyBox under qemu when this machine runs it, else the host's.

Checked: each endpoint's reply (keys, string values, headers), the checks every request passes
(the web user, local-network addresses, the cross-site rule, methods, sizes), parameter decoding
and validation with hostile values, logging, and that reads write nothing under /usr. The last
class runs the image's BusyBox httpd itself against the configuration inetd.sh makes.

Run alone: python3 -I -B tests/image/test_web_api.py
"""

import base64
import hashlib
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import xml.etree.ElementTree as ElementTree
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_core_boot import arm_busybox  # noqa: E402

WWW = ROOT / "image/core/usr/www"
LIBRARY = ROOT / "image/core/usr/share/lapinlibre/web.sh"
# The library and its area files.
LIBRARIES = sorted(LIBRARY.parent.glob("web*.sh"))
READ_ONLY = ["status", "get_version", "get_free_space", "tools_log", "lapinlibre/status", "lapinlibre/logs",
             "lapinlibre/ssh", "lapinlibre/tag_action", "sound_list", "voice_list", "display_cache", "snapshot_list", "snapshot_get", "tools_ps",
             "rfid_list", "rfid_list_ext", "rfid_info", "tag_list", "moods_list", "stories_list", "radios_list"]
# The endpoints that drive the rabbit's daemons, and those that change something else.
HARDWARE = ["leds", "ears", "ears_reset", "ears_random", "sleep", "wakeup", "sound", "sound_control", "tts",
            "snapshot", "snapshot_view", "rfid_start_record", "rfid_stop_record", "apps/moods", "apps/clock", "stories"]
CHANGES = ["reboot", "ears_mode", "clear_cache", "clear_snapshots", "lapinlibre/snapshot_delete", "setclock", "rfid_rename", "rfid_delete",
           "rfid_unassign", "clear_rfid", "rfid_assign_url", "rfid_test_url", "rfid_assign_karotz"]
CGIS = READ_ONLY + HARDWARE + CHANGES
LISTENER = ROOT / "image/core/usr/scripts/tag_listener"
# The tools the programs use, besides the stubs.
APPLETS = ["sh", "awk", "sed", "grep", "tail", "head", "cat", "tr", "sort", "uniq", "wc", "date", "sha256sum",
           "timeout", "setsid", "kill", "sleep", "cut", "printf", "rm", "mv", "mkdir", "chmod", "ls", "od",
           "flock", "usleep", "md5sum", "env", "mkfifo"]
# Absolute paths the programs use, moved into the sandbox (not /dev, not the PATH line).
SANDBOXED = re.compile(r"(?<![\w.$/-])/(usr|var|karotz|etc|proc|sys|mnt|tmp)(?=/)")
# A JPEG as the camera's stub writes it: the start and end markers around some bytes.
JPEG = b"\xff\xd8\xff\xe0 test picture \xff\xd9"
HOST = "lapinlibre.local"
CLIENT = "192.168.1.5"
BUILD_ID = "57a75d60523288de"
BUILD = "v0.9-12-g96f4966"
LAPINLIBRE_KEYS = ["build", "build_id", "hostname", "ip", "netmask", "gateway", "dns", "address_mode", "wifi_ssid",
                   "wifi_signal", "mac", "time", "timezone", "uptime", "load", "mem_total_kb", "mem_free_kb",
                   "usr_free_kb", "usr_used_percent", "usb_mounted", "language", "app_server", "speech_server",
                   "controller", "learning_left", "daemons", "ssh", "telnet", "web_user"]
STATUS_KEYS = ["version", "ears_disabled", "sleep", "sleep_time", "led_color", "led_pulse", "tts_cache_size",
               "usb_free_space", "karotz_free_space", "eth_mac", "wlan_mac", "nb_tags", "nb_moods", "nb_sounds",
               "nb_stories", "karotz_percent_used_space", "usb_percent_used_space", "data_dir", "return", "lapinlibre"]
DAEMONS = ["led", "rfid", "webcam", "button", "ears", "multimedia", "voice", "scheduler", "serial", "k2k",
           "controller", "mpd", "mdnsd"]
# Values that must never get anywhere: each would leave a marker file if any shell ran it.
HOSTILE = ["$(touch {m})", "`touch {m}`", "1;touch {m}", "1|touch {m}", "1&&touch {m}", "1'\"", "../../etc/passwd",
           "1%00", "1%0a", "1%0d%0atouch {m}", "%ff%fe", "1" * 5000, "%", "%4", "%zz", "1 ", " 1", "-1", "0x10", "1e3"]


def stub(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body + "\n", newline="\n")
    path.chmod(0o755)


def fake_key(seed):
    """An SSH key blob in base64 (the type string, then random-looking bytes)."""
    blob = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + hashlib.sha256(seed.encode()).digest()
    return base64.b64encode(blob).decode()


def fingerprint(b64):
    return "SHA256:" + base64.b64encode(hashlib.sha256(base64.b64decode(b64)).digest()).decode().rstrip("=")


def sandbox_copy(src, dst, sb):
    """A program or the library with its absolute paths and PATH moved into the sandbox."""
    text = src.read_text(encoding="utf-8")
    text = text.replace("PATH=/bin:/sbin:/usr/bin:/usr/sbin\n", f"PATH={sb}/bin\n")
    text = SANDBOXED.sub(lambda m: f"{sb}{m.group(0)}", text)
    if text.startswith("#!/bin/sh\n"):
        text = f"#!{sb}/bin/sh\n" + text[len("#!/bin/sh\n"):]
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(text, newline="\n")
    dst.chmod(src.stat().st_mode & 0o777 | 0o644)


class Sandbox:
    """A rabbit's files for the web API: the programs and library (paths moved in), its settings,
    /proc and /sys files, logs, and the tools and stubs on PATH."""

    def __init__(self, busybox):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        sb = self.sb = Path(self.tmp.name)
        self.bin = sb / "bin"
        self.bin.mkdir()
        for name in APPLETS:
            if busybox:
                stub(self.bin / name, f'exec {busybox} {name} "$@"')
            else:
                host = shutil.which(name)
                if host:
                    (self.bin / name).symlink_to(host)
        self.actions = sb / "actions.log"
        self.syslog = sb / "syslog.log"
        # busybox: its ntpd only records and answers as ntpd.fail says.
        ntpd = f'if [ "$1" = ntpd ]; then shift; echo "ntpd $*" >> {self.actions}; [ ! -f {sb}/ntpd.fail ]; exit; fi\n'
        if busybox:
            stub(self.bin / "busybox", ntpd + f'exec {busybox} "$@"')
        else:
            stub(self.bin / "busybox", ntpd + 'exec "$@"')
            if not shutil.which("usleep"):
                stub(self.bin / "usleep", 'exec /bin/sleep 0.2')
        iconv = shutil.which("iconv")
        if iconv:
            (self.bin / "iconv").symlink_to(iconv)
        stub(self.bin / "logger", f'echo "$*" >> {self.syslog}')
        stub(self.bin / "reboot", f'echo "reboot $*" >> {self.actions}')
        stub(self.bin / "killall", f'echo "killall $*" >> {self.actions}')
        for name in ("halt", "poweroff", "eval", "ntpd", "mplayer", "wget"):
            stub(self.bin / name, f'echo "FORBIDDEN {name} $*" >> {self.actions}')
        # curl: records its arguments (separated by \037) and what it read on its standard input (a
        # config), then answers as curl.code says (the HTTP status, default 200), writing curl.body
        # to its -o file; with curl.fail ("STATUS message") it fails instead; curl.sleep delays it.
        stub(self.bin / "curl",
             f'{{ printf "curl"; printf "\\037%s" "$@"; printf "\\n"; }} >> {self.actions}\n'
             'config=$(cat)\n'
             f'[ -n "$config" ] && printf "curl-config %s\\n" "$config" >> {self.actions}\n'
             f'[ -f {sb}/curl.sleep ] && /bin/sleep "$(cat {sb}/curl.sleep)"\n'
             'out="" prev=""\n'
             'for a; do [ "$prev" = -o ] && out=$a; prev=$a; done\n'
             f'if [ -f {sb}/curl.fail ]; then f=$(cat {sb}/curl.fail); echo "curl: (${{f%% *}}) ${{f#* }}" >&2; exit "${{f%% *}}"; fi\n'
             f'[ -f {sb}/curl.body ] && cat {sb}/curl.body > "$out"\n'
             f'code=200; [ -f {sb}/curl.code ] && code=$(cat {sb}/curl.code)\n'
             'printf "%s" "$code"')
        # dbus-send: records its arguments (separated by \037). The controller answers with the
        # state in controller.state (no file: not on the bus; "hang": no answer). Each daemon
        # answers as dbus/<Name>.<method> or dbus/<Name> says (default true; false, missing,
        # noreply); a reset and a snapshot send their signals into signal.out for dbus-monitor,
        # and a snapshot writes the picture (unless dbus/nopicture). With noreply, the snapshot's
        # picture and event come a second later.
        stub(self.bin / "dbus-send",
             f'{{ printf "dbus-send"; printf "\\037%s" "$@"; printf "\\n"; }} >> {self.actions}\n'
             'dest="" member="" id=""\n'
             'for a; do\n'
             '    case $a in\n'
             '    --dest=com.mindscape.karotz.*) dest=${a#--dest=com.mindscape.karotz.} ;;\n'
             '    com.mindscape.karotz.KarotzInterface.*) member=${a#com.mindscape.karotz.KarotzInterface.} ;;\n'
             '    string:*) [ -n "$id" ] || id=${a#string:} ;;\n'
             '    esac\n'
             'done\n'
             'if [ "$dest.$member" = Controller.get_status ]; then\n'
             f'    [ -f {sb}/controller.state ] || {{ echo "Error org.freedesktop.DBus.Error.ServiceUnknown: no" >&2; exit 1; }}\n'
             f'    s=$(cat {sb}/controller.state)\n'
             '    [ "$s" = hang ] && exec sleep 30\n'
             '    echo "method return time=1760000000.1 sender=:1.3 -> destination=:1.9 serial=5 reply_serial=2"\n'
             '    printf "   int32 %s\\n   int32 1\\n   int32 1\\n" "$s"\n'
             '    exit 0\n'
             'fi\n'
             'signal() {\n'
             f'    {{ printf "signal time=1 sender=:1.4 -> destination=(null destination) serial=9 path=/com/mindscape/karotz/%s; interface=com.mindscape.karotz.KarotzInterface; member=%s\\n" "$dest" "$1"; printf "   string \\"%s\\"\\n   int32 4\\n" "$2"; }} > {sb}/signal.tmp\n'
             f'    mv {sb}/signal.tmp {sb}/signal.out\n'
             '}\n'
             'picture() {\n'
             f'    [ -f {sb}/dbus/nopicture ] || printf "\\377\\330\\377\\340 test picture \\377\\331" > {sb}/tmp/picture.jpg\n'
             '    signal event "${id#*:}"\n'
             '}\n'
             'r=true\n'
             f'[ -f {sb}/dbus/$dest ] && r=$(cat {sb}/dbus/$dest)\n'
             f'[ -f {sb}/dbus/$dest.$member ] && r=$(cat {sb}/dbus/$dest.$member)\n'
             'case $r in\n'
             'missing) echo "Error org.freedesktop.DBus.Error.ServiceUnknown: The name com.mindscape.karotz.$dest was not provided" >&2; exit 1 ;;\n'
             'noreply)\n'
             '    [ "$dest.$member" = Webcam.takeSnapshot ] && { sleep 1; picture; } > /dev/null 2>&1 &\n'
             '    echo "Error org.freedesktop.DBus.Error.NoReply: Did not receive a reply." >&2\n'
             '    exit 1 ;;\n'
             'esac\n'
             'case $dest.$member in\n'
             f'Ears.reset) [ -f {sb}/dbus/nosignal ] || signal reset_finished "$id" ;;\n'
             'Webcam.takeSnapshot) [ "$r" = true ] && picture ;;\n'
             'esac\n'
             'echo "method return time=1760000000.2 sender=:1.4 -> destination=:1.9 serial=7 reply_serial=2"\n'
             'echo "   boolean $r"')
        # dbus-monitor: records its match rule, writes its own first lines, then passes on the
        # signals the daemons' stub sends. With the tag listener's rule, it plays tag.signals
        # instead ("SLEEP s" lines pause), then ends.
        stub(self.bin / "dbus-monitor",
             f'echo "dbus-monitor $*" >> {self.actions}\n'
             'case "$*" in *"member=\'tag_in\'"*)\n'
             f'    [ -f {sb}/tag.signals ] && while IFS= read -r l; do\n'
             '        case $l in "SLEEP "*) /bin/sleep "${l#SLEEP }" ;; *) printf "%s\\n" "$l" ;; esac\n'
             f'    done < {sb}/tag.signals\n'
             '    /bin/sleep 0.5; exit 0 ;;\n'
             'esac\n'
             'echo "signal time=1 sender=org.freedesktop.DBus -> destination=:1.20 serial=2 path=/org/freedesktop/DBus; '
             'interface=org.freedesktop.DBus; member=NameAcquired"\n'
             'echo \'   string ":1.20"\'\n'
             'n=0\n'
             'while [ $n -lt 150 ]; do\n'
             f'    if [ -f {sb}/signal.out ]; then cat {sb}/signal.out; rm -f {sb}/signal.out; fi\n'
             '    /bin/sleep 0.1\n'
             '    n=$((n + 1))\n'
             'done')
        # djpeg: records its arguments and writes a GIF to -outfile, unless djpeg.fail.
        stub(self.bin / "djpeg",
             f'echo "djpeg $*" >> {self.actions}\n'
             f'[ -f {sb}/djpeg.fail ] && exit 1\n'
             'out="" prev=""\n'
             'for a; do [ "$prev" = -outfile ] && out=$a; prev=$a; done\n'
             'printf "GIF89a thumb" > "$out"')
        # ps: the command lines (ps.txt), or with -o comm the kernel's names (ps-comm.txt).
        stub(self.bin / "ps", f'if [ "$1" = -o ]; then cat {sb}/ps-comm.txt; else cat {sb}/ps.txt; fi 2> /dev/null')
        stub(self.bin / "ifconfig", f'[ "$1" = wlan0 ] && cat {sb}/ifconfig.txt 2> /dev/null; exit 0')
        stub(self.bin / "iwgetid", f'cat {sb}/ssid.txt 2> /dev/null')
        stub(self.bin / "dmesg", f'cat {sb}/dmesg.txt 2> /dev/null')
        stub(self.bin / "dropbearkey", 'cat "$3"')
        stub(self.bin / "df",
             'echo "Filesystem           1024-blocks    Used Available Capacity Mounted on"\n'
             'for d; do\n'
             f'    case $d in -*) ;; */mnt/usbkey) cat {sb}/df-usb.txt ;; *) cat {sb}/df-usr.txt ;; esac\n'
             'done')
        # Every program started from PATH first writes its name into procs.log, so a test can count
        # the processes a request starts; what the stand-ins start themselves isn't counted.
        self.procs = sb / "procs.log"
        if iconv:
            (self.bin / "iconv").unlink()
            stub(self.bin / "iconv", f'exec {iconv} "$@"')
        for path in self.bin.iterdir():
            if path.is_file() and not path.is_symlink():
                count = f'[ -n "${{LL_STUB:-}}" ] || echo {path.name} >> {self.procs}\n'
                if path.name not in APPLETS + ["busybox", "iconv"]:
                    count += "LL_STUB=1\nexport LL_STUB\n"
                text = path.read_text()
                path.write_text(text.replace("#!/bin/sh\n", "#!/bin/sh\n" + count, 1), newline="\n")
        for src in [*LIBRARIES, LISTENER, *[WWW / "cgi-bin" / c for c in CGIS]]:
            sandbox_copy(src, sb / src.relative_to(ROOT / "image/core"), sb)
        self.files()

    def write(self, rel, text, mode=None):
        path = self.sb / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            path.write_text(text, newline="\n")
        if mode is not None:
            path.chmod(mode)
        return path

    def processes(self, commands):
        """The programs running: ps's list (ps.txt) and the kernel's /proc/<pid>/stat files, whose
        name is the program's file name cut to 15 characters ({name} when ps shows one). None:
        no process list at all."""
        for old in (self.sb / "proc").glob("[0-9]*"):
            shutil.rmtree(old)
        if commands is None:
            (self.sb / "ps.txt").unlink(missing_ok=True)
            (self.sb / "ps-comm.txt").unlink(missing_ok=True)
            return
        self.write("ps.txt", "COMMAND\n" + "".join(c + "\n" for c in commands))
        names = []
        for pid, command in enumerate(commands, start=100):
            m = re.match(r"\{([^}]*)\}", command)
            name = m.group(1) if m else command.split()[0].rsplit("/", 1)[-1]
            names.append(name[:15])
            self.write(f"proc/{pid}/stat", f"{pid} ({name[:15]}) S 1 {pid} {pid} 0 -1 4194560 0 0 0 0\n")
        self.write("ps-comm.txt", "COMMAND\n" + "".join(n + "\n" for n in names))

    def files(self):
        w = self.write
        w("karotz/etc/lapinlibre-build", BUILD + "\n")
        w("karotz/etc/rootfs_version", "12.07.19.00\n")
        w("usr/etc/conf/lapinlibre_build_id", BUILD_ID + "\n")
        w("usr/etc/conf/web_password.hash", "$6$salt$xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n")
        w("usr/etc/conf/network.conf", 'IP=""\nNETMASK=""\nGATEWAY=""\nDNS=""\n')
        w("usr/etc/conf/karotz.conf", "[karotz]\nip_server=192.168.1.10\nlang=en-GB\n[k2k]\nx=y\n")
        w("usr/etc/conf/voice.conf", "[tts]\nen=http://192.168.1.10:8081/en\nfr=http://192.168.1.10:8081/fr\n")
        w("proc/sys/kernel/hostname", "kitchen\n")
        w("proc/uptime", "93784.52 90000.10\n")
        w("proc/loadavg", "0.08 0.03 0.01 1/52 1234\n")
        w("proc/meminfo", "MemTotal:          59884 kB\nMemFree:            8000 kB\nBuffers:            1000 kB\n"
                          "Cached:            11000 kB\nSwapCached:          500 kB\n")
        w("proc/mounts", "rootfs / rootfs rw 0 0\n/dev/mtdblock6 /usr yaffs rw 0 0\n")
        w("proc/net/route", "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
                            "wlan0\t0001A8C0\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n"
                            "wlan0\t00000000\t0101A8C0\t0003\t0\t0\t0\t00000000\t0\t0\t0\n")
        w("proc/net/wireless", "Inter-| sta-|   Quality        |   Discarded packets\n"
                               " face | tus | link level noise |  nwid  crypt\n"
                               "wlan0: 0000   70.  -40.  -256        0      0      0      0      0        0\n")
        w("sys/class/net/wlan0/address", "00:0e:8e:12:34:56\n")
        w("etc/resolv.conf", "# made by udhcpc\nnameserver 192.168.1.2\nnameserver 192.168.1.3\n")
        w("etc/TZ", "CET-1CEST,M3.5.0,M10.5.0/3\n")
        w("ifconfig.txt", "wlan0     Link encap:Ethernet  HWaddr 00:0E:8E:12:34:56\n"
                          "          inet addr:192.168.1.20  Bcast:192.168.1.255  Mask:255.255.255.0\n")
        w("ssid.txt", "home net\n")
        self.processes(["{immortaldog} /usr/karotz/bin/immortaldog /var/run/karotz/led.pid /usr/karotz/bin/led-daemon",
                        "/usr/karotz/bin/led-daemon", "/usr/karotz/bin/ears-daemon --x", "{controller} /usr/karotz/bin/controller",
                        "/usr/bin/mpd /etc/mpd.conf",
                        "/usr/karotz/bin/immortaldog /var/run/karotz/rfid.pid /usr/karotz/bin/rfid-daemon",
                        "/usr/karotz/bin/multimedia-daemon"])
        w("df-usr.txt", "/dev/mtdblock6           112640     47308     65332  42% /usr\n")
        w("df-usb.txt", "/dev/uba1               3913728    665334   3248394  17% /mnt/usbkey\n")
        w("controller.state", "16\n")
        w("var/log/messages", "".join(f"Oct 20 10:00:{i % 60:02d} kitchen user.info test: line {i}\n" for i in range(250)))
        w("var/log/mpd.log", "mpd one\nmpd two\nmpd three\n")
        w("dmesg.txt", "kernel one\nkernel two\n")
        w("usr/karotz/sounds/test-tone.wav", "RIFF")
        w("usr/karotz/sounds/button-click.wav", "RIFF")
        w("usr/karotz/sounds/bad name.wav", "RIFF")
        w("usr/karotz/sounds/wakeup.mp3", "ID3")
        w("usr/karotz/sounds/notes.txt", "x")
        (self.sb / "var/run/karotz").mkdir(parents=True)
        (self.sb / "var/lock").mkdir(parents=True)
        (self.sb / "mnt/usbkey").mkdir(parents=True)
        (self.sb / "tmp").mkdir()
        (self.sb / "dbus").mkdir()

    def daemon(self, name, answer):
        """How a daemon (Name, or Name.method) answers: true, false, missing or noreply."""
        self.write(f"dbus/{name}", answer + "\n")

    def mount_stick(self):
        # The mount point as the sandboxed programs name it.
        self.write("proc/mounts", f"rootfs / rootfs rw 0 0\n/dev/uba1 {self.sb}/mnt/usbkey vfat rw 0 0\n")
        for rel in ("mnt/usbkey/sounds/ding.mp3", "mnt/usbkey/sounds/test-tone.mp3", "mnt/usbkey/moods/1.mp3",
                    "mnt/usbkey/moods/2.mp3", "mnt/usbkey/moods/x y.mp3", "mnt/usbkey/stories/tale.mp3"):
            self.write(rel, "ID3")

    def run(self, path, query="", method="GET", user="admin", addr=CLIENT, host=HOST, headers=None, body=None,
            content_type="application/x-www-form-urlencoded", env=None):
        """Runs cgi-bin/<path> as httpd would; returns (status, headers, body bytes)."""
        e = {"PATH": str(self.bin), "REQUEST_METHOD": method, "QUERY_STRING": query, "SCRIPT_NAME": f"/cgi-bin/{path}",
             "REQUEST_URI": f"/cgi-bin/{path}", "SERVER_PROTOCOL": "HTTP/1.1", "GATEWAY_INTERFACE": "CGI/1.1",
             "REMOTE_PORT": "40000"}
        if addr is not None:
            e["REMOTE_ADDR"] = addr
        if user is not None:
            e["REMOTE_USER"] = user
            e["AUTH_TYPE"] = "Basic"
        if host is not None:
            e["HTTP_HOST"] = host
        for name, value in (headers or {}).items():
            e["HTTP_" + re.sub(r"[^A-Z0-9]", "_", name.upper())] = value
        data = b""
        if body is not None:
            data = body if isinstance(body, bytes) else body.encode()
            e["CONTENT_LENGTH"] = str(len(data))
            if content_type:
                e["CONTENT_TYPE"] = content_type
        e.update(env or {})
        r = subprocess.run([str(self.bin / "sh"), str(self.sb / "usr/www/cgi-bin" / path)], input=data, env=e,
                           capture_output=True, timeout=120)
        head, sep, rest = r.stdout.partition(b"\r\n\r\n")
        if not sep:
            raise AssertionError(f"{path}: no header block: {r.stdout[:300]!r} {r.stderr[:500]!r}")
        hdrs = {}
        for line in head.decode().split("\r\n"):
            name, _, value = line.partition(": ")
            hdrs[name.lower()] = value
        status = int(hdrs.pop("status", "200").split()[0])
        return status, hdrs, rest

    def actions_log(self):
        return self.actions.read_text(errors="surrogateescape") if self.actions.exists() else ""

    def dbus_calls(self, controller=False):
        """The daemons' methods called, in order: (Name, method, [typed arguments])."""
        calls = []
        for line in self.actions_log().splitlines():
            if not line.startswith("dbus-send\x1f"):
                continue
            args = line.split("\x1f")[1:]
            dest = next(a[len("--dest=com.mindscape.karotz."):] for a in args if a.startswith("--dest="))
            member = next(a.rsplit(".", 1)[1] for a in args if a.startswith("com.mindscape.karotz.KarotzInterface."))
            if dest == "Controller" and not controller:
                continue
            calls.append((dest, member, [a for a in args if re.match(r"(string|int32|boolean):", a)]))
        return calls

    def syslog_lines(self):
        return self.syslog.read_text().splitlines() if self.syslog.exists() else []

    def started(self, path, query="", **kw):
        """The programs a request to cgi-bin/<path> starts from PATH, in order (the CGI's own shell
        first), and its reply's body."""
        self.procs.unlink(missing_ok=True)
        _, _, body = self.run(path, query, **kw)
        return (self.procs.read_text().split() if self.procs.exists() else []), body


class WebApiCase(unittest.TestCase):
    busybox = None

    @classmethod
    def setUpClass(cls):
        cls.busybox = arm_busybox()

    def setUp(self):
        self.box = Sandbox(self.busybox)
        self.sb = self.box.sb

    def tearDown(self):
        self.assertNotIn("FORBIDDEN", self.box.actions_log())
        self.box.tmp.cleanup()

    def json(self, path, **kw):
        status, headers, body = self.box.run(path, **kw)
        self.assertEqual(status, 200, body)
        self.assertReplyHeaders(headers)
        return json.loads(body)

    def assertReplyHeaders(self, headers):
        self.assertEqual(headers.get("content-type"), "text/plain")
        self.assertEqual(headers.get("cache-control"), "no-store")
        self.assertEqual(headers.get("x-content-type-options"), "nosniff")
        self.assertFalse([h for h in headers if h.startswith("access-control")], headers)

    def assertStrings(self, obj, skip=()):
        for key, value in obj.items():
            if key not in skip:
                self.assertIsInstance(value, str, key)

    def assertError(self, reply, word):
        self.assertEqual(set(reply), {"return", "msg"})
        self.assertEqual(reply["return"], "1")
        self.assertIn(word, reply["msg"])


class EndpointTest(WebApiCase):
    """Each endpoint's reply."""

    def test_get_version(self):
        reply = self.json("get_version")
        self.assertEqual(reply, {"version": f"{BUILD_ID} ({BUILD})", "rootfs_version": "12.07.19.00",
                                 "patch_version": "N/A", "return": "0"})
        (self.sb / "karotz/etc/rootfs_version").unlink()
        self.assertEqual(self.json("get_version")["rootfs_version"], "Unknown")

    def test_get_free_space(self):
        self.assertEqual(self.json("get_free_space"),
                         {"karotz_percent_used_space": "42", "usb_percent_used_space": "-1", "return": "0"})
        self.box.mount_stick()
        self.assertEqual(self.json("get_free_space")["usb_percent_used_space"], "17")

    def test_tools_log(self):
        status, headers, body = self.box.run("tools_log")
        self.assertEqual(status, 200)
        self.assertReplyHeaders(headers)
        lines = body.decode().splitlines()
        self.assertEqual(len(lines), 200)
        self.assertTrue(lines[0].endswith("line 50") and lines[-1].endswith("line 249"), (lines[0], lines[-1]))
        (self.sb / "var/log/messages").unlink()
        self.assertEqual(self.box.run("tools_log")[2], b"")

    def test_logs(self):
        status, headers, body = self.box.run("lapinlibre/logs", "lines=5")
        self.assertEqual(status, 200)
        self.assertReplyHeaders(headers)
        self.assertNotIn("content-disposition", headers)
        self.assertEqual([line[-8:] for line in body.decode().splitlines()], [f"line {i}" for i in range(245, 250)])
        self.assertEqual(len(self.box.run("lapinlibre/logs")[2].decode().splitlines()), 200)
        self.assertEqual(self.box.run("lapinlibre/logs", "source=kernel&lines=1")[2], b"kernel two\n")
        self.assertEqual(self.box.run("lapinlibre/logs", "source=MPD")[2], b"mpd one\nmpd two\nmpd three\n")
        # The rotated log comes first when more lines are wanted.
        self.box.write("var/log/messages.0", "old one\nold two\n")
        lines = self.box.run("lapinlibre/logs", "lines=1000")[2].decode().splitlines()
        self.assertEqual((len(lines), lines[0], lines[1]), (252, "old one", "old two"))

    def test_logs_bad_parameters(self):
        for query, word in (("lines=0", "lines"), ("lines=1001", "lines"), ("lines=abc", "lines"),
                            ("source=messages", "source"), ("source=../mpd", "source"), ("source=%00", "source")):
            with self.subTest(query):
                status, headers, body = self.box.run("lapinlibre/logs", query)
                self.assertEqual(status, 200)
                self.assertReplyHeaders(headers)
                self.assertError(json.loads(body), word)

    def test_lapinlibre_status(self):
        reply = self.json("lapinlibre/status")
        self.assertEqual(list(reply), ["return"] + LAPINLIBRE_KEYS)
        self.assertStrings(reply, skip=("uptime", "mem_total_kb", "mem_free_kb", "usr_free_kb", "usr_used_percent", "learning_left",
                                        "daemons"))
        expected = {"return": "0", "build": BUILD, "build_id": BUILD_ID, "hostname": "kitchen", "ip": "192.168.1.20",
                    "netmask": "255.255.255.0", "gateway": "192.168.1.1", "dns": "192.168.1.2", "address_mode": "dhcp",
                    "wifi_ssid": "home net", "wifi_signal": "70", "mac": "00:0e:8e:12:34:56",
                    "timezone": "CET-1CEST,M3.5.0,M10.5.0/3", "uptime": 93784, "load": "0.08 0.03 0.01",
                    "mem_total_kb": 59884, "mem_free_kb": 20000, "usr_free_kb": 65332, "usr_used_percent": 42,
                    "usb_mounted": "0", "language": "en-GB", "app_server": "192.168.1.10",
                    "speech_server": "http://192.168.1.10:8081/en", "controller": "available", "learning_left": 0,
                    "ssh": "0", "telnet": "0", "web_user": "admin"}
        for key, value in expected.items():
            self.assertEqual(reply[key], value, key)
        self.assertRegex(reply["time"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d{4}$")
        # multimedia-daemon's name is longer than the kernel keeps.
        running = {"led", "ears", "multimedia", "controller", "mpd"}
        self.assertEqual(reply["daemons"], {d: "1" if d in running else "0" for d in DAEMONS})

    def test_lapinlibre_status_variants(self):
        self.box.write("usr/etc/conf/network.conf", 'IP="192.168.1.20"\nNETMASK="255.255.255.0"\n')
        self.box.write("usr/etc/conf/telnet", "yes\n")
        self.box.write("usr/etc/conf/authorized_keys", f"ssh-ed25519 {fake_key('a')} me@pc\n")
        self.box.write("usr/karotz/.ssh/authorized_keys", f"ssh-ed25519 {fake_key('a')} me@pc\n")
        self.box.mount_stick()
        (self.sb / "controller.state").unlink()
        reply = self.json("lapinlibre/status")
        self.assertEqual((reply["address_mode"], reply["telnet"], reply["ssh"], reply["controller"]),
                         ("fixed", "1", "1", "not running"))
        self.assertEqual((reply["usb_mounted"], reply["usb_free_kb"], reply["usb_used_percent"]), ("1", 3248394, 17))
        keys = list(reply)
        self.assertEqual(keys[keys.index("usb_mounted"):keys.index("usb_mounted") + 3],
                         ["usb_mounted", "usb_free_kb", "usb_used_percent"])
        for state, name in (("14", "connecting"), ("21", "interactive"), ("22", "leaving interactive mode"),
                            ("24", "asleep"), ("25", "waking up"), ("99", "state 99"), ("x", "not running")):
            self.box.write("controller.state", state + "\n")
            self.assertEqual(self.json("lapinlibre/status")["controller"], name, state)

    def test_controller_that_does_not_answer(self):
        # Without the tag listener's state, the controller is asked, for at most 1 s (dbus-send's
        # reply timeout, and timeout around it): the status still answers.
        self.box.write("controller.state", "hang\n")
        start = time.monotonic()
        reply = self.json("lapinlibre/status")
        self.assertLess(time.monotonic() - start, 10)
        self.assertEqual(reply["controller"], "not running")
        call = [line for line in self.box.actions_log().splitlines() if "get_status" in line][0]
        self.assertIn("--reply-timeout=1000", call)
        self.assertIn("--dest=com.mindscape.karotz.Controller", call)

    def test_controller_state_from_the_listener(self):
        # The tag listener's file is read instead of asking the controller: a number, or empty
        # (not on the bus); anything else counts as not running.
        self.box.write("controller.state", "hang\n")
        for kept, name in (("21", "interactive"), ("16", "available"), ("", "not running"), ("x1", "not running")):
            with self.subTest(kept):
                self.box.write("var/run/lapinlibre-web/controller", kept + "\n")
                start = time.monotonic()
                self.assertEqual(self.json("lapinlibre/status")["controller"], name)
                self.assertLess(time.monotonic() - start, 5)
        self.assertNotIn("get_status", self.box.actions_log())
        # An application owning the rabbit, as the listener says, refuses the hardware.
        self.box.write("var/run/lapinlibre-web/controller", "21\n")
        self.assertError(self.json("leds", query="color=FF0000", method="POST"), "application")

    def test_unreadable_values_are_empty(self):
        for rel in ("karotz/etc/lapinlibre-build", "usr/etc/conf/lapinlibre_build_id", "proc/net/route", "etc/resolv.conf",
                    "proc/net/wireless", "sys/class/net/wlan0/address", "ifconfig.txt", "ssid.txt", "etc/TZ",
                    "usr/etc/conf/karotz.conf", "usr/etc/conf/voice.conf", "df-usr.txt"):
            (self.sb / rel).unlink()
        self.box.processes(None)
        reply = self.json("lapinlibre/status")
        for key in ("build", "build_id", "ip", "netmask", "gateway", "dns", "wifi_ssid", "wifi_signal", "mac",
                    "timezone", "language", "app_server", "speech_server"):
            self.assertEqual(reply[key], "", key)
        self.assertEqual((reply["usr_free_kb"], reply["usr_used_percent"]), (0, 0))
        self.assertEqual(set(reply["daemons"].values()), {"0"})
        self.assertEqual(self.json("get_version")["version"], "")

    def test_values_are_json_escaped(self):
        # A network name with quotes, a backslash, a tab, a control character and UTF-8.
        ssid = 'my "net"\\ \t\x01 caf\u00e9 <b>'
        self.box.write("ssid.txt", (ssid + "\n").encode("utf-8"))
        self.box.write("proc/sys/kernel/hostname", "a\\b\"c\n")
        reply = self.json("lapinlibre/status")
        self.assertEqual(reply["wifi_ssid"], ssid)
        self.assertEqual(reply["hostname"], "a\\b\"c")

    def test_status(self):
        reply = self.json("status")
        self.assertEqual(list(reply), STATUS_KEYS)
        self.assertStrings(reply, skip=("lapinlibre",))
        expected = {"version": f"{BUILD_ID} ({BUILD})", "ears_disabled": "0", "sleep": "0", "sleep_time": "0",
                    "led_color": "00FF00", "led_pulse": "0", "tts_cache_size": "0", "usb_free_space": "-1",
                    "karotz_free_space": "63.8M", "eth_mac": "00:00:00:00:00:00", "wlan_mac": "00:0e:8e:12:34:56",
                    "nb_tags": "0", "nb_moods": "0", "nb_sounds": "3", "nb_stories": "0",
                    "karotz_percent_used_space": "42", "usb_percent_used_space": "", "data_dir": f"{self.sb}/usr/etc/conf/web",
                    "return": "0"}
        for key, value in expected.items():
            self.assertEqual(reply[key], value, key)
        self.assertEqual(list(reply["lapinlibre"]), LAPINLIBRE_KEYS)
        self.assertEqual(reply["lapinlibre"]["build"], BUILD)

    def test_status_with_stick_and_api_state(self):
        self.box.mount_stick()
        self.box.write("var/run/lapinlibre-web/led", "FF0000 1\n")
        self.box.write("var/run/lapinlibre-web/sleep", "1760000000\n")
        self.box.write("usr/etc/conf/web/ears_disabled", "")
        reply = self.json("status")
        self.assertEqual((reply["led_color"], reply["led_pulse"], reply["sleep"], reply["sleep_time"], reply["ears_disabled"]),
                         ("FF0000", "1", "1", "1760000000", "1"))
        # Sounds: the stick's and the core's, each name once; moods and stories from the stick.
        self.assertEqual((reply["nb_sounds"], reply["nb_moods"], reply["nb_stories"]), ("4", "2", "1"))
        self.assertEqual((reply["usb_free_space"], reply["usb_percent_used_space"]), ("3.1G", "17"))

    def test_status_controller_asleep(self):
        self.box.write("controller.state", "24\n")
        reply = self.json("status")
        self.assertEqual((reply["sleep"], reply["sleep_time"]), ("1", "0"))
        # A broken state file is ignored.
        self.box.write("var/run/lapinlibre-web/led", "red; reboot\n")
        self.assertEqual(self.json("status")["led_color"], "00FF00")

    def test_status_xml(self):
        self.box.write("karotz/etc/lapinlibre-build", "v1 <&> \"'\n")
        for fmt in ("xml", "XML", "Xml"):
            status, headers, body = self.box.run("status", f"format={fmt}")
            self.assertEqual(status, 200)
            self.assertReplyHeaders(headers)
            self.assertTrue(body.startswith(b'<?xml version="1.0" encoding="UTF-8"?>'), body[:60])
            root = ElementTree.fromstring(body)
            self.assertEqual(root.tag, "OPENKAROTZ")
            self.assertEqual([c.tag for c in root], ["VERSION", "EARS_DISABLED", "SLEEP", "SLEEP_TIME", "LED_COLOR",
                                                     "LED_PULSE", "TTS_CACHE_SIZE", "ETH_MAC", "WLAN_MAC",
                                                     "KAROTZ_USED_SPACE", "USB_USED_SPACE", "RETURN"])
            values = {c.tag: c.text or "" for c in root}
            self.assertEqual(values["VERSION"], f"{BUILD_ID} (v1 <&> \"')")
            self.assertEqual((values["KAROTZ_USED_SPACE"], values["USB_USED_SPACE"], values["RETURN"]), ("42", "", "0"))
        self.assertError(json.loads(self.box.run("status", "format=html")[2]), "format")
        self.assertEqual(self.json("status", query="format=")["return"], "0")

    def test_ssh(self):
        k1, k2 = fake_key("one"), fake_key("two")
        self.box.write("usr/karotz/.ssh/authorized_keys",
                       f"# a comment\n\nssh-ed25519 {k1} me@pc\r\n"
                       f'no-pty,command="x y" ssh-rsa {k2} work laptop\n'
                       "not a key line\nssh-ed25519 !!notbase64 broken\n")
        self.box.write("usr/etc/conf/dropbear/dropbear_ed25519_host_key",
                       f"Public key portion is:\nssh-ed25519 {k1} root@kitchen\nFingerprint: sha1!! aa:bb\n")
        self.box.write("usr/etc/conf/dropbear/dropbear_rsa_host_key",
                       "Public key portion is:\nssh-rsa AAAA%%%% root@kitchen\nFingerprint: sha1!! cc:dd\n")
        status, headers, body = self.box.run("lapinlibre/ssh")
        self.assertEqual(status, 200)
        self.assertReplyHeaders(headers)
        reply = json.loads(body)
        self.assertEqual(reply["return"], "0")
        self.assertEqual(reply["enabled"], "1")
        self.assertEqual(reply["keys"], [
            {"type": "ssh-ed25519", "comment": "me@pc", "fingerprint": fingerprint(k1)},
            {"type": "ssh-rsa", "comment": "work laptop", "fingerprint": fingerprint(k2)},
            {"type": "ssh-ed25519", "comment": "broken", "fingerprint": ""}])
        self.assertEqual(reply["host_keys"], [{"type": "ssh-ed25519", "fingerprint": fingerprint(k1)},
                                              {"type": "ssh-rsa", "fingerprint": "sha1!! cc:dd"}])
        self.assertEqual((reply["hostname"], reply["ip"], reply["users"], reply["telnet"]),
                         ("kitchen", "192.168.1.20", ["root", "karotz"], "0"))
        # Never the keys themselves.
        for key in (k1, k2):
            self.assertNotIn(key.encode(), body)

    def test_ssh_without_keys(self):
        reply = self.json("lapinlibre/ssh")
        self.assertEqual((reply["enabled"], reply["keys"], reply["host_keys"]), ("0", [], []))

    def test_reboot(self):
        status, headers, body = self.box.run("reboot", "secret=hunter2")
        self.assertEqual(status, 200)
        self.assertReplyHeaders(headers)
        self.assertEqual(json.loads(body), {"return": "0"})
        deadline = time.monotonic() + 20
        while "reboot" not in self.box.actions_log() and time.monotonic() < deadline:
            time.sleep(0.2)
        self.assertIn("reboot -d 10", self.box.actions_log())
        log = self.box.syslog_lines()
        self.assertEqual(log, [f"-t lapinlibre-web reboot {CLIENT} ok"])
        self.assertNotIn("hunter2", self.box.syslog.read_text())

    def test_head_has_no_body(self):
        for path in ("get_version", "lapinlibre/status", "lapinlibre/logs", "tools_log", "status"):
            with self.subTest(path):
                status, headers, body = self.box.run(path, method="HEAD")
                self.assertEqual((status, body), (200, b""))
                self.assertReplyHeaders(headers)

    def test_post_form_body(self):
        status, _, body = self.box.run("lapinlibre/logs", method="POST", body="lines=2")
        self.assertEqual((status, len(body.decode().splitlines())), (200, 2))
        # The query string's value comes first.
        _, _, body = self.box.run("lapinlibre/logs", "lines=3", method="POST", body="lines=7")
        self.assertEqual(len(body.decode().splitlines()), 3)
        reply = json.loads(self.box.run("lapinlibre/logs", method="POST", body="lines=2", content_type="multipart/form-data")[2])
        self.assertError(reply, "form-encoded")

    def test_reads_write_nothing_under_usr(self):
        def snapshot():
            return sorted((str(p.relative_to(self.sb)), p.stat().st_mtime_ns) for p in (self.sb / "usr").rglob("*"))
        before = snapshot()
        for path in READ_ONLY:
            self.box.run(path)
        self.box.run("status", "format=xml")
        self.box.run("reboot")
        self.assertEqual(snapshot(), before)

    def test_programs_are_listed_and_safe(self):
        listed = (ROOT / "system/core-files.txt").read_text(encoding="utf-8")
        for name in CGIS:
            self.assertRegex(listed, rf"(?m)^usr:www/cgi-bin/{re.escape(name)} f 0755 0:0 core\b")
        for name in ("index.html", "app.js", "style.css"):
            self.assertRegex(listed, rf"(?m)^usr:www/{name} f 0644 0:0 core\b")
        for path in LIBRARIES:
            self.assertRegex(listed, rf"(?m)^usr:share/lapinlibre/{re.escape(path.name)} f 0644 0:0 core\b")
        on_disk = sorted(str(p.relative_to(WWW / "cgi-bin")).replace(os.sep, "/") for p in (WWW / "cgi-bin").rglob("*") if p.is_file())
        self.assertEqual(on_disk, sorted(CGIS))
        for path in [*LIBRARIES, *[WWW / "cgi-bin" / c for c in CGIS]]:
            text = path.read_text(encoding="utf-8")
            code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
            for bad in ("eval ", "sh -c", "bash -c", "`"):
                self.assertNotIn(bad, code, f"{path.name}: {bad}")
            self.assertNotRegex(code, r"(?m)^\s*source\s", path.name)
            self.assertIn("SPDX-License-Identifier: MIT", "\n".join(text.splitlines()[:5]))
            if path not in LIBRARIES:
                self.assertTrue(text.startswith("#!/bin/sh\n"), path)
                self.assertIn(". /usr/share/lapinlibre/web.sh\nweb_begin ", text)
            r = subprocess.run(["sh", "-n", str(path)], capture_output=True, text=True, timeout=30)
            self.assertEqual((r.returncode, r.stderr), (0, ""), path)
        if shutil.which("shellcheck"):
            r = subprocess.run(["shellcheck", "-s", "sh", "-e", "SC1091,SC2034,SC2154", *[str(p) for p in LIBRARIES],
                                *[str(WWW / "cgi-bin" / c) for c in CGIS]], capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stdout[-3000:])

    def test_page_uses_no_html_insertion(self):
        script = (WWW / "app.js").read_text(encoding="utf-8")
        for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "http://", "https://"):
            self.assertNotIn(bad, script)
        page = (WWW / "index.html").read_text(encoding="utf-8")
        self.assertNotRegex(page, r"(src|href)=\"(https?:)?//")
        # Stage 2's sections, each with its place for the result, and the endpoints they use.
        for section in ("sleep", "led", "ears", "sound", "speech", "camera", "tags", "stick"):
            self.assertRegex(page, rf'<section id="{section}" class="card action-card">')
        self.assertEqual(page.count('<p class="result" aria-live="polite"></p>'), 8)
        self.assertIn('id="get-url-template"', page)
        for path in ("leds", "ears", "ears_reset", "ears_random", "ears_mode", "sleep", "wakeup", "sound", "sound_control",
                     "sound_list", "tts", "voice_list", "snapshot", "snapshot_view", "snapshot_list", "snapshot_get",
                     "clear_snapshots", "lapinlibre/snapshot_delete", "status", "rfid_start_record", "rfid_stop_record", "rfid_list_ext", "rfid_rename",
                     "rfid_delete", "rfid_unassign", "clear_rfid", "rfid_assign_url", "rfid_test_url", "rfid_assign_karotz",
                     "moods_list", "stories_list", "radios_list", "apps/moods", "apps/clock", "stories"):
            self.assertRegex(script, rf'"(cgi-bin/)?{path}[?"]')
        # A tag's rabbit actions offered by the page are exactly the ones the API allows.
        allowed = re.search(r'WEB_TAG_ENDPOINTS="([^"]*)"', LIBRARY.read_text(encoding="utf-8")).group(1).split()
        kinds = re.findall(r'<option value="([^"]+)">', page[page.index('id="tags-kind"'):page.index("</select>", page.index('id="tags-kind"'))])
        self.assertEqual(sorted(k for k in kinds if k != "url"), sorted(allowed))
        # The password never goes into the URL shown for an action.
        self.assertIn('shown.password = "<password>"', script)


class CostTest(WebApiCase):
    """What a request costs the rabbit: the programs it starts (each takes it a tenth of a second
    or more), and that the library's areas are read only when needed."""

    def setUp(self):
        super().setUp()
        if not self.busybox:
            self.skipTest("counting needs the image's BusyBox")
        # The API's state folder, as the first request after a boot leaves it.
        (self.sb / "var/run/lapinlibre-web").mkdir(parents=True)

    def test_areas_have_a_stand_in_for_each_function(self):
        core = LIBRARY.read_text(encoding="utf-8")
        for path in LIBRARIES:
            if path == LIBRARY:
                continue
            area = path.name[len("web-"):-len(".sh")]
            for name in re.findall(r"(?m)^([a-z_0-9]+)\(\) \{$", path.read_text(encoding="utf-8")):
                with self.subTest(name):
                    if name in ("disk_usage_set", "human_kb", "mp3_count", "hex_ip", "mp3_names"):
                        continue
                    self.assertIn(f'{name}() {{ web_area {area} && {name} "$@"; }}', core)
        for name in re.findall(r"(?m)^([a-z_0-9]+)\(\) \{ web_area", core):
            self.assertEqual(len(re.findall(rf"(?m)^{name}\(\) \{{", core)), 1, name)

    def test_leds(self):
        procs, body = self.box.started("leds", "color=ff0000", method="POST")
        self.assertEqual(json.loads(body)["return"], "0")
        # The controller's state and the LED, each through timeout; the lock; the log line after the
        # reply; and this boot's one look for the start-up's LED tool (ps).
        self.assertEqual(sorted(procs), sorted(["sh", "timeout", "dbus-send", "flock", "ps", "timeout", "dbus-send", "logger"]))
        procs, _ = self.box.started("leds", "color=00FF00&pulse=1&color2=000000&speed=500", method="POST")
        self.assertEqual(sorted(procs), sorted(["sh", "timeout", "dbus-send", "flock", "timeout", "dbus-send", "logger"]))
        # As the page sends it: a form body.
        procs, body = self.box.started("leds", method="POST", body="color=00ff00", headers={"Origin": f"http://{HOST}"})
        self.assertEqual(json.loads(body)["color"], "00FF00")
        self.assertEqual(sorted(procs), sorted(["sh", "timeout", "dbus-send", "flock", "timeout", "dbus-send", "logger"]))
        # An encoded value is decoded by awk.
        procs, body = self.box.started("leds", method="POST", body="color=%2300ff00")
        self.assertEqual(json.loads(body)["color"], "00FF00")
        self.assertEqual(procs.count("awk"), 1)

    def test_status(self):
        procs, body = self.box.started("status")
        self.assertEqual(json.loads(body)["return"], "0")
        self.assertEqual(sorted(procs), sorted(["sh", "ifconfig", "timeout", "iwgetid", "timeout", "dbus-send", "ps", "date",
                                                "df"]))
        # One df for /usr and the stick together.
        self.box.mount_stick()
        self.box.write("usr/etc/conf/web/tags", "0123456789ABCDEF\t1\t1\t0\t-1\tone\n")
        procs, body = self.box.started("status")
        self.assertEqual((json.loads(body)["nb_tags"], json.loads(body)["nb_sounds"]), ("1", "4"))
        self.assertEqual(sorted(procs), sorted(["sh", "ifconfig", "timeout", "iwgetid", "timeout", "dbus-send", "ps", "date",
                                                "df", "awk"]))
        # With the tag listener's state, the controller isn't asked.
        self.box.write("var/run/lapinlibre-web/controller", "16\n")
        procs, body = self.box.started("status")
        self.assertEqual(sorted(procs), sorted(["sh", "ifconfig", "timeout", "iwgetid", "ps", "date", "df", "awk"]))

    def test_reads(self):
        for path, most in (("voice_list", 1), ("get_version", 1), ("sound_list", 2), ("rfid_list", 1),
                           ("lapinlibre/logs", 3), ("tools_log", 3)):
            with self.subTest(path):
                procs, _ = self.box.started(path)
                self.assertLessEqual(len(procs), most, procs)

    def test_ssh_keys_are_read_once(self):
        k1, k2 = fake_key("one"), fake_key("two")
        self.box.write("usr/karotz/.ssh/authorized_keys", f"ssh-ed25519 {k1} me@pc\n")
        self.box.write("usr/etc/conf/dropbear/dropbear_ed25519_host_key",
                       f"Public key portion is:\nssh-ed25519 {k2} root@kitchen\nFingerprint: sha1!! aa:bb\n")
        # (What is kept must be newer than the key files, by the second.)
        time.sleep(1.1)
        first, body = self.box.started("lapinlibre/ssh")
        self.assertIn("dropbearkey", first)
        # Later requests take the fingerprints from RAM, until a key file changes.
        procs, again = self.box.started("lapinlibre/ssh")
        self.assertEqual(again, body)
        self.assertEqual(sorted(procs), ["ifconfig", "sh"])
        k3 = fake_key("three")
        self.box.write("usr/karotz/.ssh/authorized_keys", f"ssh-ed25519 {k3} other@pc\n")
        procs, changed = self.box.started("lapinlibre/ssh")
        self.assertIn(fingerprint(k3), json.loads(changed)["keys"][0]["fingerprint"])
        self.assertIn("dropbearkey", procs)


class PolicyTest(WebApiCase):
    """The checks every request passes, before anything is done."""

    def refused(self, path, status, word, **kw):
        got, headers, body = self.box.run(path, **kw)
        self.assertEqual(got, status, body)
        self.assertReplyHeaders(headers)
        self.assertError(json.loads(body), word)
        return headers

    def assertNotRebooted(self):
        time.sleep(0.3)
        self.assertNotIn("reboot", self.box.actions_log())

    def test_no_web_user(self):
        for path in CGIS:
            with self.subTest(path):
                self.refused(path, 403, "authenticated", user=None)
                self.refused(path, 403, "authenticated", user="root")
                self.refused(path, 403, "authenticated", user="")
        self.assertNotRebooted()
        log = self.box.syslog_lines()
        self.assertEqual(len(log), 3 * len(CGIS))
        self.assertTrue(all("refused" in line for line in log))

    def test_addresses(self):
        for addr in ("127.0.0.1", "10.0.0.1", "10.255.255.255", "172.16.0.1", "172.31.255.254", "192.168.0.1",
                     "192.168.255.255", "169.254.1.1"):
            with self.subTest(addr):
                self.assertEqual(self.json("get_version", addr=addr)["return"], "0")
        for addr in ("127.0.0.2", "127.1.0.1", "172.15.0.1", "172.32.0.1", "192.169.0.1", "192.167.0.1", "8.8.8.8",
                     "11.0.0.1", "169.253.1.1", "1.2.3.4", "", None, "::1", "::ffff:192.168.1.5", "10.0.0",
                     "10.0.0.256", "10.0.0.1.2", "10..0.1", ".10.0.0.1", "10.0.0.1 ", "10.0.0.1;x", "1000.0.0.1"):
            with self.subTest(addr):
                self.refused("get_version", 403, "local network", addr=addr)
        self.refused("reboot", 403, "local network", addr="8.8.8.8")
        self.assertNotRebooted()

    def test_cross_site_refused(self):
        for headers, host in (({"Sec-Fetch-Site": "cross-site"}, HOST), ({"Sec-Fetch-Site": "same-site"}, HOST),
                              ({"Sec-Fetch-Site": ""}, HOST), ({"Origin": "http://evil.example"}, HOST),
                              ({"Origin": "null"}, HOST), ({"Origin": ""}, HOST),
                              ({"Origin": f"http://{HOST}:8080"}, HOST), ({"Origin": f"https://{HOST}"}, HOST),
                              ({"Origin": f"http://{HOST}"}, None), ({"Origin": f"http://{HOST}"}, f"{HOST}:8080"),
                              ({"Referer": "http://evil.example/page"}, HOST),
                              ({"Referer": f"http://{HOST}@evil.example/"}, HOST),
                              ({"Referer": f"http://{HOST}.evil.example/"}, HOST),
                              ({"Referer": "javascript:x"}, HOST),
                              ({"Origin": f"http://{HOST}", "Referer": "http://evil.example/"}, HOST),
                              ({"Origin": f"http://{HOST}", "Sec-Fetch-Site": "cross-site"}, HOST)):
            with self.subTest(headers=headers, host=host):
                self.refused("reboot", 403, "cross-site", headers=headers, host=host)
        self.assertNotRebooted()
        log = self.box.syslog_lines()
        self.assertEqual(len(log), 16)
        self.assertTrue(all("reboot" in line and "cross-site" in line for line in log), log)

    def test_same_site_allowed(self):
        for headers, host in (({}, HOST), ({}, None), ({"Sec-Fetch-Site": "same-origin"}, HOST),
                              ({"Sec-Fetch-Site": "none"}, HOST), ({"Origin": f"http://{HOST}"}, HOST),
                              ({"Origin": f"http://{HOST}"}, f"{HOST}:80"), ({"Origin": "http://LapinLibre.Local"}, HOST),
                              ({"Origin": f"http://{HOST}:8080"}, f"{HOST}:8080"),
                              ({"Referer": f"http://{HOST}/"}, HOST), ({"Referer": f"http://{HOST}/x?y=1#z"}, HOST),
                              ({"Origin": f"http://{HOST}", "Referer": f"http://{HOST}/", "Sec-Fetch-Site": "same-origin"}, HOST)):
            with self.subTest(headers=headers, host=host):
                self.assertEqual(self.json("reboot", headers=headers, host=host), {"return": "0"})

    def test_reads_skip_the_cross_site_rule(self):
        for path in READ_ONLY:
            with self.subTest(path):
                status, _, _ = self.box.run(path, headers={"Origin": "http://evil.example", "Sec-Fetch-Site": "cross-site"})
                # snapshot_get without a stored snapshot: not found, past the checks.
                self.assertEqual(status, 404 if path == "snapshot_get" else 200)

    def test_methods(self):
        for method in ("PUT", "DELETE", "OPTIONS", "PATCH", "TRACE", "get"):
            with self.subTest(method):
                headers = self.refused("get_version", 405, "GET", method=method)
                self.assertEqual(headers.get("allow"), "GET, HEAD, POST")
                self.refused("reboot", 405, "GET", method=method)
        self.assertNotRebooted()

    def test_sizes(self):
        long_ok = "lines=5&x=" + "a" * (4096 - len("lines=5&x="))
        self.assertEqual(len(self.box.run("lapinlibre/logs", long_ok)[2].decode().splitlines()), 5)
        self.refused("lapinlibre/logs", 414, "4096", query=long_ok + "a")
        # Multi-byte characters count as bytes.
        self.refused("lapinlibre/logs", 414, "4096", query="x=" + "\u00e9" * 2048)
        self.refused("lapinlibre/logs", 413, "4096", method="POST", body="x=" + "a" * 4095)
        self.refused("reboot", 413, "4096", method="POST", body="x=" + "a" * 5000)
        self.refused("reboot", 414, "4096", query="x=" + "a" * 5000)
        self.assertNotRebooted()

    def test_order_of_checks(self):
        # No web user comes before everything else; then the address, the cross-site rule, the method.
        self.refused("reboot", 403, "authenticated", user=None, addr="8.8.8.8", method="PUT", headers={"Origin": "http://e.x"})
        self.refused("reboot", 403, "local network", addr="8.8.8.8", method="PUT", headers={"Origin": "http://e.x"})
        self.refused("reboot", 403, "cross-site", method="PUT", headers={"Origin": "http://e.x"})
        self.refused("reboot", 405, "GET", method="PUT")
        self.assertNotRebooted()


class ParameterTest(WebApiCase):
    """The shared decoding, through a test program that reports what param saw."""

    def setUp(self):
        super().setUp()
        self.box.write("usr/www/cgi-bin/echo",
                       "#!/bin/sh\n"
                       f". {self.sb}/usr/share/lapinlibre/web.sh\n"
                       'WEB_SPACE_PARAMS="text"\n'
                       "web_begin read\n"
                       "jo_begin\n"
                       "for n in a b text upper_x n2; do param \"$n\"; s=$?; jo_str \"$n\" \"$s:$PV\"; done\n"
                       'reply_json "{$JO}"\n', mode=0o755)

    def decoded(self, query="", **kw):
        status, _, body = self.box.run("echo", query, **kw)
        self.assertEqual(status, 200, body)
        return json.loads(body.decode("utf-8", errors="surrogateescape"))

    def test_decoding(self):
        got = self.decoded("a=hello+world%21&b=%41%62%2b%25&text=caf%C3%A9")
        self.assertEqual((got["a"], got["b"], got["text"]), ("0:hello world!", "0:Ab+%", "0:caf\u00e9"))
        got = self.decoded("a=&b")
        self.assertEqual((got["a"], got["b"], got["n2"]), ("0:", "0:", "1:"))
        # A body: only its length is read, and line ends at its end don't count.
        got = self.decoded(method="POST", body="a=1&b=2\n\n")
        self.assertEqual((got["a"], got["b"]), ("0:1", "0:2"))
        got = self.decoded(method="POST", body="a=1%41&text=x\ny&b=3", env={"CONTENT_LENGTH": "13"})
        self.assertEqual((got["a"], got["text"], got["b"]), ("0:1A", "0:x", "1:"))

    def test_first_value_counts_and_odd_names_are_ignored(self):
        got = self.decoded("a=1&a=2&B=3&b=4&upper_X=5&" + "n" * 33 + "=6&n2=7&n-2=8&=9&&a%62=10")
        self.assertEqual((got["a"], got["b"], got["upper_x"], got["n2"]), ("0:1", "0:4", "1:", "0:7"))

    def test_control_characters_and_broken_escapes(self):
        for value in ("%00", "x%0ay", "%0d", "%7f", "%1b[31m", "%", "%4", "%zz", "%%41", "a%0", "\x01", "x\ty"):
            with self.subTest(value):
                self.assertEqual(self.decoded("a=" + value)["a"], "2:")
        # Only the parameters that allow it turn tabs and line ends into spaces.
        self.assertEqual(self.decoded("text=one%09two%0d%0athree")["text"], "0:one two  three")
        self.assertEqual(self.decoded("text=x%00")["text"], "2:")
        # A body with a raw line end.
        self.assertEqual(self.decoded(method="POST", body="a=x\ny")["a"], "2:")

    def test_hostile_values_are_only_data(self):
        marker = self.sb / "pwned"
        for value in HOSTILE:
            v = value.format(m=marker)
            if len(v) > 4000:
                continue
            with self.subTest(v[:40]):
                got = self.decoded("a=" + urllib.request.quote(v, safe="%"))
                status = got["a"][0]
                if "%" not in value and "\n" not in v:
                    self.assertEqual(got["a"], "0:" + v)
                else:
                    self.assertIn(status, "02")
        self.assertFalse(marker.exists())

    def test_hostile_values_for_lines(self):
        marker = self.sb / "pwned"
        for value in HOSTILE:
            v = value.format(m=marker)
            with self.subTest(v[:40]):
                status, _, body = self.box.run("lapinlibre/logs", "lines=" + urllib.request.quote(v, safe="%"))
                if status == 414:
                    continue
                reply = json.loads(body)
                self.assertError(reply, "lines")
                # The bad value is never echoed back.
                self.assertNotIn("touch", reply["msg"])
                self.assertNotIn(str(marker), reply["msg"])
        self.assertFalse(marker.exists())


ID = r"string:webapi:[0-9a-f]{16}"
SNAPSHOT = r"snapshot_\d{4}(_\d\d){5}"


class HardwareCase(WebApiCase):
    """Helpers for the endpoints that drive the daemons."""

    def ok(self, path, query="", **kw):
        reply = self.json(path, query=query, **kw)
        self.assertEqual(reply.get("return"), "0", reply)
        return reply

    def refused(self, path, query, word, **kw):
        reply = self.json(path, query=query, **kw)
        self.assertError(reply, word)
        return reply

    def calls(self):
        calls = self.box.dbus_calls()
        for _, _, args in calls:
            if args:
                self.assertRegex(args[0], "^" + ID + "$")
        return [(d, m, args[1:]) for d, m, args in calls]

    def clear(self):
        self.box.actions.unlink(missing_ok=True)
        self.box.syslog.unlink(missing_ok=True)

    def state(self, name):
        path = self.sb / "var/run/lapinlibre-web" / name
        return path.read_text() if path.exists() else None


class LedTest(HardwareCase):
    def test_light(self):
        self.assertEqual(self.ok("leds"), {"color": "00FF00", "secondary_color": "000000", "pulse": "0", "no_memory": "0",
                                           "speed": "700", "return": "0"})
        self.assertEqual(self.calls(), [("Led", "light", ["string:00FF00"])])
        self.clear()
        reply = self.ok("leds", "color=ff0000")
        self.assertEqual(reply["color"], "FF0000")
        self.assertEqual(self.calls(), [("Led", "light", ["string:FF0000"])])
        self.assertEqual(self.state("led"), "FF0000 0 000000 700\n")
        status = self.json("status")
        self.assertEqual((status["led_color"], status["led_pulse"]), ("FF0000", "0"))
        # Without a colour, the API's last one.
        self.clear()
        self.assertEqual(self.ok("leds")["color"], "FF0000")
        self.assertEqual(self.ok("leds", "color=%23000000")["color"], "000000")
        self.assertEqual(self.box.syslog_lines(), [f"-t lapinlibre-web leds {CLIENT} ok"] * 2)

    def test_pulse_and_nomemory(self):
        reply = self.ok("leds", "color=%23ffcc00&pulse=1&color2=00Ff00&speed=0300&blink=0")
        self.assertEqual(reply, {"color": "FFCC00", "secondary_color": "00FF00", "pulse": "1", "no_memory": "0",
                                 "speed": "300", "return": "0"})
        self.assertEqual(self.calls(), [("Led", "pulse", ["string:FFCC00", "string:00FF00", "int32:300", "int32:-1"])])
        self.assertEqual(self.state("led"), "FFCC00 1 00FF00 300\n")
        self.assertEqual(self.json("status")["led_pulse"], "1")
        self.ok("leds", "color=ff0000&nomemory=1")
        self.assertEqual(self.state("led"), "FFCC00 1 00FF00 300\n")
        # blink alone is ignored.
        self.ok("leds", "color=00ff00&blink=1")

    def test_bad_values(self):
        for query, word in (("color=red", "color"), ("color=12345", "color"), ("color=1234567", "color"),
                            ("color=%23%23123456", "color"), ("color=12345g", "color"), ("color=%0a123456", "color"),
                            ("color2=xyz&pulse=1", "color2"), ("speed=24", "speed"), ("speed=10001", "speed"),
                            ("speed=1e3", "speed"), ("pulse=2", "pulse"), ("pulse=yes", "pulse"), ("nomemory=-1", "nomemory"),
                            ("pulse=1&blink=1", "blink"), ("color=$(reboot)", "color")):
            with self.subTest(query):
                self.refused("leds", query, word)
        self.assertEqual(self.calls(), [])
        self.assertIsNone(self.state("led"))

    def test_daemon_answers(self):
        self.box.daemon("Led", "false")
        self.refused("leds", "color=ff0000", "refused")
        self.assertIsNone(self.state("led"))
        self.box.daemon("Led", "missing")
        self.refused("leds", "", "led-daemon isn't running")
        self.box.daemon("Led", "noreply")
        self.refused("leds", "", "busy")
        self.assertEqual(len(self.box.syslog_lines()), 3)

    def test_led_tool_is_stopped_first(self):
        # A refused setting leaves the next request to look again.
        self.box.processes(["/karotz/bin/led -l 00FFFF -p 000000 -d 400", "/usr/karotz/bin/led-daemon"])
        self.box.daemon("Led", "false")
        self.refused("leds", "color=00ff00", "refused")
        self.assertIn("killall led", self.box.actions_log())
        self.clear()
        (self.sb / "dbus/Led").unlink()
        self.ok("leds", "color=00ff00")
        log = self.box.actions_log()
        self.assertIn("killall led", log)
        self.assertLess(log.index("killall led"), log.index("Led"))
        self.clear()
        self.box.processes(["/usr/karotz/bin/led-daemon"])
        self.ok("leds", "color=00ff00")
        self.assertNotIn("killall", self.box.actions_log())
        # Once led-daemon has taken a setting, the tool isn't looked for again until the next boot
        # (the start-up stops it itself once led-daemon runs).
        self.assertTrue((self.sb / "var/run/lapinlibre-web.led-checked").exists())
        self.box.processes(["/karotz/bin/led -l 00FFFF", "/usr/karotz/bin/led-daemon"])
        self.ok("leds", "color=00ff00")
        self.assertNotIn("killall", self.box.actions_log())

    def test_no_led_tool(self):
        self.box.processes(["/usr/karotz/bin/led-daemon"])
        self.ok("leds", "color=00ff00")
        self.assertNotIn("killall", self.box.actions_log())
        self.assertTrue((self.sb / "var/run/lapinlibre-web.led-checked").exists())


class EarsTest(HardwareCase):
    def test_move_after_reset(self):
        self.assertEqual(self.ok("ears", "left=5&right=20"), {"left": "5", "right": "20", "return": "0"})
        self.assertEqual(self.calls(), [("Ears", "reset", ["string:B"]),
                                        ("Ears", "move", ["int32:5", "int32:100", "int32:3", "int32:100"])])
        # The reset's end was waited for with a watch on its signal, started before the reset.
        log = self.box.actions_log()
        self.assertIn("dbus-monitor --system type='signal',sender='com.mindscape.karotz.Ears',"
                      "interface='com.mindscape.karotz.KarotzInterface',member='reset_finished'", log)
        self.assertLess(log.index("dbus-monitor"), log.index("KarotzInterface.reset\x1f"))
        # Positions are taken modulo 17.
        self.clear()
        self.assertEqual(self.ok("ears", "left=255&right=0&noreset=1"), {"left": "255", "right": "0", "return": "0"})
        self.assertEqual(self.calls(), [("Ears", "move", ["int32:0", "int32:100", "int32:0", "int32:100"])])
        self.assertFalse(list((self.sb / "var/run/lapinlibre-web").glob("signals.*")))

    def test_reset_that_does_not_end(self):
        (self.sb / "dbus/nosignal").write_text("")
        start = time.monotonic()
        self.ok("ears", "left=1&right=2")
        self.assertGreater(time.monotonic() - start, 9)
        self.assertEqual([m for _, m, _ in self.calls()], ["reset", "move"])
        self.assertTrue(any("didn't end within 10 s" in line for line in self.box.syslog_lines()))

    def test_bad_values(self):
        for query, word in (("right=1", "left"), ("left=1", "right"), ("left=&right=1", "left"), ("left=256&right=1", "left"),
                            ("left=1&right=-1", "right"), ("left=1.5&right=1", "left"), ("left=1&right=1&noreset=x", "noreset"),
                            ("left=%241&right=1", "left")):
            with self.subTest(query):
                self.refused("ears", query, word)
        self.assertEqual(self.calls(), [])

    def test_reset_and_random(self):
        self.assertEqual(self.ok("ears_reset"), {"return": "0"})
        self.assertEqual(self.calls(), [("Ears", "reset", ["string:B"])])
        self.assertNotIn("dbus-monitor", self.box.actions_log())
        for noreset in ("0", "1"):
            self.clear()
            reply = self.ok("ears_random", "noreset=" + noreset)
            self.assertEqual(list(reply), ["left", "right", "return"])
            left, right = int(reply["left"]), int(reply["right"])
            self.assertTrue(0 <= left <= 16 and 0 <= right <= 16, reply)
            moves = [("Ears", "move", [f"int32:{left}", "int32:100", f"int32:{right}", "int32:100"])]
            self.assertEqual(self.calls(), ([("Ears", "reset", ["string:B"])] if noreset == "0" else []) + moves)

    def test_daemon_answers(self):
        self.box.daemon("Ears", "missing")
        self.refused("ears", "left=1&right=1", "ears-daemon isn't running")
        self.refused("ears_reset", "", "ears-daemon isn't running")
        self.box.daemon("Ears.move", "false")
        (self.sb / "dbus/Ears").unlink()
        self.refused("ears", "left=1&right=1", "refused to move")

    def test_mode(self):
        data = self.sb / "usr/etc/conf/web"
        self.assertEqual(self.ok("ears_mode", "disable=1"), {"return": "0", "disabled": "1"})
        self.assertEqual(self.calls(), [])
        self.assertEqual(data.stat().st_mode & 0o777, 0o700)
        self.assertEqual((data / "ears_disabled").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.json("status")["ears_disabled"], "1")
        for path, query in (("ears", "left=1&right=1"), ("ears_reset", ""), ("ears_random", "")):
            self.refused(path, query, "disabled")
        # Written only when it changes.
        mtime = (data / "ears_disabled").stat().st_mtime_ns
        time.sleep(0.05)
        self.ok("ears_mode", "disable=1")
        self.assertEqual((data / "ears_disabled").stat().st_mtime_ns, mtime)
        # Sleep and wake up leave the ears alone while disabled.
        self.clear()
        self.ok("sleep")
        self.ok("wakeup", "silent=1")
        self.assertNotIn("Ears", [d for d, _, _ in self.calls()])
        # Anything but 1 turns them back on, and recalibrates them.
        self.clear()
        self.assertEqual(self.ok("ears_mode", "disable=yes"), {"return": "0", "disabled": "0"})
        self.assertFalse((data / "ears_disabled").exists())
        self.assertEqual(self.calls(), [("Ears", "reset", ["string:B"])])
        self.ok("ears", "left=1&right=1")

    def test_mode_while_asleep_or_busy(self):
        self.ok("ears_mode", "disable=1")
        self.ok("sleep")
        self.clear()
        self.assertEqual(self.ok("ears_mode")["disabled"], "0")
        self.assertEqual(self.calls(), [])
        self.ok("wakeup", "silent=1")
        self.ok("ears_mode", "disable=1")
        self.box.write("controller.state", "21\n")
        self.clear()
        self.assertEqual(self.ok("ears_mode", "disable=0")["disabled"], "0")
        self.assertEqual(self.calls(), [])


class SleepTest(HardwareCase):
    def test_sleep_and_wake(self):
        before = int(time.time())
        self.assertEqual(self.ok("sleep"), {"return": "0"})
        self.assertEqual(self.calls(), [("Multimedia", "stop", ["int32:0"]), ("Voice", "tts_stop", ["int32:0"]),
                                        ("Ears", "reset", ["string:B"]),
                                        ("Ears", "move", ["int32:4", "int32:100", "int32:4", "int32:100"]),
                                        ("Led", "light", ["string:000000"])])
        status = self.json("status")
        self.assertEqual(status["sleep"], "1")
        self.assertTrue(before <= int(status["sleep_time"]) <= time.time() + 1, status["sleep_time"])
        self.refused("sleep", "", "already asleep")
        self.clear()
        self.assertEqual(self.ok("wakeup"), {"return": "0", "silent": "0"})
        wakeup = f"string:{self.sb}/usr/karotz/sounds/wakeup.mp3"
        self.assertEqual(self.calls(), [("Led", "pulse", ["string:00FF00", "string:000000", "int32:700", "int32:-1"]),
                                        ("Ears", "reset", ["string:B"]), ("Multimedia", "play", [wakeup, "int32:1"])])
        status = self.json("status")
        self.assertEqual((status["sleep"], status["sleep_time"], status["led_color"], status["led_pulse"]),
                         ("0", "0", "00FF00", "1"))
        # Awake already, and silent.
        self.clear()
        self.assertEqual(self.ok("wakeup", "silent=1"), {"return": "0", "silent": "1"})
        self.assertNotIn("Multimedia", [d for d, _, _ in self.calls()])
        self.refused("wakeup", "silent=2", "silent")
        self.assertEqual(self.box.syslog_lines()[-1], f"-t lapinlibre-web wakeup {CLIENT} parameter silent must be 0 or 1")

    def test_what_sleeping_refuses(self):
        self.ok("sleep")
        self.clear()
        for path, query in (("leds", "color=ff0000"), ("ears", "left=1&right=1"), ("ears_reset", ""), ("ears_random", ""),
                            ("sound", "id=test-tone"), ("sound_control", "cmd=stop"), ("tts", "text=hello")):
            with self.subTest(path):
                self.refused(path, query, "asleep")
        self.assertEqual(self.calls(), [])
        # What still works.
        self.assertEqual(self.ok("snapshot")["return"], "0")
        self.assertEqual(self.calls()[-1], ("Led", "light", ["string:000000"]))
        for path in READ_ONLY:
            self.box.run(path)
        self.ok("ears_mode", "disable=1")
        self.ok("setclock")
        self.ok("reboot")

    def test_controller_asleep(self):
        self.box.write("controller.state", "24\n")
        self.refused("leds", "color=ff0000", "asleep")
        self.refused("sleep", "", "already asleep")
        # The API's wake-up clears its own flag only.
        self.ok("wakeup", "silent=1")
        self.assertEqual(self.json("status")["sleep"], "1")

    def test_failures_dont_stop_sleep(self):
        for name in ("Multimedia", "Voice", "Ears", "Led"):
            self.box.daemon(name, "missing")
        self.ok("sleep")
        self.assertEqual(self.json("status")["sleep"], "1")
        self.ok("wakeup")
        self.assertEqual(self.json("status")["sleep"], "0")


class CoexistenceTest(HardwareCase):
    """The controller's interactive mode, the lock, missing daemons, and what is never called."""

    ACTIONS = (("leds", "color=ff0000"), ("ears", "left=1&right=1"), ("ears_reset", ""), ("ears_random", ""),
               ("sound", "id=test-tone"), ("sound_control", "cmd=pause"), ("tts", "text=hello"), ("snapshot", ""),
               ("snapshot_view", ""))

    def test_interactive_application(self):
        for state in ("21", "22"):
            self.box.write("controller.state", state + "\n")
            for path, query in self.ACTIONS:
                with self.subTest(state=state, path=path):
                    self.refused(path, query, "application")
            self.assertEqual(self.calls(), [])
            # Reads, sleep, wake-up and reboot still work.
            self.ok("status")
            self.ok("sleep")
            self.ok("wakeup", "silent=1")
            self.ok("reboot")
            self.clear()

    def test_works_without_controller(self):
        (self.sb / "controller.state").unlink()
        for path, query in self.ACTIONS:
            with self.subTest(path):
                status, headers, body = self.box.run(path, query)
                self.assertEqual(status, 200)
                if path != "snapshot_view":
                    self.assertEqual(json.loads(body)["return"], "0", body)
        self.assertNotIn("take_control", self.box.actions_log())
        self.assertNotIn("release_control", self.box.actions_log())

    def test_busy_lock(self):
        import fcntl
        lock = self.sb / "var/lock/lapinlibre-web.lock"
        with open(lock, "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            start = time.monotonic()
            self.refused("leds", "color=ff0000", "busy")
            self.assertLess(time.monotonic() - start, 20)
            self.refused("clear_snapshots", "", "busy")
            # Reads don't take it.
            self.ok("status")
            self.ok("sound_list")
        self.assertEqual(self.calls(), [])
        self.ok("leds", "color=ff0000")

    def test_daemons_missing(self):
        for name in ("Led", "Ears", "Multimedia", "Voice", "Webcam"):
            self.box.daemon(name, "missing")
        for path, query, daemon in (("leds", "", "led-daemon"), ("ears", "left=1&right=1", "ears-daemon"),
                                    ("ears_reset", "", "ears-daemon"), ("sound", "id=test-tone", "multimedia-daemon"),
                                    ("sound_control", "cmd=stop", "multimedia-daemon"), ("tts", "text=hi", "voice-daemon"),
                                    ("snapshot", "", "webcam-daemon"), ("snapshot_view", "", "webcam-daemon")):
            with self.subTest(path):
                reply = self.refused(path, query, daemon)
                self.assertIn("isn't running", reply["msg"])

    def test_cross_site_changes_refused(self):
        for path in HARDWARE + CHANGES:
            with self.subTest(path):
                status, _, body = self.box.run(path, "color=ff0000&left=1&right=1&id=test-tone&cmd=stop&text=hi",
                                               headers={"Sec-Fetch-Site": "cross-site"})
                self.assertEqual(status, 403)
                self.assertIn("cross-site", json.loads(body)["msg"])
        self.assertEqual(self.calls(), [])
        self.assertNotIn("reboot", self.box.actions_log())

    def test_hostile_values_reach_no_daemon(self):
        marker = self.sb / "pwned"
        for value in HOSTILE:
            v = value.format(m=marker)
            q = urllib.request.quote(v, safe="%")
            for path, query in (("leds", f"color={q}"), ("leds", f"speed={q}"), ("ears", f"left={q}&right=1"),
                                ("sound", f"id={q}"), ("sound", f"url=http://h/{q}"), ("sound_control", f"cmd={q}"),
                                ("tts", f"text=hi&voice={q}"), ("ears_random", f"noreset={q}")):
                with self.subTest(path=path, value=v[:30]):
                    status, _, body = self.box.run(path, query)
                    if status == 414:
                        continue
                    reply = json.loads(body)
                    if reply["return"] == "0":
                        # Only a sound URL made of URL characters alone gets through.
                        self.assertEqual(path, "sound", (query, reply))
                    else:
                        self.assertNotIn("touch", reply["msg"])
        self.assertFalse(marker.exists())
        for _, method, args in self.calls():
            if method == "play":
                self.assertRegex(args[0], r"^string:http://h/[-0-9a-z./]+$")
                continue
            for arg in args:
                self.assertNotRegex(arg, r"[`$;|'\"<>\\]|\.\./")

    def test_routine_commands_write_nothing_under_usr(self):
        def snapshot():
            return sorted((str(p.relative_to(self.sb)), p.stat().st_mtime_ns) for p in (self.sb / "usr").rglob("*"))
        before = snapshot()
        for path, query in (("leds", "color=ff0000"), ("ears", "left=1&right=1"), ("sound", "id=test-tone"),
                            ("sound_control", "cmd=pause"), ("tts", "text=hello"), ("snapshot", ""), ("snapshot_view", ""),
                            ("sleep", ""), ("wakeup", ""), ("clear_snapshots", ""), ("clear_cache", ""), ("setclock", "")):
            self.ok(path, query) if path != "snapshot_view" else self.box.run(path)
        self.assertEqual(snapshot(), before)


class SoundTest(HardwareCase):
    def test_play_by_name(self):
        self.assertEqual(self.ok("sound", "id=test-tone"), {"return": "0"})
        self.assertEqual(self.calls(), [("Multimedia", "stop", ["int32:1"]),
                                        ("Multimedia", "play", [f"string:{self.sb}/usr/karotz/sounds/test-tone.wav", "int32:1"])])
        self.clear()
        self.ok("sound", "id=wakeup")
        self.assertEqual(self.calls()[-1][2][0], f"string:{self.sb}/usr/karotz/sounds/wakeup.mp3")
        # The stick's sounds come first.
        self.box.mount_stick()
        self.clear()
        self.ok("sound", "id=test-tone")
        self.assertEqual(self.calls()[-1][2][0], f"string:{self.sb}/mnt/usbkey/sounds/test-tone.mp3")
        self.ok("sound", "id=ding")
        self.assertEqual(self.box.syslog_lines(), [f"-t lapinlibre-web sound {CLIENT} ok"] * 2)

    def test_play_url(self):
        for url in ("http://radio.example:8000/stream.mp3?x=1&y=%20z", "https://10.0.0.1/a/b.m3u", "http://h",
                    "http://h.example/a(b)!*,;=+$@~[]#f"):
            with self.subTest(url):
                self.clear()
                reply = self.ok("sound", "url=" + urllib.request.quote(url, safe=""))
                self.assertEqual(reply, {"return": "0", "url": url})
                self.assertEqual(self.calls()[-1], ("Multimedia", "play", ["string:" + url, "int32:1"]))

    def test_bad_requests(self):
        for query, word in (("", "give id"), ("id=test-tone&url=http%3A%2F%2Fh", "not both"), ("id=nope", "nope"),
                            ("id=bad+name", "parameter id"), ("id=..%2Fready", "parameter id"), ("id=" + "a" * 65, "parameter id"),
                            ("id=%00", "parameter id"), ("url=%0a", "parameter url")):
            with self.subTest(query):
                self.refused("sound", query, word)
        for url in ("ftp://h/x", "http://", "http:/h", "file:///etc/passwd", "/usr/karotz/sounds/test-tone.wav", "http://h/a b",
                    "http://h/a'b", 'http://h/a"b', "http://h/::x", "http://h/a::b", "http://h:0/", "http://h:65536/",
                    "http://h:/", "http://h/<x>", "http://h/{x}", "http://h/a|b", "http://h/^", "http://h/`x`",
                    "http://h/%zz", "http://h/%4", "http://h/a\\b", "javascript:alert(1)", "http://h/" + "a" * 1100,
                    "http://h_x/", "http://h/caf\u00e9", "dir::music", "http://h/\x7f"):
            with self.subTest(url):
                self.refused("sound", "url=" + urllib.request.quote(url, safe=""), "url")
        self.assertEqual(self.calls(), [])

    def test_daemon_refuses(self):
        self.box.daemon("Multimedia.play", "false")
        self.refused("sound", "id=test-tone", "music")

    def test_control(self):
        for cmd, method, state in (("pause", "pause", "paused"), ("pause", "resume", "playing"), ("PAUSE", "pause", "paused"),
                                   ("resume", "resume", "playing"), ("next", "next", "playing"),
                                   ("previous", "previous", "playing"), ("stop", "stop", None), ("quit", "stop", None)):
            with self.subTest(cmd):
                self.clear()
                self.assertEqual(self.ok("sound_control", "cmd=" + cmd), {"return": "0", "cmd": cmd.lower()})
                self.assertEqual(self.calls(), [("Multimedia", method, ["int32:1"])])
                self.assertEqual(self.state("playback"), state and state + "\n")
                if state is None:
                    self.box.write("var/run/lapinlibre-web/playback", "playing\n")
        for query in ("", "cmd=", "cmd=volume", "cmd=loadfile+x", "cmd=stop%0a"):
            with self.subTest(query):
                self.refused("sound_control", query, "cmd")
        self.box.daemon("Multimedia", "false")
        self.refused("sound_control", "cmd=next", "couldn't next")

    def test_list(self):
        self.assertEqual(self.json("sound_list"), {"sounds": [{"id": "button-click"}, {"id": "test-tone"},
                                                              {"id": "wakeup"}], "return": "0"})
        self.box.mount_stick()
        self.assertEqual([s["id"] for s in self.json("sound_list")["sounds"]], ["button-click", "ding", "test-tone", "wakeup"])


class SpeechTest(HardwareCase):
    def speak(self, query):
        reply = self.ok("tts", query)
        self.assertEqual(list(reply), ["return", "id", "played", "cache", "voicelanguage", "voicegender"])
        self.assertRegex(reply["id"], r"^[0-9a-f]{32}$")
        self.assertIs(reply["cache"], False)
        return reply

    def spoken(self):
        calls = [c for c in self.calls() if c[1] == "tts_speak"]
        return calls[-1][2] if calls else None

    def test_speak(self):
        reply = self.speak("text=Hello+there")
        self.assertEqual((reply["played"], reply["voicelanguage"], reply["voicegender"]), (True, "en-GB", "female"))
        self.assertEqual(self.spoken(), ["string:Hello there", "string:en-GB", "int32:1"])
        self.assertEqual(self.speak("text=Hello+there")["id"], reply["id"])
        self.assertNotEqual(self.speak("text=Hello")["id"], reply["id"])
        # The log has the length, never the text.
        log = "\n".join(self.box.syslog_lines())
        self.assertIn(f"tts {CLIENT} ok, 11 bytes of text", log)
        self.assertNotIn("Hello", log)

    def test_voices(self):
        for voice, lang, gender in (("7", "en-GB", "male"), ("8", "en-GB", "female"), ("1", "fr", "male"),
                                    ("04", "fr-CA", "female"), ("5", "en-US", "male"), ("36", "en-AU", "female"),
                                    ("9", "en-GB", "male"), ("88", "en-GB", "female"), ("fr", "fr", "female"),
                                    ("en-US", "en-US", "female"), ("de", "en-GB", "female"), ("claire", "en-GB", "female"),
                                    ("", "en-GB", "female")):
            with self.subTest(voice):
                reply = self.speak("text=hi&voice=" + voice)
                self.assertEqual((reply["voicelanguage"], reply["voicegender"]), (lang, gender))
                self.assertEqual(self.spoken()[1], "string:" + lang)
        for voice in ("0", "89", "100", "a1", "x" * 33, "en_GB", "EN-gb-x", "..%2Ffr", "%00", "-1"):
            with self.subTest(voice):
                self.refused("tts", "text=hi&voice=" + voice, "voice")

    def test_text_handling(self):
        for text, said in (('"Bonjour"', "Bonjour"), ("a%09b%0Ac%0D%0Ad", "a b c  d"), ("<b>hi</b>", "bhi/b"),
                           ("++pad++", "pad"), ("caf%C3%A9+%E2%82%AC", "caf\u00e9 \u20ac"), ("%24(touch+x)%3B%60id%60", "$(touch x);`id`"),
                           ("a" * 1000, "a" * 1000), ("\u00e9" * 500, "\u00e9" * 500)):
            with self.subTest(text[:20]):
                self.speak("text=" + urllib.request.quote(text, safe="%+<>/\"") if "%" not in text else "text=" + text)
                self.assertEqual(self.spoken()[0], "string:" + said)
        self.assertFalse((self.sb / "x").exists())
        for query, word in (("", "required"), ("text=", "empty"), ("text=%22%22", "empty"), ("text=%3C%3E", "empty"),
                            ("text=+++", "empty"), ("text=" + "a" * 1001, "1000 bytes"), ("text=" + "%C3%A9" * 501, "1000 bytes"),
                            ("text=%ff%fe", "UTF-8"), ("text=%C3", "UTF-8"), ("text=a%00b", "control"), ("text=a%1bb", "control"),
                            ("text=hi&mute=2", "mute"), ("text=hi&nocache=x", "nocache")):
            with self.subTest(query[:30]):
                self.refused("tts", query, word)

    def test_mute(self):
        reply = self.speak("text=hi&mute=1&nocache=1")
        self.assertIs(reply["played"], False)
        self.assertIsNone(self.spoken())

    def test_daemon_refuses(self):
        self.box.daemon("Voice", "false")
        self.refused("tts", "text=hi", "already speaking")
        # No server for the language, nor for the rabbit's own.
        self.box.write("usr/etc/conf/voice.conf", "[tts]\nfr=http://192.168.1.10:8081/fr\n")
        reply = self.refused("tts", "text=hi&voice=de", "no speech server")
        self.assertIn("en-GB", reply["msg"])

    def test_voice_list(self):
        reply = self.json("voice_list")
        self.assertEqual(list(reply), ["voices", "return"])
        self.assertEqual([v["id"] for v in reply["voices"]], ["1", "2", "3", "4", "5", "6", "7", "8", "35", "36"])
        self.assertEqual(reply["voices"][0], {"id": "1", "lang": "French, male"})
        self.assertEqual(reply["voices"][7], {"id": "8", "lang": "English (UK), female"})
        self.box.write("usr/etc/conf/voice.conf", "[tts]\r\nDE=http://x/de\r\n[asr]\r\nen=http://x/en\r\n")
        self.assertEqual(self.json("voice_list")["voices"], [{"id": "9", "lang": "German, male"},
                                                             {"id": "10", "lang": "German, female"}])
        (self.sb / "usr/etc/conf/voice.conf").unlink()
        self.assertEqual(self.json("voice_list")["voices"], [])

    def test_cache(self):
        self.assertEqual(self.json("display_cache"), {"count": "0", "return": "0"})
        reply = self.ok("clear_cache", "id=123")
        self.assertEqual(list(reply), ["return", "msg"])
        self.assertEqual(self.calls(), [])


class CameraTest(HardwareCase):
    def stored(self):
        return sorted(p.name for p in (self.sb / "var/run/lapinlibre-web/snapshots").glob("*"))

    def test_snapshot(self):
        self.box.write("tmp/picture.jpg", b"stale")
        reply = self.ok("snapshot", "silent=yes")
        self.assertEqual(list(reply), ["filename", "thumb", "return"])
        self.assertRegex(reply["filename"], f"^{SNAPSHOT}\\.jpg$")
        self.assertEqual(reply["thumb"], reply["filename"][:-4] + ".thumb.gif")
        self.assertEqual(self.calls(), [("Webcam", "takeSnapshot", ["string:file", "int32:1"]),
                                        ("Led", "light", ["string:00FF00"])])
        self.assertIn("djpeg -scale 1/4 -gif -outfile", self.box.actions_log())
        self.assertEqual(self.stored(), [reply["filename"], reply["thumb"]])
        self.assertFalse((self.sb / "tmp/picture.jpg").exists())
        self.assertEqual(self.json("snapshot_list"), {"snapshots": [{"id": reply["filename"]}], "return": "0"})
        # The API's LED state comes back after the camera's flash.
        self.ok("leds", "color=ffcc00&pulse=1&speed=500")
        self.clear()
        second = self.ok("snapshot")
        self.assertNotEqual(second["filename"], reply["filename"])
        self.assertEqual(self.calls()[-1], ("Led", "pulse", ["string:FFCC00", "string:000000", "int32:500", "int32:-1"]))

    def test_get_and_clear(self):
        name = self.ok("snapshot")["filename"]
        thumb = name[:-4] + ".thumb.gif"
        status, headers, body = self.box.run("snapshot_get", "filename=" + name)
        self.assertEqual((status, headers["content-type"], headers["cache-control"], body),
                         (200, "image/jpeg", "private, no-store", JPEG))
        status, headers, body = self.box.run("snapshot_get", "filename=" + thumb)
        self.assertEqual((status, headers["content-type"], body), (200, "image/gif", b"GIF89a thumb"))
        self.assertEqual(self.box.run("snapshot_get", "filename=" + name, method="HEAD")[2], b"")
        for bad in ("", "../led", "snapshot_2026.jpg", name.replace(".jpg", ".png"), "snapshot_2000_01_01_00_00_00.jpg",
                    "x" + name, name + "%00", "%2F" + name, "snapshots"):
            with self.subTest(bad):
                status, headers, body = self.box.run("snapshot_get", "filename=" + bad)
                self.assertEqual(status, 404)
                self.assertReplyHeaders(headers)
                self.assertError(json.loads(body), "snapshot")
        self.assertEqual(self.ok("clear_snapshots"), {"return": "0"})
        self.assertEqual(self.stored(), [])
        self.assertEqual(self.json("snapshot_list")["snapshots"], [])

    def test_delete_one(self):
        snapshots = self.sb / "var/run/lapinlibre-web/snapshots"
        snapshots.mkdir(parents=True)
        names = ["snapshot_2020_01_01_00_00_01", "snapshot_2020_01_01_00_00_02"]
        for name in names:
            (snapshots / (name + ".jpg")).write_bytes(JPEG)
            (snapshots / (name + ".thumb.gif")).write_bytes(b"GIF")
        first = names[0] + ".jpg"
        self.assertEqual(self.ok("lapinlibre/snapshot_delete", "filename=" + first), {"return": "0", "filename": first})
        self.assertEqual(self.stored(), [names[1] + ".jpg", names[1] + ".thumb.gif"])
        self.assertEqual(self.box.syslog_lines()[-1], f"-t lapinlibre-web lapinlibre/snapshot_delete {CLIENT} ok")
        for bad in ("", first, names[1] + ".thumb.gif", "../led", "snapshot_2026.jpg", "x" + names[1] + ".jpg",
                    names[1] + ".jpg%00", "%2F" + names[1] + ".jpg", "snapshots"):
            with self.subTest(bad):
                self.refused("lapinlibre/snapshot_delete", "filename=" + bad, "snapshot")
        self.assertEqual(self.stored(), [names[1] + ".jpg", names[1] + ".thumb.gif"])
        # A picture without a thumbnail.
        (snapshots / (names[1] + ".thumb.gif")).unlink()
        self.ok("lapinlibre/snapshot_delete", "filename=" + names[1] + ".jpg")
        self.assertEqual(self.stored(), [])

    def test_ten_kept(self):
        snapshots = self.sb / "var/run/lapinlibre-web/snapshots"
        snapshots.mkdir(parents=True)
        old = [f"snapshot_2020_01_01_00_00_{i:02d}" for i in range(10)]
        for name in old:
            (snapshots / (name + ".jpg")).write_bytes(JPEG)
            (snapshots / (name + ".thumb.gif")).write_bytes(b"GIF")
        (self.sb / "djpeg.fail").write_text("")
        reply = self.ok("snapshot")
        self.assertEqual(reply["thumb"], "")
        expected = sorted([n + ext for n in old[1:] for ext in (".jpg", ".thumb.gif")] + [reply["filename"]])
        self.assertEqual(self.stored(), expected)
        ids = [s["id"] for s in self.json("snapshot_list")["snapshots"]]
        self.assertEqual(ids, [n + ".jpg" for n in old[1:]] + [reply["filename"]])

    def test_view(self):
        status, headers, body = self.box.run("snapshot_view", "silent=1")
        self.assertEqual((status, headers["content-type"], headers["cache-control"], body), (200, "image/jpeg", "no-store", JPEG))
        self.assertEqual(self.stored(), [])
        self.assertEqual(sorted(p.name for p in (self.sb / "var/run/lapinlibre-web").iterdir()), [])
        self.assertEqual(self.box.syslog_lines(), [f"-t lapinlibre-web snapshot_view {CLIENT} ok"])
        self.box.daemon("Webcam", "false")
        status, headers, body = self.box.run("snapshot_view")
        self.assertEqual(status, 200)
        self.assertReplyHeaders(headers)
        self.assertError(json.loads(body), "camera")

    def test_failures(self):
        self.box.daemon("Webcam", "false")
        self.refused("snapshot", "", "camera couldn't")
        # The LED comes back after a failed shot too.
        self.assertEqual(self.calls()[-1], ("Led", "light", ["string:00FF00"]))
        self.assertEqual(self.stored(), [])

    def test_no_answer_then_the_picture(self):
        # No answer in time: the picture counts once its event with this request's id came.
        self.box.daemon("Webcam", "noreply")
        reply = self.ok("snapshot")
        self.assertRegex(reply["filename"], f"^{SNAPSHOT}\\.jpg$")

    def test_no_picture(self):
        (self.sb / "dbus/nopicture").write_text("")
        start = time.monotonic()
        self.refused("snapshot", "", "no picture")
        self.assertGreater(time.monotonic() - start, 9)
        self.assertEqual(self.stored(), [])


class ToolsTest(HardwareCase):
    def test_ps(self):
        status, headers, body = self.box.run("tools_ps")
        self.assertEqual(status, 200)
        self.assertReplyHeaders(headers)
        self.assertIn(b"/usr/karotz/bin/led-daemon", body)

    def test_setclock(self):
        self.assertEqual(self.ok("setclock"), {"return": "0"})
        self.assertIn("ntpd -q -p pool.ntp.org", self.box.actions_log())
        (self.sb / "ntpd.fail").write_text("")
        self.refused("setclock", "", "clock")
        self.assertEqual(self.box.syslog_lines()[-1], f"-t lapinlibre-web setclock {CLIENT} the clock couldn't be set: no time server answered")


TAG_A = "D00213A4B5C6D7E8"
TAG_B = "E0040100AABBCCDD"
TAGS = "usr/etc/conf/web/tags"


def b64(text):
    return base64.b64encode(text.encode()).decode()


def tag_line(tag, type_="1", color="3", cmd="0", var="-1", name="", a1="", a2="", a3=""):
    return "\t".join((tag, type_, color, cmd, var, name, a1, a2, a3)) + "\n"


class TagCase(HardwareCase):
    """Helpers for the tag store."""

    def store(self, *lines):
        self.box.write(TAGS, "# a comment\n" + "".join(lines))

    def stored(self):
        path = self.sb / TAGS
        if not path.exists():
            return []
        return [line.split("\t") for line in path.read_text(encoding="utf-8").splitlines() if not line.startswith("#")]

    def tones(self):
        for rel in ("usr/karotz/sounds/button-long.wav", "usr/karotz/sounds/button-triple.wav",
                    "usr/karotz/sounds/notify.mp3"):
            self.box.write(rel, "RIFF")

    def raw_pairs(self, path, query=""):
        status, _, body = self.box.run(path, query)
        self.assertEqual(status, 200, body)
        return json.loads(body, object_pairs_hook=lambda pairs: pairs)


class TagStoreTest(TagCase):
    """Listing, naming, deleting and the store's own checks."""

    def test_empty(self):
        self.assertEqual(self.json("rfid_list"), {"tags": [], "return": "0"})
        self.assertEqual(self.json("rfid_list_ext"), {"tags": [], "return": "0"})
        self.assertError(self.json("tag_list"), "rfid_list")
        self.assertError(self.json("rfid_info", query="tag=" + TAG_A), "no tag")
        self.assertEqual(self.json("status")["nb_tags"], "0")

    def stock(self):
        self.store(tag_line(TAG_A, "15", "51", "3", "-1", "Kitchen \"lights\" é", "http://192.168.1.9/x?y=1", "bob", "s3cr3t!"),
                   tag_line(TAG_B, "2", "4", "5", "-1", "", "tts", "text=Hello"))

    def test_list_shapes(self):
        self.stock()
        self.assertEqual(self.json("rfid_list"), {"tags": [
            {"id": TAG_A, "type": "15", "color": "51", "cmd": "3", "var": "-1", "name": "Kitchen \"lights\" é"},
            {"id": TAG_B, "type": "2", "color": "4", "cmd": "5", "var": "-1", "name": ""}], "return": "0"})
        # rfid_list_ext's keys in order, with the second "type" holding the action's name.
        pairs = self.raw_pairs("rfid_list_ext")
        self.assertEqual([k for k, _ in pairs], ["tags", "return"])
        first = pairs[0][1][0]
        self.assertEqual(first, [("id", TAG_A), ("name", "Kitchen \"lights\" é"), ("type", "15"), ("type_name", "KEYRING"),
                                 ("color", "51"), ("color_name", "BROWN"), ("type", "URL"), ("cmd", "3"), ("cmd_name", "URL")])
        second = dict(pairs[0][1][1])
        self.assertEqual((second["type"], second["type_name"], second["color_name"], second["cmd_name"]),
                         ("KAROTZ", "NANOZTAG", "YELLOW", "KAROTZ"))
        self.assertEqual(self.json("rfid_list_ext")["tags"][0]["type"], "URL")
        reply = self.json("rfid_info", query="tag=" + TAG_A.lower())
        self.assertEqual(list(reply), ["id", "type", "type_name", "color", "color_name", "cmd", "cmd_name", "var", "name", "return"])
        self.assertEqual((reply["id"], reply["type_name"], reply["cmd_name"]), (TAG_A, "KEYRING", "URL"))
        self.assertEqual(self.json("status")["nb_tags"], "2")
        # An action's data is never shown.
        for path in ("rfid_list", "rfid_list_ext", "rfid_info?tag=" + TAG_A, "status"):
            name, _, query = path.partition("?")
            body = self.box.run(name, query)[2]
            for secret in (b"s3cr3t", b"bob", b"192.168.1.9", b"Hello"):
                self.assertNotIn(secret, body, path)

    def test_codes_without_names(self):
        self.store(tag_line(TAG_A, "7", "99", "0"))
        reply = self.json("rfid_info", query="tag=" + TAG_A)
        self.assertEqual((reply["type_name"], reply["color_name"], reply["cmd_name"]), ("UNKNOWN", "UNKNOWN", "Unknown"))

    def test_store_is_checked_when_read(self):
        marker = self.sb / "pwned"
        self.store("not a tag line\n",
                   tag_line("D00213A4B5C6D7E", name="short id"),
                   tag_line("D00213A4B5C6D7EG", name="bad id"),
                   tag_line(TAG_A, "x", "1;2", "3", "-1", "a\x01b", f"http://h/`touch {marker}`"),
                   tag_line(TAG_A, name="repeated id"),
                   tag_line(TAG_B, "1", "1", "5", "-1", "ok", "cmd", "x=1"),
                   tag_line("1111111111111111", cmd="5", a1="tts", a2="text='x'"),
                   tag_line("2222222222222222", cmd="5", a1="tts", a2="text=a::b"),
                   tag_line("3333333333333333", cmd="3", a1="http://h::1/"),
                   tag_line("4444444444444444", cmd="3", a1="http://h/x", a2="us:er", a3="pw"),
                   tag_line("5555555555555555", cmd="3", a1="http://h/x", a2="user", a3=""),
                   tag_line("6666666666666666", cmd="3", a1="file:///etc/passwd"),
                   tag_line("7777777777777777", cmd="5", a1="../reboot", a2=""),
                   tag_line("8888888888888888", cmd="5", a1="apps/clock", a2="hour=7"))
        tags = {t["id"]: t for t in self.json("rfid_list")["tags"]}
        self.assertEqual(sorted(tags), sorted([TAG_A, TAG_B] + [str(i) * 16 for i in range(1, 9)]))
        self.assertEqual(tags[TAG_A], {"id": TAG_A, "type": "-1", "color": "-1", "cmd": "0", "var": "-1", "name": ""})
        for tag in [TAG_B] + [str(i) * 16 for i in range(1, 8)]:
            self.assertEqual(tags[tag]["cmd"], "0", tag)
        self.assertEqual(tags["8888888888888888"]["cmd"], "5")
        self.assertFalse(marker.exists())

    def test_rename(self):
        self.stock()
        self.assertEqual(self.ok("rfid_rename", f"tag={TAG_B.lower()}&name=Hall+%E2%80%94+door&color=8"), {"return": "0"})
        info = self.json("rfid_info", query="tag=" + TAG_B)
        self.assertEqual((info["name"], info["color"], info["color_name"], info["cmd"]), ("Hall — door", "8", "ORANGE", "5"))
        # Absent colour: kept; absent name: removed.
        self.ok("rfid_rename", f"tag={TAG_B}")
        info = self.json("rfid_info", query="tag=" + TAG_B)
        self.assertEqual((info["name"], info["color"]), ("", "8"))
        self.ok("rfid_rename", f"tag={TAG_B}&name=" + "%C3%A9" * 64)
        data = self.sb / "usr/etc/conf/web"
        self.assertEqual(data.stat().st_mode & 0o777, 0o700)
        self.assertEqual((data / "tags").stat().st_mode & 0o777, 0o600)
        # The other tag and its action are unchanged.
        self.assertEqual(self.stored()[0], [TAG_A, "15", "51", "3", "-1", "Kitchen \"lights\" é", "http://192.168.1.9/x?y=1",
                                            "bob", "s3cr3t!"])
        for query, word in (("", "tag is required"), ("tag=" + TAG_A[:-1], "tag must"), ("tag=" + TAG_A + "0", "tag must"),
                            ("tag=../../etc", "tag must"), ("tag=%24(reboot)", "tag must"), ("tag=1111111111111111", "no tag"),
                            (f"tag={TAG_A}&color=100", "color"), (f"tag={TAG_A}&color=-1", "color"),
                            (f"tag={TAG_A}&name=" + "a" * 65, "64"), (f"tag={TAG_A}&name=" + "é" * 65, "64"),
                            (f"tag={TAG_A}&name=%ff", "UTF-8"), (f"tag={TAG_A}&name=a%09b", "control"),
                            (f"tag={TAG_A}&name=a%0ab", "control")):
            with self.subTest(query):
                self.refused("rfid_rename", urllib.request.quote(query, safe="=&%"), word)
        self.assertEqual(self.stored()[0][5], "Kitchen \"lights\" é")

    def test_delete_unassign_clear(self):
        self.stock()
        self.assertEqual(self.ok("rfid_unassign", "tag=" + TAG_A), {"return": "0", "tag": TAG_A})
        self.assertEqual(self.stored()[0], [TAG_A, "15", "51", "0", "-1", "", "", "", ""])
        self.assertNotIn("s3cr3t", (self.sb / TAGS).read_text())
        self.assertEqual(self.ok("rfid_delete", "tag=" + TAG_B.lower()), {"return": "0", "tag": TAG_B})
        self.assertEqual([t[0] for t in self.stored()], [TAG_A])
        self.refused("rfid_delete", "tag=" + TAG_B, "no tag")
        self.refused("rfid_unassign", "tag=" + TAG_B, "no tag")
        self.refused("rfid_delete", "", "tag is required")
        self.stock()
        self.assertEqual(self.ok("clear_rfid"), {"return": "0"})
        self.assertEqual(self.stored(), [])
        self.assertEqual(self.json("rfid_list")["tags"], [])
        self.assertTrue(all(f"{CLIENT} ok" in line or "no tag" in line or "required" in line for line in self.box.syslog_lines()))

    def test_store_lock(self):
        import fcntl
        self.stock()
        with open(self.sb / "var/lock/lapinlibre-tags.lock", "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            self.refused("rfid_delete", "tag=" + TAG_A, "couldn't be written")
        self.assertEqual(len(self.stored()), 2)
        self.ok("rfid_delete", "tag=" + TAG_A)

    def test_management_while_asleep_or_busy(self):
        self.stock()
        self.box.write("controller.state", "24\n")
        self.ok("rfid_rename", f"tag={TAG_A}&name=x")
        self.box.write("controller.state", "21\n")
        self.ok("rfid_unassign", f"tag={TAG_A}")
        self.ok("rfid_assign_karotz", f"tag={TAG_A}&url=" + b64("/cgi-bin/ears_reset"))
        self.assertEqual(self.calls(), [])


class TagActionTest(TagCase):
    """Assigning actions, and testing a URL request."""

    def setUp(self):
        super().setUp()
        self.store(tag_line(TAG_A, name="old name"))

    def test_assign_url(self):
        query = f"tag={TAG_A}&url=" + urllib.request.quote("http://192.168.1.9:8080/api/run?scene=3", safe="")
        self.assertEqual(self.ok("rfid_assign_url", query), {"return": "0"})
        self.assertEqual(self.stored()[0], [TAG_A, "1", "3", "3", "-1", "old name", "http://192.168.1.9:8080/api/run?scene=3", "", ""])
        self.assertEqual(self.json("rfid_info", query="tag=" + TAG_A)["cmd_name"], "URL")
        # param is added after & (or ?), the credentials and the name are kept.
        self.ok("rfid_assign_url", query + "&param=" + urllib.request.quote(b64("x=1&y=%20z")) + "&user=bob&password=p%40ss%22w%5Cd&name=Lights")
        self.assertEqual(self.stored()[0], [TAG_A, "1", "3", "3", "-1", "Lights", "http://192.168.1.9:8080/api/run?scene=3&x=1&y=%20z",
                                            "bob", 'p@ss"w\\d'])
        self.ok("rfid_assign_url", f"tag={TAG_A}&url=http%3A%2F%2Fh.example&param=" + b64("?q=1").replace("+", " "))
        self.assertEqual(self.stored()[0][6], "http://h.example?q=1")
        log = self.box.syslog.read_text()
        self.assertIn(f"rfid_assign_url {CLIENT} ok, a request to 192.168.1.9", log)
        for secret in ("p@ss", "scene", "api/run", "bob"):
            self.assertNotIn(secret, log)
        self.assertEqual(self.calls(), [])

    def test_keep_stored_password(self):
        # A user without a password keeps the password the tag's URL action stores for that user.
        self.store(tag_line(TAG_A, cmd="3", a1="http://h/x", a2="bob", a3="s3cret"))
        self.ok("rfid_assign_url", f"tag={TAG_A}&url=http%3A%2F%2Fh%2Fy&user=bob&password=")
        self.assertEqual(self.stored()[0][6:9], ["http://h/y", "bob", "s3cret"])
        # Another user needs a password of its own; so does a tag without a URL action.
        self.refused("rfid_assign_url", f"tag={TAG_A}&url=http%3A%2F%2Fh%2Fy&user=eve&password=", "both")
        self.assertEqual(self.stored()[0][7:9], ["bob", "s3cret"])
        self.store(tag_line(TAG_A, cmd="5", a1="tts", a2="text=hi"))
        self.refused("rfid_assign_url", f"tag={TAG_A}&url=http%3A%2F%2Fh%2Fy&user=bob&password=", "both")

    def test_tag_action(self):
        self.store(tag_line(TAG_A, cmd="5", a1="tts", a2="text=Hello%20there"),
                   tag_line(TAG_B, cmd="3", a1="http://h/x?y=1", a2="bob", a3="s3cret"),
                   tag_line("0123456789ABCDEF"))
        self.assertEqual(self.json("lapinlibre/tag_action", query="tag=" + TAG_A),
                         {"return": "0", "tag": TAG_A, "cmd": "5", "endpoint": "tts", "query": "text=Hello%20there"})
        self.assertEqual(self.json("lapinlibre/tag_action", query="tag=" + TAG_B),
                         {"return": "0", "tag": TAG_B, "cmd": "3", "url": "http://h/x?y=1", "user": "bob", "password_set": "1"})
        _, _, body = self.box.run("lapinlibre/tag_action", "tag=" + TAG_B)
        self.assertNotIn(b"s3cret", body)
        self.assertEqual(self.json("lapinlibre/tag_action", query="tag=0123456789ABCDEF"),
                         {"return": "0", "tag": "0123456789ABCDEF", "cmd": "0"})
        self.assertError(self.json("lapinlibre/tag_action", query="tag=FEDCBA9876543210"), "no tag")

    def test_assign_url_refusals(self):
        marker = self.sb / "pwned"
        good = "url=http%3A%2F%2Fh%2Fx"
        for query, word in (("url=http%3A%2F%2Fh", "tag is required"), (f"tag={TAG_B}&{good}", "no tag"),
                            (f"tag={TAG_A}", "url is required"), (f"tag={TAG_A}&url=", "url is required"),
                            (f"tag={TAG_A}&url=ftp%3A%2F%2Fh", "url"), (f"tag={TAG_A}&url=http%3A%2F%2Fh%2F%3A%3Ax", "url"),
                            (f"tag={TAG_A}&url=" + urllib.request.quote(f"http://h/$(touch {marker})", safe=""), "url"),
                            (f"tag={TAG_A}&url=" + urllib.request.quote("http://h/`id`", safe=""), "url"),
                            (f"tag={TAG_A}&{good}&user=bob", "both"), (f"tag={TAG_A}&{good}&password=x", "both"),
                            (f"tag={TAG_A}&{good}&user=b:ob&password=x", "user"),
                            (f"tag={TAG_A}&{good}&user=b+ob&password=x", "user"),
                            (f"tag={TAG_A}&{good}&user=bob&password=a+b", "password"),
                            (f"tag={TAG_A}&{good}&user=bob&password=" + "x" * 65, "password"),
                            (f"tag={TAG_A}&{good}&param=%%%", "param"), (f"tag={TAG_A}&{good}&param=abc", "param"),
                            (f"tag={TAG_A}&{good}&param=" + b64("a b"), "param"),
                            (f"tag={TAG_A}&{good}&param=" + b64("a'b"), "param"),
                            (f"tag={TAG_A}&{good}&param=" + b64("x\ny"), "param"),
                            (f"tag={TAG_A}&{good}&param=" + b64("a\x00b"), "param"),
                            (f"tag={TAG_A}&{good}&param=" + b64("a=" + "b" * 1100), "param")):
            with self.subTest(query[:60]):
                self.refused("rfid_assign_url", query, word)
        self.assertEqual(self.stored()[0][3], "0")
        self.assertFalse(marker.exists())

    def test_assign_karotz(self):
        for target, endpoint, query in (("/cgi-bin/tts?text=Hello+there&voice=8", "tts", "text=Hello+there&voice=8"),
                                        ("http://127.0.0.1/cgi-bin/leds?color=ff0000&pulse=1", "leds", "color=ff0000&pulse=1"),
                                        ("http://kitchen.local:80/cgi-bin/ears?left=1&right=2", "ears", "left=1&right=2"),
                                        ("http://KITCHEN/cgi-bin/sleep", "sleep", ""),
                                        ("http://192.168.1.20/cgi-bin/apps/clock?hour=7#x", "apps/clock", "hour=7"),
                                        ("http://localhost/cgi-bin/sound?id=test-tone", "sound", "id=test-tone"),
                                        ("/cgi-bin/wakeup?silent=1", "wakeup", "silent=1"),
                                        ("/cgi-bin/snapshot", "snapshot", ""),
                                        ("/cgi-bin/sound_control?cmd=stop", "sound_control", "cmd=stop"),
                                        ("/cgi-bin/ears_random", "ears_random", "")):
            with self.subTest(target):
                self.assertEqual(self.ok("rfid_assign_karotz", f"tag={TAG_A}&url=" + urllib.request.quote(b64(target))),
                                 {"return": "0"})
                self.assertEqual(self.stored()[0], [TAG_A, "1", "3", "5", "-1", "old name", endpoint, query, ""])
        # Checking the query neither drives the rabbit nor logs the endpoint's own lines.
        self.assertEqual(self.calls(), [])
        self.assertTrue(all("rfid_assign_karotz" in line for line in self.box.syslog_lines()), self.box.syslog_lines())
        self.assertEqual(self.json("rfid_info", query="tag=" + TAG_A)["cmd_name"], "KAROTZ")
        # The checks ignore sleep, the ears' switch and an application: they come when it runs.
        self.box.write("var/run/lapinlibre-web/sleep", "1760000000\n")
        self.box.write("usr/etc/conf/web/ears_disabled", "")
        self.box.write("controller.state", "21\n")
        self.ok("rfid_assign_karotz", f"tag={TAG_A}&url=" + b64("/cgi-bin/ears?left=3&right=4") + "&name=Ears")
        self.assertEqual(self.stored()[0][3:8], ["5", "-1", "Ears", "ears", "left=3&right=4"])
        self.assertEqual(self.calls(), [])

    def test_assign_karotz_refusals(self):
        marker = self.sb / "pwned"
        for target, word in (("/cgi-bin/reboot", "can't run"), ("/cgi-bin/cmd?cmd=reboot", "can't run"),
                             ("/cgi-bin/rfid_delete?tag=" + TAG_A, "can't run"), ("/cgi-bin/../cgi-bin/tts?text=x", "can't run"),
                             ("/cgi-bin/lapinlibre/status", "can't run"), ("/cgi-bin/", "cgi-bin"), ("/tts?text=x", "cgi-bin"),
                             ("tts?text=x", "cgi-bin"), ("http://evil.example/cgi-bin/tts?text=x", "this rabbit"),
                             ("http://kitchen.evil/cgi-bin/tts?text=x", "this rabbit"), ("http://h::1/cgi-bin/tts", "URL"),
                             ("/cgi-bin/leds?color=red", "color"), ("/cgi-bin/ears?left=1", "right"),
                             ("/cgi-bin/tts", "text is required"), ("/cgi-bin/tts?text=a%00b", "control"),
                             (f"/cgi-bin/tts?text=$(touch {marker})", "URL characters"),
                             ("/cgi-bin/tts?text='x'", "URL characters"), ("/cgi-bin/sound?id=nope", "nope"),
                             ("/cgi-bin/sound_control?cmd=volume", "cmd"), ("/cgi-bin/apps/clock?hour=24", "hour"),
                             ("/cgi-bin/stories", "id is required"), ("/cgi-bin/tts?text=a::b", "URL characters")):
            with self.subTest(target):
                self.refused("rfid_assign_karotz", f"tag={TAG_A}&url=" + urllib.request.quote(b64(target)), word)
        for query, word in (("", "tag is required"), (f"tag={TAG_A}", "url is required"), (f"tag={TAG_A}&url=%2F", "base64"),
                            (f"tag={TAG_A}&url=" + b64("/cgi-bin/tts?text=\x01"), "control"),
                            (f"tag={TAG_B}&url=" + b64("/cgi-bin/sleep"), "no tag")):
            with self.subTest(query):
                self.refused("rfid_assign_karotz", query, word)
        self.assertEqual(self.stored()[0][3], "0")
        self.assertEqual(self.calls(), [])
        self.assertFalse(marker.exists())

    def test_test_url(self):
        self.box.write("curl.body", b"<b>ok</b> \"quoted\"\n\x00\x01caf\xc3\xa9 \xff" + b"z" * 2000)
        reply = self.ok("rfid_test_url", "url=" + urllib.request.quote("http://192.168.1.9/run?scene=3", safe="") +
                        "&user=bob&password=s3cr3t%22&tag=" + TAG_A)
        self.assertEqual(list(reply), ["return", "msg"])
        self.assertTrue(reply["msg"].startswith("<b>ok</b> \"quoted\"\n\x01café zzz"), reply["msg"][:40])
        self.assertLessEqual(len(reply["msg"].encode()), 1024)
        call = [line for line in self.box.actions_log().splitlines() if line.startswith("curl\x1f")][0].split("\x1f")
        for option in ("-q", "-L", "-K", "-", "--max-redirs", "3", "--max-time", "15", "--proto", "=http,https",
                       "--proto-redir", "=http,https", "http://192.168.1.9/run?scene=3"):
            self.assertIn(option, call)
        # The credentials go to curl's standard input, never its arguments.
        self.assertNotIn("s3cr3t", "".join(call))
        self.assertIn('curl-config user = "bob:s3cr3t\\""', self.box.actions_log())
        log = self.box.syslog.read_text()
        self.assertIn(f"rfid_test_url {CLIENT} ok, 192.168.1.9 answered HTTP 200", log)
        for secret in ("s3cr3t", "scene", "bob"):
            self.assertNotIn(secret, log)
        self.assertEqual(self.stored()[0][3], "0")

    def test_test_url_failure(self):
        self.box.write("curl.fail", "6 Could not resolve host: nowhere.example")
        reply = self.refused("rfid_test_url", "url=http%3A%2F%2Fnowhere.example%2Fsecret-path", "Could not resolve host")
        self.assertIn("nowhere.example", reply["msg"])
        log = self.box.syslog.read_text()
        self.assertIn("failed (curl status 6)", log)
        self.assertNotIn("secret-path", log)
        self.assertNotIn("Could not", log)
        self.refused("rfid_test_url", "", "url is required")
        self.assertNotIn("curl\x1fhttp", self.box.actions_log())


class LearningTest(TagCase):
    def test_start_and_stop(self):
        self.tones()
        self.assertEqual(self.ok("rfid_start_record"), {"return": "0"})
        self.assertRegex(self.state("learning"), r"^\d+\n$")
        long = f"string:{self.sb}/usr/karotz/sounds/button-long.wav"
        self.assertEqual(self.calls(), [("Multimedia", "stop", ["int32:1"]),
                                        ("Led", "pulse", ["string:FF8800", "string:000000", "int32:400", "int32:-1"]),
                                        ("Multimedia", "play", [long, "int32:1"])])
        # The learning pulse isn't the API's LED state.
        self.assertEqual(self.json("status")["led_color"], "00FF00")
        self.clear()
        self.assertEqual(self.ok("rfid_stop_record"), {"return": "0"})
        self.assertIsNone(self.state("learning"))
        click = f"string:{self.sb}/usr/karotz/sounds/button-click.wav"
        self.assertEqual(self.calls(), [("Led", "light", ["string:00FF00"]), ("Multimedia", "play", [click, "int32:1"])])

    def test_learning_left(self):
        # lapinlibre/status (and status, inside "lapinlibre") gives the seconds of learning left.
        self.assertEqual(self.json("lapinlibre/status")["learning_left"], 0)
        self.tones()
        self.ok("rfid_start_record")
        left = self.json("lapinlibre/status")["learning_left"]
        self.assertTrue(110 <= left <= 120, left)
        # The second request may fall in the next second.
        self.assertIn(self.json("status")["lapinlibre"]["learning_left"], (left, left - 1))
        self.ok("rfid_stop_record")
        self.assertEqual(self.json("lapinlibre/status")["learning_left"], 0)

    def test_asleep_and_busy(self):
        self.tones()
        self.ok("sleep")
        self.clear()
        self.ok("rfid_start_record")
        self.assertNotIn("play", [m for _, m, _ in self.calls()])
        self.clear()
        self.ok("rfid_stop_record")
        self.assertEqual(self.calls(), [("Led", "light", ["string:000000"])])
        self.ok("wakeup", "silent=1")
        self.box.write("controller.state", "21\n")
        self.refused("rfid_start_record", "", "application")
        self.box.write("var/run/lapinlibre-web/learning", "1760000000\n")
        self.clear()
        self.ok("rfid_stop_record")
        self.assertIsNone(self.state("learning"))
        self.assertEqual(self.calls(), [])

    def test_ends_by_itself(self):
        lib = self.sb / "usr/share/lapinlibre/web.sh"
        lib.write_text(lib.read_text().replace("WEB_LEARN_TIME=120", "WEB_LEARN_TIME=2"), newline="\n")
        start = time.monotonic()
        self.ok("rfid_start_record")
        self.assertLess(time.monotonic() - start, 2)
        deadline = time.monotonic() + 20
        while self.state("learning") is not None and time.monotonic() < deadline:
            time.sleep(0.2)
        self.assertIsNone(self.state("learning"))
        time.sleep(1)
        self.assertEqual(self.calls()[-2:], [("Led", "light", ["string:00FF00"]),
                                             ("Multimedia", "play", [f"string:{self.sb}/usr/karotz/sounds/button-click.wav", "int32:1"])])
        self.assertIn(f"rfid_stop_record 127.0.0.1 ok", self.box.syslog.read_text())
        # A learning started again meanwhile is left alone by the first one's timer.
        self.clear()
        self.ok("rfid_start_record")
        first = self.state("learning")
        time.sleep(1.1)
        self.ok("rfid_start_record")
        time.sleep(1.5)
        self.assertIsNotNone(self.state("learning"))
        self.assertNotEqual(self.state("learning"), first)


def tag_in(tag, type_=15, color=4, member="tag_in", path="/com/mindscape/karotz/Rfid"):
    """A tag_in signal as dbus-monitor prints it."""
    return (f"signal time=1760000000.1 sender=:1.6 -> destination=(null destination) serial=12 path={path}; "
            f"interface=com.mindscape.karotz.KarotzInterface; member={member}\n"
            f'   string "{tag}"\n   string "00000000:11111111:"\n   int32 0\n   int32 {type_}\n   int32 0\n   int32 {color}\n')


class TagListenerTest(TagCase):
    """image/core's tag listener, sourcing the sandboxed web.sh, with a stub dbus-monitor playing
    tag_in signals."""

    def listen(self, *signals, timeout=60):
        self.box.write("tag.signals", "".join(signals))
        r = subprocess.run([str(self.box.bin / "sh"), str(self.sb / "usr/scripts/tag_listener")],
                           env={"PATH": str(self.box.bin)}, capture_output=True, timeout=timeout)
        return r.returncode, r.stdout + r.stderr

    def listener_log(self):
        return [line for line in self.box.syslog_lines() if "tag_listener" in line]

    def test_rule_and_exit(self):
        rc, out = self.listen()
        self.assertEqual((rc, out), (1, b""))
        rule = [line for line in self.box.actions_log().splitlines() if line.startswith("dbus-monitor")]
        self.assertEqual(rule, ["dbus-monitor --system type='signal',path='/com/mindscape/karotz/Rfid',"
                                "interface='com.mindscape.karotz.KarotzInterface',member='tag_in' "
                                "type='signal',path='/com/mindscape/karotz/Controller',"
                                "interface='com.mindscape.karotz.KarotzInterface',member='karotz_status_changed' "
                                "type='signal',sender='org.freedesktop.DBus',interface='org.freedesktop.DBus',"
                                "member='NameOwnerChanged',arg0='com.mindscape.karotz.Controller'"])
        self.assertTrue([line for line in self.listener_log() if "monitor ended" in line])
        self.assertEqual([p.name for p in (self.sb / "var/run/lapinlibre-web").iterdir()], [])

    def test_controller_state(self):
        # The controller asked at the start, then its signal, its leaving the bus and its coming
        # back (asked again); each step read from the file while the listener runs.
        def owner(old, new):
            return ("signal time=1 sender=org.freedesktop.DBus -> destination=(null destination) serial=11 "
                    "path=/org/freedesktop/DBus; interface=org.freedesktop.DBus; member=NameOwnerChanged\n"
                    f'   string "com.mindscape.karotz.Controller"\n   string "{old}"\n   string "{new}"\n')
        state = ("signal time=1 sender=:1.3 -> destination=(null destination) serial=10 path=/com/mindscape/karotz/Controller; "
                 "interface=com.mindscape.karotz.KarotzInterface; member=karotz_status_changed\n   int32 21\n")
        other = owner("", ":1.7").replace("com.mindscape.karotz.Controller", "com.mindscape.karotz.Led")
        self.box.write("tag.signals", "SLEEP 4\n" + state + "SLEEP 4\n" + other + owner(":1.3", "") + "SLEEP 4\n"
                       + owner("", ":1.9") + "SLEEP 4\n")
        kept = self.sb / "var/run/lapinlibre-web/controller"
        seen = []
        p = subprocess.Popen([str(self.box.bin / "sh"), str(self.sb / "usr/scripts/tag_listener")],
                             env={"PATH": str(self.box.bin)}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            time.sleep(3)
            for _ in range(4):
                seen.append(kept.read_text() if kept.exists() else None)
                time.sleep(4)
            self.assertEqual(p.wait(timeout=60), 1)
        finally:
            p.kill()
        self.assertEqual(seen, ["16\n", "21\n", "\n", "16\n"])
        self.assertFalse(kept.exists())

    def test_learning(self):
        self.tones()
        self.box.write("var/run/lapinlibre-web/learning", f"{int(time.time())}\n")
        self.listen(tag_in(TAG_A.lower()), "SLEEP 0.3\n", tag_in(TAG_A), tag_in("nothex!!nothex!!"), tag_in("D0021"))
        self.assertEqual(self.stored(), [[TAG_A, "15", "4", "0", "-1", "", "", "", ""]])
        bubble = f"string:{self.sb}/usr/karotz/sounds/notify.mp3"
        triple = f"string:{self.sb}/usr/karotz/sounds/button-triple.wav"
        pulse = ("Led", "pulse", ["string:FF8800", "string:000000", "int32:400", "int32:-1"])
        self.assertEqual(self.calls(), [("Led", "light", ["string:00FF00"]), ("Multimedia", "play", [bubble, "int32:1"]), pulse,
                                        ("Led", "light", ["string:FFFF00"]), ("Multimedia", "play", [triple, "int32:1"]), pulse])
        self.assertTrue([line for line in self.listener_log() if f"tag {TAG_A} learned" in line])

    def test_learning_expired_or_off(self):
        self.box.write("var/run/lapinlibre-web/learning", f"{int(time.time()) - 200}\n")
        self.listen(tag_in(TAG_A))
        self.assertEqual(self.stored(), [])
        self.assertIsNone(self.state("learning"))
        self.assertEqual(self.calls(), [])

    def test_url_action(self):
        self.store(tag_line(TAG_A, cmd="3", a1="http://192.168.1.9/run?scene=3", a2="bob", a3="pa\\ss\"wd"))
        self.listen(tag_in(TAG_A))
        calls = [line.split("\x1f") for line in self.box.actions_log().splitlines() if line.startswith("curl\x1f")]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][-1], "http://192.168.1.9/run?scene=3")
        self.assertIn("/dev/null", calls[0])
        self.assertNotIn("wd", "".join(calls[0]))
        self.assertIn('curl-config user = "bob:pa\\\\ss\\"wd"', self.box.actions_log())
        # The flash, the API's LED state after it, then again after the action.
        self.assertEqual(self.calls(), [("Led", "light", ["string:00FF00"])] * 3)
        log = "\n".join(self.listener_log())
        self.assertIn(f"tag {TAG_A}: request to 192.168.1.9: HTTP 200", log)
        self.assertNotIn("scene", log)
        self.assertNotIn("wd", log)

    def test_internal_action(self):
        marker = self.sb / "pwned"
        self.store(tag_line(TAG_A, cmd="5", a1="tts", a2="text=%24(touch+" + urllib.request.quote(str(marker)) + ")%3B%60id%60"),
                   tag_line(TAG_B, cmd="5", a1="leds", a2="color=ffff00"))
        self.listen(tag_in(TAG_A), "SLEEP 2\n", tag_in(TAG_B))
        calls = self.calls()
        self.assertIn(("Voice", "tts_speak", [f"string:$(touch {marker});`id`", "string:en-GB", "int32:1"]), calls)
        self.assertIn(("Led", "light", ["string:FFFF00"]), calls)
        self.assertFalse(marker.exists())
        # The endpoint ran as a local request, with its own log line.
        log = self.box.syslog.read_text()
        self.assertIn("lapinlibre-web tts 127.0.0.1 ok", log)
        self.assertIn(f"tag {TAG_A}: tts done", log)
        self.assertNotIn("touch", log)

    def test_asleep(self):
        self.store(tag_line(TAG_A, cmd="5", a1="tts", a2="text=hi"), tag_line(TAG_B, cmd="3", a1="http://h.example/x"))
        self.box.write("var/run/lapinlibre-web/sleep", "1760000000\n")
        self.listen(tag_in(TAG_A), "SLEEP 1.5\n", tag_in(TAG_B))
        self.assertNotIn("tts_speak", [m for _, m, _ in self.calls()])
        self.assertIn("asleep", "\n".join(self.listener_log()))
        # URL requests still go; no flash while asleep, only the LED kept off.
        self.assertIn("curl\x1f", self.box.actions_log())
        self.assertNotIn(("Led", "light", ["string:00FF00"]), self.calls())

    def test_unknown_tags_and_no_action(self):
        self.store(tag_line(TAG_A))
        self.listen(tag_in(TAG_A), tag_in(TAG_B), tag_in(TAG_A, member="tag_out"),
                    tag_in(TAG_A, path="/com/mindscape/karotz/Rfidx"))
        self.assertEqual(self.calls(), [])
        self.assertNotIn("curl", self.box.actions_log())

    def test_one_action_at_a_time(self):
        self.box.write("curl.sleep", "2\n")
        self.store(tag_line(TAG_A, cmd="3", a1="http://h.example/x"))
        self.listen(tag_in(TAG_A), "SLEEP 0.8\n", tag_in(TAG_A), "SLEEP 3\n", tag_in(TAG_A))
        curls = [line for line in self.box.actions_log().splitlines() if line.startswith("curl\x1f")]
        self.assertEqual(len(curls), 2)
        self.assertTrue([line for line in self.listener_log() if "still running: ignored" in line])

    def test_listed_and_safe(self):
        listed = (ROOT / "system/core-files.txt").read_text(encoding="utf-8")
        self.assertRegex(listed, r"(?m)^usr:scripts/tag_listener f 0755 0:0 core\b")
        text = LISTENER.read_text(encoding="utf-8")
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        for bad in ("eval ", "sh -c", "bash -c", "`"):
            self.assertNotIn(bad, code)
        self.assertTrue(text.startswith("#!/bin/sh\n# SPDX-License-Identifier: MIT\n"))
        r = subprocess.run(["sh", "-n", str(LISTENER)], capture_output=True, text=True, timeout=30)
        self.assertEqual((r.returncode, r.stderr), (0, ""))


class StickListsTest(HardwareCase):
    """Moods, stories and radios from the stick, and the talking clock."""

    def setUp(self):
        super().setUp()
        self.box.mount_stick()
        for name in ("10", "2", "abc", "007"):
            self.box.write(f"mnt/usbkey/moods/{name}.mp3", "ID3")
        self.box.write("mnt/usbkey/moods/3.wav", "RIFF")

    def test_lists_without_stick(self):
        self.box.write("proc/mounts", "rootfs / rootfs rw 0 0\n")
        self.assertEqual(self.json("moods_list"), {"moods": [], "return": "0"})
        self.assertEqual(self.json("stories_list"), {"stories": [], "return": "0"})
        self.assertEqual(self.json("radios_list"), {"streams": [], "return": "0"})
        self.refused("apps/moods", "", "no USB stick")
        self.refused("stories", "id=tale", "no USB stick")

    def test_moods_list(self):
        self.box.write("mnt/usbkey/moods/moods.txt",
                       "1:Happy é\r\n2 : Grumpy \"one\"\n2:second label\nabc:A\x01B\nnope:Not a file\nbad line\n:x\n"
                       "10:" + "w" * 300 + "\n")
        reply = self.json("moods_list")
        self.assertEqual([m["id"] for m in reply["moods"]], ["1", "2", "007", "10", "abc"])
        labels = {m["id"]: m["text"] for m in reply["moods"]}
        self.assertEqual((labels["1"], labels["2"], labels["007"], labels["abc"], len(labels["10"])),
                         ("Happy é", "Grumpy \"one\"", "007", "AB", 200))
        self.assertEqual(list(reply["moods"][0]), ["id", "text"])
        self.assertEqual(self.json("status")["nb_moods"], "5")

    def test_stories_and_radios(self):
        self.box.write("mnt/usbkey/stories/stories.txt", "tale:The tale\n")
        self.box.write("mnt/usbkey/stories/other.mp3", "ID3")
        self.assertEqual(self.json("stories_list"), {"stories": [{"id": "other", "name": "other"},
                                                                 {"id": "tale", "name": "The tale"}], "return": "0"})
        self.box.write("mnt/usbkey/radios.txt",
                       "# my stations\n\nJazz | http://jazz.example:8000/live.mp3\r\n  # indented comment\n"
                       "Bad|ftp://x/y\nNo url line\nQuote|http://h/a'b\nPipes|http://h/a?x=1|2\n"
                       "News é|https://news.example/s.m3u\n|http://h/noname\nColons|http://h/::x\n")
        reply = self.json("radios_list")
        self.assertEqual(reply, {"streams": [{"id": 1, "name": "Jazz", "url": "http://jazz.example:8000/live.mp3"},
                                             {"id": 2, "name": "News é", "url": "https://news.example/s.m3u"},
                                             {"id": 3, "name": "", "url": "http://h/noname"}], "return": "0"})

    def test_play_mood(self):
        self.assertEqual(self.ok("apps/moods", "id=10&lang=fr"), {"moods": "10", "return": "0"})
        mood = f"string:{self.sb}/mnt/usbkey/moods/10.mp3"
        self.assertEqual(self.calls(), [("Ears", "reset", ["string:B"]), ("Multimedia", "stop", ["int32:1"]),
                                        ("Multimedia", "play", [mood, "int32:1"])])
        self.clear()
        reply = self.ok("apps/moods")
        self.assertIn(reply["moods"], ["1", "2", "10", "abc", "007"])
        self.assertEqual(self.calls()[-1][2][0], f"string:{self.sb}/mnt/usbkey/moods/{reply['moods']}.mp3")
        self.ok("ears_mode", "disable=1")
        self.clear()
        self.ok("apps/moods", "id=2")
        self.assertNotIn("Ears", [d for d, _, _ in self.calls()])
        for query, word in (("id=3", "no mood"), ("id=nope", "no mood"), ("id=..%2F1", "id"), ("id=%00", "id"),
                            ("id=" + "a" * 65, "id")):
            with self.subTest(query):
                self.refused("apps/moods", query, word)
        self.ok("sleep")
        self.refused("apps/moods", "id=2", "asleep")

    def test_no_moods(self):
        for p in (self.sb / "mnt/usbkey/moods").glob("*.mp3"):
            p.unlink()
        self.refused("apps/moods", "", "no moods")

    def test_play_story(self):
        self.assertEqual(self.ok("stories", "id=tale"), {"return": "0"})
        self.assertEqual(self.calls()[-1], ("Multimedia", "play", [f"string:{self.sb}/mnt/usbkey/stories/tale.mp3", "int32:1"]))
        for query, word in (("", "id is required"), ("id=nope", "no story"), ("id=..%2Ftale", "id")):
            with self.subTest(query):
                self.refused("stories", query, word)
        self.box.write("controller.state", "21\n")
        self.refused("stories", "id=tale", "application")

    def spoken(self):
        return [args for _, m, args in self.calls() if m == "tts_speak"][-1]

    def test_clock(self):
        self.assertEqual(self.ok("apps/clock", "hour=07"), {"return": "0", "hour": "7"})
        self.assertEqual(self.spoken(), ["string:It is 7 o'clock.", "string:en-GB", "int32:1"])
        reply = self.ok("apps/clock")
        self.assertRegex(self.spoken()[0], r"^string:It is \d{1,2}:\d\d\.$")
        self.assertEqual(reply["hour"], self.spoken()[0].split()[2].split(":")[0])
        for lang, hour, text in (("fr-FR", "1", "Il est 1 heure."), ("fr-FR", "14", "Il est 14 heures."),
                                 ("de-DE", "8", "Es ist 8 Uhr."), ("es-ES", "1", "Es la una."), ("es-ES", "9", "Son las 9."),
                                 ("it-IT", "9", "It is 9 o'clock.")):
            with self.subTest(lang):
                self.box.write("usr/etc/conf/karotz.conf", f"[karotz]\nlang={lang}\n")
                self.ok("apps/clock", "hour=" + hour)
                expected_lang = "en-US" if lang == "it-IT" else lang
                self.assertEqual(self.spoken()[:2], ["string:" + text, "string:" + expected_lang])
        for query in ("hour=24", "hour=-1", "hour=x", "hour=%241"):
            self.refused("apps/clock", query, "hour")
        self.box.daemon("Voice", "false")
        self.refused("apps/clock", "hour=1", "already speaking")
        self.ok("sleep")
        self.refused("apps/clock", "hour=1", "asleep")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class HttpdTest(unittest.TestCase):
    """The image's own BusyBox httpd (under qemu) with the configuration inetd.sh makes and the
    API's programs: basic auth with the SHA-512 hash, REMOTE_USER and the browser's headers
    reaching the programs, and the address rules."""

    @classmethod
    def setUpClass(cls):
        cls.busybox = arm_busybox()
        if not cls.busybox:
            raise unittest.SkipTest("the image's BusyBox can't run here (no system build or no qemu-user)")

    def setUp(self):
        self.box = Sandbox(self.busybox)
        sb = self.sb = self.box.sb
        for name in ("index.html", "app.js", "style.css"):
            shutil.copy(WWW / name, sb / "usr/www" / name)
        self.box.write("usr/www/cgi-bin/env", "#!/bin/sh\nprintf 'Content-Type: text/plain\\r\\n\\r\\n'\n/usr/bin/env\n", mode=0o755)
        self.httpd = None

    def tearDown(self):
        self.stop()
        self.box.tmp.cleanup()

    def make_conf(self, password):
        """inetd.sh's configuration for the sandbox, with or without a web password."""
        sb = self.sb
        for d in ("etc", "usr/etc/conf", "var/run", "sbin", "karotz/etc"):
            (sb / d).mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / "image/core/rootfs/karotz/etc/httpd.conf", sb / "karotz/etc/httpd.conf")
        (sb / "etc/inetd.conf").write_text("# none\n", newline="\n")
        if password:
            (sb / "usr/etc/conf/web_password").write_text(password + "\n", newline="\n")
        stub(sb / "sbin/inetd", "exit 0")
        script = (ROOT / "image/core/rootfs/karotz/scripts/inetd.sh").read_text(encoding="utf-8")
        for path in ("/var/run/", "/etc/inetd.conf", "/usr/etc/conf/", "/sbin/inetd", "/karotz/etc/httpd.conf"):
            script = script.replace(path, f"{sb}{path}")
        script = script.replace("exec sleep 2147483647", "exit 0")
        subprocess.run(["sh", "-c", script], env={"PATH": f"{sb}/bin:/usr/bin:/bin"}, capture_output=True, timeout=120)
        return sb / "var/run/httpd.conf"

    def start(self, conf):
        self.port = free_port()
        self.httpd = subprocess.Popen([str(self.busybox), "httpd", "-f", "-p", f"127.0.0.1:{self.port}", "-c", str(conf),
                                       "-h", str(self.sb / "usr/www")], env={"PATH": str(self.box.bin)},
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=1).close()
                return
            except OSError:
                time.sleep(0.2)
        self.fail("httpd didn't start")

    def stop(self):
        if self.httpd:
            self.httpd.terminate()
            self.httpd.wait(timeout=10)
            self.httpd = None

    def get(self, path, auth=None, headers=None, method="GET"):
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}/{path}", method=method, headers=headers or {})
        if auth:
            request.add_header("Authorization", "Basic " + base64.b64encode(auth.encode()).decode())
        try:
            with urllib.request.urlopen(request, timeout=120) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def test_password_and_headers(self):
        conf = self.make_conf("secretpass1")
        self.assertIn("/:admin:$6$", conf.read_text())
        self.start(conf)
        for path in ("", "index.html", "app.js", "cgi-bin/get_version"):
            with self.subTest(path):
                self.assertEqual(self.get(path)[0], 401)
                self.assertEqual(self.get(path, auth="admin:wrongpass")[0], 401)
                self.assertEqual(self.get(path, auth="root:secretpass1")[0], 401)
                self.assertEqual(self.get(path, auth="admin:secretpass1")[0], 200)
        status, headers, body = self.get("cgi-bin/get_version", auth="admin:secretpass1")
        self.assertEqual(json.loads(body)["version"], f"{BUILD_ID} ({BUILD})")
        self.assertEqual(headers.get("Content-Type"), "text/plain")
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        # What reaches a program: the web user, the address, and the browser's own headers.
        _, _, body = self.get("cgi-bin/env", auth="admin:secretpass1",
                              headers={"Origin": "http://evil.example", "Sec-Fetch-Site": "cross-site", "Referer": "http://evil.example/"})
        env = dict(line.split("=", 1) for line in body.decode().splitlines() if "=" in line)
        self.assertEqual((env.get("REMOTE_USER"), env.get("REMOTE_ADDR")), ("admin", "127.0.0.1"))
        self.assertEqual((env.get("HTTP_ORIGIN"), env.get("HTTP_SEC_FETCH_SITE"), env.get("HTTP_REFERER")),
                         ("http://evil.example", "cross-site", "http://evil.example/"))
        self.assertEqual(env.get("HTTP_HOST"), f"127.0.0.1:{self.port}")
        self.assertNotIn("HTTP_AUTHORIZATION", env)
        # So the cross-site rule holds through httpd.
        status, _, body = self.get("cgi-bin/reboot", auth="admin:secretpass1", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual((status, json.loads(body)["return"]), (403, "1"))
        status, _, body = self.get("cgi-bin/reboot", auth="admin:secretpass1", headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        self.assertNotIn("reboot", self.box.actions_log())
        status, _, _ = self.get("cgi-bin/get_version", auth="admin:secretpass1", method="PUT")
        self.assertEqual(status, 405)

    def test_no_password_no_login(self):
        # No web password (the default): every page and endpoint answers the local network without a
        # login; the policy checks still hold.
        (self.sb / "usr/etc/conf/web_password.hash").unlink()
        self.start(self.make_conf(None))
        for path in ("", "index.html", "cgi-bin/get_version"):
            with self.subTest(path):
                self.assertEqual(self.get(path)[0], 200)
        status, _, body = self.get("cgi-bin/lapinlibre/status")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["web_user"], "")
        status, _, _ = self.get("cgi-bin/reboot", headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        self.assertNotIn("reboot", self.box.actions_log())

    def test_outside_addresses_refused(self):
        conf = self.make_conf("secretpass1")
        # The same rules without the loopback address: this client is then outside them.
        conf.write_text("".join(line + "\n" for line in conf.read_text().splitlines() if line != "A:127.0.0.1"))
        self.start(conf)
        # httpd answers 403 before reading the request and closes; when the request is still
        # arriving, the close resets the connection and the client may not see the 403. Both refuse.
        for path in ("cgi-bin/get_version", ""):
            try:
                status = self.get(path, auth="admin:secretpass1")[0]
            except (ConnectionError, http.client.HTTPException, urllib.error.URLError):
                status = "closed"
            self.assertIn(status, (403, "closed"), path)


if __name__ == "__main__":
    unittest.main()
