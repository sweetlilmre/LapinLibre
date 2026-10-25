#!/bin/bash
# SPDX-License-Identifier: MIT
# Does a key's own boot hold together? Under qemu-user, on the key's installed
# files only:
#
#   bash system/tests/core-boot.sh [KEY]     (default KEY: $LL_OUT/usbkey, from image/tools/image.py)
#
# - every absolute path the boot files in image/core name is in the image, or made at run time
#   (the list below says by what);
# - every boot script parses with the image's own shell (BusyBox ash);
# - the system bus starts with the image's configuration, and its policy lets a Karotz service
#   own its name while refusing other names;
# - Dropbear, started as sshd.sh starts it, takes the rabbit's authorized_keys key and refuses a
#   password (the key ~/.ssh/lapinlibre_rabbit, written where the
#   install writes lapinlibre.txt's SSH_PUBKEY: /usr/etc/conf/authorized_keys);
# - the image's telnetd gives a shell (inetd.sh starts it when the install set TELNET=yes).
# What qemu-user can't run (the kernel's mounts, init, the hardware daemons' devices) is left to
# the rabbit. Prints PASS or FAIL per check; the exit status is non-zero if any failed.
. "$(dirname "$0")/../../scripts/common.sh"
set +e
KEY=${1:-$LL_OUT/usbkey}
[ -f "$KEY/rootfs.img.gz" ] || ll_die "core-boot: no key at $KEY (image/tools/image.py --out $KEY)"
D=$(mktemp -d "${TMPDIR:-/tmp}/core-boot.XXXXXX")
# Everything started here runs from $D/root: at the end, stop it all and remove $D.
trap 'pkill -f -- "$D/root" 2> /dev/null; sleep 1; rm -rf "$D"' EXIT
R=$D/root
failed=0
result() { # name ok|other [detail]
    if [ "$2" = ok ]; then echo "PASS  core-boot/$1"; else echo "FAIL  core-boot/$1: $3"; failed=$((failed + 1)); fi
}
q() { timeout "${TMO:-20}" qemu-arm -L "$R" "$@"; }

"$LL_PY" -I -B -c '
import gzip, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1] + "/image/tools"); sys.path.insert(0, sys.argv[1] + "/scripts")
import fstree, sysroot
key = Path(sys.argv[2])
rootfs = fstree.CramfsPart(gzip.decompress((key / "rootfs.img.gz").read_bytes()))
usr = fstree.TarPart(gzip.decompress((key / "usr.tar.gz").read_bytes()), mtime=0)
sysroot.write_sysroot(rootfs, usr, Path(sys.argv[3]))' "$LL_ROOT" "$KEY" "$R" > /dev/null || ll_die "core-boot: can't unpack $KEY"
grep -q . "$R/lib/firmware/rt73.bin" 2> /dev/null || ll_die "core-boot: $KEY isn't a LapinLibre key (no /lib/firmware/rt73.bin)"

# --- references -------------------------------------------------------------------------------
# Made at run time, brought by the key's install, or a URL path on the rabbit's web server
# (/cgi-bin/..., served from /usr/www): these needn't be in the image.
RUNTIME='^/(proc|sys|dev|var|tmp|mnt/usbkey)(/|$)|^/com/mindscape/karotz/|^/org/freedesktop/|^/(usr/)?etc/dropbear$|^/etc/(TZ|resolv\.conf|ld\.so\.cache|mpd\.conf|inetd\.conf|passwd|inittab)$|^/usr/etc/(shells|passwd|inittab|inetd\.conf|conf)(/|$)|^/usr/karotz/uuid\.conf$|^/usr/spool(/|$)|^/usr/etc/usbkey\.d(/|$)|^/usr/karotz/\.ssh/authorized_keys$|^/usr/www(/|$)|^/cgi-bin(/|$)|^/etc/conf(/|$)|^/usr/karotz/sounds/[a-z][a-z]_(asr|rfid)_not_recognized\.mp3$'
missing=""
for p in $(grep -rhoE '(^|[[:space:]"=])/[a-z][A-Za-z0-9_./-]*' "$LL_ROOT/image/core" | sed 's/^[^/]*//' | sed 's/[.,]$//' | sort -u); do
    echo "$p" | grep -qE "$RUNTIME" && continue
    [ -e "$R$p" ] || [ -L "$R$p" ] || missing="$missing $p"
done
[ -z "$missing" ] && s=ok || s=missing
result references "$s" "not in the image:$missing"

# --- syntax -------------------------------------------------------------------------------------
bad=""
for f in "$R"/linuxrc "$R"/karotz/scripts/*.sh "$R"/usr/etc/init.d/S30dbus-daemon "$R"/usr/etc/init.d/S40lapinlibre; do
    q "$R/bin/busybox" sh -n "$f" 2> "$D/sh.err" || bad="$bad $(basename "$f"): $(head -1 "$D/sh.err")"
done
[ -z "$bad" ] && s=ok || s=bad
result syntax "$s" "$bad"

# The rabbit's accounts, with karotz as this user (so Dropbear can switch to it) and this user
# itself (dbus-daemon and its policy look it up). The sysroot's /etc is /usr/etc, as on the rabbit.
me=$(id -un)
{ echo "$me:*:$(id -u):$(id -g):test:/usr/karotz:/bin/sh"; sed "s|^karotz:\*:0:0:|karotz:*:$(id -u):$(id -g):|" "$R/karotz/etc/passwd"; } > "$R/usr/etc/passwd"

# --- the system bus -----------------------------------------------------------------------------
# D-Bus's own system.conf, with its paths moved here; /etc/dbus-1's includes (which qemu-user
# would look for on this host) become this test's system.d, where karotz.conf goes.
sed -e "s|<user>dbus</user>||" -e "s|<fork/>||" -e "s|<syslog/>||" -e "s|/run/dbus-daemon.pid|$D/bus.pid|" \
    -e "s|unix:path=/run/dbus/system_bus_socket|unix:path=$D/bus|" \
    -e "s|<includedir>system.d</includedir>||" \
    -e "s|<includedir>/etc/dbus-1/system.d</includedir>|<includedir>$D/system.d</includedir>|" \
    -e "s|/etc/dbus-1/system.conf|$D/none.conf|" -e "s|/etc/dbus-1/system-local.conf|$D/none-local.conf|" \
    "$R/usr/share/dbus-1/system.conf" > "$D/bus.conf"
mkdir -p "$D/system.d"
sed "s|user=\"root\"|user=\"$me\"|" "$R/usr/etc/dbus-1/system.d/karotz.conf" > "$D/system.d/karotz.conf"
TMO=60 q "$R/usr/bin/dbus-daemon" --config-file="$D/bus.conf" --nofork > "$D/bus.log" 2>&1 &
for i in $(seq 1 40); do [ -S "$D/bus" ] && break; sleep 0.5; done
own() { TMO=20 q -E "DBUS_SYSTEM_BUS_ADDRESS=unix:path=$D/bus" "$R/usr/bin/dbus-send" --system --print-reply \
            --dest=org.freedesktop.DBus / org.freedesktop.DBus.RequestName string:"$1" uint32:0 2>&1 | tr -s ' \n' ' '; }
a=$(own com.mindscape.karotz.Led); b=$(own org.example.Other)
case "$a$b" in *"uint32 1"*AccessDenied*) s=ok ;; *) s=bad ;; esac
result dbus-policy "$s" "Led: $a | other: $b | daemon: $(head -c 200 "$D/bus.log")"

# --- Dropbear -----------------------------------------------------------------------------------
# The key, as the install writes it; the image links ~/.ssh/authorized_keys to it.
[ -f "$HOME/.ssh/lapinlibre_rabbit.pub" ] && cp "$HOME/.ssh/lapinlibre_rabbit.pub" "$R/usr/etc/conf/authorized_keys"
chmod 700 "$R/usr/karotz/.ssh"; chmod 600 "$R/usr/etc/conf/authorized_keys" 2> /dev/null
if [ -s "$R/usr/karotz/.ssh/authorized_keys" ] && [ -f "$HOME/.ssh/lapinlibre_rabbit" ]; then
    # Dropbear starts itself again for each connection; that start goes through the host's
    # binfmt hook, which finds the sysroot through QEMU_LD_PREFIX.
    # Its host keys, as sshd.sh links them: /etc/dropbear into /usr/etc/conf.
    mkdir -p "$R/usr/etc/conf/dropbear"; rm -rf "$R/usr/etc/dropbear"; ln -s conf/dropbear "$R/usr/etc/dropbear"
    # qemu-user finds a path in the sysroot only if it is there already, so a host key Dropbear
    # made itself (-R) would go to the host's /etc/dropbear: make it first, with the image's own
    # dropbearkey. Making it on first use is for the rabbit to show.
    q -0 dropbearkey "$R/sbin/dropbear" -t ed25519 -f "$R/usr/etc/conf/dropbear/dropbear_ed25519_host_key" > "$D/key.log" 2>&1
    TMO=90 q -E "QEMU_LD_PREFIX=$R" "$R/sbin/dropbear" -F -E -s -R -p 127.0.0.1:2222 > "$D/dropbear.log" 2>&1 &
    for i in $(seq 1 60); do (echo > /dev/tcp/127.0.0.1/2222) 2> /dev/null && break; sleep 0.5; done
    SSHO="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=20 -p 2222"
    # Logging in is what is tested: the shell after it would start through the host's binfmt
    # hook, which doesn't know the sysroot.
    k=$(timeout 30 ssh $SSHO -N -v -o BatchMode=yes -o IdentitiesOnly=yes -i "$HOME/.ssh/lapinlibre_rabbit" karotz@127.0.0.1 < /dev/null 2>&1 | grep -m1 -E 'Authenticated to|Permission denied')
    p=$(timeout 30 ssh $SSHO -o BatchMode=yes -o PubkeyAuthentication=no -o PreferredAuthentications=password,keyboard-interactive karotz@127.0.0.1 true < /dev/null 2>&1)
    keys=$(ls "$R/usr/etc/conf/dropbear" | tr '\n' ' ')
    case "$k|$p|$keys" in *"Authenticated to"*"|"*"Permission denied"*"|"*host_key*) s=ok ;; *) s=bad ;; esac
    result ssh-key "$s" "key: $(echo "$k" | head -c 160) | password: $(echo "$p" | head -c 160) | host keys: $keys| $(tail -c 200 "$D/dropbear.log")"
else
    result ssh-key no "no ~/.ssh/lapinlibre_rabbit here, or the image's authorized_keys isn't linked to /usr/etc/conf"
fi

# --- telnet (TELNET=yes) -------------------------------------------------------------------------
TMO=40 q "$R/bin/busybox" telnetd -F -p 2323 -l /bin/sh > "$D/telnetd.log" 2>&1 &
for i in $(seq 1 40); do (echo > /dev/tcp/127.0.0.1/2323) 2> /dev/null && break; sleep 0.5; done
t=$( { sleep 2; echo 'echo telnet-$((40 + 2))-ok'; sleep 2; } | timeout 20 nc 127.0.0.1 2323 2>&1 | tr -cd '\11\12\40-\176')
case "$t" in *telnet-42-ok*) s=ok ;; *) s=bad ;; esac
result telnet "$s" "$(echo "$t" | tail -c 160)"
[ "$failed" = 0 ]
