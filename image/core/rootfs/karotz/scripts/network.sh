#!/bin/sh
# SPDX-License-Identifier: MIT
# The Wi-Fi, for startup.sh:   network.sh [SECONDS]   (the time limit, default 60)
# From the install's settings: /usr/etc/conf/wpa.conf (wpa_supplicant's network block) and
# /usr/etc/conf/network.conf (IP, NETMASK, GATEWAY, DNS; no IP, or no file, means DHCP).
# Exits 0 once wlan0 is associated with an access point and has an IPv4 address, 1 when that
# doesn't happen in time or there are no Wi-Fi settings. Safe to run again while the network is up
# or half up: it never starts a second wpa_supplicant or udhcpc.
PATH=/bin:/sbin:/usr/bin:/usr/sbin
export PATH
CONF=/usr/etc/conf
WPA_CONF=$CONF/wpa.conf
LIMIT=${1:-60}

log() {
    echo "network: $*"
    logger -t network "$*" 2> /dev/null
}

# Is PROGRAM running for wlan0? (Its command line names both.)
running() {
    for cmdline in /proc/[0-9]*/cmdline; do
        case "$(tr '\000' ' ' < "$cmdline" 2> /dev/null)" in
        *"$1"*wlan0*) return 0 ;;
        esac
    done
    return 1
}

# Associated with an access point (the kernel's wireless status; the image has no wpa_cli), and an
# IPv4 address.
associated() {
    iwconfig wlan0 2> /dev/null | grep -q 'Access Point: [0-9A-Fa-f][0-9A-Fa-f]:'
}
addressed() {
    ifconfig wlan0 2> /dev/null | grep -q 'inet addr:[0-9]'
}

if [ ! -s "$WPA_CONF" ]; then
    log "no Wi-Fi settings ($WPA_CONF)"
    exit 1
fi

IP= NETMASK= GATEWAY= DNS=
[ -r $CONF/network.conf ] && . $CONF/network.conf

ifconfig lo 127.0.0.1 up
ifconfig wlan0 up
running wpa_supplicant || wpa_supplicant -iwlan0 -Dwext -B -c$WPA_CONF

if [ -n "$IP" ]; then
    ifconfig wlan0 "$IP" netmask "${NETMASK:-255.255.255.0}" up
    if [ -n "$GATEWAY" ]; then
        route del default dev wlan0 2> /dev/null
        route add default gw "$GATEWAY" dev wlan0
    fi
    [ -n "$DNS" ] && echo "nameserver $DNS" > /etc/resolv.conf
else
    running udhcpc || udhcpc -b -i wlan0 -s /karotz/scripts/udhcpc_script.sh
fi

i=0
while :; do
    if associated && addressed; then
        log "wlan0 up"
        exit 0
    fi
    [ $i -ge "$LIMIT" ] && break
    sleep 1
    i=$((i + 1))
done
log "wlan0 not up after $LIMIT s"
exit 1
