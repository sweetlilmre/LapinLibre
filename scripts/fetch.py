# SPDX-License-Identifier: MIT
"""Fetch pinned downloads into the cache (dl/) and print their paths.

    fetch.py NAME...            fetch each [download.NAME] from config.toml
    fetch.py --group GROUP      fetch every download whose group is GROUP
    fetch.py --all              fetch everything
    fetch.py --list             list the downloads
    fetch.py NAME --export DIR  (git downloads) write the pinned commit's tree
                                into DIR

A file download has `urls` (tried in order) and `sha256`, and optionally
`file` (the cached name; default: the first URL's last component). A git
download has `git` (the repository URL) and `commit` (a full hash); it is
kept as a bare mirror under dl/git/ and checked by its commit.

Nothing is used unless its checksum (or commit) matches: a mismatch removes
the bad file and fails.
"""

import hashlib
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lapin import dl_dir, load_config  # noqa: E402

RETRIES = 3
USER_AGENT = "LapinLibre-fetch/1"


def log(message):
    print(f"   {message}", file=sys.stderr)


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def download(url, dest):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response, open(dest, "wb") as out:
        shutil.copyfileobj(response, out, 1 << 20)


def fetch_file(name, item):
    urls = item["urls"]
    target = dl_dir() / item.get("file", urls[0].rstrip("/").rsplit("/", 1)[-1])
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if sha256_of(target) == item["sha256"]:
            return target
        log(f"{target.name}: cached copy has the wrong checksum, fetching again")
        target.unlink()
    errors = []
    for url in urls:
        for attempt in range(1, RETRIES + 1):
            partial = target.with_name(target.name + ".part")
            try:
                log(f"{name}: {url}")
                download(url, partial)
            except OSError as e:
                errors.append(f"{url}: {e}")
                partial.unlink(missing_ok=True)
                time.sleep(2 * attempt)
                continue
            digest = sha256_of(partial)
            if digest != item["sha256"]:
                partial.unlink()
                errors.append(f"{url}: sha256 {digest}, expected {item['sha256']}")
                break
            partial.rename(target)
            return target
    raise SystemExit(f"error: {name}: could not fetch\n  " + "\n  ".join(errors))


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def fetch_git(name, item):
    mirror = dl_dir() / "git" / f"{name}.git"
    commit = item["commit"]
    if mirror.exists():
        try:
            git("cat-file", "-e", f"{commit}^{{commit}}", cwd=mirror)
            return mirror
        except subprocess.CalledProcessError:
            pass
    else:
        mirror.parent.mkdir(parents=True, exist_ok=True)
        log(f"{name}: cloning {item['git']}")
        git("clone", "--bare", "--quiet", item["git"], str(mirror))
    try:
        git("cat-file", "-e", f"{commit}^{{commit}}", cwd=mirror)
    except subprocess.CalledProcessError:
        log(f"{name}: fetching {commit}")
        git("fetch", "--quiet", "origin", commit, cwd=mirror)
        git("cat-file", "-e", f"{commit}^{{commit}}", cwd=mirror)
    return mirror


def export_git(name, item, dest):
    mirror = fetch_git(name, item)
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "tree.tar"
        git("archive", "--format=tar", "-o", str(archive), item["commit"], cwd=mirror)
        subprocess.run(["tar", "-xf", str(archive), "-C", str(dest)], check=True)
    return dest


def fetch(name, item):
    return fetch_git(name, item) if "git" in item else fetch_file(name, item)


def main(argv):
    downloads = load_config().get("download", {})
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    if argv[0] == "--list":
        for name, item in downloads.items():
            print(f"{name}\t{item.get('group', '')}\t{item.get('git') or item['urls'][0]}")
        return 0
    if len(argv) == 3 and argv[1] == "--export":
        print(export_git(argv[0], downloads[argv[0]], argv[2]))
        return 0
    if argv[0] == "--all":
        names = list(downloads)
    elif argv[0] == "--group":
        names = [n for n, item in downloads.items() if item.get("group") == argv[1]]
    else:
        names = argv
    for name in names:
        if name not in downloads:
            raise SystemExit(f"error: config.toml has no download {name!r}")
        print(fetch(name, downloads[name]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
