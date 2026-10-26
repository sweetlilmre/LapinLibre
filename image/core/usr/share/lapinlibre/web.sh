# SPDX-License-Identifier: MIT
# The web API's shared request handling, sourced by every program under /usr/www/cgi-bin. It holds
# the checks every request passes before anything is done (the web user, a local-network client,
# the cross-site rule for state-changing endpoints, the method, the sizes), the parameters (decoded
# once, the first value of each name kept), the replies (JSON or text, always as text/plain with
# no caching and no type sniffing), the log lines, the rabbit's state that the hardware endpoints
# check, and the calls to the daemons. It lives outside the web root, so httpd never runs it.
#
# The functions only some endpoints use live beside it in web-<area>.sh: extra (decoding encoded
# parameters, JSON escaping, the URL and UTF-8 checks, a daemon's failure), info (the status's
# readers), media (sounds, voices and the stick's lists), hw (signal watches, the ears' reset and
# the camera) and tags (URLs, the tag store, running endpoints here). Each is read the first time
# one of its functions is called (web_area), so a request only parses what it needs. The settings
# are here, but for the awk programs, which live with the functions that run them.
#
# An endpoint starts with
#     . /usr/share/lapinlibre/web.sh
#     web_begin read       (or: web_begin change, for an endpoint that changes anything)
# and then reads its parameters with param, param_int, param_choice, param_flag or param_color,
# and answers with reply_json, reply_text_begin, reply_file or web_fail. An endpoint that drives
# the rabbit then calls web_hardware (the controller's and the sleep's rules, and the lock) and
# talks to the daemons with dbus_call or dbus_do. No input is ever evaluated: values are only
# expanded inside double quotes, passed to commands as single arguments, and never used as a
# printf format.
#
# Every program started costs the rabbit a tenth of a second or more, so the common paths use the
# shell's own builtins (read, case, parameter expansion, printf, arithmetic) wherever they can.

PATH=/bin:/sbin:/usr/bin:/usr/sbin
export PATH
LC_ALL=C
export LC_ALL
umask 077

WEB_USER=admin
# The web password's hash: with it, httpd asks every request for the web user and its password;
# without it there is no login.
WEB_HASH=/usr/etc/conf/web_password.hash
WEB_TAG=lapinlibre-web
# The library's folder, holding the area files.
WEB_LIB=/usr/share/lapinlibre
# The API's own state: in RAM (lost on reboot), and the data it keeps on the flash.
WEB_RUN=/var/run/lapinlibre-web
WEB_DATA=/usr/etc/conf/web
# The controller's state as the tag listener follows it (its number, or empty while the controller
# isn't on the bus); no file while the listener doesn't know it.
WEB_CONTROLLER=$WEB_RUN/controller
# The longest query string and request body taken, in bytes.
WEB_MAX=4096
# Parameters whose tabs and line ends become spaces instead of refusing the value.
WEB_SPACE_PARAMS=""
WEB_KIND=read
WEB_HEAD=0
WEB_PARAMS=""
# The API's programs, which a tag's action and the learning timer run here.
WEB_CGI=/usr/www/cgi-bin
# 1 when a program only checks its parameters (WEB_CHECK_ONLY=1, set by api_run's check): it
# reads no daemon, logs nothing, and answers success where it would start to act.
WEB_CHECK=0
[ "${WEB_CHECK_ONLY:-}" = 1 ] && WEB_CHECK=1
# The area files read so far, and the success line logged once the reply is out.
WEB_AREAS=""
WEB_LAST=""
NL='
'
CR=$'\r'
[ ${#CR} = 1 ] || CR=$(printf '\r')
TAB=$'\t'
[ ${#TAB} = 1 ] || TAB=$(printf '\t')

# --- replies ---

# web_headers STATUS [CONTENT-TYPE [CACHE-CONTROL]]: the CGI header block. Every reply carries
# the same caching (no-store unless given) and sniffing headers, and never a cross-origin one.
web_headers() {
    case $1 in
    200) ;;
    400) printf 'Status: 400 Bad Request\r\n' ;;
    403) printf 'Status: 403 Forbidden\r\n' ;;
    404) printf 'Status: 404 Not Found\r\n' ;;
    405) printf 'Status: 405 Method Not Allowed\r\nAllow: GET, HEAD, POST\r\n' ;;
    413) printf 'Status: 413 Payload Too Large\r\n' ;;
    414) printf 'Status: 414 URI Too Long\r\n' ;;
    *) printf 'Status: 500 Internal Server Error\r\n' ;;
    esac
    printf 'Content-Type: %s\r\nCache-Control: %s\r\nX-Content-Type-Options: nosniff\r\n\r\n' "${2:-text/plain}" \
        "${3:-no-store}"
}

# reply_json BODY [STATUS]: a JSON reply (as text/plain), then the end of the program. HEAD gets
# the headers only.
reply_json() {
    web_headers "${2:-200}" text/plain
    [ "$WEB_HEAD" = 1 ] || printf '%s\n' "$1"
    exit 0
}

# reply_text_begin: the headers of a plain-text reply; the caller writes the body unless
# WEB_HEAD is 1.
reply_text_begin() {
    web_headers 200 text/plain
}

# reply_file FILE TYPE [CACHE-CONTROL]: the file as the reply's body, then the end of the program.
reply_file() {
    web_headers 200 "$2" "${3:-no-store}"
    [ "$WEB_HEAD" = 1 ] || cat "$1"
    exit 0
}

# web_error_body MESSAGE: the error shape, in WEB_BODY.
web_error_body() {
    json_str "$1"
    WEB_BODY="{\"return\":\"1\",\"msg\":$J}"
}

# web_fail MESSAGE: an API-level error (HTTP 200, the error shape), logged when the endpoint
# changes state; then the end of the program.
web_fail() {
    [ "$WEB_KIND" = change ] && web_log "$1"
    web_error_body "$1"
    reply_json "$WEB_BODY"
}

# web_refuse STATUS MESSAGE: a policy refusal (an HTTP error status with the error shape),
# always logged; then the end of the program.
web_refuse() {
    web_log "refused: $2"
    web_error_body "$2"
    reply_json "$WEB_BODY" "$1"
}

# web_log TEXT: one syslog line: the endpoint, the client's address and the text. TEXT never
# holds a parameter's value.
web_log() {
    [ "$WEB_CHECK" = 1 ] && return 0
    logger -t "$WEB_TAG" "${WEB_ENDPOINT:-?} ${WEB_CLIENT:-?} $1" 2> /dev/null
}

# web_ok: logs the success of a state-changing request, once the reply is out (web_cleanup).
web_ok() {
    WEB_LAST=ok
}

# --- JSON ---

# json_str VALUE: VALUE as a JSON string literal, in J. Printable ASCII without a quote or a
# backslash needs no other process.
json_str() {
    case $1 in
    *[![:print:]]* | *[\"\\]*) json_escape "$1" ;;
    *) J="\"$1\"" ;;
    esac
}

# A JSON object built member by member: jo_begin, then jo_str KEY VALUE (a string), jo_raw KEY
# JSON (a number, an object or a list), then "{$JO}".
jo_begin() {
    JO=""
}
jo_str() {
    json_str "$2"
    JO="$JO${JO:+,}\"$1\":$J"
}
jo_raw() {
    JO="$JO${JO:+,}\"$1\":$2"
}

# --- small helpers ---

# nozeros DIGITS: the number without leading zeros (the shell's arithmetic would read them as
# octal), in NZ.
nozeros() {
    NZ=$1
    while :; do
        case $NZ in 0?*) NZ=${NZ#0} ;; *) break ;; esac
    done
}

# strip_zeros DIGITS: nozeros, printed.
strip_zeros() {
    nozeros "$1"
    printf '%s' "$NZ"
}

# lower VALUE: VALUE in lower case, in LO.
lower() {
    case $1 in *[A-Z]*) ;; *) LO=$1; return 0 ;; esac
    LO=""
    _lw_s=$1
    while [ -n "$_lw_s" ]; do
        _lw_r=${_lw_s#?}
        _lw_c=${_lw_s%"$_lw_r"}
        _lw_s=$_lw_r
        case $_lw_c in
        A) _lw_c=a ;; B) _lw_c=b ;; C) _lw_c=c ;; D) _lw_c=d ;; E) _lw_c=e ;; F) _lw_c=f ;; G) _lw_c=g ;; H) _lw_c=h ;; I) _lw_c=i ;;
        J) _lw_c=j ;; K) _lw_c=k ;; L) _lw_c=l ;; M) _lw_c=m ;; N) _lw_c=n ;; O) _lw_c=o ;; P) _lw_c=p ;; Q) _lw_c=q ;; R) _lw_c=r ;;
        S) _lw_c=s ;; T) _lw_c=t ;; U) _lw_c=u ;; V) _lw_c=v ;; W) _lw_c=w ;; X) _lw_c=x ;; Y) _lw_c=y ;; Z) _lw_c=z ;;
        esac
        LO=$LO$_lw_c
    done
}

# upper_hex VALUE: VALUE with its letters a to f in upper case, in UP.
upper_hex() {
    case $1 in *[a-f]*) ;; *) UP=$1; return 0 ;; esac
    UP=""
    _lw_s=$1
    while [ -n "$_lw_s" ]; do
        _lw_r=${_lw_s#?}
        _lw_c=${_lw_s%"$_lw_r"}
        _lw_s=$_lw_r
        case $_lw_c in a) _lw_c=A ;; b) _lw_c=B ;; c) _lw_c=C ;; d) _lw_c=D ;; e) _lw_c=E ;; f) _lw_c=F ;; esac
        UP=$UP$_lw_c
    done
}

# first_line FILE: the file's first line (without a carriage return), in FL; empty when there is
# none.
first_line() {
    FL=""
    [ -f "$1" ] && [ -r "$1" ] || return 1
    IFS= read -r FL < "$1" || [ -n "$FL" ]
    FL=${FL%"$CR"}
    return 0
}

# --- the checks of every request ---

# lan_address ADDRESS: true for a dotted IPv4 address in the ranges the web server serves:
# 127.0.0.1, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16 and 169.254.0.0/16.
lan_address() {
    case $1 in '' | *[!0-9.]* | .* | *. | *..*) return 1 ;; esac
    _ifs=$IFS
    IFS=.
    set -f
    # shellcheck disable=SC2086
    set -- $1
    set +f
    IFS=$_ifs
    [ $# -eq 4 ] || return 1
    for _o; do
        [ ${#_o} -le 3 ] && [ "$_o" -le 255 ] || return 1
    done
    nozeros "$1"
    _a=$NZ
    nozeros "$2"
    _b=$NZ
    if [ "$_a" = 127 ]; then
        nozeros "$3"
        _c=$NZ
        nozeros "$4"
        [ "$_b.$_c.$NZ" = 0.0.1 ]
        return
    fi
    [ "$_a" -eq 10 ] && return 0
    [ "$_a" -eq 172 ] && [ "$_b" -ge 16 ] && [ "$_b" -le 31 ] && return 0
    [ "$_a" -eq 192 ] && [ "$_b" -eq 168 ] && return 0
    [ "$_a" -eq 169 ] && [ "$_b" -eq 254 ] && return 0
    return 1
}

# url_hostport URL: the host:port of an http(s) URL (the default port added), lower case, in HP.
# False for anything else.
url_hostport() {
    case $1 in
    http://*) _r=${1#http://} _p=80 ;;
    https://*) _r=${1#https://} _p=443 ;;
    *) return 1 ;;
    esac
    _r=${_r%%/*}
    _r=${_r%%\?*}
    _r=${_r%%#*}
    _r=${_r##*@}
    [ -n "$_r" ] || return 1
    case $_r in *:*) ;; *) _r=$_r:$_p ;; esac
    lower "$_r"
    HP=$LO
}

# same_origin: the cross-site rule. A browser's Sec-Fetch-Site must say same-origin or none, and
# an Origin or Referer it sends must name this server's own host and port (the Host header).
# Clients that send none of these (scripts, home-automation boxes) pass.
same_origin() {
    case ${HTTP_HOST:-} in
    '') _host="" ;;
    *:*) lower "$HTTP_HOST"; _host=$LO ;;
    *) lower "$HTTP_HOST:80"; _host=$LO ;;
    esac
    if [ -n "${HTTP_SEC_FETCH_SITE+set}" ]; then
        case $HTTP_SEC_FETCH_SITE in same-origin | none) ;; *) return 1 ;; esac
    fi
    if [ -n "${HTTP_ORIGIN+set}" ]; then
        url_hostport "$HTTP_ORIGIN" || return 1
        [ -n "$_host" ] && [ "$HP" = "$_host" ] || return 1
    fi
    if [ -n "${HTTP_REFERER+set}" ]; then
        url_hostport "$HTTP_REFERER" || return 1
        [ -n "$_host" ] && [ "$HP" = "$_host" ] || return 1
    fi
    return 0
}

# web_begin read|change [NAMES]: the checks of every request, in order, then its parameters. A
# failed check refuses the request (and ends the program) before anything is done. NAMES are the
# parameters (space-separated) whose tabs and line ends become spaces.
web_begin() {
    WEB_KIND=$1
    [ $# -gt 1 ] && WEB_SPACE_PARAMS=$2
    WEB_ENDPOINT=${SCRIPT_NAME#/cgi-bin/}
    case $WEB_ENDPOINT in '' | *[!A-Za-z0-9/_.-]*) WEB_ENDPOINT=? ;; esac
    WEB_CLIENT=${REMOTE_ADDR:-}
    case $WEB_CLIENT in '' | *[!0-9.]*) WEB_CLIENT=? ;; esac
    [ "${REQUEST_METHOD:-}" = HEAD ] && WEB_HEAD=1
    # 1. With a web password, the web user, set by httpd after it has checked the password.
    if [ -s "$WEB_HASH" ] && [ "${REMOTE_USER:-}" != "$WEB_USER" ]; then
        web_refuse 403 "the request isn't authenticated as the web user"
    fi
    # 2. A client on the local network.
    lan_address "$WEB_CLIENT" || web_refuse 403 "requests are only taken from the local network"
    # 3. No cross-site request may change anything.
    if [ "$WEB_KIND" = change ] && ! same_origin; then
        web_refuse 403 "a cross-site request was refused"
    fi
    # 4. The methods.
    case ${REQUEST_METHOD:-} in GET | HEAD | POST) ;; *) web_refuse 405 "only GET, HEAD and POST are accepted" ;; esac
    web_params
}

# web_params: checks the sizes and decodes the query string and a form body into WEB_PARAMS.
web_params() {
    _q=${QUERY_STRING:-}
    # The shell counts characters, never more than the bytes: count the bytes only when it matters.
    if [ ${#_q} -gt $WEB_MAX ] || { [ ${#_q} -gt 1024 ] && [ "$(printf '%s' "$_q" | wc -c)" -gt $WEB_MAX ]; }; then
        web_refuse 414 "the query string is longer than $WEB_MAX bytes"
    fi
    _body=""
    if [ "$REQUEST_METHOD" = POST ] && [ -n "${CONTENT_LENGTH:-}" ]; then
        case $CONTENT_LENGTH in *[!0-9]*) web_refuse 400 "the request body's length is not a number" ;; esac
        nozeros "$CONTENT_LENGTH"
        _len=$NZ
        [ ${#_len} -le 6 ] && [ "$_len" -le $WEB_MAX ] || web_refuse 413 "the request body is longer than $WEB_MAX bytes"
        if [ "$_len" -gt 0 ]; then
            case ${CONTENT_TYPE:-} in
            application/x-www-form-urlencoded | "application/x-www-form-urlencoded;"*) ;;
            *) web_fail "only a form-encoded request body is accepted" ;;
            esac
            read_body "$_len"
        fi
    fi
    _all="$_q&$_body"
    case $_all in
    *[!A-Za-z0-9_.=\&:,/@~-]*) decode_params "$_all" ;;
    *) plain_params "$_all" ;;
    esac
}

# read_body LENGTH: at most LENGTH bytes of the request body, in _body, as a command substitution
# would give them (NUL bytes and the line ends at the end left out).
read_body() {
    _body=""
    _left=$1
    while [ "$_left" -gt 0 ]; do
        _part=""
        IFS= read -r -n "$_left" _part
        _st=$?
        _body=$_body$_part
        _left=$((_left - ${#_part}))
        # Fewer bytes than asked and no end of input: read stopped at a line end, which it took.
        if [ $_st != 0 ]; then
            break
        elif [ "$_left" -gt 0 ]; then
            _body=$_body$NL
            _left=$((_left - 1))
        fi
    done
    while :; do
        case $_body in *"$NL") _body=${_body%"$NL"} ;; *) break ;; esac
    done
}

# plain_params QUERY: decode_params' work for a query of characters that need no decoding
# (letters, digits and _ . = & : , / @ ~ -), done here.
plain_params() {
    WEB_PARAMS=""
    _seen=" "
    _ifs=$IFS
    IFS='&'
    set -f
    # shellcheck disable=SC2086
    set -- $1
    set +f
    IFS=$_ifs
    for _pair; do
        [ -n "$_pair" ] || continue
        case $_pair in
        *=*) _name=${_pair%%=*} _raw=${_pair#*=} ;;
        *) _name=$_pair _raw="" ;;
        esac
        case $_name in '' | *[!a-z0-9_]*) continue ;; esac
        [ ${#_name} -le 32 ] || continue
        case $_seen in *" $_name "*) continue ;; esac
        _seen="$_seen$_name "
        WEB_PARAMS="$WEB_PARAMS${WEB_PARAMS:+$NL}$_name=$_raw"
    done
}

# --- parameters ---

# param NAME: the parameter's decoded value, in PV. Status 0 when given, 1 when absent, 2 when
# its value can't be taken (a control character or a broken %-escape).
param() {
    PV=""
    _all="$NL$WEB_PARAMS$NL"
    case $_all in
    *"$NL!$1$NL"*) return 2 ;;
    *"$NL$1="*)
        _v=${_all#*"$NL$1="}
        PV=${_v%%"$NL"*}
        return 0
        ;;
    esac
    return 1
}

# param_int NAME MIN MAX DEFAULT: a decimal whole number from MIN to MAX, in PV; DEFAULT when
# absent or empty. Anything else fails the request.
param_int() {
    param "$1"
    case $? in
    1) PV=$4; return 0 ;;
    2) web_fail "parameter $1 must be a whole number from $2 to $3" ;;
    esac
    [ -n "$PV" ] || { PV=$4; return 0; }
    case $PV in *[!0-9]*) web_fail "parameter $1 must be a whole number from $2 to $3" ;; esac
    nozeros "$PV"
    PV=$NZ
    [ ${#PV} -le 9 ] && [ "$PV" -ge "$2" ] && [ "$PV" -le "$3" ] || web_fail "parameter $1 must be a whole number from $2 to $3"
}

# param_choice NAME DEFAULT WORD...: one of the words (case-insensitive), in PV, in lower case;
# DEFAULT when absent or empty. Anything else fails the request.
param_choice() {
    _name=$1
    _default=$2
    shift 2
    param "$_name"
    case $? in
    1) PV=$_default; return 0 ;;
    2) web_fail "parameter $_name must be one of: $*" ;;
    esac
    [ -n "$PV" ] || { PV=$_default; return 0; }
    lower "$PV"
    for _w; do
        [ "$LO" = "$_w" ] && { PV=$LO; return 0; }
    done
    web_fail "parameter $_name must be one of: $*"
}

# valid_id VALUE: a sound, mood or story name ([A-Za-z0-9_-]{1,64}).
valid_id() {
    case $1 in '' | *[!A-Za-z0-9_-]*) return 1 ;; esac
    [ ${#1} -le 64 ]
}

# is_color VALUE: six upper-case hexadecimal digits.
is_color() {
    case $1 in [0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F]) return 0 ;; esac
    return 1
}

# param_flag NAME DEFAULT: 0 or 1, in PV; DEFAULT when absent or empty. Anything else fails the
# request.
param_flag() {
    param "$1"
    case $? in
    1) PV=$2; return 0 ;;
    2) web_fail "parameter $1 must be 0 or 1" ;;
    esac
    case $PV in
    '') PV=$2 ;;
    0 | 1) ;;
    *) web_fail "parameter $1 must be 0 or 1" ;;
    esac
}

# param_color NAME DEFAULT: a colour (six hexadecimal digits, optionally after one #), in PV, in
# upper case without the #; DEFAULT when absent or empty. Anything else fails the request.
param_color() {
    param "$1"
    case $? in
    1) PV=$2; return 0 ;;
    2) web_fail "parameter $1 must be a colour: six hexadecimal digits (RRGGBB)" ;;
    esac
    [ -n "$PV" ] || { PV=$2; return 0; }
    PV=${PV#\#}
    case $PV in
    [0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f]) ;;
    *) web_fail "parameter $1 must be a colour: six hexadecimal digits (RRGGBB)" ;;
    esac
    upper_hex "$PV"
    PV=$UP
}

# --- the rabbit's state ---

# The build: BUILD (the version the build stamped), BUILD_ID (the installed build's id) and
# VERSION (both, as the install logs them).
read_build() {
    first_line /karotz/etc/lapinlibre-build
    BUILD=$FL
    first_line /usr/etc/conf/lapinlibre_build_id
    BUILD_ID=$FL
    if [ -n "$BUILD_ID" ] && [ -n "$BUILD" ]; then
        VERSION="$BUILD_ID ($BUILD)"
    else
        VERSION=$BUILD_ID$BUILD
    fi
}

# usb_mounted: true when the stick is mounted at /mnt/usbkey (looked up once per program).
usb_mounted() {
    if [ -z "${WEB_USB:-}" ]; then
        WEB_USB=0
        if [ -r /proc/mounts ]; then
            while read -r _dev _mp _rest; do
                [ "$_mp" = /mnt/usbkey ] && { WEB_USB=1; break; }
            done < /proc/mounts
        fi
    fi
    [ "$WEB_USB" = 1 ]
}

# The controller's state: CONTROLLER_STATE (its number, empty when it isn't on the bus or
# didn't answer) and CONTROLLER (its name, or "not running"). Read from the tag listener's file
# when it has one, else asked on the bus (controller_ask 1).
read_controller() {
    CONTROLLER_STATE=""
    if [ "$WEB_CHECK" = 1 ]; then
        CONTROLLER="not running"
        return 0
    fi
    if first_line "$WEB_CONTROLLER"; then
        case $FL in *[!0-9]*) ;; *) CONTROLLER_STATE=$FL ;; esac
    else
        controller_ask 1
    fi
    controller_name
}

# controller_ask SECONDS: the controller's state asked on the bus, waiting at most SECONDS for
# its answer, in CONTROLLER_STATE (empty without one).
controller_ask() {
    CONTROLLER_STATE=""
    _out=$(timeout $(($1 + 1)) dbus-send --system --print-reply --reply-timeout=$(($1 * 1000)) \
        --dest=com.mindscape.karotz.Controller /com/mindscape/karotz/Controller \
        com.mindscape.karotz.KarotzInterface.get_status 2> /dev/null 9>&-) || _out=""
    set -f
    # shellcheck disable=SC2086
    set -- $_out
    set +f
    while [ $# -gt 1 ]; do
        if [ "$1" = int32 ]; then
            case $2 in '' | *[!0-9]*) ;; *) CONTROLLER_STATE=$2 ;; esac
            break
        fi
        shift
    done
}

# controller_name: CONTROLLER, the name of CONTROLLER_STATE.
controller_name() {
    case $CONTROLLER_STATE in
    '') CONTROLLER="not running" ;;
    14) CONTROLLER=connecting ;;
    15) CONTROLLER=connected ;;
    16) CONTROLLER=available ;;
    19) CONTROLLER=updating ;;
    20) CONTROLLER="updating configuration" ;;
    21) CONTROLLER=interactive ;;
    22) CONTROLLER="leaving interactive mode" ;;
    23) CONTROLLER=disconnected ;;
    24) CONTROLLER=asleep ;;
    25) CONTROLLER="waking up" ;;
    *) CONTROLLER="state $CONTROLLER_STATE" ;;
    esac
}

# The API's own state, kept in RAM: SLEEP (1 or 0, the controller asleep counts), SLEEP_TIME, the
# LED it last set (LED_COLOR, LED_PULSE, and for a pulse LED_COLOR2 and LED_SPEED), and
# EARS_DISABLED, kept with its data. Needs read_controller first.
read_api_state() {
    SLEEP=0 SLEEP_TIME=0
    first_line "$WEB_RUN/sleep"
    case $FL in '' | *[!0-9]*) ;; *) SLEEP=1 SLEEP_TIME=$FL ;; esac
    [ "$CONTROLLER_STATE" = 24 ] && SLEEP=1
    LED_COLOR=00FF00 LED_PULSE=0 LED_COLOR2=000000 LED_SPEED=700
    first_line "$WEB_RUN/led"
    set -f
    # shellcheck disable=SC2086
    set -- $FL
    set +f
    is_color "${1:-}" && LED_COLOR=$1
    [ "${2:-}" = 1 ] && LED_PULSE=1
    is_color "${3:-}" && LED_COLOR2=$3
    case ${4:-} in 2[5-9] | [3-9][0-9] | [1-9][0-9][0-9] | [1-9][0-9][0-9][0-9] | 10000) LED_SPEED=$4 ;; esac
    EARS_DISABLED=0
    ears_disabled && EARS_DISABLED=1
    # A check takes the rabbit as awake.
    [ "$WEB_CHECK" = 1 ] && SLEEP=0 SLEEP_TIME=0
    return 0
}

# ears_disabled: true when ears_mode has turned the API's ear moves off (never for a check).
ears_disabled() {
    [ "$WEB_CHECK" != 1 ] && [ -e "$WEB_DATA/ears_disabled" ]
}

# The programs running, by the name of the file each runs (the kernel's name for the process, at
# most 15 characters), in PROGRAMS, one per line.
read_programs() {
    PROGRAMS=$NL$(ps -o comm 2> /dev/null)
}

# running PROGRAM: true when PROGRAMS lists it (by its first 15 characters, as the kernel keeps
# them).
running() {
    _rn_name=$1
    if [ ${#_rn_name} -gt 15 ]; then
        _rn_rest=${_rn_name#???????????????}
        _rn_name=${_rn_name%"$_rn_rest"}
    fi
    case "$PROGRAMS$NL" in *"$NL$_rn_name$NL"*) return 0 ;; esac
    return 1
}

# --- the user's own files on the stick, the sounds and the voices ---

WEB_MOODS=/mnt/usbkey/moods
WEB_STORIES=/mnt/usbkey/stories
WEB_RADIOS=/mnt/usbkey/radios.txt
# The most stations radios_list gives, and the most of a list file read.
WEB_RADIOS_MAX=500
WEB_LIST_BYTES=65536
# The languages of the voice numbers: two numbers per language, in order from 1 (the odd one male,
# the even one female).
VOICE_LANGS="fr fr-CA en-US en-GB de it es nl af sq ar hy bs pt-BR hr cs da en-AU eo fi el ht hi hu is id ja ko
la no pl pt-PT ro ru sr sh sk sw sv ta th tr vi cy"

# --- the hardware: our daemons on the system bus ---

# The lock every endpoint that drives a daemon holds while it starts its action.
WEB_LOCK=/var/lock/lapinlibre-web.lock
# The one speed of the API's ear moves (the motors' PWM, 1 to 255), and the ears' position while
# the rabbit sleeps (the controller's own).
WEB_EAR_SPEED=100
WEB_EARS_ASLEEP=4
# The camera's picture file, and the API's stored snapshots (the newest WEB_SNAPSHOT_MAX).
WEB_PICTURE=/tmp/picture.jpg
WEB_SNAPSHOTS=$WEB_RUN/snapshots
WEB_SNAPSHOT_MAX=10
# The flag that says the start-up's own LED tool was looked for since the boot (in RAM).
WEB_LED_CHECKED=/var/run/lapinlibre-web.led-checked
WEB_MONITOR=""
WEB_MONITOR_OUT=""
WEB_ID=""
WEB_CORR=""
# A temporary file of the program's own, removed when it ends.
WEB_TEMP=""

# web_cleanup: stops a signal watch and removes the temporary files, however the program ends;
# then logs the success line. The reply is complete once the program's output is closed, so the
# client doesn't wait for the log.
web_cleanup() {
    signal_stop
    [ -n "$WEB_TEMP" ] && rm -f "$WEB_TEMP"
    if [ -n "$WEB_LAST" ]; then
        [ -n "${WEB_LOCK_WAITED:-}" ] && flock -u 9 2> /dev/null
        exec > /dev/null 2>&1 9>&-
        web_log "$WEB_LAST"
    fi
    return 0
}
trap web_cleanup EXIT

# signal_stop: ends a signal watch (signal_watch).
signal_stop() {
    [ -n "$WEB_MONITOR" ] && kill "$WEB_MONITOR" 2> /dev/null
    [ -n "$WEB_MONITOR_OUT" ] && rm -f "$WEB_MONITOR_OUT"
    WEB_MONITOR="" WEB_MONITOR_OUT=""
    return 0
}

# hex4 NUMBER: the number's lowest 16 bits as 4 hexadecimal digits, added to WEB_CORR.
hex4() {
    for _hx_s in 12 8 4 0; do
        _hx_d=$((($1 >> _hx_s) & 15))
        case $_hx_d in 10) _hx_d=a ;; 11) _hx_d=b ;; 12) _hx_d=c ;; 13) _hx_d=d ;; 14) _hx_d=e ;; 15) _hx_d=f ;; esac
        WEB_CORR=$WEB_CORR$_hx_d
    done
}

# new_id: a fresh identifier for the API's own D-Bus calls, "webapi:" and 16 hexadecimal digits,
# in WEB_ID; the part after the colon in WEB_CORR (webcam-daemon's events carry that part). It
# comes from the shell's own random numbers (seeded per program) and the process id, else from
# /dev/urandom.
new_id() {
    WEB_CORR=""
    if [ -n "${RANDOM:-}" ]; then
        hex4 "$RANDOM"
        hex4 "$RANDOM"
        hex4 "$RANDOM"
        hex4 "$(($$ ^ RANDOM))"
    fi
    case $WEB_CORR in
    ????????????????) ;;
    *)
        WEB_CORR=$(od -An -N8 -tx1 /dev/urandom 2> /dev/null | tr -cd '0-9a-f')
        case $WEB_CORR in ????????????????) ;; *) WEB_CORR=$(date +%s)$$ ;; esac
        ;;
    esac
    WEB_ID=webapi:$WEB_CORR
}

# random_byte: a random number from 0 to 255, in RB.
random_byte() {
    if [ -n "${RANDOM:-}" ]; then
        RB=$((RANDOM & 255))
        return 0
    fi
    RB=$(od -An -N1 -tu1 /dev/urandom 2> /dev/null | tr -cd '0-9')
    [ -n "$RB" ] || RB=$$
}

# dbus_call NAME METHOD ARG...: calls a daemon's method (bus name com.mindscape.karotz.NAME),
# waiting at most 5 s for its answer. Status 0 when it answered TRUE, 1 FALSE, 2 when the daemon
# isn't on the bus, 3 when it didn't answer.
dbus_call() {
    _dn=$1 _dm=$2
    shift 2
    # timeout's own watcher outlives the call by up to a second: it never gets the lock's
    # descriptor, so the lock goes as soon as the program lets go of it.
    _out=$(timeout 7 dbus-send --system --print-reply --reply-timeout=5000 --dest="com.mindscape.karotz.$_dn" \
        "/com/mindscape/karotz/$_dn" "com.mindscape.karotz.KarotzInterface.$_dm" "$@" 2>&1 9>&-)
    case $_out in
    *"boolean true"*) return 0 ;;
    *"boolean false"*) return 1 ;;
    *ServiceUnknown* | *NameHasNoOwner*) return 2 ;;
    esac
    return 3
}

# dbus_do NAME MESSAGE METHOD ARG...: dbus_call, failing the request unless the daemon answered
# TRUE (MESSAGE when it answered FALSE).
dbus_do() {
    _dd=$1 _dmsg=$2
    shift 2
    dbus_call "$_dd" "$@" || dbus_failed "$_dd" $? "$_dmsg"
}

# web_lock: the API's one hardware lock, waited for at most 5 s; else the busy error. It is
# released when the program ends, or by web_unlock.
web_lock() {
    # A check ends here, before anything is done.
    [ "$WEB_CHECK" = 1 ] && reply_json '{"return":"0"}'
    [ -d "${WEB_LOCK%/*}" ] || mkdir -p "${WEB_LOCK%/*}" 2> /dev/null
    exec 9>> "$WEB_LOCK" || web_fail "the rabbit is busy: the API's lock can't be taken"
    # A free lock is taken at the first try; else it is waited for.
    WEB_LOCK_WAITED=""
    if ! flock -n -x 9 2> /dev/null; then
        WEB_LOCK_WAITED=1
        timeout 5 flock -x 9 || web_fail "the rabbit is busy with another request: try again"
    fi
}

# web_unlock: lets go of the lock by closing the program's descriptor of it; after a wait it is
# unlocked first, since timeout's watcher may still hold a copy of the descriptor.
web_unlock() {
    [ -n "${WEB_LOCK_WAITED:-}" ] && flock -u 9 2> /dev/null
    WEB_LOCK_WAITED=""
    exec 9>&-
}

# web_hardware [asleep]: the rules of an endpoint that drives the rabbit, then the lock and a
# fresh WEB_ID. Refused while an application owns the rabbit (the controller interactive, or
# leaving it), and while the rabbit sleeps unless "asleep" says the endpoint works then too.
# Leaves read_api_state's values.
web_hardware() {
    [ "$WEB_CHECK" = 1 ] && reply_json '{"return":"0"}'
    read_controller
    case $CONTROLLER_STATE in
    21 | 22) web_fail "the rabbit is busy: an application is running on it" ;;
    esac
    read_api_state
    if [ "${1:-}" != asleep ] && [ "$SLEEP" = 1 ]; then
        web_fail "the rabbit is asleep: wake it up first"
    fi
    web_lock
    new_id
}

# state_write NAME TEXT: one line of the API's state in RAM, replaced whole.
state_write() {
    [ -d "$WEB_RUN" ] || mkdir -p "$WEB_RUN" || return 1
    printf '%s\n' "$2" > "$WEB_RUN/$1"
}

# led_set COLOR PULSE [COLOR2 SPEED]: the LED fixed, or pulsing between the colours for ever. The
# start-up's own LED tool, if it still runs, is stopped first; once led-daemon has taken a
# setting it is never looked for again until the next boot (the start-up stops it itself once
# led-daemon runs). dbus_call's status.
led_set() {
    _ls_look=""
    if [ ! -e "$WEB_LED_CHECKED" ]; then
        _ls_look=1
        read_programs
        if running led; then
            killall led 2> /dev/null
        fi
    fi
    if [ "$2" = 1 ]; then
        dbus_call Led pulse "string:$WEB_ID" "string:$1" "string:$3" "int32:$4" int32:-1
    else
        dbus_call Led light "string:$WEB_ID" "string:$1"
    fi
    _ls_r=$?
    if [ $_ls_r = 0 ] && [ -n "$_ls_look" ]; then
        printf '' 2> /dev/null > "$WEB_LED_CHECKED"
    fi
    return $_ls_r
}

# led_restore: the LED as the API last set it, or off while the rabbit sleeps. Needs
# read_api_state.
led_restore() {
    if [ "$SLEEP" = 1 ]; then
        led_set 000000 0
    else
        led_set "$LED_COLOR" "$LED_PULSE" "$LED_COLOR2" "$LED_SPEED"
    fi
}

# --- RFID tags ---

# The endpoints a tag's action may run.
WEB_TAG_ENDPOINTS="tts sound sound_control leds ears ears_reset ears_random sleep wakeup snapshot apps/clock apps/moods stories"
# The tag store, on the flash with the API's data (0600: it holds URL actions' passwords), and
# its lock. One line per tag, tab-separated: id (16 upper-case hexadecimal digits), type, color
# (rfid-daemon's codes), cmd (0 none, 3 a URL request, 5 one of the API's endpoints), var (-1),
# name, then the action: for cmd 3 the URL, the user and the password; for cmd 5 the endpoint and
# its query string. Lines starting with # are comments.
WEB_TAGS=$WEB_DATA/tags
WEB_TAGS_LOCK=/var/lock/lapinlibre-tags.lock
# Tag learning: how long it lasts, the LED meanwhile, and the tones (played through
# multimedia-daemon, never while the rabbit sleeps).
WEB_LEARN_TIME=120
WEB_TONE_LEARN_START=/usr/karotz/sounds/button-long.wav
WEB_TONE_LEARN_STOP=/usr/karotz/sounds/button-click.wav
WEB_TONE_LEARNED=/usr/karotz/sounds/notify.mp3
WEB_TONE_KNOWN=/usr/karotz/sounds/button-triple.wav

# valid_tag VALUE: a tag's id, 16 hexadecimal digits, upper case in TG.
valid_tag() {
    case $1 in
    [0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f]) ;;
    *) return 1 ;;
    esac
    upper_hex "$1"
    TG=$UP
    return 0
}

# --- the areas ---

# web_area NAME: reads web-NAME.sh, whose functions replace the stand-ins below. False when it was
# read already or isn't there, so a stand-in never calls itself again.
web_area() {
    case " $WEB_AREAS " in *" $1 "*) return 1 ;; esac
    WEB_AREAS="$WEB_AREAS $1"
    [ -r "$WEB_LIB/web-$1.sh" ] || return 1
    . "$WEB_LIB/web-$1.sh"
}

# web_need NAME: web_area, for a program that uses an area's settings itself.
web_need() {
    web_area "$1" || :
}

# The less common helpers.
json_escape() { web_area extra && json_escape "$@"; }
decode_params() { web_area extra && decode_params "$@"; }
valid_url() { web_area extra && valid_url "$@"; }
valid_utf8() { web_area extra && valid_utf8 "$@"; }
daemon_name() { web_area extra && daemon_name "$@"; }
dbus_failed() { web_area extra && dbus_failed "$@"; }
# The status's readers.
xml_text() { web_area info && xml_text "$@"; }
ini_get() { web_area info && ini_get "$@"; }
disk_usage() { web_area info && disk_usage "$@"; }
count_lines() { web_area info && count_lines "$@"; }
read_counts() { web_area info && read_counts "$@"; }
read_address() { web_area info && read_address "$@"; }
read_network() { web_area info && read_network "$@"; }
ssh_enabled() { web_area info && ssh_enabled "$@"; }
telnet_on() { web_area info && telnet_on "$@"; }
lapinlibre_status() { web_area info && lapinlibre_status "$@"; }
# Sounds, voices and the stick's lists.
media_ids() { web_area media && media_ids "$@"; }
sound_dirs() { web_area media && sound_dirs "$@"; }
mp3_ids() { web_area media && mp3_ids "$@"; }
list_text() { web_area media && list_text "$@"; }
labelled_ids() { web_area media && labelled_ids "$@"; }
labelled_json() { web_area media && labelled_json "$@"; }
sound_path() { web_area media && sound_path "$@"; }
voice_of() { web_area media && voice_of "$@"; }
lang_name() { web_area media && lang_name "$@"; }
read_tts_servers() { web_area media && read_tts_servers "$@"; }
has_tts_server() { web_area media && has_tts_server "$@"; }
rabbit_lang() { web_area media && rabbit_lang "$@"; }
# Signal watches, the ears' reset and the camera.
signal_watch() { web_area hw && signal_watch "$@"; }
signal_seen() { web_area hw && signal_seen "$@"; }
signal_wait() { web_area hw && signal_wait "$@"; }
ears_reset_wait() { web_area hw && ears_reset_wait "$@"; }
ears_move() { web_area hw && ears_move "$@"; }
picture_ready() { web_area hw && picture_ready "$@"; }
take_picture() { web_area hw && take_picture "$@"; }
snapshot_names() { web_area hw && snapshot_names "$@"; }
valid_snapshot_file() { web_area hw && valid_snapshot_file "$@"; }
store_picture() { web_area hw && store_picture "$@"; }
# URLs, running the API's endpoints here, and the tag store.
valid_query() { web_area tags && valid_query "$@"; }
url_host() { web_area tags && url_host "$@"; }
param_base64() { web_area tags && param_base64 "$@"; }
url_action_params() { web_area tags && url_action_params "$@"; }
url_request() { web_area tags && url_request "$@"; }
cgi_run() { web_area tags && cgi_run "$@"; }
api_run() { web_area tags && api_run "$@"; }
tags_read() { web_area tags && tags_read "$@"; }
tags_change() { web_area tags && tags_change "$@"; }
tag_fields() { web_area tags && tag_fields "$@"; }
tag_get() { web_area tags && tag_get "$@"; }
param_tag() { web_area tags && param_tag "$@"; }
param_tag_name() { web_area tags && param_tag_name "$@"; }
tag_change_failed() { web_area tags && tag_change_failed "$@"; }
type_name() { web_area tags && type_name "$@"; }
color_name() { web_area tags && color_name "$@"; }
cmd_name() { web_area tags && cmd_name "$@"; }
tags_json() { web_area tags && tags_json "$@"; }
learning() { web_area tags && learning "$@"; }
learning_left() { web_area tags && learning_left "$@"; }
led_learning() { web_area tags && led_learning "$@"; }
tone() { web_area tags && tone "$@"; }
learn_timer() { web_area tags && learn_timer "$@"; }
