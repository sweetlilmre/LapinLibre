# SPDX-License-Identifier: MIT
"""LapinLibre's core image.

build_core() makes the root filesystem and /usr from nothing, as system/core-files.txt lists them:
the system build's target files, LapinLibre's own builds (own_builds()), links, directories and device nodes,
each with its mode and owner. Then the core is checked:

1. Until nothing changes, every ELF's libraries and interpreter must resolve, as on the rabbit
   the root's programs from the root's /lib alone (they run before
   /usr is mounted, and while a USB-key install has wiped it); /usr's through their RPATH,
   /usr/karotz/lib (ld.so.conf), /lib, /usr/lib. A library missing from the list comes from the
   system build's target (into /lib if a root program needs it, otherwise /usr/lib, never both) and is
   reported; one the target lacks fails the build.
2. A library in /lib, /usr/lib or /usr/karotz/lib that nothing resolves to fails the build (apart
   from DLOPENED and LapinLibre's own): the list says what the core has.
3. Every undefined dynamic symbol of every ELF must be defined by the libraries it resolves to,
   apart from those system/image-accepted-undefined.txt accepts.
"""

import hashlib
import os
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

LIBDIRS = {"rootfs": ("lib",), "usr": ("lib", "karotz/lib")}
# Loaded with dlopen(), so no DT_NEEDED names them: uClibc-ng's thread cancellation unwinds with it.
DLOPENED = {"libgcc_s.so.1"}
# Plug-ins that take symbols from the program that loads them: (part, directory prefix) -> program.
HOSTS = {("usr", "lib/python2.4/"): ("usr", "bin/python2.4")}


class SystemBuildError(RuntimeError):
    pass


_cache = {}


def _tool(name, args, data):
    key = (name, tuple(args), hashlib.sha1(data).hexdigest())
    if key not in _cache:
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(data)
            path = f.name
        try:
            _cache[key] = subprocess.run([name, *args, path], capture_output=True, text=True,
                                         errors="replace").stdout
        finally:
            os.unlink(path)
    return _cache[key]


def is_elf(data):
    return data[:4] == b"\x7fELF"


def stripped(data):
    """An ELF file's data without its debug information and unneeded symbols (the host's ARM strip,
    --strip-unneeded, as Buildroot strips its packages); any other file's data unchanged."""
    if not is_elf(data):
        return data
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(data)
        path = f.name
    try:
        subprocess.run(["arm-linux-gnueabi-strip", "--strip-unneeded", "--remove-section=.comment", path],
                       check=True, capture_output=True)
        with open(path, "rb") as f:
            return f.read()
    except subprocess.CalledProcessError as e:
        raise SystemBuildError(f"arm-linux-gnueabi-strip failed: {e.stderr.decode(errors='replace').strip()}")
    finally:
        os.unlink(path)


def is_arm_elf(data):
    return is_elf(data) and data[5] == 1 and int.from_bytes(data[18:20], "little") == 40


def dynamic(data):
    """(needed, soname, rpath dirs, interpreter) of an ELF."""
    out = _tool("arm-linux-gnueabi-readelf", ["-d", "-l", "-W"], data)
    def bracket(line):
        return line.split("[", 1)[1].rsplit("]", 1)[0]
    needed = [bracket(l) for l in out.splitlines() if "(NEEDED)" in l]
    soname = next((bracket(l) for l in out.splitlines() if "(SONAME)" in l), None)
    rpath = [d for l in out.splitlines() if "(RPATH)" in l or "(RUNPATH)" in l for d in bracket(l).split(":") if d]
    interp = next((l.split(":", 1)[1].strip().rstrip("]") for l in out.splitlines()
                   if "Requesting program interpreter" in l), None)
    return needed, soname, rpath, interp


def symbols(data):
    """(defined, undefined required) dynamic symbol names, versions stripped.

    A weak reference may stay unresolved, unless it names a symbol version: uClibc-ng's loader
    refuses an unresolved versioned weak reference ("can't resolve symbol"), as it did for k2k's
    playback and rec, whose __register_frame_info@GLIBC_2.0 no libgcc_s has."""
    defined = {l.split()[-1].split("@")[0]
               for l in _tool("arm-linux-gnueabi-nm", ["-D", "--defined-only"], data).splitlines() if l.strip()}
    undefined = set()
    for l in _tool("arm-linux-gnueabi-nm", ["-D", "--undefined-only"], data).splitlines():
        parts = l.split()
        if len(parts) >= 2 and (parts[-2] == "U" or (parts[-2] in ("w", "v") and "@" in parts[-1])):
            undefined.add(parts[-1].split("@")[0])
    return defined, undefined


def stem(soname):
    return soname.split(".so")[0]


class Tree:
    """The system build's target: its ARM libraries by soname (in lib/ and usr/lib/), with their links."""

    def __init__(self, root: Path):
        self.root = root
        self.by_soname, self.by_stem = {}, {}
        for d in ("lib", "usr/lib"):
            for p in sorted((root / d).iterdir()):
                if p.is_symlink() or not p.is_file():
                    continue
                data = p.read_bytes()
                if not is_arm_elf(data):
                    continue
                soname = dynamic(data)[1]
                if soname and soname not in self.by_soname:
                    self.by_soname[soname] = p
                    self.by_stem.setdefault(stem(soname), []).append(soname)

    def file(self, rel):
        """The ARM ELF at rel (links followed, inside the tree), or None."""
        p = self.root / rel
        for _ in range(10):
            if p.is_symlink():
                t = os.readlink(p)
                p = (self.root / t.lstrip("/")) if t.startswith("/") else p.parent / t
            else:
                break
        if p.is_file():
            data = p.read_bytes()
            if is_arm_elf(data):
                return data
        return None

    def links_to(self, real: Path):
        """The names in real's directory that lead to it (its links), and its own name."""
        names = [real.name]
        for p in real.parent.iterdir():
            if p.is_symlink():
                t = Path(os.path.normpath(p.parent / os.readlink(p)))
                if t == real or (t.is_symlink() and Path(os.path.normpath(t.parent / os.readlink(t))) == real):
                    names.append(p.name)
        return names


class Image:
    """The two parts with an index kept in step with every change."""

    def __init__(self, rootfs, usr):
        self.parts = {"rootfs": rootfs, "usr": usr}
        self.index = {label: {e.path: e for e in part.entries()} for label, part in self.parts.items()}

    def put(self, label, path, data):
        self.parts[label].put_file(path, data)
        e = self.index[label].get(path)
        if e is not None and e.kind == "f":
            e.data = data
        else:  # new, or it replaced a link
            self.index[label] = {e.path: e for e in self.parts[label].entries()}

    def link(self, label, path, target):
        if path in self.index[label]:
            self.parts[label].remove(path)
        self.parts[label].symlink(path, target)
        self.index[label] = {e.path: e for e in self.parts[label].entries()}

    def remove(self, label, path):
        self.parts[label].remove(path)
        self.index[label].pop(path, None)

    def entry(self, label, path):
        return self.index[label].get(path)

    def resolve(self, label, path):
        """The file entry path leads to (links followed within the part), or None."""
        path = os.path.normpath(path).lstrip("/")
        for _ in range(10):
            e = self.index[label].get(path)
            if e is None or e.kind != "l":
                return e
            t = e.target
            path = t.lstrip("/") if t.startswith("/") else str(PurePosixPath(path).parent / t)
            path = os.path.normpath(path).lstrip("/")
        return None

    def names_in(self, label, d):
        return {p.rsplit("/", 1)[-1] for p, e in self.index[label].items()
                if e.kind in "fl" and str(PurePosixPath(p).parent) == d}

    def links_to(self, label, path):
        d = str(PurePosixPath(path).parent)
        return [p for p, e in self.index[label].items()
                if e.kind == "l" and str(PurePosixPath(p).parent) == d and self.resolve(label, p) is self.index[label].get(path)]

    def elfs(self):
        for label in self.parts:
            for p, e in self.index[label].items():
                if e.kind == "f" and is_elf(e.data):
                    yield label, p, e


def read_accepted(path):
    """{file name: symbols} from "name: symbol ...: reason" lines."""
    out = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                name, _, rest = line.partition(":")
                out[name.strip()] = set(rest.partition(":")[0].split())
    return out


def _libdir_of(label, path):
    d = str(PurePosixPath(path).parent)
    return d if d in LIBDIRS[label] else None


def install_library(img, tree, soname, label, d, changes):
    """The system build's library soname into label:d, under its own name and links; returns its path."""
    real = tree.by_soname[soname]
    img.put(label, f"{d}/{real.name}", real.read_bytes())
    # Its mode as the system build installs it: a new file would otherwise be 0644, and the kernel won't start a
    # program whose interpreter (ld-uClibc) isn't executable (qemu-user doesn't check, the rabbit does).
    img.parts[label].chmod(f"{d}/{real.name}", real.stat().st_mode & 0o7777)
    for name in tree.links_to(real):
        if name != real.name:
            img.link(label, f"{d}/{name}", real.name)
    if soname not in tree.links_to(real):  # the loader looks for the soname
        img.link(label, f"{d}/{soname}", real.name)
    changes.append(f"{label}: {d}/{real.name} (system build, {soname}: not in system/core-files.txt)")
    return f"{d}/{real.name}"


def remove_library(img, label, path):
    """Remove a library file and the links in its directory that lead to it."""
    for p in img.links_to(label, path):
        img.remove(label, p)
    img.remove(label, path)


def read_core(path):
    """[(n, label, path, kind, fields, reason)] from system/core-files.txt (format in its header)."""
    out = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        body, _, reason = line.partition("  # ")
        body = body.strip()
        if not body or body.startswith("#"):
            continue
        words = body.split()
        label, _, p = words[0].partition(":")
        kind = words[1] if len(words) > 1 else ""
        want = {"f": 5, "l": 3, "d": 4, "c": 5, "b": 5}.get(kind)
        if label not in LIBDIRS or not p or want is None or len(words) != want:
            raise SystemBuildError(f"{path.name}:{n}: can't read {line!r}")
        out.append((n, label, p, kind, words[2:], reason.strip()))
    return out


def build_core(rootfs, usr, core_files, target: Path, own: dict, accepted_file: Path, changes,
               core_dir: "Path | None" = None, generated: "dict | None" = None):
    """Fill the empty parts from the core lists (system/core-files.txt), then check the libraries
    and symbols; returns the count of entries made.
    Sources beyond target and own: core, core_dir/<part>/<path>; generated, generated[(part, path)],
    or for a path dir/*, every generated file in dir."""
    if not (target / "lib").is_dir():
        raise SystemBuildError(f"no {target} (run ./build.sh system)")
    parts = {"rootfs": rootfs, "usr": usr}
    generated = generated or {}
    entries, seen, placed = [], {}, set()
    for core_file in core_files:
        for n, label, path, kind, f, reason in read_core(core_file):
            where = f"{core_file.name}:{n}: {label}:{path}"
            if (label, path) in seen:
                raise SystemBuildError(f"{where}: listed already ({seen[(label, path)]})")
            seen[(label, path)] = f"{core_file.name}:{n}"
            entries.append((where, label, path, kind, f, reason))
    for where, label, path, kind, f, _ in entries:
        part = parts[label]
        if kind == "l":
            part.symlink(path, f[0])
            continue
        mode, (uid, gid) = int(f[0], 8), (int(x) for x in f[1].split(":"))
        if kind == "d":
            part.mkdir(path, mode)
        elif kind in "cb":
            if label != "rootfs":
                raise SystemBuildError(f"{where}: device nodes go in the root filesystem")
            part.mknod(path, kind, mode, int(f[2], 0))
        else:
            source = f[2]
            if source == "own":
                if (label, path) not in own:
                    raise SystemBuildError(f"{where}: no such build of LapinLibre's (run ./build.sh userland v8)")
                data = own[(label, path)]
            elif source == "core":
                src = (core_dir or Path("/nonexistent")) / label / path
                if not src.is_file():
                    raise SystemBuildError(f"{where}: no {src}")
                data = src.read_bytes()
                if data.startswith(b"#!") and b"\r" in data:
                    raise SystemBuildError(f"{where}: {src} has CRLF line endings; the rabbit's shell can't run it")
            elif source == "generated" and path.endswith("/*"):
                # Every generated file in that directory, each with this entry's mode and owner.
                folder = path[:-1]
                names = sorted(p for (l, p) in generated if l == label and p.startswith(folder) and "/" not in p[len(folder):])
                if not names:
                    raise SystemBuildError(f"{where}: the image build made no files there")
                for name in names:
                    part.put_file(name, generated[(label, name)])
                    part.chmod(name, mode)
                    part.chown(name, uid, gid)
                    placed.add((label, name))
                continue
            elif source == "generated":
                if (label, path) not in generated:
                    raise SystemBuildError(f"{where}: the image build made no such file")
                data = generated[(label, path)]
            elif source == "target" or source.startswith("target:"):
                rel = source[7:] if source.startswith("target:") else (path if label == "rootfs" else f"usr/{path}")
                src = target / rel
                if not src.is_file() or src.is_symlink():
                    raise SystemBuildError(f"{where}: no {rel} in the system build's target")
                data = src.read_bytes()
            else:
                raise SystemBuildError(f"{where}: unknown source {source!r}")
            part.put_file(path, data)
        part.chmod(path, mode)
        part.chown(path, uid, gid)
    listed = {(label, path) for _, label, path, kind, f, _ in entries if kind == "f" and f[2] == "own"}
    missing_own = sorted(f"{l}:{p}" for l, p in own if (l, p) not in listed)
    if missing_own:
        raise SystemBuildError("LapinLibre builds not in system/core-files.txt: " + ", ".join(missing_own))
    used = placed | {(label, path) for _, label, path, kind, f, _ in entries if kind == "f" and f[2] == "generated"}
    unused = sorted(f"{l}:{p}" for l, p in generated if (l, p) not in used)
    if unused:
        raise SystemBuildError("generated files no core list places: " + ", ".join(unused))
    check_core(Image(rootfs, usr), Tree(target), own, read_accepted(accepted_file), changes)
    return len(entries)


def check_core(img, tree, own, accepted, changes):
    """The library and symbol checks (the module's steps 1 to 3)."""
    def search(label, rpath):
        if label == "rootfs":
            return [("rootfs", "lib")]
        dirs = []
        for r in rpath:
            r = r.lstrip("/")
            dirs.append(("usr", r[4:]) if r.startswith("usr/") else ("rootfs", r))
        return dirs + [("usr", "karotz/lib"), ("rootfs", "lib"), ("usr", "lib")]

    def find(soname, dirs):
        for label, d in dirs:
            if soname in img.names_in(label, d):
                return label, f"{d}/{soname}"
        return None

    # 1. Closure.
    unresolved = []
    for _ in range(50):
        missing = {}  # soname -> set of labels that need it
        unresolved = []
        for label, path, e in list(img.elfs()):
            needed, _, rpath, interp = dynamic(e.data)
            if interp and img.resolve("rootfs", interp.lstrip("/")) is None:
                unresolved.append(f"{label}:{path}: interpreter {interp}")
            for n in needed:
                if find(n, search(label, rpath)) is None:
                    missing.setdefault(n, set()).add(label)
        changed = False
        for soname, labels in sorted(missing.items()):
            into = "rootfs" if "rootfs" in labels else "usr"
            if soname in tree.by_soname:
                # a /usr copy moves to /lib when a root program needs it
                if into == "rootfs" and soname in img.names_in("usr", "lib"):
                    e = img.resolve("usr", f"lib/{soname}")
                    p = next(p for p, x in img.index["usr"].items() if x is e)
                    remove_library(img, "usr", p)
                install_library(img, tree, soname, into, "lib", changes)
                changed = True
            else:
                users = sorted(f"{l}:{p}" for l, p, e in img.elfs() if soname in dynamic(e.data)[0])
                unresolved.append(f"{soname}, needed by {', '.join(users[:6])}")
        if not changed:
            break
    if unresolved:
        raise SystemBuildError("unresolved in the core image: " + "; ".join(sorted(set(unresolved))))

    # 2. Libraries nothing resolves to.
    wanted = set()
    for label, path, e in img.elfs():
        needed, _, rpath, _ = dynamic(e.data)
        for n in needed:
            hit = find(n, search(label, rpath))
            if hit:
                wanted.add(id(img.resolve(*hit)))
    dead = sorted(f"{label}:{path}" for label, path, e in img.elfs()
                  if _libdir_of(label, path) and ".so" in path.rsplit("/", 1)[-1] and id(e) not in wanted
                  and (label, path) not in own and path.rsplit("/", 1)[-1] not in DLOPENED)
    if dead:
        raise SystemBuildError("system/core-files.txt lists libraries nothing uses: " + ", ".join(dead))

    # 3. Symbols. As the loader binds: a program's global scope is the program and every library it
    # resolves to, and each of those objects' undefined symbols must be defined somewhere in that
    # scope (a library may leave a symbol to the program that loads it). A library no program links
    # (a plug-in, loaded with dlopen) is checked with its own scope, and its host program's (HOSTS).
    def scope(label, path, e):
        """[(label, path, entry)] of e and every library it resolves to, transitively."""
        objs, seen = [(label, path, e)], {(label, path)}
        queue = [(label, n, dynamic(e.data)[2]) for n in dynamic(e.data)[0]]
        while queue:
            l0, n, rp = queue.pop()
            hit = find(n, search(l0, rp))
            if hit is None:
                continue
            le = img.resolve(*hit)
            key = (hit[0], next(p for p, x in img.index[hit[0]].items() if x is le))
            if key in seen:
                continue
            seen.add(key)
            objs.append((key[0], key[1], le))
            needed2, _, rp2, _ = dynamic(le.data)
            queue += [(key[0], m, rp2) for m in needed2]
        return objs

    def lacking(objs):
        defined = set().union(*(symbols(o.data)[0] for _, _, o in objs))
        out = []
        for l, p, o in objs:
            missing = symbols(o.data)[1] - defined - accepted.get(p.rsplit("/", 1)[-1], set())
            if missing:
                out.append((l, p, missing))
        return out

    failures, in_a_program = {}, set()
    for label, path, e in img.elfs():
        if dynamic(e.data)[3]:  # a program: it has an interpreter
            objs = scope(label, path, e)
            in_a_program |= {(l, p) for l, p, _ in objs}
            for l, p, missing in lacking(objs):
                failures.setdefault((l, p), set()).update(missing)
    for label, path, e in img.elfs():
        if (label, path) in in_a_program or dynamic(e.data)[3]:
            continue
        objs = scope(label, path, e)
        for (hl, prefix), (pl, program) in HOSTS.items():
            host = img.resolve(pl, program)
            if hl == label and path.startswith(prefix) and host is not None:
                objs += scope(pl, program, host)
        for l, p, missing in lacking(objs):
            if (l, p) == (label, path):
                failures.setdefault((l, p), set()).update(missing)
    failures = [f"{l}:{p} lacks {len(m)}: {' '.join(sorted(m)[:8])}" for (l, p), m in sorted(failures.items())]
    if failures:
        raise SystemBuildError("undefined symbols in the core image:\n  " + "\n  ".join(failures))


def own_builds(userland: Path, v8: Path):
    """{(label, path): data}: system/userland.sh's tree at its paths on the rabbit, and the system build's V8,
    the ELF files stripped."""
    own = {}
    for p in sorted((userland / "rootfs").glob("*")):
        if p.is_file():
            own[("rootfs", f"karotz/bin/{p.name}")] = p.read_bytes()
    for p in sorted((userland / "sbin").glob("*")):  # programs for /sbin
        if p.is_file():
            own[("rootfs", f"sbin/{p.name}")] = p.read_bytes()
    for p in sorted((userland / "usr").glob("*")):
        if p.is_file():
            own[("usr", f"karotz/bin/{p.name}")] = p.read_bytes()
    for p in sorted((userland / "usr" / "bin").glob("*")):  # karotz-player
        if p.is_file():
            own[("usr", f"bin/{p.name}")] = p.read_bytes()
    for p in sorted((userland / "usr" / "scripts" / "k2k").glob("*")):  # k2k's playback and rec
        if p.is_file():
            own[("usr", f"scripts/k2k/{p.name}")] = p.read_bytes()
    for p in sorted((userland / "usr" / "lib").glob("*")):
        if p.is_file():
            own[("usr", f"karotz/lib/{p.name}")] = p.read_bytes()
    if v8.is_file():
        own[("usr", "lib/libv8.so")] = v8.read_bytes()
    shell = v8.parent / "v8-shell"  # V8's sample shell, built with libv8.so
    if shell.is_file():
        own[("usr", "bin/v8-shell")] = shell.read_bytes()
    # The image gets them stripped; the build trees keep their debug information for debugging.
    return {key: stripped(data) for key, data in own.items()}
