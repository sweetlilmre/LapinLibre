# SPDX-License-Identifier: MIT
# Shared settings and helpers for LapinLibre's build scripts. Source it:
#   . "$(dirname "$0")/../scripts/common.sh"
#
# Sets LL_ROOT (the repository), LL_LOCATION (the work location), LL_DL,
# LL_WORK and LL_OUT (the download cache, build trees and results under it),
# and LL_PY (a Python 3.11+ interpreter).
#
# Where the build writes: inside the repository on a Linux filesystem, but
# under ~/.cache/lapinlibre when the repository is on a Windows drive seen
# through WSL (/mnt/*), which is case-insensitive and slow for builds.
# LAPINLIBRE_WORK overrides both.

set -euo pipefail

LL_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

if [ -n "${LAPINLIBRE_WORK:-}" ]; then
    LL_LOCATION=$LAPINLIBRE_WORK
elif [[ "$LL_ROOT" == /mnt/* ]]; then
    LL_LOCATION=$HOME/.cache/lapinlibre
else
    LL_LOCATION=$LL_ROOT
fi
LL_DL=$LL_LOCATION/dl
LL_WORK=$LL_LOCATION/work
LL_OUT=$LL_LOCATION/out
export LL_ROOT LL_LOCATION LL_DL LL_WORK LL_OUT

LL_PY=${LL_PY:-python3}
# Nothing is written into the repository, Python's bytecode caches included:
# every call is "$LL_PY" -I -B (-I ignores PYTHON* variables, so -B is what
# stops the caches); the variable covers Python started without -I.
export LL_PY PYTHONDONTWRITEBYTECODE=1

# Messages go to stderr, so a stage's stdout can carry results.
ll_log() { printf '== %s\n' "$*" >&2; }
ll_note() { printf '   %s\n' "$*" >&2; }
ll_die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# ll_fetch NAME: download (or reuse from the cache) the pinned item NAME from
# config.toml and print its path.
ll_fetch() { "$LL_PY" -I -B "$LL_ROOT/scripts/fetch.py" "$@"; }

# ll_config KEY: a value from config.toml ("section.key").
ll_config() { "$LL_PY" -I -B "$LL_ROOT/scripts/config.py" "$@"; }

# ll_version: the build's version (image/tools/image.py git_describe), from the release tags (v*)
# only: the release tag on the commit (v0.1.0), else the last one, the commits since it and the
# commit (v0.1.0-3-g898dd4892f69), else the commit; -dirty when tracked files have changes;
# "unversioned" outside git.
ll_version() { git -C "$LL_ROOT" describe --tags --match 'v[0-9]*' --dirty --always --abbrev=12 2>/dev/null || echo unversioned; }

# ll_source_epoch: the build's time (image/tools/image.py source_epoch): SOURCE_DATE_EPOCH, else
# the commit's time; nothing outside git.
ll_source_epoch() {
    if [ -n "${SOURCE_DATE_EPOCH:-}" ]; then
        echo "$SOURCE_DATE_EPOCH"
    else
        git -C "$LL_ROOT" log -1 --format=%ct 2>/dev/null || true
    fi
}

# Stamps: a stage's step is skipped when its stamp holds the same key (a hash
# of everything the step depends on).
#   ll_stamp_ok NAME KEY  -> success when up to date
#   ll_stamp_set NAME KEY
ll_stamp_ok() { [ -f "$LL_WORK/stamps/$1" ] && [ "$(cat "$LL_WORK/stamps/$1")" = "$2" ]; }
ll_stamp_set() { mkdir -p "$LL_WORK/stamps"; printf '%s\n' "$2" > "$LL_WORK/stamps/$1"; }

# ll_hash PATH...: one hash over the contents of files and directories
# (sorted, names included), for stamp keys.
ll_hash() {
    local p
    for p in "$@"; do
        if [ -d "$p" ]; then
            (cd "$p" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum)
        elif [ -e "$p" ]; then
            sha256sum "$p"
        else
            printf 'missing %s\n' "$p"
        fi
    done | sha256sum | cut -c1-16
}
