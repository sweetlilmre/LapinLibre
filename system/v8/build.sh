#!/bin/bash
# SPDX-License-Identifier: MIT
# V8 2.5.9.23 (the last release of 2.5.8's branch, with its ARM code generator
# fixes) as libv8.so for the rabbit, built with the system toolchain (GCC 14.3, uClibc-ng).
# GCC 4.3.5 miscompiles V8's try/catch; GCC 14 compiles it correctly.
#
#   bash system/v8/build.sh            (after ./build.sh system)
#
# Built as V8's SConstruct would for library=shared, os=linux, arch=arm, mode=release,
# snapshot=off (no snapshot: Snapshot::data_ is one byte), without SCons: js2c turns the
# JavaScript library into libraries.cc, then the library sources SConscript lists
# (all + arch:arm + os:linux) are compiled and linked. No soname. The flags are
# V8's, plus -mno-thumb-interwork (V8 stops on Thumb interworking below ARMv5T) and the
# warnings GCC 14 adds, turned off so that V8's -Werror stays. system/v8/patches:
# js2c on Python 3, platform-linux.cc on uClibc. Output: $LL_WORK/v8/libv8.so, and V8's sample shell
# $LL_WORK/v8/v8-shell (/usr/bin/v8-shell on the rabbit).
#
# Two flags keep GCC 14 from optimising on rules V8 2.5.8 breaks (each found by a crash under
# qemu and fixed by that flag alone; with both, V8's mjsunit suite passes in full):
#   -fno-delete-null-pointer-checks  V8 tests "this == NULL" (GCC 14 warns: -Wnonnull-compare);
#                                    without it every script crashed at start on a NULL access.
#   -fno-strict-aliasing             V8 reads and writes tagged pointers through other types;
#                                    without it the scavenger read a bad map word walking the
#                                    stack (4 mjsunit tests crashed).
# -fno-lifetime-dse was tried for the second and made no difference, so it isn't used.
. "$(dirname "$0")/../../scripts/common.sh"

W=$LL_WORK/v8
X=${V8_CROSS:-$LL_WORK/system/out/host/bin/arm-buildroot-linux-uclibcgnueabi-}
[ -x "${X}g++" ] || ll_die "v8: no system toolchain at ${X}g++ (run ./build.sh system)"
JOBS=${LAPIN_JOBS:-$(nproc)}

# Rebuilt when the system build changes (its stamp: the C library and headers, not only the compiler),
# unless another toolchain is given.
deps=("${X}g++")
[ -n "${V8_CROSS:-}" ] || deps+=("$LL_WORK/stamps/system")
key=$(ll_hash "$LL_ROOT/system/v8" "$LL_ROOT/config.toml" "${deps[@]}")
if ll_stamp_ok v8 "$key" && [ -f "$W/libv8.so" ]; then
    ll_note "v8: up to date ($W/libv8.so)"
    exit 0
fi

ll_log "v8: V8 2.5.9.23 in $W"
rm -rf "$W"; mkdir -p "$W/obj"
tar -xzf "$(ll_fetch v8-branch-src)" -C "$W" && mv "$W/v8-2.5.9.23" "$W/src" || ll_die "v8: can't unpack the source"
S=$W/src
for p in "$LL_ROOT"/system/v8/patches/*.patch; do
    ll_note "patch: $(basename "$p")"
    patch -d "$S" -p1 --quiet < "$p" || ll_die "v8: $(basename "$p") doesn't apply"
done

# The JavaScript library, as SConscript's LIBRARY_FILES and macros.py.
libs=$(sed -n "/^LIBRARY_FILES = '''/,/^'''/p" "$S/src/SConscript" | grep '\.js$')
(cd "$S/src" && python3 -E -s -B ../tools/js2c.py "$W/libraries.cc" "$W/libraries-empty.cc" CORE $libs macros.py) ||
    ll_die "v8: js2c failed"

# The library sources: SConscript's 'all' and 'arch:arm' lists, Linux's, and the empty snapshot.
files=$(sed -n "/^SOURCES = {/,/^}/p" "$S/src/SConscript" | awk '
    /^  .all.: Split/ {on=1; next} /^  .arch:arm.: Split/ {on=1; next} /^  .arch:/ {on=0} /"""\),/ {on=0}
    on && $1 ~ /\.cc$/ {print $1}')
files="$files platform-linux.cc platform-posix.cc snapshot-empty.cc"

DEFS="-DV8_TARGET_ARCH_ARM -DCAN_USE_UNALIGNED_ACCESSES=0 -DENABLE_VMSTATE_TRACKING -DENABLE_LOGGING_AND_PROFILING
      -DENABLE_DEBUGGER_SUPPORT -DV8_SHARED -DBUILDING_V8_SHARED"
FLAGS="-O3 -fomit-frame-pointer -fdata-sections -ffunction-sections -ansi -fno-rtti -fno-exceptions -fPIC
       -fvisibility=hidden -march=armv4t -marm -mno-thumb-interwork -msoft-float
       -fno-delete-null-pointer-checks -fno-strict-aliasing"
WARN="-Wall -Werror -W -Wno-unused-parameter -Wnon-virtual-dtor
      -Wno-unused-local-typedefs -Wno-narrowing -Wno-unused-but-set-variable -Wno-misleading-indentation
      -Wno-implicit-fallthrough -Wno-nonnull-compare -Wno-class-memaccess -Wno-dangling-pointer
      -Wno-array-bounds -Wno-c++11-compat"
export X DEFS FLAGS WARN S W
printf '%s\n' $files | sort -u > "$W/files.txt"
echo "$W/libraries.cc" >> "$W/files.txt"
ll_note "v8: compiling $(wc -l < "$W/files.txt") files with $("${X}g++" --version | head -1)"
xargs -P "$JOBS" -I{} sh -c '
    f={}; n=$(basename "$f" .cc); case $f in /*) src=$f ;; *) src=$S/src/$f ;; esac
    ${X}g++ $DEFS $FLAGS $WARN -I"$S/src" -I"$S/include" -c "$src" -o "$W/obj/$(echo "$f" | tr / _).o" 2> "$W/obj/$n.log" ||
        { echo "FAIL $f"; sed -n 1,20p "$W/obj/$n.log"; }' < "$W/files.txt" > "$W/compile.txt"
if grep -q '^FAIL' "$W/compile.txt"; then
    cat "$W/compile.txt" >&2
    ll_die "v8: $(grep -c '^FAIL' "$W/compile.txt") files failed to compile"
fi

ll_note "v8: linking libv8.so"
"${X}g++" -shared -o "$W/libv8.so" "$W"/obj/*.o -lpthread || ll_die "v8: link failed"
# V8's sample shell, linked to it: the rabbit's /usr/bin/v8-shell.
"${X}g++" -O2 -fno-rtti -fno-exceptions -march=armv4t -marm -mno-thumb-interwork -msoft-float -Wall -Werror \
    -Wno-unused-parameter -Wno-c++11-compat -I"$S/include" -o "$W/v8-shell" "$S/samples/shell.cc" -L"$W" -lv8 -lpthread ||
    ll_die "v8: the sample shell failed to build"
ll_stamp_set v8 "$key"
ll_note "v8: $W/libv8.so ($(stat -c %s "$W/libv8.so") bytes)"
