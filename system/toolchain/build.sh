#!/bin/bash
# SPDX-License-Identifier: MIT
# The cross toolchain (config.toml [toolchain]): GCC 14.3, binutils 2.44 and uClibc-ng 1.0.57 for the
# ARM920T, in $LL_WORK/toolchain/sdk, where system/buildroot.config takes it as its external toolchain.
#
# With a [download.toolchain] pin in config.toml, the published toolchain is fetched and unpacked.
# Without one, or with LAPIN_TOOLCHAIN=build, Buildroot builds it from system/toolchain/buildroot.config
# and packs it with `make sdk` as $LL_OUT/toolchain/<name>.tar.gz, the file the toolchain workflow
# publishes, and that file is unpacked in the same way.
. "$(dirname "$0")/../../scripts/common.sh"

NAME=$(ll_config toolchain.name)
W=$LL_WORK/toolchain
SDK=$W/sdk
BR=$W/buildroot-2026.02.3
O=$W/out
DL=$LL_DL/br2026
CC=$SDK/bin/arm-buildroot-linux-uclibcgnueabi-gcc

mode=${LAPIN_TOOLCHAIN:-}
if [ -z "$mode" ]; then
    if ll_config download.toolchain.sha256 > /dev/null 2>&1; then mode=download; else mode=build; fi
fi
case $mode in
    build) key=build-$NAME-$(ll_hash "$LL_ROOT/system/toolchain" "$LL_ROOT/system/patches/uclibc" \
                             "$LL_ROOT/system/external/uclibc-ng.fragment")-$(ll_config download.buildroot-2026.sha256) ;;
    download) key=download-$NAME-$(ll_config download.toolchain.sha256) ;;
    *) ll_die "toolchain: LAPIN_TOOLCHAIN is build or download, not $mode" ;;
esac
if ll_stamp_ok toolchain "$key" && [ -x "$CC" ]; then
    ll_note "toolchain: up to date ($SDK)"
    exit 0
fi

if [ "$mode" = download ]; then
    ll_log "toolchain: fetching $NAME"
    tarball=$(ll_fetch toolchain) || ll_die "toolchain: can't fetch $NAME"
else
    ll_log "toolchain: fetching sources"
    br_tarball=$(ll_fetch buildroot-2026) && ll_fetch linux-2-6-32 > /dev/null || ll_die "toolchain: can't fetch Buildroot"
    ll_fetch --group system > /dev/null || ll_die "toolchain: can't fetch the pinned downloads"

    ll_log "toolchain: building $NAME with Buildroot in $BR (log: $O/build.log)"
    rm -rf "$W"
    mkdir -p "$W" "$O"
    tar -xJf "$br_tarball" -C "$W" || ll_die "toolchain: can't unpack $br_tarball"
    sed -e "s|@DL_DIR@|$DL|" -e "s|@PATCH_DIR@|$LL_ROOT/system/patches|" "$LL_ROOT/system/toolchain/buildroot.config" \
        > "$BR/configs/lapin_toolchain_defconfig"
    # WSL appends the Windows PATH (with spaces), which Buildroot refuses.
    CLEAN_PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
    br() {
        env -i HOME="$HOME" PATH="$CLEAN_PATH" TERM=dumb LC_ALL=C \
            make -C "$BR" O="$O" BR2_EXTERNAL="$LL_ROOT/system/external" "$@"
    }
    br lapin_toolchain_defconfig < /dev/null > "$O/build.log" 2>&1 ||
        ll_die "toolchain: Buildroot configuration failed (see $O/build.log)"
    start=$SECONDS
    # The whole output goes to the log; Buildroot's step lines also go to the console.
    if ! br sdk < /dev/null 2>&1 | tee -a "$O/build.log" | { grep --line-buffered '^>>>' >&2 || true; }; then
        tail -40 "$O/build.log" >&2
        ll_die "toolchain: Buildroot failed (see $O/build.log)"
    fi
    ll_note "toolchain: built in $(( (SECONDS - start) / 60 )) min"
    built=$(ls "$O"/images/*_sdk-buildroot.tar.gz 2>/dev/null | head -1)
    [ -n "$built" ] || ll_die "toolchain: make sdk wrote no SDK in $O/images"
    mkdir -p "$LL_OUT/toolchain"
    tarball=$LL_OUT/toolchain/$NAME.tar.gz
    cp "$built" "$tarball"
    ll_note "toolchain: $tarball ($(stat -c %s "$tarball") bytes, sha256 $(sha256sum "$tarball" | cut -d' ' -f1))"
fi

rm -rf "$SDK"
mkdir -p "$SDK"
tar -xzf "$tarball" -C "$SDK" --strip-components=1 || ll_die "toolchain: can't unpack $tarball"
[ -x "$CC" ] || ll_die "toolchain: $tarball has no bin/arm-buildroot-linux-uclibcgnueabi-gcc"
ll_note "toolchain: $("$CC" --version | head -1)"
ll_stamp_set toolchain "$key"
