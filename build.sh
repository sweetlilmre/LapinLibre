#!/bin/bash
# SPDX-License-Identifier: MIT
# Build LapinLibre.
#
#   ./build.sh [stage...]
#
# Stages, run in this order when none is named:
#   check      host prerequisites and the repository's file rules
#   fetch      download every pinned input into the cache
#   toolchain  the cross toolchain, GCC 14.3 and uClibc-ng: the published one, or built
#              with Buildroot (system/toolchain/build.sh)
#   system     Buildroot 2026.02: the rabbit's packages, with that toolchain
#              (system/build.sh)
#   v8         V8 2.5.9.23's libv8.so and sample shell with the system toolchain
#              (system/v8/build.sh)
#   kernel     the kernel on Linux 2.6.32 with the system toolchain (system/kernel/build.sh)
#   userland   the Karotz programs and libraries and karotz-player, with
#              the system toolchain (system/userland.sh), and the USB key's
#              statically linked install tools (system/install-tools.sh)
#   image      the USB-key install image                (image/build.sh)
#   test       the test suite                           (tests/run.sh)
# Other commands:
#   clean      remove the work location (downloads are kept: clean-all removes them too)
#   where      print the work location
#
# Each stage skips what is already up to date. Output goes to dl/, work/ and
# out/ under the work location (see scripts/common.sh).
. "$(dirname "$0")/scripts/common.sh"

ALL=(check fetch toolchain system v8 kernel userland image test)

run_stage() {
    case $1 in
        check)
            bash "$LL_ROOT/scripts/host-prereqs.sh"
            "$LL_PY" -I -B "$LL_ROOT/scripts/check-repo.py" ;;
        fetch) ll_fetch --all > /dev/null ;;
        toolchain) bash "$LL_ROOT/system/toolchain/build.sh" ;;
        system) bash "$LL_ROOT/system/build.sh" ;;
        v8) bash "$LL_ROOT/system/v8/build.sh" ;;
        kernel) bash "$LL_ROOT/system/kernel/build.sh" ;;
        userland) bash "$LL_ROOT/system/userland.sh" && bash "$LL_ROOT/system/install-tools.sh" ;;
        image) bash "$LL_ROOT/image/build.sh" ;;
        test) bash "$LL_ROOT/tests/run.sh" ;;
        clean) rm -rf "$LL_WORK" "$LL_OUT" ;;
        clean-all) rm -rf "$LL_WORK" "$LL_OUT" "$LL_DL" ;;
        where) echo "$LL_LOCATION" ;;
        *) ll_die "unknown stage: $1 (stages: ${ALL[*]} clean clean-all where)" ;;
    esac
}

stages=("$@")
[ ${#stages[@]} -gt 0 ] || stages=("${ALL[@]}")
[ "${stages[0]}" = where ] || ll_log "work location: $LL_LOCATION"
for stage in "${stages[@]}"; do
    [ "$stage" = where ] || ll_log "stage: $stage"
    start=$SECONDS
    run_stage "$stage"
    [ "$stage" = where ] || ll_log "stage $stage: done ($((SECONDS - start)) s)"
done
