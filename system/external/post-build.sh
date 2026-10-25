#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-or-later
# Buildroot's post-build script for the system build (BR2_ROOTFS_POST_BUILD_SCRIPT): installs
# uClibc-ng's ldconfig and getconf from the external toolchain's sysroot (copied into STAGING_DIR)
# into the target, stripped as Buildroot strips the target. An external toolchain only gives the
# target its libraries; the rabbit's linuxrc makes the loader cache with ldconfig.
set -eu
TARGET=$1
STRIP=$HOST_DIR/bin/arm-buildroot-linux-uclibcgnueabi-strip
for f in sbin/ldconfig usr/bin/getconf; do
    [ -f "$STAGING_DIR/$f" ] || { echo "post-build: the toolchain has no $f" >&2; exit 1; }
    install -D -m 0755 "$STAGING_DIR/$f" "$TARGET/$f"
    "$STRIP" --remove-section=.comment --remove-section=.note "$TARGET/$f"
done
