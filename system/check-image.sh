#!/bin/bash
# SPDX-License-Identifier: MIT
# Does the USB key's own system work?
#
#   bash system/check-image.sh [KEY]     (default KEY: $LL_OUT/usbkey, from
#                                         image/tools/image.py --out ...)
#
# Makes $LL_WORK/sysroot-image from the key's rootfs.img.gz and usr.tar.gz (image/tools/sysroot.py
# sysroot-image), with an ld.so.cache from the image's own ldconfig, as linuxrc makes one on the
# rabbit. Then, under qemu on it:
# - the root's flash_erase (the system build's mtd-utils: the key's flashing), the system build's openssl, the
#   timeout link to BusyBox, and karotz-player: it opens and decodes one of the rabbit's
#   sounds up to the audio device, which qemu doesn't have;
# - the image's programs: bash, gpg, busybox, mpd and python's imports;
# - every behaviour scenario of tests/run.sh, on this sysroot.
# Prints PASS or FAIL per check; the exit status is non-zero if any failed.
. "$(dirname "$0")/../scripts/common.sh"
set +e
T=$LL_ROOT/tests
K=${1:-$LL_OUT/usbkey}
S=$LL_WORK/sysroot-image
B=$LL_WORK/userland/system
[ -s "$K/rootfs.img.gz" ] || ll_die "check-image: no key in $K"
"$LL_PY" -I -B "$LL_ROOT/image/tools/sysroot.py" sysroot-image "$K" || ll_die "can't make $S"

status=0
run() {  # run [-E VAR=VALUE] PROGRAM ARGS...: under qemu on the image's sysroot
    local env=(); [ "$1" = -E ] && { env=(-E "$2"); shift 2; }
    timeout 60 qemu-arm -cpu arm926 "${env[@]}" -L "$S" "$@" < /dev/null 2>&1 | head -c 4000
}
check() {  # check NAME PATTERN COMMAND...: PASS if the output matches PATTERN
    local name=$1 pattern=$2; shift 2
    local out; out=$(run "$@")
    if printf '%s' "$out" | grep -qE "$pattern"; then echo "PASS  image/$name"; else
        echo "FAIL  image/$name"; printf '%s\n' "$out" | head -5 | sed 's/^/      /'; status=1; fi
}
echo "== the image's files, on its own sysroot"
# The loader finds /usr/karotz/lib through ld.so.conf and the cache, as on the rabbit (no RPATH,
# no LD_LIBRARY_PATH): uClibc-ng needs LDSO_CACHE_SUPPORT for it (system/external/uclibc-ng.fragment).
check ld.so.cache "libkarotz-common.so => /usr/karotz/lib/libkarotz-common.so" \
    -E LD_TRACE_LOADED_OBJECTS=1 "$S/usr/karotz/bin/controller"
check flash_erase "mtd-utils" "$S/sbin/flash_erase" --version
check karotz-player "Can.t open audio device" "$S/usr/bin/karotz-player" "$S/usr/karotz/sounds/boot-ready.wav"
check openssl "OpenSSL" "$S/usr/bin/openssl" version
check timeout "Usage: timeout" "$S/bin/timeout" --help
check bash "GNU bash" "$S/bin/bash" --version
check gpg "gpg \(GnuPG\) 1\.4" "$S/bin/gpg" --version
check busybox "BusyBox v1\.37" "$S/bin/busybox"
check mpd "\[ffmpeg\]" "$S/usr/bin/mpd" --version
check python "imports ok" "$S/usr/bin/python2.4" -c \
    "import dbus, simplejson, socket, select, time, os, sys, struct, md5, sha, zlib; print 'imports ok'"

SCENARIOS="button_daemon ears_daemon led_daemon rfid_daemon scheduler_daemon webcam_daemon webcam_capture serial_daemon
           controller controller_platform controller_webcam_direct controller_message_query controller_schedule_gone
           controller_k2k_no_config controller_bad_descriptor app_server
           multimedia_daemon voice_daemon k2k_daemon karotzVM"
echo "== behaviour scenarios, on the image's sysroot"
cd "$LL_ROOT" || exit 1
# shellcheck disable=SC2086
timeout 7200 "$LL_PY" -I -B "$T/harness.py" --suite scenarios --sysroot "$S" --build "$B" --fakes "$LL_WORK/tests" \
    --expected "$T/expected" --label image/ $SCENARIOS || status=1
exit $status
