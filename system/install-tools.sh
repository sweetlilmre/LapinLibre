#!/bin/bash
# SPDX-License-Identifier: MIT
# The USB key's install tools: madplay and ears, statically linked, for the install
# to run from /tmp on whatever system the rabbit still has (the factory system, Free Rabbits OS or LapinLibre)
# while /usr is erased: static, so they need none of that system's libraries.
#
#   bash system/install-tools.sh     (after system/build.sh and system/userland.sh)
#
# madplay 0.15.2b with libmad 0.15.1b, libid3tag 0.16.3 and zlib 1.3.2 built in: Buildroot's own
# source trees for them (unpacked and patched by system/build.sh), copied aside and configured as
# Buildroot configures them, but static, with OSS output (not ALSA) and no NLS. ears is
# userland/src/rootfs/ears. Writes $LL_OUT/install-tools/install-tools.tar (both programs at its
# top, root-owned, the build's fixed time stamp), and prints their sizes.
. "$(dirname "$0")/../scripts/common.sh"

B=$LL_WORK/system/out/build
H=$LL_WORK/system/out/host
HOST=arm-buildroot-linux-uclibcgnueabi
X=$H/bin/$HOST-
W=$LL_WORK/install-tools
O=$LL_OUT/install-tools
for d in libzlib-1.3.2 libid3tag-0.16.3 libmad-0.15.1b madplay-0.15.2b; do
    [ -d "$B/$d" ] || ll_die "install-tools: no $B/$d (run ./build.sh system)"
done
rm -rf "$W" && mkdir -p "$W/src" "$W/prefix" "$O"
for d in libzlib-1.3.2 libid3tag-0.16.3 libmad-0.15.1b madplay-0.15.2b; do cp -a "$B/$d" "$W/src/"; done
P=$W/prefix
export PATH="$H/bin:$PATH" CC="${X}gcc" AR="${X}ar" RANLIB="${X}ranlib" STRIP="${X}strip"
CFLAGS="-O2 -marm"
log=$W/build.log
step() { echo "== install-tools: $1"; }
run() { "$@" >> "$log" 2>&1 || ll_die "install-tools: $* failed (see $log)"; }

step "zlib"
cd "$W/src/libzlib-1.3.2" || exit 1
run make distclean
CFLAGS="$CFLAGS" run ./configure --static --prefix="$P"
run make -j"$(nproc)" libz.a
run make install

step "libid3tag"
cd "$W/src/libid3tag-0.16.3" || exit 1
rm -rf build-static CMakeCache.txt CMakeFiles
run cmake -S . -B build-static -DCMAKE_TOOLCHAIN_FILE="$H/share/buildroot/toolchainfile.cmake" \
    -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF -DCMAKE_INSTALL_PREFIX="$P" \
    -DZLIB_LIBRARY="$P/lib/libz.a" -DZLIB_INCLUDE_DIR="$P/include"
run cmake --build build-static -j"$(nproc)"
run cmake --install build-static

step "libmad"
cd "$W/src/libmad-0.15.1b" || exit 1
run make distclean
CFLAGS="$CFLAGS" run ./configure --host=$HOST --build="$(gcc -dumpmachine)" --prefix="$P" \
    --enable-static --disable-shared --disable-debugging --disable-sso --enable-aso --disable-strict-iso
run make -j"$(nproc)"
run make install

step "madplay"
cd "$W/src/madplay-0.15.2b" || exit 1
run make distclean
CFLAGS="$CFLAGS" CPPFLAGS="-I$P/include" LDFLAGS="-L$P/lib -static" \
    PKG_CONFIG_LIBDIR="$P/lib/pkgconfig:$P/share/pkgconfig" PKG_CONFIG_SYSROOT_DIR= \
    run ./configure --host=$HOST --build="$(gcc -dumpmachine)" --prefix="$P" \
        --disable-shared --enable-static --disable-nls --without-alsa --without-esd
run make -j"$(nproc)" madplay

step "ears"
mkdir -p "$W/out"
run "${X}gcc" $CFLAGS -static -Wall -o "$W/out/ears" "$LL_ROOT/userland/src/rootfs/ears/ears.c"
cp "$W/src/madplay-0.15.2b/madplay" "$W/out/madplay"
for p in madplay ears; do
    run "$STRIP" "$W/out/$p"
    "${X}readelf" -d "$W/out/$p" 2>/dev/null | grep -q NEEDED && ll_die "install-tools: $p is not static"
    "${X}readelf" -l "$W/out/$p" | grep -q "program interpreter" && ll_die "install-tools: $p asks for a loader"
done
tar --sort=name --owner=0 --group=0 --numeric-owner --mtime="@${SOURCE_DATE_EPOCH:-0}" --mode=0755 \
    -C "$W/out" -cf "$O/install-tools.tar" madplay ears
step "$O/install-tools.tar: madplay $(stat -c %s "$W/out/madplay") bytes, ears $(stat -c %s "$W/out/ears") bytes, md5 $(md5sum < "$O/install-tools.tar" | cut -c1-32)"
