# SPDX-License-Identifier: MIT
"""The harness's reading of qemu-arm's syscall log (harness.parse_qemu_strace).

A shell started by system() or popen() is recorded as "system <command>", also when the parent's own
log output lands between the child's execve arguments, on the same line or across lines (the lines
below were seen in runs). Device opens, reads, writes and ioctls are kept in order around it.
"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
import harness  # noqa: E402

MAC = "/sbin/ifconfig wlan0 | head -n1 | awk -F' ' '{ print $5 }' > /tmp/mac_addr"
FLAC = "flac - --endian=little --channels=1 --bps=16 --sample-rate=16000 --sign=unsigned   -o /tmp/record.flac -f"


def parse(text):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "daemon.log"
        path.write_text(text)
        return harness.parse_qemu_strace(path)


class ShellCommands(unittest.TestCase):
    def test_whole_line(self):
        self.assertEqual(parse('64 execve("/bin/sh",{"/bin/sh","-c","cp -f /a /b",NULL}) = 0\n'),
                         ["system cp -f /a /b"])

    def test_parent_between_the_arguments(self):
        line = '114 execve("/bin/sh",{"/bin/sh",57 "-c",wait4(114,0x4080022c,0,(nil))"' + MAC + '",NULL})\n'
        self.assertEqual(parse(line), [f"system {MAC}"])

    def test_parent_call_split_around_the_command(self):
        line = ('41 execve("/bin/sh",{"sh","-c",9 clock_gettime(CLOCK_MONOTONIC,"' + FLAC
                + '",0x408004d8NULL})) = 0 ({tv_sec = 1,tv_nsec = 2})\n')
        self.assertEqual(parse(line), [f"system {FLAC}"])

    def test_across_lines(self):
        text = ('35 10 execve(ppoll(0x40d780,2,0x40800510,(nil))"/bin/sh", = {0"sh",\n'
                '"-c","madplay -a 1.5 -",NULL})\n')
        self.assertEqual(parse(text), ["system madplay -a 1.5 -"])

    def test_escaped_quotes(self):
        self.assertEqual(parse('5 execve("/bin/sh",{"sh","-c","echo \\"x\\"",NULL}) = 0\n'),
                         ['system echo \\"x\\"'])

    def test_other_programs_are_not_commands(self):
        self.assertEqual(parse('5 execve("/usr/karotz/bin/karotzVM",{"/usr/karotz/bin/karotzVM","--app_folder=/a",NULL})\n'),
                         [])


class DeviceLines(unittest.TestCase):
    def test_order_around_a_command(self):
        text = ('57 open("/dev/leds",O_RDWR) = 7\n'
                '57 write(7,0x1,4) = 4\n'
                '64 execve("/bin/sh",{"/bin/sh","-c","true",NULL}) = 0\n'
                '57 ioctl(7,0x4004f901,0x1) = 0\n'
                '57 close(7) = 0\n'
                '57 write(7,0x1,4) = 4\n')
        self.assertEqual(parse(text), ["open /dev/leds", "write /dev/leds 4", "system true", "ioctl /dev/leds 0x4004f901"])


if __name__ == "__main__":
    unittest.main()
