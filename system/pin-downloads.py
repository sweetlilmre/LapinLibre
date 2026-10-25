# SPDX-License-Identifier: MIT
"""Write config.toml pins for the system build's Buildroot downloads.

    pin-downloads.py SHOW_INFO DL_DIR > pins.toml

SHOW_INFO is Buildroot's `make show-info` JSON for system/buildroot.config, and DL_DIR a download
directory Buildroot filled itself (`make source`, every file checked against its .hash files).
For each package's downloads, prints a [download.br-...] entry in group "system": the file under
br2026/<dl_dir>/ (where system/build.sh points BR2_DL_DIR), its sha256, and its URLs, Buildroot's
sources mirror first, then the upstream sites Buildroot names. A file Buildroot generates (from
git and the like) is on the mirror only. Files already pinned elsewhere in config.toml are left
out (the Linux headers tarball is the system-source group's). Lists any downloaded file no package
claims on stderr.
"""

import hashlib
import json
import re
import sys
import tomllib
from pathlib import Path

MIRROR = "https://sources.buildroot.net"
ROOT = Path(__file__).resolve().parent.parent


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main(show_info, dl):
    info = json.load(open(show_info, encoding="utf-8"))
    dl = Path(dl)
    pinned = {d.get("file") for d in tomllib.loads((ROOT / "config.toml").read_text())["download"].values()}
    claimed, out, names = set(), [], set()
    for pkg in sorted(info):
        p = info[pkg]
        for d in p.get("downloads", []):
            src, sub = d["source"], p.get("dl_dir", pkg)
            path = dl / sub / src
            rel = f"br2026/{sub}/{src}"
            if path in claimed:  # a host package and its target package share the file
                continue
            claimed.add(path)
            if rel in pinned or not path.is_file():
                if not path.is_file():
                    print(f"missing: {path}", file=sys.stderr)
                continue
            urls = [f"{MIRROR}/{sub}/{src}"]
            for uri in d.get("uris", []):
                scheme, _, base = uri.partition("+")
                if scheme.split("|")[0] in ("http", "https", "ftp") and base.startswith(("http", "ftp")):
                    url = f"{base.rstrip('/')}/{src}"
                    if url not in urls and not base.startswith(MIRROR):
                        urls.append(url)
            name = "br-" + re.sub(r"[^a-z0-9]+", "-", f"{pkg}-{src}".lower()).strip("-")
            name = name[:80]
            assert name not in names, name
            names.add(name)
            out.append(f'[download.{name}]\ngroup = "system"\n'
                       f'urls = [{", ".join(json.dumps(u) for u in urls)}]\n'
                       f'file = "{rel}"\nsha256 = "{sha256(path)}"\n')
    for f in sorted(dl.rglob("*")):
        if (f.is_file() and f.name != ".lock" and f not in claimed  # .lock: Buildroot's own
                and f"br2026/{f.relative_to(dl)}" not in pinned):
            print(f"unclaimed: {f.relative_to(dl)}", file=sys.stderr)
    print("# The system build's Buildroot downloads (system/pin-downloads.py).\n")
    print("\n".join(out))
    print(f"{len(out)} pins", file=sys.stderr)


if __name__ == "__main__":
    main(*sys.argv[1:3])
