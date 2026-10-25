#!/bin/bash
# SPDX-License-Identifier: MIT
# The userland stage: LapinLibre's programs built with the system toolchain (GCC 14.3, uClibc-ng)
# against the system build's staging, for the image and the behaviour tests.
#
#   bash system/userland.sh        (./build.sh userland; after ./build.sh system v8)
#
# The tools, both shared libraries and every daemon (libmpd and linphone are system/external's
# packages), and karotz-player. Output in
# $LL_WORK/userland/system, the make log beside it. The build isn't bound to fixed paths
# (scripts/fixed-root.sh), so the debug paths it records name the work location.
. "$(dirname "$0")/../scripts/common.sh"

O=$LL_WORK/system/out
V8=$LL_WORK/v8
OUT=$LL_WORK/userland/system
X=$O/host/bin/arm-buildroot-linux-uclibcgnueabi-
[ -x "${X}gcc" ] || ll_die "userland: no system toolchain (run ./build.sh system)"
[ -f "$V8/libv8.so" ] || ll_die "userland: no system libv8.so (run ./build.sh v8)"
HOST_DBUS_BINDING_TOOL=$(command -v dbus-binding-tool) || ll_die "userland: no host dbus-binding-tool"
HOST_GLIB_GENMARSHAL=$(command -v glib-genmarshal) || ll_die "userland: no host glib-genmarshal"

# Source and build paths as relative names in the code (__FILE__ in assertion texts) and in the
# debug information, so the programs don't depend on where the repository or the work folder is.
PREFIX_MAP="-ffile-prefix-map=$LL_ROOT/=./ -ffile-prefix-map=$OUT/=./ -ffile-prefix-map=$LL_WORK/=./"
ll_log "userland: build in $OUT (log: $OUT.log)"
rm -rf "$OUT"
mkdir -p "$LL_WORK/userland"
if ! make -C "$LL_ROOT/userland" -j"${LAPIN_JOBS:-$(nproc)}" OUT="$OUT" CROSS="arm-buildroot-linux-uclibcgnueabi-" \
        CC="${X}gcc" CXX="${X}g++" PKG_CONFIG="$O/host/bin/pkg-config" \
        DBUS_BINDING_TOOL="$HOST_DBUS_BINDING_TOOL" GLIB_GENMARSHAL="$HOST_GLIB_GENMARSHAL" \
        PROTOC="$O/host/bin/protoc-2.3.0" CFLAGS="-O2 -g $PREFIX_MAP" CXXFLAGS="-O2 -g $PREFIX_MAP -I$V8/src/include" LDFLAGS="-L$V8" \
        all player > "$OUT.log" 2>&1; then
    grep -E "error:|Error [0-9]|undefined reference" "$OUT.log" | sed "s|$LL_ROOT/||" | sort -u | head -30 >&2
    ll_die "userland: make failed (see $OUT.log)"
fi
warnings=$(grep -c "warning:" "$OUT.log")
ll_note "userland: built ($warnings compiler warnings, see $OUT.log)"
