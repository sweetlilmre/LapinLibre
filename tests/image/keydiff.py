# SPDX-License-Identifier: MIT
"""Compare the installed files of two USB keys, or list a key's.

    keydiff.py KEY_A KEY_B     compare
    keydiff.py --list KEY      the layout, for tests/expected/key-files.txt

For the root filesystem (rootfs.img.gz) and /usr (usr.tar.gz) of each key, every entry's path,
kind, mode, owner, link target, device number and data (by SHA-256). Prints one line per
difference and a summary; the exit status is 1 if the installed files differ. The build stamp
(rootfs:karotz/etc/lapinlibre-build) differs from build to build and is left out. The list has
everything but the data, which changes with every build of LapinLibre's programs.
"""

import gzip
import hashlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "image" / "tools"))
import fstree  # noqa: E402

STAMP = ("rootfs", "karotz/etc/lapinlibre-build")


def installed(key: Path):
    """{(part, path): (kind, mode, uid, gid, link target, rdev, sha256)}."""
    parts = {"rootfs": fstree.CramfsPart(gzip.decompress((key / "rootfs.img.gz").read_bytes())),
             "usr": fstree.TarPart(gzip.decompress((key / "usr.tar.gz").read_bytes()), mtime=0)}
    out = {}
    for label, part in parts.items():
        for e in part.entries():
            if (label, e.path) == STAMP:
                continue
            digest = hashlib.sha256(e.data).hexdigest()[:16] if e.kind == "f" else ""
            out[(label, e.path)] = (e.kind, f"{e.mode:04o}", e.uid, e.gid, e.target, e.rdev, digest)
    return out


def diff(a, b):
    """Lines describing how b differs from a."""
    names = ("kind", "mode", "uid", "gid", "link", "rdev", "data")
    lines = []
    for k in sorted(a.keys() | b.keys()):
        where = f"{k[0]}:{k[1]}"
        if k not in b:
            lines.append(f"only in A: {where}")
        elif k not in a:
            lines.append(f"only in B: {where}")
        elif a[k] != b[k]:
            what = ", ".join(f"{n} {x!s} -> {y!s}" for n, x, y in zip(names, a[k], b[k]) if x != y)
            lines.append(f"differs:   {where}: {what}")
    return lines


def layout(key: Path):
    """The key's entries as lines, without their data."""
    lines = []
    for (label, path), (kind, mode, uid, gid, target, rdev, _) in sorted(installed(key).items()):
        extra = f" -> {target}" if kind == "l" else f" {rdev:#x}" if kind in "cb" else ""
        lines.append(f"{label}:{path} {kind} {mode} {uid}:{gid}{extra}")
    return lines


def main():
    if sys.argv[1:2] == ["--list"]:
        print("\n".join(layout(Path(sys.argv[2]))))
        return 0
    a, b = installed(Path(sys.argv[1])), installed(Path(sys.argv[2]))
    lines = diff(a, b)
    for line in lines:
        print(line)
    print(f"{len(a)} entries in A, {len(b)} in B, {len(lines)} differences")
    return 1 if lines else 0


if __name__ == "__main__":
    sys.exit(main())
