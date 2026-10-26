# SPDX-License-Identifier: MIT
# The web API's status readers (web.sh reads this file the first time one is called): the
# network, the space, the counts, the servers and LapinLibre's own status. Each reads the kernel's
# and the settings' files with the shell's builtins; only the values no file holds come from a
# program (the address from ifconfig, the network's name from iwgetid, the time from date, the
# space from df, the controller from dbus-send).

# Escapes each input line for XML text, leaving out control characters.
WEB_XML_AWK='BEGIN { for (i = 1; i < 32; i++) drop[sprintf("%c", i)] = 1; drop[sprintf("%c", 127)] = 1 }
{
    o = ""
    n = length($0)
    for (i = 1; i <= n; i++) {
        c = substr($0, i, 1)
        if (c in drop) continue
        if (c == "&") c = "&amp;"
        else if (c == "<") c = "&lt;"
        else if (c == ">") c = "&gt;"
        else if (c == "\"") c = "&quot;"
        else if (c == "'"'"'") c = "&apos;"
        o = o c
    }
    printf "%s%s", (NR > 1 ? " " : ""), o
}'

# xml_text VALUE: VALUE escaped for XML text, in X. Printable ASCII without & < > " or ' needs no
# other process.
xml_text() {
    case $1 in
    *[![:print:]]* | *[\&\<\>\"\']*) X=$(printf '%s' "$1" | awk "$WEB_XML_AWK") ;;
    *) X=$1 ;;
    esac
}

# ini_get FILE SECTION KEY: KEY's value in [SECTION] of an ini file, in IV (the first one).
ini_get() {
    IV=""
    [ -f "$1" ] && [ -r "$1" ] || return 0
    _in=0
    while IFS= read -r _l || [ -n "$_l" ]; do
        _l=${_l%"$CR"}
        case $_l in
        \[*) if [ "$_l" = "[$2]" ]; then _in=1; else _in=0; fi ;;
        "$3="*) [ $_in = 1 ] && { IV=${_l#"$3="}; return 0; } ;;
        esac
    done < "$1"
    return 0
}

# disk_usage DIR: DU_FREE_KB, DU_USED_PERCENT (digits) and DU_FREE_HUMAN (df -h style) of the file
# system holding DIR; all empty when df can't tell. /usr and the stick are asked once per
# program, both with one df while the stick is mounted.
disk_usage() {
    DU_FREE_KB="" DU_USED_PERCENT="" DU_FREE_HUMAN=""
    case $1 in
    /usr) _dk=usr ;;
    /mnt/usbkey) _dk=usb ;;
    *) _dk="" ;;
    esac
    case $_dk in
    usr) [ -n "${DU_USR:-}" ] && { _du=$DU_USR; disk_usage_set; return; } ;;
    usb) [ -n "${DU_USB:-}" ] && { _du=$DU_USB; disk_usage_set; return; } ;;
    esac
    if [ -n "$_dk" ] && usb_mounted; then
        _df=$(df -Pk /usr /mnt/usbkey 2> /dev/null)
        _dk=both
    else
        _df=$(df -Pk "$1" 2> /dev/null)
    fi
    # After the header, one line per folder asked: file system, size, used, available, capacity,
    # mount point.
    _da=$1 _du=- DU_USR2=- DU_USB2=- _dn=0
    _rest=${_df#*"$NL"}
    [ "$_rest" = "$_df" ] && _rest=""
    while [ -n "$_rest" ]; do
        _l=${_rest%%"$NL"*}
        case $_rest in *"$NL"*) _rest=${_rest#*"$NL"} ;; *) _rest="" ;; esac
        _dn=$((_dn + 1))
        _dv=-
        set -f
        # shellcheck disable=SC2086
        set -- $_l
        set +f
        if [ $# -ge 5 ]; then
            case $4 in
            '' | *[!0-9]*) ;;
            *)
                human_kb "$4"
                _dv="$4 ${5%\%} $HK"
                ;;
            esac
        fi
        case $_dn in 1) DU_USR2=$_dv _du=$_dv ;; 2) DU_USB2=$_dv ;; esac
    done
    case $_dk in
    usr) DU_USR=$_du ;;
    usb) DU_USB=$_du ;;
    both)
        DU_USR=$DU_USR2 DU_USB=$DU_USB2
        [ "$_da" = /usr ] && _du=$DU_USR || _du=$DU_USB
        ;;
    esac
    disk_usage_set
}

# disk_usage_set: disk_usage's values from _du ("free used% human", or - when df couldn't tell).
disk_usage_set() {
    [ "$_du" != - ] || return 1
    set -f
    # shellcheck disable=SC2086
    set -- $_du
    set +f
    DU_FREE_KB=$1 DU_USED_PERCENT=${2:-} DU_FREE_HUMAN=${3:-}
    case $DU_USED_PERCENT in '' | *[!0-9]*) DU_USED_PERCENT="" ;; esac
    return 0
}

# human_kb KB: the size as df -h shows it, in HK: whole kilobytes below 1024 (123K), else one
# decimal of the largest unit up to T (63.8M, 3.1G), rounded to the nearest (to even on a tie).
human_kb() {
    nozeros "$1"
    _v=${NZ:-0}
    _div=1
    _u=K
    for _n in M G T; do
        [ "$_v" -ge $((_div * 1024)) ] || break
        _div=$((_div * 1024))
        _u=$_n
    done
    if [ $_u = K ]; then
        HK=$_v$_u
        return 0
    fi
    _t=$((_v * 10 / _div))
    _rem=$((_v * 10 % _div * 2))
    if [ $_rem -gt $_div ] || { [ $_rem -eq $_div ] && [ $((_t % 2)) = 1 ]; }; then
        _t=$((_t + 1))
    fi
    HK=$((_t / 10)).$((_t % 10))$_u
}

# count_lines TEXT: the number of lines in TEXT, in N.
count_lines() {
    N=0
    [ -n "$1" ] || return 0
    _t=$1$NL
    while [ -n "$_t" ]; do
        N=$((N + 1))
        _t=${_t#*"$NL"}
    done
    return 0
}

# The numbers of sounds, moods, stories and tags: NB_SOUNDS, NB_MOODS, NB_STORIES, NB_TAGS.
read_counts() {
    sound_dirs
    # Each sound name once, whichever folder and type it comes in.
    _seen=$NL
    NB_SOUNDS=0
    for _d in $SOUND_DIRS; do
        for _f in "$_d"/*.mp3 "$_d"/*.wav; do
            [ -f "$_f" ] || continue
            _n=${_f##*/}
            _n=${_n%.*}
            valid_id "$_n" || continue
            case $_seen in *"$NL$_n$NL"*) continue ;; esac
            _seen=$_seen$_n$NL
            NB_SOUNDS=$((NB_SOUNDS + 1))
        done
    done
    NB_MOODS=0 NB_STORIES=0
    if usb_mounted; then
        mp3_count "$WEB_MOODS"
        NB_MOODS=$N
        mp3_count "$WEB_STORIES"
        NB_STORIES=$N
    fi
    NB_TAGS=0
    if [ -e "$WEB_TAGS" ]; then
        count_lines "$(tags_read)"
        NB_TAGS=$N
    fi
}

# mp3_count DIR: the number of MP3 files in the folder whose names pass valid_id, in N.
mp3_count() {
    N=0
    for _f in "$1/"*.mp3; do
        [ -f "$_f" ] || continue
        _n=${_f##*/}
        valid_id "${_n%.mp3}" && N=$((N + 1))
    done
    return 0
}

# read_address: the rabbit's name (HOSTNAME_), and wlan0's address and mask (IP, NETMASK).
read_address() {
    first_line /proc/sys/kernel/hostname
    HOSTNAME_=$FL
    IP="" NETMASK=""
    _if=$(ifconfig wlan0 2> /dev/null)
    set -f
    # shellcheck disable=SC2086
    set -- $_if
    set +f
    _prev=""
    for _w; do
        case $_w in
        addr:*) [ "$_prev" = inet ] && IP=${_w#addr:} ;;
        Mask:*) NETMASK=${_w#Mask:} ;;
        esac
        _prev=$_w
    done
}

# hex_ip HEX: a little-endian IPv4 address in 8 hexadecimal digits (as /proc/net/route has them),
# dotted, in HI. False when it isn't one.
hex_ip() {
    case $1 in [0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f]) ;; *) return 1 ;; esac
    _h=${1#??}
    _b3=${_h%????}
    _h=${1#????}
    _b2=${_h%??}
    HI=$((0x${1#??????})).$((0x$_b2)).$((0x$_b3)).$((0x${1%??????}))
    return 0
}

# read_network: read_address's values, and GATEWAY, DNS, ADDRESS_MODE, WIFI_SSID, WIFI_SIGNAL and
# MAC.
read_network() {
    read_address
    GATEWAY=""
    if [ -r /proc/net/route ]; then
        while read -r _a _dst _gw _rest; do
            if [ "$_dst" = 00000000 ] && hex_ip "$_gw"; then
                GATEWAY=$HI
                break
            fi
        done < /proc/net/route
    fi
    DNS=""
    if [ -r /etc/resolv.conf ]; then
        while read -r _a _b _rest || [ -n "$_a" ]; do
            [ "$_a" = nameserver ] && { DNS=$_b; break; }
        done < /etc/resolv.conf
    fi
    ADDRESS_MODE=dhcp
    if [ -r /usr/etc/conf/network.conf ]; then
        while IFS= read -r _l || [ -n "$_l" ]; do
            case $_l in
            IP=* | IP)
                _v=${_l#IP}
                _v=${_v#=}
                _v=${_v%%=*}
                case $_v in *[!"\"' $CR"]*) ADDRESS_MODE=fixed ;; esac
                break
                ;;
            esac
        done < /usr/etc/conf/network.conf
    fi
    _s=$(timeout 5 iwgetid -r wlan0 2> /dev/null)
    WIFI_SSID=${_s%%"$NL"*}
    WIFI_SIGNAL=""
    if [ -r /proc/net/wireless ]; then
        while read -r _a _b _c _rest; do
            [ "$_a" = wlan0: ] && { WIFI_SIGNAL=${_c%.}; break; }
        done < /proc/net/wireless
    fi
    first_line /sys/class/net/wlan0/address
    MAC=$FL
}

# ssh_enabled: true when authorized_keys holds a key (sshd takes logins only then).
ssh_enabled() {
    [ -f /usr/karotz/.ssh/authorized_keys ] && [ -r /usr/karotz/.ssh/authorized_keys ] || return 1
    while IFS= read -r _l || [ -n "$_l" ]; do
        _l=${_l#"${_l%%[![:space:]]*}"}
        case $_l in [a-z]*) return 0 ;; esac
    done < /usr/karotz/.ssh/authorized_keys
    return 1
}

# telnet_on: true when the install turned telnet on.
telnet_on() {
    first_line /usr/etc/conf/telnet
    [ "$FL" = yes ]
}

# lapinlibre_status: LapinLibre's own status as JSON members (without "return"), in JO.
lapinlibre_status() {
    read_build
    read_network
    read_controller
    read_programs
    jo_begin
    jo_str build "$BUILD"
    jo_str build_id "$BUILD_ID"
    jo_str hostname "$HOSTNAME_"
    jo_str ip "$IP"
    jo_str netmask "$NETMASK"
    jo_str gateway "$GATEWAY"
    jo_str dns "$DNS"
    jo_str address_mode "$ADDRESS_MODE"
    jo_str wifi_ssid "$WIFI_SSID"
    jo_str wifi_signal "$WIFI_SIGNAL"
    jo_str mac "$MAC"
    jo_str time "$(date +%Y-%m-%dT%H:%M:%S%z)"
    first_line /etc/TZ
    jo_str timezone "$FL"
    first_line /proc/uptime
    _up=${FL%%[. ]*}
    case $_up in '' | *[!0-9]*) _up=0 ;; esac
    jo_raw uptime "$_up"
    first_line /proc/loadavg
    set -f
    # shellcheck disable=SC2086
    set -- $FL
    set +f
    jo_str load "${1:-} ${2:-} ${3:-}"
    _total=0 _free=0
    if [ -r /proc/meminfo ]; then
        while read -r _a _b _rest; do
            case $_b in '' | *[!0-9]*) continue ;; esac
            case $_a in
            MemTotal:) _total=$_b ;;
            MemFree: | Buffers: | Cached:) _free=$((_free + _b)) ;;
            esac
        done < /proc/meminfo
    fi
    jo_raw mem_total_kb "$_total"
    jo_raw mem_free_kb "$_free"
    disk_usage /usr
    jo_raw usr_free_kb "${DU_FREE_KB:-0}"
    jo_raw usr_used_percent "${DU_USED_PERCENT:-0}"
    if usb_mounted; then
        jo_str usb_mounted 1
        disk_usage /mnt/usbkey
        jo_raw usb_free_kb "${DU_FREE_KB:-0}"
        jo_raw usb_used_percent "${DU_USED_PERCENT:-0}"
    else
        jo_str usb_mounted 0
    fi
    ini_get /usr/etc/conf/karotz.conf karotz lang
    _lang=$IV
    jo_str language "$_lang"
    ini_get /usr/etc/conf/karotz.conf karotz ip_server
    jo_str app_server "$IV"
    _l2=${_lang%"${_lang#??}"}
    IV=""
    case $_l2 in [a-z][a-z]) ini_get /usr/etc/conf/voice.conf tts "$_l2" ;; esac
    jo_str speech_server "$IV"
    jo_str controller "$CONTROLLER"
    learning_left
    jo_raw learning_left "$LL_LEFT"
    _outer=$JO
    jo_begin
    for _d in led rfid webcam button ears multimedia voice scheduler serial k2k; do
        running "$_d-daemon" && jo_str "$_d" 1 || jo_str "$_d" 0
    done
    for _d in controller mpd mdnsd; do
        running "$_d" && jo_str "$_d" 1 || jo_str "$_d" 0
    done
    _daemons="{$JO}"
    JO=$_outer
    jo_raw daemons "$_daemons"
    ssh_enabled && jo_str ssh 1 || jo_str ssh 0
    telnet_on && jo_str telnet 1 || jo_str telnet 0
    if [ -s "$WEB_HASH" ]; then jo_str web_user "$WEB_USER"; else jo_str web_user ""; fi
}
