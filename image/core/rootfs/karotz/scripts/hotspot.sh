#!/bin/sh
# SPDX-License-Identifier: MIT
# The setup network, for startup.sh while the rabbit has no network:
#   hotspot.sh press      waits for three presses of the head within 3 s, then asks for the setup network
#                         (the start-up's retry loop opens it: $RUN/wanted)
#   hotspot.sh [SECONDS]  opens it (default 600 s)
# The rabbit becomes the open access point "Karotz-XXXX" (the last four hex digits of its MAC), as
# a smart plug's setup network is (Clara, 2026-11-19). Open: what is typed on the page crosses the
# air in the clear, so it opens only on presses of the head -- whoever opens it has the rabbit in
# hand -- and closes as soon as the rabbit is set up, or after SECONDS. The nose pulses purple. A
# phone that joins gets an address (udhcpd) and the setup page, http://192.168.75.1/setup.html,
# which hands the Wi-Fi settings over (cgi-bin/wifi_setup, as the user web, into $RUN/in); this
# checks them again, writes them as the key's install does and reboots the rabbit.
# Exits 0 once settings are saved (the reboot follows), 1 when nobody set it up in time or the
# radio refused to be an access point; the network is then left down, for the retry loop.
PATH=/bin:/sbin:/usr/bin:/usr/sbin:/karotz/bin:/usr/karotz/bin
export PATH
RUN=/var/run/hotspot
LIMIT=${1:-600}
ADDR=192.168.75.1
LED=/karotz/bin/led
BUTTON=/dev/rabbit_button

log() {
    echo "hotspot: $*"
    logger -t hotspot "$*" 2> /dev/null
}

stop() {
    [ -f $RUN/udhcpd.pid ] && kill "$(cat $RUN/udhcpd.pid)" 2> /dev/null
    [ -f $RUN/wpa.pid ] && kill "$(cat $RUN/wpa.pid)" 2> /dev/null
    ifconfig wlan0 0.0.0.0 2> /dev/null
    rm -rf $RUN/active $RUN/wpa.conf $RUN/udhcpd.conf $RUN/in
}

# press: three presses of the head within 3 s, then $RUN/wanted. Not a long press: on a Karotz,
# holding the head is the power gesture (/dev/rabbit_power '0', and power reboots the rabbit), which
# a 4 s and a 10 s press did on Gengar (2026-11-20). /dev/rabbit_button gives '1' on a press and
# anything else on its release, with the contact's bounces (userland button-daemon.c waits 50 ms
# for a level); the daemons that read it run only once the network is up, so it is free while this
# waits.

# Time in hundredths of a second since boot: the head's presses are timed finer than date's seconds.
cs() {
    local u
    read -r u _ < /proc/uptime
    u="${u%.*}${u#*.}"
    echo "${u#"${u%%[!0]*}"}"     # no leading zero: ash reads 08 as octal
}
PRESS_MIN=2           # cs a press must last: a quick tap is 8 to 13 cs (Gengar, 2026-11-20)
PRESSES_WITHIN=300    # cs from the first of the three presses to the third

press() {
    mkdir -p $RUN
    n=0
    first=0
    up=0
    while :; do
        b=$(dd if=$BUTTON bs=1 count=1 2> /dev/null) || { sleep 5; continue; }
        t=$(cs)
        if [ "$b" = 1 ]; then
            up=$t
            continue
        fi
        # A release: a press counts when the head stayed down PRESS_MIN cs or more; the contact's
        # bounces, a few ms each, do not.
        [ "$up" -gt 0 ] && [ $((t - up)) -ge $PRESS_MIN ] || { up=0; continue; }
        [ $n -gt 0 ] && [ $((up - first)) -le $PRESSES_WITHIN ] || { n=0; first=$up; }
        up=0
        n=$((n + 1))
        if [ $n -ge 3 ]; then
            n=0
            log "three presses: the setup network is asked for"
            : > $RUN/wanted
            killall led 2> /dev/null
            $LED -l 8000FF -p 000000 -d 200 &
        fi
    done
}

[ "$1" = press ] && { press; exit 0; }

# take: the settings the page handed over, checked again (the page's own rules), into
# /usr/etc/conf as applysettings.sh writes them (WPA2 and WPA3, an address from the router).
take() {
    f=$RUN/in/settings
    [ -s $f ] || return 1
    ssid=$(sed -n 1p $f)
    psk=$(sed -n 2p $f)
    rm -f $f
    [ ${#ssid} -ge 1 ] && [ ${#ssid} -le 32 ] && [ ${#psk} -ge 8 ] && [ ${#psk} -le 63 ] || return 1
    case "$ssid$psk" in *[\"\\]* | *[![:print:]]*) return 1 ;; esac
    (umask 077 && printf 'network={\nssid="%s"\npsk="%s"\nkey_mgmt=WPA-PSK SAE\nieee80211w=1\nscan_ssid=1\nproto=WPA RSN\n}\n' \
        "$ssid" "$psk" > /usr/etc/conf/wpa.conf.new) && mv -f /usr/etc/conf/wpa.conf.new /usr/etc/conf/wpa.conf || return 1
    printf 'IP=""\nNETMASK=""\nGATEWAY=""\nDNS=""\n' > /usr/etc/conf/network.conf
    log "Wi-Fi network $ssid saved"
    : > $RUN/saved
}

mac=$(tr -d ':' < /sys/class/net/wlan0/address 2> /dev/null)
[ ${#mac} -eq 12 ] || { log "no wlan0"; exit 1; }
suffix=$(printf '%s' "${mac#????????}" | tr a-f A-F)

mkdir -p $RUN
(
    umask 077
    printf 'ap_scan=2\nnetwork={\nssid="Karotz-%s"\nmode=2\nfrequency=2437\nkey_mgmt=NONE\n}\n' \
        "$suffix" > $RUN/wpa.conf
    printf 'start 192.168.75.10\nend 192.168.75.40\ninterface wlan0\nmax_leases 8\nlease_file %s/leases\npidfile %s/udhcpd.pid\nopt subnet 255.255.255.0\nopt lease 600\n' \
        "$RUN" "$RUN" > $RUN/udhcpd.conf
)
rm -f $RUN/saved $RUN/wanted
rm -rf $RUN/in
mkdir -p $RUN/in && chown web $RUN/in && chmod 700 $RUN/in || { log "no drop folder for the web user"; exit 1; }
: > $RUN/leases
killall wpa_supplicant udhcpc 2> /dev/null
sleep 1
ifconfig wlan0 "$ADDR" netmask 255.255.255.0 up
if ! wpa_supplicant -iwlan0 -Dnl80211 -B -P$RUN/wpa.pid -c$RUN/wpa.conf; then
    log "the radio refused to be an access point"
    stop
    exit 1
fi
# busybox's own: the image links udhcpc, not udhcpd (a phone got no address, Gengar, 2026-11-20).
busybox udhcpd $RUN/udhcpd.conf
: > $RUN/active
log "Karotz-$suffix open for ${LIMIT} s: http://$ADDR/setup.html"
killall led 2> /dev/null
$LED -l 8000FF -p 000000 -d 700 &

i=0
while [ $i -lt "$LIMIT" ] && [ ! -f $RUN/saved ]; do
    take
    sleep 2
    i=$((i + 2))
done
killall led 2> /dev/null
if [ -f $RUN/saved ]; then
    log "settings saved: rebooting"
    sleep 3
    stop
    setsid reboot < /dev/null > /dev/null 2>&1 &
    exit 0
fi
log "nobody set it up in ${LIMIT} s: closed"
stop
exit 1
