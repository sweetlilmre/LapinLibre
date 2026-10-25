#!/bin/sh
# SPDX-License-Identifier: MIT
# immortaldog: behaviour tests, POSIX sh (busybox on the rabbit, or Linux).
#
#   immortaldog.sh <immortaldog> <work dir> [<runs>]
#
# On the rabbit, with a build copied to RAM (nothing is written to flash):
#   ssh karotz@<rabbit> 'mkdir -p /dev/shm/new && cat > /dev/shm/new/immortaldog && chmod 755 /dev/shm/new/immortaldog' \
#       < <build>/usr/immortaldog
#   ssh karotz@<rabbit> sh -s -- /dev/shm/new/immortaldog /dev/shm/immtest 50 < tests/device/immortaldog.sh
# Natively (a real fork(), many runs quickly):
#   gcc -O2 -Wall -o /tmp/immortaldog userland/src/usr/immortaldog/immortaldog.c
#   sh tests/device/immortaldog.sh /tmp/immortaldog /tmp/immtest 50
#
# Under qemu-user the program can't be judged: qemu runs vfork() as fork().
#
# Checked: a program that can't be run stops immortaldog and removes its pid
# file, every time (<runs> times: the child's SIGTERM could once overtake
# the parent recording the child); a program that exits at once is relaunched
# at most once every 5 s; SIGTERM stops the child and removes the pid file; a
# child killed from outside is relaunched; an existing pid file is refused
# and left alone; the program starts with SIGTERM unblocked. Everything lives
# in <work dir>, and every immortaldog and child started here is stopped at
# the end. Prints PASS/FAIL per check; exits 1 if any failed.
IMM=$1
W=$2
RUNS=${3:-20}
if [ -z "$IMM" ] || [ -z "$W" ]; then
    echo "usage: $0 <immortaldog> <work dir> [<runs>]" >&2
    exit 2
fi
rm -rf "$W"
mkdir -p "$W" || exit 2
failed=0

pass() { echo "PASS  $1"; }
fail() { echo "FAIL  $1"; failed=1; }
check() { if [ "$2" = "$3" ]; then pass "$1"; else fail "$1: got '$2', expected '$3'"; fi; }
alive() { [ -n "$1" ] && kill -0 "$1" 2>/dev/null && echo yes || echo no; }
present() { [ -e "$1" ] && echo present || echo removed; }
psall() { ps axww 2>/dev/null || ps w; }

# The test programs.
printf '#!/bin/sh\necho start >> %s/quick.log\n' "$W" > "$W/quick"
printf '#!/bin/sh\necho "$$" >> %s/long-$1.log\nexec sleep 60\n' "$W" > "$W/long"
printf '#!/bin/sh\ngrep SigBlk /proc/$$/status > %s/mask.txt\nexec sleep 60\n' "$W" > "$W/mask"
# immortaldog passes no arguments: a wrapper per run gives each its own log.
for n in 3 4; do printf '#!/bin/sh\nexec %s/long %s\n' "$W" "$n" > "$W/long$n"; done
chmod 755 "$W/quick" "$W/long" "$W/mask" "$W/long3" "$W/long4"

# 1. A program that can't be run.
left=0
running=0
i=1
while [ "$i" -le "$RUNS" ]; do
    P=$W/t1.pid
    rm -f "$P"
    $IMM "$P" "$W/no-such-program"
    sleep 1
    [ -e "$P" ] && left=$((left + 1))
    [ "$(psall | grep -c "[i]mmortaldog $P")" -gt 0 ] && running=$((running + 1))
    rm -f "$P"
    i=$((i + 1))
done
check "missing program: pid file left in how many of $RUNS runs" "$left" 0
check "missing program: immortaldog still running in how many runs" "$running" 0

# 2. A program that exits at once: launches at 0, 5 and 10 s.
P=$W/t2.pid
$IMM "$P" "$W/quick"
sleep 12
IPID=$(cat "$P" 2>/dev/null)
check "quick program: launches in 12 s" "$(wc -l < "$W/quick.log" 2>/dev/null | tr -d ' ')" 3
kill -TERM "$IPID" 2>/dev/null
sleep 1
check "quick program: immortaldog alive after SIGTERM" "$(alive "$IPID")" no
check "quick program: pid file after SIGTERM" "$(present "$P")" removed

# 3. A running program, then SIGTERM to immortaldog.
P=$W/t3.pid
$IMM "$P" "$W/long3"
sleep 2
IPID=$(cat "$P" 2>/dev/null)
CPID=$(head -n1 "$W/long-3.log" 2>/dev/null)
check "running program: child started" "$(alive "$CPID")" yes
kill -TERM "$IPID" 2>/dev/null
sleep 1
check "running program: immortaldog alive after SIGTERM" "$(alive "$IPID")" no
check "running program: child alive after SIGTERM" "$(alive "$CPID")" no
check "running program: pid file after SIGTERM" "$(present "$P")" removed

# 4. The child killed from outside: relaunched (at least 5 s after its start).
P=$W/t4.pid
$IMM "$P" "$W/long4"
sleep 2
IPID=$(cat "$P" 2>/dev/null)
kill -KILL "$(head -n1 "$W/long-4.log" 2>/dev/null)" 2>/dev/null
sleep 7
check "killed child: launches" "$(wc -l < "$W/long-4.log" | tr -d ' ')" 2
check "killed child: new child alive" "$(alive "$(tail -n1 "$W/long-4.log")")" yes
kill -TERM "$IPID" 2>/dev/null
sleep 1
check "killed child: pid file after SIGTERM" "$(present "$P")" removed

# 5. A pid file already there.
P=$W/t5.pid
echo 999 > "$P"
$IMM "$P" "$W/quick" 2>/dev/null
sleep 2
check "existing pid file: its content" "$(cat "$P")" 999
check "existing pid file: immortaldog running" "$(psall | grep -c "[i]mmortaldog $P")" 0

# 6. The started program's signal mask (SIGTERM is bit 0x4000).
P=$W/t6.pid
$IMM "$P" "$W/mask"
sleep 2
IPID=$(cat "$P" 2>/dev/null)
check "signal mask of the program" "$(awk '{print $2}' "$W/mask.txt" 2>/dev/null)" 0000000000000000
kill -TERM "$IPID" 2>/dev/null
sleep 1

# Clean up whatever is left.
for f in "$W"/*.pid; do
    [ -e "$f" ] && kill -TERM "$(cat "$f")" 2>/dev/null
done
sleep 1
for c in "$W"/long-*.log; do
    [ -e "$c" ] && while read -r pid; do kill -KILL "$pid" 2>/dev/null; done < "$c"
done
check "left running: immortaldog" "$(psall | grep -c "[i]mmortaldog $W/")" 0
check "left running: test programs" "$(psall | grep -c "[s]leep 60")" 0
rm -rf "$W"
exit $failed
