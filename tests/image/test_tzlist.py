# SPDX-License-Identifier: MIT
"""The image's time-zone list: image/tools/tzlist.py on the pinned tzdata and tzcode.

- The controller's lookup (the first line containing the zone's name) finds every zone's own line,
  and current names (Europe/Kyiv, America/Nuuk) are there.
- Under qemu, with the system build's uClibc-ng, every zone's TZ string gives the same local time as tzdata
  for the year after tzdata's release (at each change, an hour either side, and at noon every day).
  Only the zones the generator reports as approximated may differ, by at most what it reports.
"""

import re
import shutil
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "image" / "tools"))
sys.path.insert(0, str(ROOT / "scripts"))
import tzlist  # noqa: E402
from lapin import work_dir  # noqa: E402

CROSS = work_dir() / "system" / "out" / "host" / "bin" / "arm-buildroot-linux-uclibcgnueabi-gcc"
TARGET = work_dir() / "system" / "out" / "target"


def fetch(*names):
    r = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "scripts" / "fetch.py"), *names],
                       capture_output=True, text=True, timeout=600)
    return r.stdout.split() if r.returncode == 0 else None


class TzListTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if shutil.which("make") is None or shutil.which("gcc") is None:
            raise unittest.SkipTest("no host make and gcc to build zic")
        paths = fetch("tzdata", "tzcode")
        if not paths:
            raise unittest.SkipTest("tzdata or tzcode not fetched (./build.sh fetch)")
        cls.work = work_dir() / "tz"
        text, cls.report = tzlist.generate(paths[0], paths[1], cls.work)
        cls.lines = [tuple(l.split(";", 1)) for l in text.splitlines()]
        cls.version = re.search(r"tzdata (\S+)", cls.report[0]).group(1)
        cls.zi = cls.work / f"zoneinfo-{cls.version}"

    def test_lookup_finds_every_zone(self):
        wrong = [n for n, tz in self.lines if tzlist.lookup(self.lines, n) != tz]
        self.assertEqual(wrong, [])

    def test_current_names_listed(self):
        names = {n for n, _ in self.lines}
        for n in ("Europe/Kyiv", "America/Nuuk", "Asia/Tehran", "Europe/Paris"):
            self.assertIn(n, names)

    def test_lengths(self):
        self.assertEqual([n for n, tz in self.lines if len(tz) > tzlist.MAX_TZ or len(n) + 1 + len(tz) > tzlist.MAX_LINE], [])

    def test_uclibc_local_times(self):
        if not CROSS.exists() or not (TARGET / "lib").is_dir() or shutil.which("qemu-arm") is None:
            self.skipTest("no system build or qemu-arm (./build.sh system)")
        prog = self.work / "tzcheck-arm"
        subprocess.run([str(CROSS), "-O2", "-Wall", "-march=armv4t", "-marm", "-o", str(prog),
                        str(Path(__file__).with_name("tzcheck.c"))], check=True, timeout=300)
        released = re.search(r"released (\d{4}-\d\d-\d\d)", self.report[0]).group(1)
        start = int(datetime.strptime(released, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
        end = start + 365 * 86400
        lines, expect = [], {}
        for name, tz in self.lines:
            with open(self.zi / name, "rb") as f:
                info = ZoneInfo.from_file(f, key=name)
            times, prev = set(range(start + 43200, end, 86400)), None
            for t in range(start, end, 3600):
                off = datetime.fromtimestamp(t, info).utcoffset()
                if prev is not None and off != prev:
                    times.update({t - 3601, t - 1, t, t + 3599})
                prev = off
            times = sorted(times)
            lines.append(f"{name}\t{tz}\t{' '.join(map(str, times))}")
            for t in times:
                expect[(name, t)] = f"{datetime.fromtimestamp(t, info):%Y-%m-%d %H:%M:%S}"
        out = subprocess.run(["qemu-arm", "-cpu", "arm926", "-L", str(TARGET), str(prog)], input="\n".join(lines) + "\n",
                             capture_output=True, text=True, timeout=1200).stdout.splitlines()
        self.assertEqual(len(out), len(expect))
        wrong = {}
        for l in out:
            name, t, day, clock, _ = l.split(" ")
            if expect[(name, int(t))] != f"{day} {clock}":
                wrong[name] = wrong.get(name, 0) + 1
        approximated = {r.split(":", 1)[0] for r in self.report[1:] if ":" in r}
        self.assertEqual(sorted(n for n in wrong if n not in approximated), [], "zones that should be exact")
        # An approximated zone is wrong at most on a change night or two, or (Gaza, Hebron) about
        # their Ramadan changes: well under a tenth of the instants checked.
        samples = {}
        for name, _ in expect:
            samples[name] = samples.get(name, 0) + 1
        self.assertEqual(sorted(n for n in wrong if wrong[n] > samples[n] // 10), [], "approximations too far off")


if __name__ == "__main__":
    unittest.main()
