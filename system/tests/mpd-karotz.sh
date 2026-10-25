#!/bin/bash
# SPDX-License-Identifier: MIT
# Does the system build's mpd 0.15.12 (the external-client count, the FFmpeg 6.1 port) work?
#
#   bash system/tests/mpd-karotz.sh      (after ./build.sh system)
#
# Runs the system build's mpd (on the system build's target) under qemu with mpd_check.py: tags, playback of mp3,
# ogg, flac, wav, m4a and wma through a FIFO, a seek, and count_externalClient. Needs the host's ffmpeg for the
# test files.
# Prints PASS or FAIL.
. "$(dirname "$0")/../../scripts/common.sh"
set +e
O=$LL_WORK/system/out
D=$(dirname "$0")
[ -x "$O/target/usr/bin/mpd" ] || ll_die "mpd-karotz: no system mpd (build mpd-karotz)"
command -v ffmpeg > /dev/null || ll_die "mpd-karotz: the host's ffmpeg is needed for the test files"
T=$(mktemp -d)
timeout 900 "$LL_PY" -I -B "$D/mpd_check.py" "$T" "$O/target:$O/target/usr/bin/mpd"
rc=$?
rm -rf "$T"
if [ $rc = 0 ]; then echo "PASS  system/mpd-karotz"; else echo "FAIL  system/mpd-karotz"; fi
exit $rc
