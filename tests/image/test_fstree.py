# SPDX-License-Identifier: MIT
"""Unit tests for the cramfs codec, the tar wrapper and overlays (image/tools)."""

import io
import stat
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "image" / "tools"))
sys.path.insert(0, str(ROOT / "scripts"))
import cramfs  # noqa: E402
import fstree  # noqa: E402

N = cramfs.Node


def sample_image():
    big = bytes(range(256)) * 40  # 10 KiB: three blocks
    root = N(b"", stat.S_IFDIR | 0o755, 0, 0)
    root.children = [
        N(b"bin", stat.S_IFDIR | 0o755, 0, 0, children=[
            N(b"busybox", stat.S_IFREG | 0o4755, 0, 0, big),
            N(b"sh", stat.S_IFLNK | 0o777, 0, 0, b"busybox"),
        ]),
        N(b"dev", stat.S_IFDIR | 0o755, 0, 0, children=[
            N(b"console", stat.S_IFCHR | 0o600, 0, 0, rdev=(5 << 8) | 1),
            N(b"initctl", stat.S_IFIFO | 0o600, 0, 0),
        ]),
        N(b"empty", stat.S_IFDIR | 0o700, 0, 0),
        N(b"etc", stat.S_IFDIR | 0o755, 0, 0, children=[
            N(b"a", stat.S_IFREG | 0o644, 1000, 100, b"same\n"),
            N(b"b", stat.S_IFREG | 0o644, 0, 0, b"same\n"),
            N(b"zero", stat.S_IFREG | 0o644, 0, 0, b""),
        ]),
    ]
    return cramfs.Image(root, b"Compressed", 0, cramfs.FLAG_FSID_VERSION_2 | cramfs.FLAG_SORTED_DIRS)


def entries(part):
    return {e.path: e for e in part.entries()}


class CramfsTest(unittest.TestCase):
    def test_write_read_round_trip(self):
        blob = cramfs.write(sample_image())
        self.assertEqual(len(blob) % cramfs.PAGE, 0)
        self.assertEqual(cramfs.write(cramfs.read(blob)), blob)
        e = entries(fstree.CramfsPart(blob))
        self.assertEqual(e["bin/busybox"].data, bytes(range(256)) * 40)
        self.assertEqual(e["bin/busybox"].mode, 0o4755)
        self.assertEqual(e["bin/sh"].target, "busybox")
        self.assertEqual((e["dev/console"].kind, e["dev/console"].rdev), ("c", 0x501))
        self.assertEqual(e["dev/initctl"].kind, "p")
        self.assertEqual((e["etc/a"].uid, e["etc/a"].gid), (1000, 100))
        self.assertEqual(e["empty"].kind, "d")
        self.assertEqual(e["etc/zero"].data, b"")

    def test_identical_files_share_data(self):
        img = sample_image()
        one = cramfs.write(img)
        img.root.children[3].children[1].data = b"diff\n"
        self.assertGreater(len(cramfs.write(img).rstrip(b"\0")), len(one.rstrip(b"\0")))

    def test_corruption_is_detected(self):
        blob = bytearray(cramfs.write(sample_image()))
        blob[200] ^= 0xFF
        with self.assertRaisesRegex(ValueError, "CRC"):
            cramfs.read(bytes(blob))


class OverlayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.overlay = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, data):
        f = self.overlay / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(data)

    def parts(self):
        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode="w", format=tarfile.GNU_FORMAT) as t:
            for name, mode, data in [("./", 0o755, None), ("./etc/", 0o755, None), ("./etc/a", 0o600, b"same\n")]:
                m = tarfile.TarInfo(name)
                m.mode = mode
                if data is None:
                    m.type = tarfile.DIRTYPE
                    t.addfile(m)
                else:
                    m.size = len(data)
                    t.addfile(m, io.BytesIO(data))
        return [fstree.CramfsPart(cramfs.write(sample_image())), fstree.TarPart(out.getvalue(), mtime=1234)]

    def reparse(self, part):
        if isinstance(part, fstree.CramfsPart):
            return fstree.CramfsPart(part.serialize())
        return fstree.TarPart(part.serialize(), mtime=0)

    def test_files_and_ops(self):
        self.write("files/etc/a", b"changed\n")
        self.write("files/opt/new/run.sh", b"#!/bin/sh\necho hi\n")
        self.write("files/opt/new/data.txt", b"plain\n")
        self.write("files/opt/.gitkeep", b"")
        (self.overlay / "ops").write_text(
            "# comment\n"
            "mkdir var/log 0700\n"
            "symlink opt/current new\n"
            "chmod opt/new/data.txt 0600\n"
            "chown opt/new/data.txt 1000 100\n"
            "rm etc/a\n",
            encoding="utf-8",
        )
        for part in self.parts():
            with self.subTest(type(part).__name__):
                fstree.apply_overlay(part, self.overlay)
                e = entries(self.reparse(part))
                self.assertNotIn("etc/a", e)
                self.assertNotIn("opt/.gitkeep", e)
                self.assertEqual(e["opt/new/run.sh"].mode, 0o755)
                self.assertEqual(e["opt/new/data.txt"].data, b"plain\n")
                self.assertEqual(e["opt/new/data.txt"].mode, 0o600)
                self.assertEqual((e["opt/new/data.txt"].uid, e["opt/new/data.txt"].gid), (1000, 100))
                self.assertEqual(e["opt"].kind, "d")
                self.assertEqual((e["var/log"].kind, e["var/log"].mode), ("d", 0o700))
                self.assertEqual((e["opt/current"].kind, e["opt/current"].target), ("l", "new"))

    def test_replacing_a_file_keeps_its_metadata(self):
        self.write("files/etc/a", b"changed\n")
        for part in self.parts():
            with self.subTest(type(part).__name__):
                before = entries(part)["etc/a"]
                fstree.apply_overlay(part, self.overlay)
                after = entries(self.reparse(part))["etc/a"]
                self.assertEqual(after.data, b"changed\n")
                self.assertEqual((after.mode, after.uid, after.gid), (before.mode, before.uid, before.gid))

    def test_crlf_script_is_rejected(self):
        self.write("files/x.sh", b"#!/bin/sh\r\necho hi\r\n")
        with self.assertRaisesRegex(ValueError, "CRLF"):
            fstree.apply_overlay(self.parts()[0], self.overlay)

    def test_bad_op_reports_its_line(self):
        (self.overlay / "ops").write_text("mkdir ok\nfrobnicate x\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, r"ops:2: unknown op"):
            fstree.apply_overlay(self.parts()[0], self.overlay)

    def test_rm_of_missing_path_fails(self):
        (self.overlay / "ops").write_text("rm nope\n", encoding="utf-8")
        for part in self.parts():
            with self.subTest(type(part).__name__), self.assertRaisesRegex(ValueError, "ops:1"):
                fstree.apply_overlay(part, self.overlay)


if __name__ == "__main__":
    unittest.main()
