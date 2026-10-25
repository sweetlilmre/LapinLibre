#!/bin/bash
# SPDX-License-Identifier: MIT
# Build the RABBITV3 kernel on Linux 2.6.32.71 with the system toolchain (GCC 14.3):
# stock 2.6.32.71, the modern-GCC patch (system/kernel/linux-2.6.32-modern-gcc.patch), kernel/src
# (the RABBITV3 board and its drivers, first written for 2.6.28), system/kernel/patches in order,
# and YAFFS2, configured from system/kernel/rabbitv3_defconfig. Results in $LL_OUT/system-kernel:
# zImage, Image, System.map and the .config.
#
# Independent of the build host: built at the fixed path config.toml kernel.build_path
# (scripts/fixed-root.sh), with the banner's user and host from config.toml, the build's version
# (ll_version) as the release suffix, build number 1, the version and the commit's date in place of
# the build time, and GNU gzip 1.3.12, so two builds of the same commit give the same kernel.
. "$(dirname "$0")/../../scripts/common.sh"

K=$LL_ROOT/kernel
S=$LL_ROOT/system/kernel
WORK=$LL_WORK/system-kernel
BUILD_PATH=$(ll_config kernel.build_path)
TREE=linux-2.6.32.71
X=$LL_WORK/system/out/host/bin/arm-buildroot-linux-uclibcgnueabi-

if [ -z "${LL_KERNEL_IN_ROOT:-}" ]; then
    [ -x "${X}gcc" ] || ll_die "kernel: no system toolchain (run ./build.sh system)"

    # The release suffix and the banner's date: the build's version and the commit's date. The
    # commit's time also dates what the compile stamps (__DATE__, the built-in initramfs).
    version=$(ll_version)
    tag=-$version
    epoch=$(ll_source_epoch)
    [ -n "$epoch" ] || ll_die "kernel: no SOURCE_DATE_EPOCH and no git commit to take the build's time from"
    stamp="$version ($(date -u -d "@$epoch" +%Y-%m-%d))"

    key=$(ll_hash "$S" "$K/src" "$K/yaffs.py" "$S/linux-2.6.32-modern-gcc.patch" \
                  "$LL_ROOT/config.toml" "${X}gcc" "$LL_WORK/stamps/system")-$tag-$stamp
    if ll_stamp_ok system-kernel "$key" && [ -f "$LL_OUT/system-kernel/zImage" ]; then
        ll_note "kernel: up to date ($LL_OUT/system-kernel/zImage)"
        exit 0
    fi

    ll_log "kernel: fetching sources"
    tarball=$(ll_fetch linux-2-6-32) || ll_die "kernel: can't fetch Linux 2.6.32.71"

    # GNU gzip 1.3.12 compresses the zImage, as the Karotz kernel's own build did.
    GZIP_DIR=$LL_WORK/hosttools/gzip-1.3.12
    if [ ! -x "$GZIP_DIR/gzip" ]; then
        ll_log "kernel: GNU gzip 1.3.12"
        rm -rf "$GZIP_DIR"
        mkdir -p "$LL_WORK/hosttools"
        tar -xzf "$(ll_fetch gzip)" -C "$LL_WORK/hosttools"
        (
            cd "$GZIP_DIR"
            # gnulib's futimens of 2007 clashes with current glibc's.
            grep -rl futimens --include=*.c --include=*.h . | xargs sed -i 's/\bfutimens\b/gl_futimens/g'
            ./configure && make -j"$(nproc)"
        ) > "$LL_WORK/hosttools/gzip.log" 2>&1 || { tail -20 "$LL_WORK/hosttools/gzip.log" >&2; ll_die "kernel: gzip failed"; }
    fi

    ll_log "kernel: preparing $WORK/$TREE"
    rm -rf "${WORK:?}/$TREE"
    mkdir -p "$WORK"
    tar -xJf "$tarball" -C "$WORK"
    patch -d "$WORK/$TREE" -p1 --quiet < "$S/linux-2.6.32-modern-gcc.patch"
    cp -r "$K/src/." "$WORK/$TREE/"
    for p in "$S"/patches/*.patch; do
        ll_note "patch: $(basename "$p")"
        patch -d "$WORK/$TREE" -p1 --quiet < "$p" || ll_die "kernel: $(basename "$p") doesn't apply"
    done
    # The machine name from config.toml.
    board=$WORK/$TREE/arch/arm/mach-s3c2440/mach-rabbitv3.c
    machine=$(ll_config kernel.machine)
    case $machine in *[\"\\]*) ll_die "kernel: kernel.machine can't contain \" or \\" ;; esac
    [ "$(grep -c '^MACHINE_START(RABBITV3, "' "$board")" = 1 ] || ll_die "kernel: MACHINE_START not found once in $board"
    sed -i "s|^MACHINE_START(RABBITV3, \".*\")|MACHINE_START(RABBITV3, \"${machine//|/\\|}\")|" "$board"
    yaffs=$(mktemp -d)
    ll_fetch yaffs2 --export "$yaffs" > /dev/null
    "$LL_PY" -I -B "$K/yaffs.py" "$yaffs" "$WORK/$TREE" >&2
    rm -rf "$yaffs"
    sed "s|^CONFIG_LOCALVERSION=.*|CONFIG_LOCALVERSION=\"$(ll_config kernel.localversion)\"|" \
        "$S/rabbitv3_defconfig" > "$WORK/$TREE/.config"

    ident=$LL_WORK/system-kernel-identity
    mkdir -p "$ident"
    printf '#!/bin/sh\necho %s\n' "$(ll_config kernel.build_user)" > "$ident/whoami"
    printf '#!/bin/sh\necho %s\n' "$(ll_config kernel.build_host)" > "$ident/hostname"
    # gzip with -n: no time in the compressed kernel's header (gzip reads it from a pipe).
    rm -f "$ident/gzip"
    printf '#!/bin/sh\nexec %s -n "$@"\n' "$GZIP_DIR/gzip" > "$ident/gzip"
    chmod 755 "$ident/whoami" "$ident/hostname" "$ident/gzip"

    ll_log "kernel: compiling at $BUILD_PATH/$TREE (version $version)"
    export LL_KERNEL_IN_ROOT=1 LL_KERNEL_TAG=$tag LL_KERNEL_STAMP=$stamp LL_KERNEL_IDENT=$ident
    export SOURCE_DATE_EPOCH=$epoch
    bash "$LL_ROOT/scripts/fixed-root.sh" "$BUILD_PATH=$WORK" -- bash "$0"

    mkdir -p "$LL_OUT/system-kernel"
    for f in arch/arm/boot/zImage arch/arm/boot/Image System.map .config; do
        cp "$WORK/$TREE/$f" "$LL_OUT/system-kernel/$(basename "$f")"
    done
    size=$(stat -c %s "$LL_OUT/system-kernel/zImage")
    limit=$(ll_config limits.kernel_bytes)
    [ "$size" -le "$limit" ] || ll_die "kernel: zImage is $size bytes, over the $limit-byte limit (mtd1)"
    ll_stamp_set system-kernel "$key"
    ll_log "kernel: $LL_OUT/system-kernel/zImage ($size bytes, $((limit - size)) under the limit)"
    # Not a pipe: grep -m1 stops early, and strings' SIGPIPE would fail the stage under pipefail.
    grep -m1 "^Linux version" < <(strings "$LL_OUT/system-kernel/Image") >&2
    exit 0
fi

# In the fixed root (scripts/fixed-root.sh): configure and compile with the system toolchain.
export ARCH=arm CROSS_COMPILE=$X
export PATH=$LL_KERNEL_IDENT:$PATH
export KBUILD_BUILD_VERSION=1
export KBUILD_BUILD_TIMESTAMP=$LL_KERNEL_STAMP
cd "$BUILD_PATH/$TREE"
set +o pipefail
yes "" | make oldconfig > "$BUILD_PATH/config.log" 2>&1
rc=${PIPESTATUS[1]}
set -o pipefail
[ "$rc" = 0 ] || { tail -20 "$BUILD_PATH/config.log" >&2; ll_die "kernel: oldconfig failed"; }
grep -q "(NEW)" "$BUILD_PATH/config.log" && ll_die "kernel: oldconfig asked new questions (see $WORK/config.log): update system/kernel/rabbitv3_defconfig"
make -j"$(nproc)" LOCALVERSION="$LL_KERNEL_TAG" zImage > "$BUILD_PATH/build.log" 2>&1 ||
    { tail -40 "$BUILD_PATH/build.log" >&2; ll_die "kernel: compile failed (see $WORK/build.log)"; }
