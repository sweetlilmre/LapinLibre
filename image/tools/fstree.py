# SPDX-License-Identifier: MIT
"""The two Karotz filesystems behind one interface, plus manifests and overlays.

`CramfsPart` wraps the root filesystem image and `TarPart` wraps the /usr
(yaffs) archive. Paths are POSIX, relative to the filesystem's mount point,
with no leading slash ("etc/inittab", "karotz/scripts/network.sh").

An overlay is a directory with two optional parts:

  files/   copied over the filesystem; replaced files keep their metadata,
           new files get 0755 if they start with "#!" and 0644 otherwise
  ops      one operation per line, applied after files/:
             rm <path>                 remove (directories recursively)
             mkdir <path> [mode]
             symlink <path> <target>
             chmod <path> <mode>
             chown <path> <uid> <gid>
"""

import io
import shlex
import stat
import tarfile
from dataclasses import dataclass
from pathlib import Path

import cramfs

KIND_BY_FMT = {
    stat.S_IFREG: "f",
    stat.S_IFDIR: "d",
    stat.S_IFLNK: "l",
    stat.S_IFCHR: "c",
    stat.S_IFBLK: "b",
    stat.S_IFIFO: "p",
    stat.S_IFSOCK: "s",
}


@dataclass
class Entry:
    path: str
    kind: str  # f d l c b p s
    mode: int  # permission bits, including setuid/setgid/sticky
    uid: int
    gid: int
    target: str = ""  # symlinks
    rdev: int = 0  # device nodes
    data: bytes = b""  # regular files

    def manifest_line(self):
        extra = self.target if self.kind == "l" else f"{self.rdev:#x}" if self.kind in "cb" else ""
        return "\t".join([self.kind, f"{self.mode:04o}", str(self.uid), str(self.gid), self.path, extra]).rstrip("\t")


def _split(path):
    parts = [p for p in path.strip("/").split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        raise ValueError(f"bad path {path!r}")
    return parts


class CramfsPart:
    def __init__(self, image: bytes):
        self.img = cramfs.read(image)

    @classmethod
    def empty(cls):
        """A root filesystem with nothing in it, headed as the Karotz's own images are."""
        part = cls.__new__(cls)
        part.img = cramfs.Image(cramfs.Node(b"", stat.S_IFDIR | 0o755, 0, 0), b"Compressed", 0, 0x3)
        return part

    def serialize(self) -> bytes:
        return cramfs.write(self.img)

    def entries(self):
        def walk(node, prefix):
            for c in sorted(node.children, key=lambda c: c.name):
                path = prefix + c.name.decode("utf-8", "surrogateescape")
                kind = KIND_BY_FMT[stat.S_IFMT(c.mode)]
                yield Entry(
                    path,
                    kind,
                    stat.S_IMODE(c.mode),
                    c.uid,
                    c.gid,
                    target=c.data.decode("utf-8", "surrogateescape") if kind == "l" else "",
                    rdev=c.rdev,
                    data=c.data if kind == "f" else b"",
                )
                if c.is_dir:
                    yield from walk(c, path + "/")

        yield from walk(self.img.root, "")

    def _lookup(self, path, create_parents=False):
        parts = _split(path)
        node = self.img.root
        for p in parts[:-1]:
            child = self._child(node, p)
            if child is None:
                if not create_parents:
                    raise FileNotFoundError(path)
                child = cramfs.Node(p.encode(), stat.S_IFDIR | 0o755, 0, 0)
                node.children.append(child)
            if not child.is_dir:
                raise NotADirectoryError(path)
            node = child
        return node, parts[-1], self._child(node, parts[-1])

    @staticmethod
    def _child(node, name):
        name = name.encode("utf-8", "surrogateescape")
        return next((c for c in node.children if c.name == name), None)

    def _put(self, path, mode, data=b"", replace_keeps_meta=False):
        parent, name, existing = self._lookup(path, create_parents=True)
        if existing is not None:
            if replace_keeps_meta and stat.S_ISREG(existing.mode):
                existing.data = data
                return
            parent.children.remove(existing)
        parent.children.append(cramfs.Node(name.encode("utf-8", "surrogateescape"), mode, 0, 0, data))

    def put_file(self, path, data):
        self._put(path, stat.S_IFREG | _default_mode(data), data, replace_keeps_meta=True)

    def mkdir(self, path, mode=0o755):
        _, _, existing = self._lookup(path, create_parents=True)
        if existing is None:
            self._put(path, stat.S_IFDIR | mode)
        elif not existing.is_dir:
            raise FileExistsError(path)

    def symlink(self, path, target):
        self._put(path, stat.S_IFLNK | 0o777, target.encode("utf-8", "surrogateescape"))

    def mknod(self, path, kind, mode, rdev):
        """A device node: kind "c" (character) or "b" (block)."""
        parent, name, existing = self._lookup(path, create_parents=True)
        if existing is not None:
            parent.children.remove(existing)
        fmt = stat.S_IFCHR if kind == "c" else stat.S_IFBLK
        parent.children.append(cramfs.Node(name.encode("utf-8", "surrogateescape"), fmt | mode, 0, 0, rdev=rdev))

    def remove(self, path):
        parent, _, existing = self._lookup(path)
        if existing is None:
            raise FileNotFoundError(path)
        parent.children.remove(existing)

    def chmod(self, path, mode):
        node = self._existing(path)
        node.mode = stat.S_IFMT(node.mode) | mode

    def chown(self, path, uid, gid):
        node = self._existing(path)
        node.uid, node.gid = uid, gid

    def _existing(self, path):
        _, _, node = self._lookup(path)
        if node is None:
            raise FileNotFoundError(path)
        return node


class TarPart:
    """A tar archive kept as an ordered member list so it repacks exactly."""

    def __init__(self, archive: bytes, mtime: int):
        self.mtime = mtime  # stamped on members the overlay creates or changes
        with tarfile.open(fileobj=io.BytesIO(archive)) as t:
            self.members = [(m, _read_member(t, m)) for m in t.getmembers()]
        # A /usr archive is GNU tar ("ustar  " magic), as serialize() writes it. TarFile.format on a
        # read is just the library default, so it can't be used to detect this.
        if archive[257:265] != b"ustar  \0":
            raise ValueError("expected a GNU-format tar archive")
        for m, _ in self.members:
            if not (m.isreg() or m.isdir() or m.issym()):
                raise ValueError(f"unsupported tar member type {m.type!r}: {m.name}")

    @classmethod
    def empty(cls, mtime: int):
        """A /usr archive with only its top directory ("."), which the install unpacks over /usr."""
        part = cls.__new__(cls)
        part.mtime, part.members = mtime, []
        top = tarfile.TarInfo(".")
        top.type, top.mode, top.mtime, top.uname, top.gname = tarfile.DIRTYPE, 0o755, mtime, "root", "root"
        part.members.append((top, b""))
        return part

    def serialize(self) -> bytes:
        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode="w", format=tarfile.GNU_FORMAT) as t:
            for m, data in self.members:
                t.addfile(m, io.BytesIO(data) if m.isreg() else None)
        return out.getvalue()

    def entries(self):
        for m, data in self.members:
            path = _norm(m.name)
            if not path:
                continue
            kind = "f" if m.isreg() else "d" if m.isdir() else "l"
            yield Entry(path, kind, m.mode, m.uid, m.gid, target=m.linkname, data=data)

    def _index(self, path):
        want = "/".join(_split(path))
        return next((i for i, (m, _) in enumerate(self.members) if _norm(m.name) == want), None)

    def _new(self, path, kind, mode, data=b"", target=""):
        self._ensure_parents(path)
        m = tarfile.TarInfo("./" + "/".join(_split(path)) + ("/" if kind == tarfile.DIRTYPE else ""))
        m.type, m.mode, m.mtime, m.size, m.linkname = kind, mode, self.mtime, len(data), target
        m.uname = m.gname = "root"
        i = self._index(path)
        if i is None:
            self.members.append((m, data))
        else:
            self.members[i] = (m, data)

    def _ensure_parents(self, path):
        parts = _split(path)
        for n in range(1, len(parts)):
            parent = "/".join(parts[:n])
            i = self._index(parent)
            if i is None:
                self._new(parent, tarfile.DIRTYPE, 0o755)
            elif not self.members[i][0].isdir():
                raise NotADirectoryError(parent)

    def put_file(self, path, data):
        i = self._index(path)
        if i is not None and self.members[i][0].isreg():
            m = self.members[i][0]
            m.size, m.mtime = len(data), self.mtime
            self.members[i] = (m, data)
        else:
            self._new(path, tarfile.REGTYPE, _default_mode(data), data)

    def mkdir(self, path, mode=0o755):
        i = self._index(path)
        if i is None:
            self._new(path, tarfile.DIRTYPE, mode)
        elif not self.members[i][0].isdir():
            raise FileExistsError(path)

    def symlink(self, path, target):
        self._new(path, tarfile.SYMTYPE, 0o777, target=target)

    def remove(self, path):
        want = "/".join(_split(path))
        before = len(self.members)
        self.members = [(m, d) for m, d in self.members if not (_norm(m.name) == want or _norm(m.name).startswith(want + "/"))]
        if len(self.members) == before:
            raise FileNotFoundError(path)

    def chmod(self, path, mode):
        self._member(path).mode = mode

    def chown(self, path, uid, gid):
        m = self._member(path)
        m.uid, m.gid = uid, gid

    def _member(self, path):
        i = self._index(path)
        if i is None:
            raise FileNotFoundError(path)
        return self.members[i][0]


def _read_member(t, m):
    if not m.isreg():
        return b""
    f = t.extractfile(m)
    assert f is not None
    return f.read()


def _norm(name):
    return "/".join(p for p in name.split("/") if p not in ("", "."))


def _default_mode(data):
    return 0o755 if data.startswith(b"#!") else 0o644


def apply_overlay(part, overlay: Path, with_ops=True):
    """Apply overlay/files then (with_ops) overlay/ops to `part`; return a list of changes."""
    log = []
    files = overlay / "files"
    if files.is_dir():
        for f in sorted(p for p in files.rglob("*") if p.is_file() and p.name != ".gitkeep"):
            rel = f.relative_to(files).as_posix()
            data = f.read_bytes()
            if data.startswith(b"#!") and b"\r" in data:
                raise ValueError(f"{f}: script has CRLF line endings; it would not run on the Karotz")
            part.put_file(rel, data)
            log.append(f"put {rel}")
    ops = overlay / "ops"
    if with_ops and ops.is_file():
        for lineno, line in enumerate(ops.read_text(encoding="utf-8").splitlines(), 1):
            args = shlex.split(line, comments=True)
            if not args:
                continue
            op, rest = args[0], args[1:]
            try:
                if op == "rm" and len(rest) == 1:
                    part.remove(rest[0])
                elif op == "mkdir" and len(rest) in (1, 2):
                    part.mkdir(rest[0], int(rest[1], 8) if len(rest) == 2 else 0o755)
                elif op == "symlink" and len(rest) == 2:
                    part.symlink(rest[0], rest[1])
                elif op == "chmod" and len(rest) == 2:
                    part.chmod(rest[0], int(rest[1], 8))
                elif op == "chown" and len(rest) == 3:
                    part.chown(rest[0], int(rest[1]), int(rest[2]))
                else:
                    raise ValueError(f"unknown op or wrong arguments: {line.strip()}")
            except (OSError, ValueError) as e:
                raise ValueError(f"{ops}:{lineno}: {e}") from e
            log.append(" ".join(args))
    return log
