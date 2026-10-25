# SPDX-License-Identifier: MIT
"""Sysroots of LapinLibre's core, for running its programs under qemu-user.

    sysroot.py core [DEST]          (default DEST: $LL_WORK/sysroot-core)
                      the core as image/tools/image.py builds it (make_core), with no key: what
                      the tests run on
    sysroot.py sysroot-image KEY    $LL_WORK/sysroot-image: a built USB key's own rootfs.img.gz
                      and usr.tar.gz, to test exactly what it installs

Either way the two parts are laid out as the rabbit mounts them (write_sysroot), /etc is set up as
the boot sets it up (finish_etc), the core's own ldconfig makes ld.so.cache under qemu-user, as
linuxrc does on the rabbit, and /var is left empty. Run on Linux (WSL), where symlinks and modes
are kept.
"""

import gzip
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent / "scripts"))
import fstree  # noqa: E402
import image  # noqa: E402
import system  # noqa: E402
from lapin import load_config, work_dir  # noqa: E402


class SysrootError(Exception):
    pass


def write_sysroot(rootfs, usr, dest):
    """rootfs at dest and usr at dest/usr, as files, links and directories (no device nodes)."""
    if dest.exists():
        shutil.rmtree(dest)
    for prefix, part in (("", rootfs), ("usr/", usr)):
        for e in part.entries():
            path = dest / (prefix + e.path)
            if e.kind == "d":
                path.mkdir(parents=True, exist_ok=True)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.is_symlink() or path.exists():
                if path.is_dir() and not path.is_symlink():
                    continue
                path.unlink()
            if e.kind == "l":
                target = e.target
                if target.startswith("/"):
                    # qemu-user resolves absolute links on the host: keep them inside the sysroot.
                    target = os.path.relpath(dest / target.lstrip("/"), path.parent)
                os.symlink(target, path)
            elif e.kind == "f":
                path.write_bytes(e.data)
                path.chmod(e.mode & 0o777)
    finish_etc(dest)
    print(f"sysroot: {dest}")


def finish_etc(dest):
    """Set up /etc as the rabbit's boot does, and prepare for ld.so.cache.

    /etc is a symlink to /usr/etc; at boot, linuxrc links these files from
    the read-only /karotz/etc and copies a few more if they are missing.
    qemu-user only redirects a path into the sysroot if it already exists
    there, so the files ldconfig writes are created empty first (make_cache
    then runs the core's own ldconfig under qemu-user).
    """
    etc = dest / "usr" / "etc"
    etc.mkdir(parents=True, exist_ok=True)
    for name in ("fstab", "group", "hosts", "ld.so.conf", "mdev.conf", "modules.conf", "motd", "profile", "services", "shells"):
        link = etc / name
        if link.is_symlink() or link.exists():
            link.unlink()
        os.symlink(f"../../karotz/etc/{name}", link)
    for name in ("passwd", "inittab", "inetd.conf"):
        if not (etc / name).exists():
            (etc / name).write_bytes((dest / "karotz" / "etc" / name).read_bytes())
    for name in ("ld.so.cache", "ld.so.cache~"):
        (etc / name).touch()
    # The time zone: startup.sh creates /etc/TZ at boot. Empty here, which
    # uClibc reads as UTC, as with no file; it exists so that tests can bind
    # their own file over it (see tests/scenarios/controller_platform.py).
    (etc / "TZ").touch()


def make_cache(dest):
    """ld.so.cache from the sysroot's own ldconfig, and an empty /var, as on the rabbit at boot."""
    try:
        subprocess.run(["qemu-arm", "-cpu", "arm926", "-L", str(dest), str(dest / "sbin" / "ldconfig")],
                       capture_output=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SysrootError(f"ldconfig under qemu-arm: {e}") from e
    # ldconfig writes ld.so.cache~ and renames it, but qemu-user doesn't redirect the rename.
    cache = dest / "usr" / "etc" / "ld.so.cache~"
    if not cache.is_file() or cache.stat().st_size == 0:
        raise SysrootError(f"the sysroot's ldconfig wrote no cache ({cache})")
    shutil.copyfile(cache, dest / "usr" / "etc" / "ld.so.cache")
    shutil.rmtree(dest / "var", ignore_errors=True)
    if (dest / "var").is_symlink():
        (dest / "var").unlink()
    (dest / "var").mkdir()


def cmd_core(dest=None):
    """The core, built as the key's is (image.make_core), as a sysroot."""
    dest = Path(dest) if dest else work_dir() / "sysroot-core"
    rootfs, usr = image.make_core(load_config(), image.source_epoch(), image.git_describe(), [])
    write_sysroot(rootfs, usr, dest)
    make_cache(dest)


def cmd_sysroot_image(key):
    """A built key's images as a sysroot: what an install puts on the rabbit."""
    key = Path(key)
    usr_gz = (key / "usr.tar.gz").read_bytes()
    rootfs = fstree.CramfsPart(gzip.decompress((key / "rootfs.img.gz").read_bytes()))
    usr = fstree.TarPart(gzip.decompress(usr_gz), mtime=int.from_bytes(usr_gz[4:8], "little"))
    dest = work_dir() / "sysroot-image"
    write_sysroot(rootfs, usr, dest)
    make_cache(dest)


def main(argv):
    commands = {"core": (cmd_core, (0, 1)), "sysroot-image": (cmd_sysroot_image, (1,))}
    if not argv or argv[0] not in commands or len(argv) - 1 not in commands[argv[0]][1]:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        commands[argv[0]][0](*argv[1:])
    except (SysrootError, image.ImageError, system.SystemBuildError, ValueError, OSError, RuntimeError,
            subprocess.CalledProcessError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
