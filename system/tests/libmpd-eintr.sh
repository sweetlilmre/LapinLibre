#!/bin/bash
# SPDX-License-Identifier: MIT
# Does the system build's libmpd survive a signal while it waits for MPD?
#
#   bash system/tests/libmpd-eintr.sh      (after ./build.sh system, and tests/run.sh's sysroot)
#
# Builds libmpd_eintr.c with the system toolchain against its libmpd, starts fake_slow_mpd.py
# (which answers "status" after 1.5 s), and runs the client under qemu on the core's sysroot
# with a timer signal arriving every 0.2 s. Prints PASS or FAIL; the exit status follows.
. "$(dirname "$0")/../../scripts/common.sh"
set +e
O=$LL_WORK/system/out
S=$LL_WORK/sysroot-core
X=$O/host/bin/arm-buildroot-linux-uclibcgnueabi-
D=$(dirname "$0")
[ -d "$S/usr/lib" ] || ll_die "libmpd-eintr: no core sysroot (run bash tests/run.sh)"
T=$(mktemp -d)
"${X}gcc" -O2 -o "$T/libmpd_eintr" "$D/libmpd_eintr.c" $("$O/host/bin/pkg-config" --cflags --libs libmpd) ||
    ll_die "libmpd-eintr: the client doesn't build"
# The library under test is the system build's, from its target.
cp "$(readlink -f "$O/target/usr/lib/libmpd.so.0")" "$S/usr/lib/libmpd.so.0"
"$LL_PY" -I -B "$D/fake_slow_mpd.py" "$T/port" &
server=$!
for _ in $(seq 50); do [ -s "$T/port" ] && break; sleep 0.1; done
out=$(timeout 60 qemu-arm -cpu arm926 -L "$S" "$T/libmpd_eintr" "$(cat "$T/port")" 2>&1)
rc=$?
kill $server 2> /dev/null
wait $server 2> /dev/null
printf '%s\n' "$out" | sed 's/^/      /'
rm -rf "$T"
if [ $rc = 0 ]; then echo "PASS  system/libmpd-eintr"; else echo "FAIL  system/libmpd-eintr"; fi
exit $rc
