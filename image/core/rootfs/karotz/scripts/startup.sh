#!/bin/sh
# SPDX-License-Identifier: MIT
# The start, run once by init: the time zone, cron, the Wi-Fi (network.sh), then the services
# (/etc/init.d/S??*), the clock and the ready signal. The nose pulses cyan while it waits for the
# Wi-Fi. Without a network the nose turns solid red and the Wi-Fi is retried in the background until
# it comes up; the services start then. init's other lines (SSH, telnet, power) don't wait for it.
PATH=/bin:/sbin:/usr/bin:/usr/sbin:/karotz/bin:/usr/karotz/bin
export PATH
LED=/karotz/bin/led
NETWORK_WAIT=60
RETRY_EVERY=30
INITD=/etc/init.d

log() {
    echo "startup: $*"
    logger -t startup "$*" 2> /dev/null
}

# The nose, from the led tool (led-daemon isn't running yet): any running led is stopped first.
nose_waiting() {
    killall led 2> /dev/null
    $LED -l 00FFFF -p 000000 -d 400 &
}
nose_red() {
    killall led 2> /dev/null
    $LED -l FF0000
}

# Is led-daemon on the system bus?
led_daemon_up() {
    dbus-send --system --print-reply --dest=org.freedesktop.DBus / org.freedesktop.DBus.NameHasOwner \
        string:com.mindscape.karotz.Led 2> /dev/null | grep -q 'boolean true'
}

# Everything that needs the network: the services, the clock, the ready signal.
services() {
    # Buildroot's rcS convention: every executable S??* in name order, with `start`. D-Bus
    # (S30dbus-daemon), the core's daemons (S40lapinlibre), then any later ones (50 and up).
    for script in "$INITD"/S??*; do
        [ -x "$script" ] && "$script" start
    done
    timeout 30 busybox ntpd -q -p pool.ntp.org
    # Ready: led-daemon pulses the nose green for good, once it owns its name (at most 30 s).
    i=0
    while [ $i -lt 30 ] && ! led_daemon_up; do
        sleep 1
        i=$((i + 1))
    done
    killall led 2> /dev/null
    dbus-send --system --print-reply --dest=com.mindscape.karotz.Led /com/mindscape/karotz/Led \
        com.mindscape.karotz.KarotzInterface.pulse string:"" string:00FF00 string:000000 int32:700 int32:-1 \
        > /dev/null 2>&1 || log "led-daemon didn't take the ready pulse"
    /usr/bin/karotz-player /usr/karotz/sounds/boot-ready.wav > /dev/null 2>&1
    log "end"
}

log "start"
[ -e /etc/TZ ] || echo UTC0 > /etc/TZ

# cron, with root's (possibly empty) table.
mkdir -p /usr/spool/cron/crontabs
[ -e /usr/spool/cron/crontabs/root ] || : > /usr/spool/cron/crontabs/root
crond -c /usr/spool/cron/crontabs

nose_waiting
if /karotz/scripts/network.sh $NETWORK_WAIT; then
    services
else
    nose_red
    log "no network"
    (
        while :; do
            sleep $RETRY_EVERY
            nose_waiting
            /karotz/scripts/network.sh $NETWORK_WAIT && break
            nose_red
        done
        log "network up"
        services
    ) &
fi
exit 0
