#!/bin/bash
# SPDX-License-Identifier: MIT
# Run LapinLibre's tests and print each result:
#
#   bash tests/run.sh            run everything, compare with tests/expected/
#   bash tests/run.sh --record   (re)write the expected records instead
#   bash tests/run.sh SCENARIO...   only these behaviour scenarios (no other section)
#
# In order: the image tests, the test preloads, the app server tests, every behaviour scenario
# and the aggressive tests for LapinLibre's userland under qemu-user, and the system build's own library
# and program tests (system/tests: libmpd-eintr, mpd-karotz). The scenarios
# run on $LL_WORK/sysroot-core: LapinLibre's core as the key installs it (image/tools/sysroot.py
# core: image/tools/image.py's make_core, with no key), with the ld.so.cache its own ldconfig
# makes and an empty /var. Each test prints a PASS or FAIL line (SKIP where an input is missing);
# a summary follows, and the exit status is non-zero if anything failed. Needs ./build.sh system
# v8 kernel userland (the image tests build a key); system/check-image.sh tests a built key's own
# files.
. "$(dirname "$0")/../scripts/common.sh"
set +e

RECORD=()
ONLY=()
for arg in "$@"; do
    case "$arg" in
        --record) RECORD=(--record) ;;
        -*) ll_die "usage: tests/run.sh [--record] [SCENARIO...]" ;;
        *) ONLY+=("$arg") ;;
    esac
done

T=$LL_ROOT/tests
F=$LL_WORK/tests
S=$LL_WORK/sysroot-core
B=$LL_WORK/userland/system
O=$LL_WORK/system/out
X=$O/host/bin/arm-buildroot-linux-uclibcgnueabi-
mkdir -p "$F"
LOG=$F/run.log
: > "$LOG"
status=0

SCENARIOS="button_daemon ears_daemon led_daemon rfid_daemon scheduler_daemon webcam_daemon webcam_capture serial_daemon
           controller controller_platform controller_webcam_direct controller_message_query controller_schedule_gone
           controller_k2k_no_config controller_bad_descriptor app_server
           multimedia_daemon voice_daemon k2k_daemon karotzVM"
AGGRESSIVE=$(cd "$T/aggressive" && ls [a-z]*.py | sed 's/\.py$//')

# section TITLE COMMAND...: run a step, its output to the terminal and the log.
section() {
    local title=$1 t0=$SECONDS; shift
    printf '== %s\n' "$title" | tee -a "$LOG"
    "$@" 2>&1 | tee -a "$LOG"
    [ "${PIPESTATUS[0]}" = 0 ] || status=1
    printf '   (%s: %d s)\n' "$title" $((SECONDS - t0)) | tee -a "$LOG"
}

# The app server (tools/app-server) against a fake controller, with the system build's protoc 2.3.0;
# then its speech recognition (the sessions through the model only where sherpa-onnx and the model
# files are, else skipped).
app_server_tests() {
    local out
    if out=$("$LL_PY" -I -B -W ignore::ResourceWarning "$T/app_server/test_app_server.py" \
             "$O/host/bin/protoc-2.3.0" 2>&1); then
        echo "PASS  app server: $(printf '%s\n' "$out" | grep -o 'Ran [0-9]* tests')"
    else
        printf '%s\n' "$out" | tail -40
        echo "FAIL  app server"
        return 1
    fi
    if out=$("$LL_PY" -I -B "$T/app_server/test_asr_server.py" 2>&1); then
        echo "PASS  app server speech recognition: $(printf '%s\n' "$out" | grep -oE 'Ran [0-9]* tests|skipped=[0-9]*' | tr '\n' ' ')"
    else
        printf '%s\n' "$out" | tail -40
        echo "FAIL  app server speech recognition"
        return 1
    fi
}

build_fakes() {
    gcc -O2 -Wall -fPIC -shared -o "$F/devtrace.so" "$T/fakes/devtrace.c" -ldl &&
    for f in ioctlfake-arm syslogfake-arm timeshift-arm; do
        "${X}gcc" -march=armv4t -marm -O2 -nostdlib -shared -fPIC -Wl,--hash-style=sysv \
            -o "$F/$f.so" "$T/fakes/$f.c" || return 1
    done &&
    echo "PASS  test preloads built in $F"
}

make_sysroot() {
    [ -x "$B/usr/controller" ] || { echo "FAIL  no userland in $B (./build.sh userland)"; return 1; }
    [ -f "$LL_WORK/v8/libv8.so" ] || { echo "FAIL  no libv8.so (./build.sh v8)"; return 1; }
    "$LL_PY" -I -B "$LL_ROOT/image/tools/sysroot.py" core "$S" || { echo "FAIL  the core's sysroot"; return 1; }
    echo "PASS  sysroot $S ($(readlink "$S/usr/lib/libglib-2.0.so.0"), $(readlink "$S/lib/ld-uClibc.so.1"))"
}

# LL_TEST_JOBS (default: one per core; two per core made memory and timing checks fail, 2026-11-20): the behaviour scenarios and the aggressive tests run in that
# many shards at once. Each test checks itself (a scenario against its own record), so they are
# independent; each shard has its own network namespace, as some listen on fixed ports (9123, 8088,
# 6600). They wait mostly on real time, which is why the whole run took 65 minutes in a row on four
# idle cores, and 47 with only the aggressive tests in four round-robin shards (2026-11-19): the
# longest tests landed together. Shards are now filled longest first, each test into the
# least-loaded shard, from the durations the last run wrote ($F/durations; 30 s when unknown).
# SERIAL run first and alone: their timings break under load (presses measured in milliseconds).
# --record: one at a time.
JOBS=${LL_TEST_JOBS:-$(nproc 2> /dev/null || echo 1)}
# multimedia_daemon compares its whole record, whose order shifted under load (2026-11-20). Not
# karotzVM, whose order differs here even alone, nor karotzvm_reply_ends, alone it held the run 17
# minutes: both measure karotzVM's speed under qemu-user, slow on a 4-core host.
SERIAL="button_bounce button_empty_reads multimedia_daemon"
DURATIONS=$F/durations

# harness_pool SUITE:NAME...: every test, SERIAL first and alone, the rest in JOBS balanced shards;
# the shards' outputs follow one another, and the durations are kept for the next run.
harness_pool() {
    local items=() serial=() rc=0 dir x
    for x in "$@"; do
        case " $SERIAL " in *" ${x#*:} "*) serial+=("$x") ;; *) items+=("$x") ;; esac
    done
    if [ ${#RECORD[@]} -gt 0 ] || [ "$JOBS" -le 1 ] \
       || ! unshare --user --map-current-user --net true 2> /dev/null; then
        for x in "${serial[@]}" "${items[@]}"; do harness "${x%%:*}" "${x#*:}" || rc=1; done
        return $rc
    fi
    for x in "${serial[@]}"; do harness "${x%%:*}" "${x#*:}" || rc=1; done
    export LL_PY T S B F
    dir=$(mktemp -d)
    # The shards: a line per shard, "suite:name ..." (awk: longest first, least-loaded shard).
    printf '%s\n' "${items[@]}" | awk -v jobs="$JOBS" -v known="$DURATIONS" '
        BEGIN { while ((getline line < known) > 0) { split(line, f, " "); d[f[2]] = f[1] } }
        { n++; name[n] = $0; split($0, p, ":"); t[n] = (p[2] in d) ? d[p[2]] : 30 }
        END {
            for (i = 1; i <= n; i++) for (j = i + 1; j <= n; j++)
                if (t[j] > t[i]) { x = t[i]; t[i] = t[j]; t[j] = x; y = name[i]; name[i] = name[j]; name[j] = y }
            if (jobs > n) jobs = n
            for (i = 1; i <= n; i++) {
                best = 1; for (k = 2; k <= jobs; k++) if (load[k] < load[best]) best = k
                load[best] += t[i]; shard[best] = shard[best] " " name[i]
            }
            for (k = 1; k <= jobs; k++) print shard[k]
        }' > "$dir/shards"
    local pids=() i=0 line
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        # shellcheck disable=SC2086 # the shard's words, one test each
        unshare --user --map-current-user --net -- bash -c '
            ip link set lo up 2> /dev/null
            rc=0
            for suite in scenarios aggressive; do
                names=()
                for x in "$@"; do [ "${x%%:*}" = "$suite" ] && names+=("${x#*:}"); done
                [ ${#names[@]} -gt 0 ] || continue
                timeout 7200 "$LL_PY" -I -B "$T/harness.py" --suite "$suite" --sysroot "$S" --build "$B" \
                    --fakes "$F" --expected "$T/expected" "${names[@]}" || rc=1
            done
            exit $rc' shard $line > "$dir/$i.out" 2>&1 &
        pids+=($!)
        i=$((i + 1))
    done < "$dir/shards"
    for ((k = 0; k < i; k++)); do
        wait "${pids[k]}" || rc=1
        cat "$dir/$k.out"
    done
    # "PASS  name: ... (12 s)" -> "12 name", for the next run's shards.
    cat "$dir"/*.out | sed -n 's/^\(PASS\|FAIL\)  \([a-zA-Z0-9_]*\).*(\([0-9][0-9]*\)\(\.[0-9]*\)\{0,1\} s)$/\3 \2/p' \
        > "$DURATIONS.new" && [ -s "$DURATIONS.new" ] && mv -f "$DURATIONS.new" "$DURATIONS"
    rm -rf "$dir"
    return $rc
}

# harness SUITE NAME...
harness() {
    local suite=$1; shift
    # Only the behaviour scenarios have records; the aggressive tests check either way.
    local record=("${RECORD[@]}")
    [ "$suite" = scenarios ] || record=()
    timeout 7200 "$LL_PY" -I -B "$T/harness.py" "${record[@]}" --suite "$suite" --sysroot "$S" --build "$B" \
        --fakes "$F" --expected "$T/expected" "$@"
}

start=$SECONDS
cd "$LL_ROOT" || exit 1
if [ ${#ONLY[@]} -gt 0 ]; then
    section "sysroot" make_sysroot
    section "test preloads" build_fakes
    section "behaviour scenarios" harness scenarios "${ONLY[@]}"
else
    # The image tests need nothing the others build: they run alongside, and
    # their section is printed when they end (before the system tests).
    IMG=$(mktemp)
    "$LL_PY" -I -B "$T/image/run.py" > "$IMG" 2>&1 & img_pid=$!
    section "test preloads" build_fakes
    section "app server tests" app_server_tests
    section "sysroot" make_sysroot
    # shellcheck disable=SC2046,SC2086
    section "behaviour scenarios and aggressive tests (qemu-user)" harness_pool \
        $(printf 'scenarios:%s ' $SCENARIOS) $(printf 'aggressive:%s ' $AGGRESSIVE)
    printf '== %s\n' "image tests" | tee -a "$LOG"
    wait "$img_pid" || status=1
    tee -a "$LOG" < "$IMG"
    rm -f "$IMG"
    if [ ${#RECORD[@]} = 0 ]; then
        section "libmpd retries on EINTR" bash "$LL_ROOT/system/tests/libmpd-eintr.sh"
        section "mpd-karotz" bash "$LL_ROOT/system/tests/mpd-karotz.sh"
    fi
fi

pass=$(grep -c '^PASS' "$LOG")
fail=$(grep -c '^FAIL' "$LOG")
skip=$(grep -c '^SKIP' "$LOG")
recorded=$(grep -c '^RECORDED' "$LOG")
echo "== summary"
grep '^FAIL' "$LOG" | sed 's/^/   /'
printf 'tests: %s passed, %s failed, %s skipped' "$pass" "$fail" "$skip"
[ "$recorded" = 0 ] || printf ', %s expected records written' "$recorded"
printf ' (%d min; log in %s)\n' $(((SECONDS - start + 30) / 60)) "$LOG"
[ "$fail" = 0 ] || status=1
exit $status
