#!/bin/bash
# SPDX-License-Identifier: MIT
# The key's install helpers, sourced by autorun and applysettings.sh: the log, errors, the nose,
# the install sounds, erasing a flash partition and emptying /usr for a new install.
#
# Sourcing defines functions only. Runs on whichever system the rabbit has when the key's install
# starts (LapinLibre, Free Rabbits OS or the factory's), so bash 3 and the common tools only.

SETUPLOG=/mnt/usbkey/install-log.txt
LOOP_PID=
LED_PID=

# A timestamped line in the key's install-log.txt, on the console and in syslog.
LOG() {
    local line
    line="$(date '+%Y-%m-%d %H:%M:%S') $*"
    echo "${line}" >> "${SETUPLOG}"
    echo "${line}"
    logger -t lapinlibre-install "$*" > /dev/null 2>&1
    return 0
}

# Stop the install: log why, the nose solid red, the loop sound stopped, the error sound, exit 1.
ERROR() {
    LOG "ERROR: $*"
    nose_solid FF0000
    stop_loop
    # ERROR_SOUND: the sounds to play, one after another (the error tones by default); none for a
    # script whose caller announces the failure itself.
    if [ "${ERROR_SOUND:-install-error.mp3}" != none ]; then
        for sound in ${ERROR_SOUND:-install-error.mp3}; do
            play "${sound}"
        done
    fi
    exit 1
}

# Free memory in kB once the page cache is dropped (what /tmp, which is RAM, can still take).
free_kb() {
    local key value unit
    sync
    { echo 3 > /proc/sys/vm/drop_caches; } 2> /dev/null
    while read -r key value unit; do
        [ "${key}" = "MemFree:" ] && { echo "${value}"; return 0; }
    done < /proc/meminfo
    echo 0
}

# An archive's contents into /usr; fails if either the decompression or tar does.
unpack_into_usr() { # archive
    ( set -o pipefail; gzip -dc "$1" | tar -xf - -C /usr )
}

# The nose. A pulse runs in the background until the nose is set again; any other led stops first.
nose_stop() {
    if [ -n "${LED_PID}" ]; then
        kill "${LED_PID}" > /dev/null 2>&1
        wait "${LED_PID}" > /dev/null 2>&1
    fi
    LED_PID=
    [ -x /bin/killall ] && /bin/killall led > /dev/null 2>&1
    return 0
}
nose_solid() { # colour
    nose_stop
    /karotz/bin/led -l "$1" > /dev/null 2>&1
    return 0
}
nose_pulse() { # colour, period in ms
    nose_stop
    /karotz/bin/led -l "$1" -p 000000 -d "$2" > /dev/null 2>&1 &
    LED_PID=$!
}

# The install tools (madplay, ears) and sounds, in /tmp so they outlive /usr's cleaning.
start_tools() {
    local sum
    sum=$(md5sum /mnt/usbkey/install-tools.tar 2> /dev/null | cut -d' ' -f1)
    [ -n "${sum}" ] && [ "${sum}" = "${INSTALL_TOOLS_MD5}" ] || ERROR "MD5 checksum error: install-tools.tar"
    tar -xf /mnt/usbkey/install-tools.tar -C /tmp || ERROR "can't unpack install-tools.tar into /tmp"
    cp /mnt/usbkey/sound/*.mp3 /tmp/ || ERROR "can't copy the install sounds to /tmp"
    return 0
}

# Play one of the install sounds (a name in /tmp); a sound that can't play is skipped.
play() { # name
    [ -x /tmp/madplay ] && [ -f "/tmp/$1" ] && /tmp/madplay -Q "/tmp/$1" > /dev/null 2>&1
    return 0
}

# The loop sound while the flash is written: install-busy.mp3, a second's pause, again.
sound_loop() {
    while :; do
        play install-busy.mp3
        sleep 1
    done
}
start_loop() {
    stop_loop
    sound_loop & LOOP_PID=$!
}
# Stopping the loop's shell leaves a install-busy.mp3 it was playing still holding the sound device, so the
# next sound couldn't open it: that madplay goes too (nothing else plays while the loop runs), and
# this returns once none is left, or after 2 s.
stop_loop() {
    local pids i
    if [ -n "${LOOP_PID}" ]; then
        kill "${LOOP_PID}" > /dev/null 2>&1
        wait "${LOOP_PID}" > /dev/null 2>&1
        for i in 1 2 3 4 5 6 7 8 9 10; do
            pids=$(pidof madplay)
            [ -n "${pids}" ] || break
            kill ${pids} > /dev/null 2>&1
            usleep 200000 2> /dev/null || sleep 1
        done
    fi
    LOOP_PID=
    return 0
}

# Erase a whole flash partition: current mtd-utils' flash_erase where the system has it, else the
# older flash_eraseall.
erase_partition() { # device
    if [ -x /sbin/flash_erase ]; then
        /sbin/flash_erase "$1" 0 0
    else
        /sbin/flash_eraseall "$1"
    fi
}

# Remove everything in a directory (hidden names too) but the names given.
remove_all_but() { # directory, names to keep
    local dir=$1 entry name keep k
    shift
    for entry in "${dir}"/* "${dir}"/.[!.]* "${dir}"/..?*; do
        [ -e "${entry}" ] || [ -L "${entry}" ] || continue
        name=${entry##*/}
        keep=
        for k in "$@"; do
            [ "${name}" = "${k}" ] && keep=1
        done
        [ -n "${keep}" ] || rm -rf "${entry}"
    done
    return 0
}

# Empty /usr for a new install, keeping the settings (/usr/etc/conf), the apps and messages
# (/usr/karotz/apps, /usr/karotz/messages) and lost+found; /usr, /usr/etc and /usr/karotz stay.
clean_usr() {
    remove_all_but /usr etc karotz lost+found
    [ -d /usr/etc ] && remove_all_but /usr/etc conf
    [ -d /usr/karotz ] && remove_all_but /usr/karotz apps messages uuid.conf
    return 0
}
