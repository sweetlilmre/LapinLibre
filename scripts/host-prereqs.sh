#!/bin/bash
# SPDX-License-Identifier: MIT
# The build host's packages (Ubuntu 24.04 or Debian-like), in one place for
# local builds and CI.
#
#   bash scripts/host-prereqs.sh            check, list what is missing
#   bash scripts/host-prereqs.sh --install  install what is missing (sudo apt-get)
set -euo pipefail

PACKAGES=(
    # general build tools (toolchain, kernel, gzip). Not autoconf, automake
    # or libtool: Buildroot builds its own host autotools.
    build-essential bison flex gawk texinfo patch perl bc cpio rsync file
    unzip bzip2 xz-utils wget ca-certificates git python3 gettext
    libncurses-dev zlib1g-dev
    # the key's install tools (libid3tag builds with CMake)
    cmake
    # the key's install sounds (MP3, which the install's madplay plays)
    ffmpeg
    # the kernel build's namespace
    util-linux
    # userland code generators and the host build of the daemons (tests)
    libglib2.0-dev libdbus-glib-1-dev libdbus-1-dev protobuf-compiler libprotobuf-dev
    libcurl4-openssl-dev libjpeg-dev libexpat1-dev uuid-dev libxml2-dev
    # tests
    qemu-user dbus gcc-arm-linux-gnueabi python3-dbus
)

missing=()
for p in "${PACKAGES[@]}"; do
    dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q "install ok installed" || missing+=("$p")
done
# pkg-config is checked as a command: depending on the distribution's version it comes from
# the pkg-config or the pkgconf package. (The ARM builds use Buildroot's own;
# the host build of the daemons uses this one.)
command -v pkg-config > /dev/null || missing+=(pkgconf)

if [ "${1:-}" = "--install" ] && [ ${#missing[@]} -gt 0 ]; then
    sudo apt-get update
    sudo apt-get install -y --no-install-recommends "${missing[@]}"
    exec bash "$0"
fi

if [ ${#missing[@]} -gt 0 ]; then
    echo "missing host packages: ${missing[*]}" >&2
    echo "install them with: bash scripts/host-prereqs.sh --install" >&2
    exit 1
fi
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || { echo "python 3.11 or later is needed" >&2; exit 1; }
unshare --user --map-root-user true 2>/dev/null || {
    echo "unprivileged user namespaces are disabled (the kernel build needs them)" >&2
    echo "on Ubuntu 24.04: sudo sysctl kernel.apparmor_restrict_unprivileged_userns=0" >&2
    exit 1
}
echo "host prerequisites: all present" >&2
