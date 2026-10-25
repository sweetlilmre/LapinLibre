#!/bin/bash
# SPDX-License-Identifier: MIT
# The system build: Buildroot 2026.02.3 builds the rabbit's packages
# (system/buildroot.config) into $LL_WORK/system/out/target, with the toolchain
# stage's toolchain (GCC 14.3, uClibc-ng 1.0.57 with NPTL, Linux 2.6.32.71 headers) in
# $LL_WORK/toolchain/sdk as its external toolchain.
#
# Every download is pinned (config.toml: the
# system-source group's Buildroot and kernel, and group "system", made by
# system/pin-downloads.py) and fetched into dl/br2026 first; Buildroot itself may fetch nothing.
. "$(dirname "$0")/../scripts/common.sh"

W=$LL_WORK/system
BR=$W/buildroot-2026.02.3
O=$W/out
DL=$LL_DL/br2026
# The system build's patches to Buildroot's packages (uClibc-ng with old kernel headers among them).
PATCH_DIRS="$LL_ROOT/system/patches"
# LapinLibre's own packages (libraries kept at their old versions), as a Buildroot external tree.
EXTERNAL=$LL_ROOT/system/external
TOOLCHAIN=$LL_WORK/toolchain/sdk
[ -x "$TOOLCHAIN/bin/arm-buildroot-linux-uclibcgnueabi-gcc" ] || ll_die "system: no toolchain (run ./build.sh toolchain)"

# The stage is up to date when nothing in it changed (key). The Buildroot tree depends only on
# what goes into Buildroot (build_key), the toolchain's stamp among them: a change to the system
# build's other files alone doesn't rebuild it.
key=$(ll_hash "$LL_ROOT/system" "$LL_ROOT/config.toml" "$LL_WORK/stamps/toolchain")
build_key=$(ll_hash "$LL_ROOT/system/buildroot.config" "$LL_ROOT/system/patches" "$EXTERNAL" "$LL_ROOT/config.toml" \
                    "$LL_WORK/stamps/toolchain")
if ll_stamp_ok system "$key" && [ -d "$O/target/usr/lib" ]; then
    ll_note "system: up to date ($O/target)"
    exit 0
fi

ll_log "system: fetching sources"
tarball=$(ll_fetch buildroot-2026) && ll_fetch linux-2-6-32 > /dev/null || ll_die "system: can't fetch Buildroot"
ll_fetch --group system > /dev/null || ll_die "system: can't fetch the pinned downloads"

# WSL appends the Windows PATH (with spaces), which Buildroot refuses.
CLEAN_PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
# A build interrupted with the same inputs carries on where it stopped (Buildroot's own
# stamps); any change to the inputs starts from a clean tree.
if [ "$(cat "$W/.prepared" 2>/dev/null)" = "$build_key" ] && [ -f "$O/.config" ]; then
    ll_log "system: resuming the interrupted build in $BR"
else
    ll_log "system: preparing Buildroot in $BR"
    rm -rf "$W"
    mkdir -p "$W" "$O"
    tar -xJf "$tarball" -C "$W" || ll_die "system: can't unpack $tarball"
    sed -e "s|@DL_DIR@|$DL|" -e "s|@PATCH_DIR@|$PATCH_DIRS|" -e "s|@TOOLCHAIN@|$TOOLCHAIN|" "$LL_ROOT/system/buildroot.config" \
        > "$BR/configs/lapin_system_defconfig"
    env -i HOME="$HOME" PATH="$CLEAN_PATH" TERM=dumb LC_ALL=C \
        make -C "$BR" O="$O" BR2_EXTERNAL="$EXTERNAL" lapin_system_defconfig < /dev/null > "$O/build.log" 2>&1 ||
        ll_die "system: Buildroot configuration failed (see $O/build.log)"
    echo "$build_key" > "$W/.prepared"
fi
missing=$(grep -oE '^BR2_[A-Z0-9_]+=y' "$LL_ROOT/system/buildroot.config" | cut -d= -f1 |
          while read -r sym; do grep -q "^$sym=y" "$O/.config" || echo "$sym"; done)
[ -z "$missing" ] || ll_die "system: not enabled by Buildroot: $missing"

ll_log "system: Buildroot (log: $O/build.log)"
start=$SECONDS
# The whole output goes to the log; Buildroot's step lines (">>> package version Step") also go
# to the console, to show the progress.
if ! env -i HOME="$HOME" PATH="$CLEAN_PATH" TERM=dumb LC_ALL=C \
        make -C "$BR" O="$O" BR2_EXTERNAL="$EXTERNAL" 2>&1 |
        tee -a "$O/build.log" | { grep --line-buffered '^>>>' >&2 || true; }; then
    tail -40 "$O/build.log" >&2
    ll_die "system: Buildroot failed (see $O/build.log)"
fi
ll_note "system: built in $(( (SECONDS - start) / 60 )) min"
ll_stamp_set system "$key"
