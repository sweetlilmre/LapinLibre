#!/bin/bash
# SPDX-License-Identifier: MIT
# Run a command with directories bound at fixed paths, so the paths compilers
# record (__FILE__, debug info) are the same whoever builds and wherever the
# work location is.
#
#   fixed-root.sh FIXED=REAL... -- COMMAND [ARG...]
#
# The command runs in an unprivileged user and mount namespace (unshare),
# chrooted into a root of its own: a tmpfs holding every entry of the real
# root (directories bound, symbolic links copied, files bound onto empty
# ones), plus each FIXED path bound to its REAL directory. Nothing outside the
# namespace changes, and no other process sees the fixed paths. The
# environment and the working directory (when it exists in the new root) are
# kept.
set -euo pipefail

if [ -z "${LL_FIXED_ROOT_INNER:-}" ]; then
    LL_FIXED_ROOT_INNER=1 LL_FIXED_ROOT_CWD=$PWD exec unshare --user --map-root-user --mount bash "$0" "$@"
fi

binds=()
while [ $# -gt 0 ] && [ "$1" != -- ]; do binds+=("$1"); shift; done
[ "${1:-}" = -- ] || { echo "fixed-root.sh: missing -- before the command" >&2; exit 2; }
shift

root=$(mktemp -d)
mount -t tmpfs none "$root"
for d in /* /.[!.]*; do
    [ -e "$d" ] || [ -L "$d" ] || continue
    name=${d#/}
    if [ -L "$d" ]; then
        cp -P "$d" "$root/$name"
    elif [ -d "$d" ]; then
        mkdir "$root/$name"
        mount --rbind "$d" "$root/$name"
    else
        : > "$root/$name"
        mount --bind "$d" "$root/$name"
    fi
done
for b in "${binds[@]}"; do
    fixed=${b%%=*} real=${b#*=}
    [ -d "$real" ] || { echo "fixed-root.sh: $real is not a directory" >&2; exit 2; }
    mkdir -p "$root$fixed"
    mount --bind "$real" "$root$fixed"
done
cwd=$LL_FIXED_ROOT_CWD
unset LL_FIXED_ROOT_INNER LL_FIXED_ROOT_CWD
exec chroot "$root" bash -c 'cd "$1" 2>/dev/null || cd /; shift; exec "$@"' bash "$cwd" "$@"
