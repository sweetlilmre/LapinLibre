# SPDX-License-Identifier: MIT
"""Write the USB-key install image: LapinLibre's core.

    image.py [--out DIR]     (default: $LL_OUT/usbkey)

Builds the core from system/core-files.txt (the system build, LapinLibre's userland and V8, and
image/core's files; image/tools/system.py) and image/overlay/{rootfs,usr}/files (make_core), with
the system build's kernel ($LL_OUT/system-kernel/zImage, Linux 2.6.32), and writes the key: the images,
autorun.sig (image/usbkey, pinned), the install sounds (tones generated, prompts from
image/usbkey/sound), install-tools.tar, the generated install.conf (the build's id and version and
the checksums the install checks), the key's own scripts from image/usbkey (autorun, install-lib.sh
and applysettings.sh) and lapinlibre.txt, where the user fills in their settings for the install to
apply. The core's Karotz keyring (/karotz/etc/gpg, from image/core) is pinned by MD5 as autorun.sig
is (config.toml [key.md5]). The build reads no .env. The core generates /usr/etc/timezone.list from
the pinned tzdata (image/tools/tzlist.py). Files added carry SOURCE_DATE_EPOCH, or the commit's time.
"""

import argparse
import gzip
import hashlib
import io
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent / "scripts"))
import fstree  # noqa: E402
import system  # noqa: E402
import tzlist  # noqa: E402
from lapin import ROOT, load_config, out_dir, work_dir  # noqa: E402

IMAGE = ROOT / "image"
# Violet's signed file, from the repo as it is (config.toml [key.md5]): the factory system runs a
# stick's autorun only when `gpg --verify autorun.sig` passes.
SIGNED = "autorun.sig"
# The key's own files, copied as they are: the install (autorun and its helpers), the settings the
# user fills in, and their installer. The install's tools are LapinLibre's install-tools.tar
# (system/install-tools.sh).
KEY_FILES = ["autorun", "install-lib.sh", "applysettings.sh", "lapinlibre.txt"]


class ImageError(Exception):
    pass


def md5(data):
    return hashlib.md5(data).hexdigest()


def gz(data, name, mtime):
    out = io.BytesIO()
    with gzip.GzipFile(filename=name, mode="wb", fileobj=out, mtime=mtime, compresslevel=9) as f:
        f.write(data)
    return out.getvalue()


def git_describe():
    """The build's version (scripts/common.sh ll_version), from the release tags (v*) only: the
    release tag on the commit (v0.1.0), else the last one, the commits since it and the commit
    (v0.1.0-3-g898dd4892f69), else the commit; -dirty when tracked files have changes;
    "unversioned" outside git."""
    try:
        return subprocess.run(["git", "describe", "--tags", "--match", "v[0-9]*", "--dirty", "--always",
                               "--abbrev=12"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unversioned"


def source_epoch():
    """The timestamp for the files the build makes: SOURCE_DATE_EPOCH, else the commit's time."""
    if os.environ.get("SOURCE_DATE_EPOCH"):
        return int(os.environ["SOURCE_DATE_EPOCH"])
    try:
        return int(subprocess.run(["git", "log", "-1", "--format=%ct"], cwd=ROOT, capture_output=True,
                                  text=True, check=True).stdout.strip())
    except (OSError, subprocess.CalledProcessError, ValueError):
        raise ImageError("no SOURCE_DATE_EPOCH and no git commit to take the build's time from") from None


def tone_wav(notes, rate=22050):
    """A WAV file (16-bit mono) of notes [(frequency Hz, seconds)], each a sine that fades out. The
    peak level matches the tones' loudness to the spoken sounds' (about -17 dBFS RMS)."""
    frames = bytearray()
    for freq, seconds in notes:
        n = int(rate * seconds)
        for i in range(n):
            level = 0.67 * math.exp(-4.0 * i / n) * min(1.0, i / 200)
            frames += struct.pack("<h", int(32767 * level * math.sin(2 * math.pi * freq * i / rate)))
    header = struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + len(frames), b"WAVE", b"fmt ", 16, 1, 1, rate,
                         rate * 2, 2, 16, b"data", len(frames))
    return header + bytes(frames)


# The core's own sounds (system/core-files.txt): the ready chime and the head button's.
SOUNDS = {"boot-ready.wav": [(523.25, 0.18), (659.25, 0.18), (783.99, 0.45)],
          "button-click.wav": [(880.0, 0.12)],
          "button-double.wav": [(880.0, 0.1), (1046.5, 0.12)],
          "button-triple.wav": [(880.0, 0.1), (987.77, 0.1), (1174.66, 0.14)],
          "button-long.wav": [(440.0, 0.5)]}
# The effects the controller and webcam-daemon play (MP3, by their paths in /usr/karotz/sounds):
# waking up, a known tag or the platform gone, new settings or a recognised command, the shutter.
EFFECTS = {"wakeup.mp3": [(523.25, 0.2), (659.25, 0.2), (783.99, 0.2), (1046.5, 0.9)],
           "notify.mp3": [(1318.51, 0.07), (1760.0, 0.12)],
           "acknowledge.mp3": [(783.99, 0.12), (1174.66, 0.3)],
           "shutter.mp3": [(2093.0, 0.04), (1567.98, 0.08)]}


# The key's install sounds, played by the install's madplay (so MP3). Generated tones: the stick
# seen, the loop while the install runs, and an error. Spoken (image/usbkey/sound, made by
# image/tools/install_prompts.py): the checks starting, passed or failed, the install starting,
# and done.
INSTALL_SOUNDS = {"key-found.mp3": [(659.25, 0.12), (987.77, 0.2)],
                  "install-busy.mp3": [(440.0, 0.1)],
                  "install-error.mp3": [(392.0, 0.3), (311.13, 0.3), (261.63, 0.6)]}
SPOKEN = ["checks-start.mp3", "checks-passed.mp3", "checks-failed.mp3", "install-start.mp3", "install-done.mp3"]


def mp3(wav):
    """wav encoded as MP3 by the host's ffmpeg, without tags or timestamps."""
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "wav", "-i", "pipe:0", "-c:a", "libmp3lame",
           "-b:a", "64k", "-map_metadata", "-1", "-fflags", "+bitexact", "-flags:a", "+bitexact",
           "-write_xing", "0", "-id3v2_version", "0", "-f", "mp3", "pipe:1"]
    try:
        r = subprocess.run(cmd, input=wav, capture_output=True, timeout=120)
    except FileNotFoundError:
        raise ImageError("no ffmpeg on the build host (bash scripts/host-prereqs.sh --install)") from None
    if r.returncode != 0 or not r.stdout:
        raise ImageError(f"ffmpeg failed on an install sound: {r.stderr.decode(errors='replace')[-300:]}")
    return r.stdout


def timezone_list(changes):
    """/usr/etc/timezone.list's text, generated from the pinned tzdata."""
    r = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "scripts" / "fetch.py"), "tzdata", "tzcode"],
                       capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        raise ImageError(f"fetching tzdata and tzcode failed: {r.stderr.strip()[-500:]}")
    tzdata, tzcode = r.stdout.split()
    try:
        text, report = tzlist.generate(tzdata, tzcode, work_dir() / "tz")
    except tzlist.TzListError as e:
        raise ImageError(f"timezone.list: {e}") from e
    changes.append(f"usr: etc/timezone.list generated: {report[0]}, {report[-1]}")
    changes.extend(f"usr: etc/timezone.list: {line}" for line in report[1:-1])
    return text.encode("ascii")


def ca_certificates(changes):
    """{path in /usr: data} of the CA directory: each of the system build's hash links in
    etc/ssl/certs (ca-certificates, made with c_rehash) as a file of its own, with the certificate
    it leads to. OpenSSL reads only the one a server's chain needs; curl's bundle file is left out."""
    target = (work_dir() / "system" / "out" / "target").resolve()
    certs = target / "etc" / "ssl" / "certs"
    out = {}
    for link in sorted(certs.glob("*")) if certs.is_dir() else []:
        if not re.fullmatch(r"[0-9a-f]{8}\.[0-9]+", link.name):
            continue
        real = link.resolve()
        if not real.is_file() or target not in real.parents:
            raise ImageError(f"etc/ssl/certs/{link.name} leads outside the system build's target")
        data = real.read_bytes()
        if not data.startswith(b"-----BEGIN CERTIFICATE-----"):
            raise ImageError(f"etc/ssl/certs/{link.name} isn't a PEM certificate")
        out[f"etc/ssl/certs/{link.name}"] = data
    if not out:
        raise ImageError("the system build has no CA certificates in etc/ssl/certs (BR2_PACKAGE_CA_CERTIFICATES)")
    changes.append(f"usr: etc/ssl/certs: {len(out)} CA certificates")
    return out


def core_generated(changes):
    """{(part, path): data} of the files system/core-files.txt marks generated. The core's inetd.conf
    serves the rabbit's web page (BusyBox httpd on port 80, IPv4, at most 60 connections a minute)
    with the configuration inetd.sh makes in RAM at boot (the password and the local-network rules);
    inetd.sh adds telnet when the install set it."""
    inetd = ("# inetd's services (written by the image build; inetd.sh makes httpd's configuration and adds telnet\n"
             "# when the install set it).\n"
             "80 stream tcp nowait.60 root /bin/busybox httpd -i -c /var/run/httpd.conf -h /usr/www\n")
    r = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "scripts" / "fetch.py"), "rt73-firmware", "rt73-licence"],
                       capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        raise ImageError(f"fetching the RT73 firmware failed: {r.stderr.strip()[-500:]}")
    firmware, licence = (Path(p) for p in r.stdout.split())
    out = {("rootfs", "karotz/etc/inetd.conf"): inetd.encode(),
           ("rootfs", "lib/firmware/rt73.bin"): firmware.read_bytes(),
           ("rootfs", "lib/firmware/LICENCE.ralink-firmware.txt"): licence.read_bytes(),
           ("usr", "etc/timezone.list"): timezone_list(changes)}
    for path, data in ca_certificates(changes).items():
        out[("usr", path)] = data
    for name, notes in SOUNDS.items():
        out[("usr", f"karotz/sounds/{name}")] = tone_wav(notes)
    for name, notes in EFFECTS.items():
        out[("usr", f"karotz/sounds/{name}")] = mp3(tone_wav(notes))
    return out


def install_conf(values):
    """The key's install.conf, which autorun sources: one NAME=value line per value, nothing else."""
    lines = []
    for name, value in values.items():
        if not re.fullmatch(r"[A-Za-z0-9._+-]+", value):
            raise ImageError(f"install.conf: {name}={value!r} is not a plain word")
        lines.append(f"{name}={value}\n")
    return "".join(lines).encode("ascii")


def patch_bytes(data, patch):
    """data with patch (a unified diff of one file) applied."""
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "file"
        target.write_bytes(data)
        subprocess.run(["patch", "--quiet", "--no-backup-if-mismatch", "--forward", str(target), str(patch)],
                       check=True, stdout=subprocess.DEVNULL)
        return target.read_bytes()


def patch_zip_member(data, member, patch):
    """A zip archive with one member patched, every member's metadata and order kept."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(out, "w") as dst:
        if member not in src.namelist():
            raise ImageError(f"{patch}: the archive has no {member}")
        for info in src.infolist():
            content = src.read(info)
            if info.filename == member:
                content = patch_bytes(content, patch)
            dst.writestr(info, content, compress_type=info.compress_type)
    return out.getvalue()


def stick_files(config, names):
    """{name on the stick: data} of what every LapinLibre stick carries: the named files from
    image/usbkey (CRLF refused), autorun.sig, the install tools and the install sounds."""
    files = {}
    for name in names:
        files[name] = (IMAGE / "usbkey" / name).read_bytes()
        if b"\r" in files[name]:
            raise ImageError(f"image/usbkey/{name} has CRLF line endings; the rabbit's shell can't run it")
    signed = (IMAGE / "usbkey" / SIGNED).read_bytes()
    if md5(signed) != config["key"]["md5"][SIGNED]:
        raise ImageError(f"image/usbkey/{SIGNED} has md5 {md5(signed)}, not config.toml's: it must stay as it is")
    files[SIGNED] = signed
    tools_file = out_dir() / "install-tools" / "install-tools.tar"
    if not tools_file.is_file():
        raise ImageError(f"no {tools_file} (run ./build.sh userland)")
    files["install-tools.tar"] = tools_file.read_bytes()
    for name, notes in INSTALL_SOUNDS.items():
        files[f"sound/{name}"] = mp3(tone_wav(notes))
    for name in SPOKEN:
        files[f"sound/{name}"] = (IMAGE / "usbkey" / "sound" / name).read_bytes()
    return files


def write_stick(out, files):
    for name, data in files.items():
        (out / name).parent.mkdir(parents=True, exist_ok=True)
        (out / name).write_bytes(data)
        if name == "autorun" or name.endswith(".sh"):
            (out / name).chmod(0o755)


def check_pinned(config, parts):
    """The core's files that config.toml [key.md5] pins by "part:path" (the Karotz keyring): each
    must be in the core, with that MD5."""
    for name, want in config["key"]["md5"].items():
        label, sep, path = name.partition(":")
        if not sep:
            continue
        if label not in parts:
            raise ImageError(f"config.toml [key.md5]: {name}: no such part")
        e = next((e for e in parts[label].entries() if e.path == path), None)
        if e is None or e.kind != "f":
            raise ImageError(f"the core has no file {name}, which config.toml [key.md5] pins")
        if md5(e.data) != want:
            raise ImageError(f"{name} has md5 {md5(e.data)}, not config.toml's: it must stay as it is")


def make_core(config, epoch, version, changes):
    """(rootfs, usr): LapinLibre's core as the key installs it. The system build's target, LapinLibre's
    userland and V8, and image/core's files (system/core-files.txt), the pinned files checked,
    then image/overlay and the build stamp."""
    rootfs, usr = fstree.CramfsPart.empty(), fstree.TarPart.empty(epoch)
    own = system.own_builds(work_dir() / "userland" / "system", work_dir() / "v8" / "libv8.so")
    made = system.build_core(rootfs, usr, [ROOT / "system" / "core-files.txt"], work_dir() / "system" / "out" / "target", own,
                             ROOT / "system" / "image-accepted-undefined.txt", changes,
                             core_dir=IMAGE / "core", generated=core_generated(changes))
    print(f"Core: {made} entries (system/core-files.txt)")
    check_pinned(config, {"rootfs": rootfs, "usr": usr})
    # The system build's library cache names the build host's paths; linuxrc runs ldconfig when there is none.
    for name in ("etc/ld.so.cache",):
        if any(e.path == name for e in usr.entries()):
            usr.remove(name)
            changes.append(f"usr: {name} removed (linuxrc makes a new one)")
    for label, part in (("rootfs", rootfs), ("usr", usr)):
        for change in fstree.apply_overlay(part, IMAGE / "overlay" / label):
            changes.append(f"{label}: {change}")
    # /etc links into /usr, so the build stamp lives in the rootfs's own etc.
    rootfs.put_file("karotz/etc/lapinlibre-build", f"{version}\n".encode())
    return rootfs, usr


def build(out):
    if "LAPIN_KERNEL" in os.environ:  # a kernel override that no longer exists
        raise ImageError("LAPIN_KERNEL is gone: the image has the system build's kernel, Linux 2.6.32")
    config = load_config()
    epoch = source_epoch()
    version = git_describe()
    changes = []

    # The system build's kernel, Linux 2.6.32: its userland needs it (its headers), and BusyBox 1.37's mdev
    # makes the MTD character nodes under it that the key's flashing needs.
    kernel_file = out_dir() / "system-kernel" / "zImage"
    if not kernel_file.is_file():
        raise ImageError(f"no {kernel_file} (run ./build.sh kernel)")
    rootfs, usr = make_core(config, epoch, version, changes)

    kernel = kernel_file.read_bytes()
    rootfs_img = rootfs.serialize()
    images = {"zImage": kernel, "rootfs.img.gz": gz(rootfs_img, "rootfs.img", epoch), "usr.tar.gz": gz(usr.serialize(), "", epoch)}
    limits = config["limits"]
    for what, size, limit in (("rootfs (uncompressed, RAM disk)", len(rootfs_img), limits["rootfs_bytes"]),
                              ("rootfs.img.gz (mtd2)", len(images["rootfs.img.gz"]), limits["rootfs_gz_bytes"]),
                              ("zImage (mtd1)", len(kernel), limits["kernel_bytes"])):
        if size > limit:
            raise ImageError(f"{what} is {size} bytes, over the {limit}-byte limit in config.toml")
    build_id = hashlib.sha256(b"".join(hashlib.sha256(images[k]).digest() for k in sorted(images))).hexdigest()[:16]

    # Every input is read before the old key goes, so a missing one leaves it as it was.
    common = stick_files(config, KEY_FILES)
    if out.exists():
        if any(out.iterdir()) and not (out / "build_id").is_file():
            raise ImageError(f"{out} is not empty and not a previous build; refusing to replace it")
        shutil.rmtree(out)
    out.mkdir(parents=True)
    write_stick(out, common)
    for name, data in images.items():
        (out / name).write_bytes(data)
    (out / "install.conf").write_bytes(install_conf({
        "BUILD_ID": build_id, "BUILD_VERSION": version, "KERNEL_MD5": md5(images["zImage"]),
        "ROOTFS_MD5": md5(images["rootfs.img.gz"]), "USR_MD5": md5(images["usr.tar.gz"]),
        "INSTALL_TOOLS_MD5": md5(common["install-tools.tar"])}))
    (out / "build_id").write_text(build_id + "\n", encoding="utf-8", newline="\n")

    print(f"Build {build_id} ({version}) -> {out}")
    print(f"  rootfs {len(rootfs_img)} bytes uncompressed ({limits['rootfs_bytes'] - len(rootfs_img)} under the RAM-disk limit)")
    for name, data in images.items():
        print(f"  {name:14} {len(data):>9} bytes  md5 {md5(data)}")
    print(f"  {len(changes)} notes on the core:")
    for c in changes:
        print(f"    {c}")
    print(f"Copy the contents of {out} (not the folder) to the root of an empty FAT32 USB key.")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", type=Path, default=out_dir() / "usbkey")
    args = p.parse_args()
    try:
        build(args.out.resolve())
    except (ImageError, system.SystemBuildError, ValueError, OSError,
            subprocess.CalledProcessError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
