# SPDX-License-Identifier: MIT
# The web API's URL actions, the running of its own endpoints here, and the RFID tag store (web.sh
# reads this file the first time one of its functions is called).

# An http(s) URL as valid_url takes it, for awk: goodurl(URL).
WEB_URL_AWK='function goodurl(u,   h, p, re) {
    re = "^https?://[A-Za-z0-9.-]+(:[0-9]+)?([/?#]([]A-Za-z0-9._~:/?#@!$&()*+,;=[-]|%[0-9A-Fa-f][0-9A-Fa-f])*)?$"
    if (length(u) > 1024 || u !~ re || index(u, "::")) return 0
    h = u
    sub("^https?://", "", h)
    sub("[/?#].*", "", h)
    p = ""
    if (index(h, ":")) {
        p = substr(h, index(h, ":") + 1)
        h = substr(h, 1, index(h, ":") - 1)
    }
    if (h == "" || length(h) > 253) return 0
    if (p != "" && (length(p) > 5 || p + 0 < 1 || p + 0 > 65535)) return 0
    return 1
}
'

# valid_query VALUE: a query string of URL characters and %XX escapes only (the characters
# valid_url allows after the host, without #), at most 1024 bytes.
valid_query() {
    [ ${#1} -le 1024 ] || return 1
    case $1 in *::*) return 1 ;; esac
    [ -z "$1" ] && return 0
    printf '%s\n' "$1" | grep -Eq '^([]A-Za-z0-9._~:/?@!$&()*+,;=[-]|%[0-9A-Fa-f]{2})*$'
}

# url_host URL: the host of an http(s) URL (without its port), in UH.
url_host() {
    UH=${1#*://}
    UH=${UH%%[/?#]*}
    UH=${UH%%:*}
}

# param_base64 NAME: the parameter decoded from standard base64 (a space is read as the + a
# client left unencoded), in PV. Status 1 when absent or empty. Fails the request unless it is
# base64 of 1 to 1024 bytes of printable ASCII.
param_base64() {
    param "$1"
    case $? in
    1) return 1 ;;
    2) web_fail "parameter $1 must be base64" ;;
    esac
    [ -n "$PV" ] || return 1
    _b=$(printf '%s' "$PV" | tr ' ' '+')
    case $_b in *[!A-Za-z0-9+/=]*) web_fail "parameter $1 must be base64" ;; esac
    [ $((${#_b} % 4)) = 0 ] && [ ${#_b} -le 1368 ] || web_fail "parameter $1 must be base64 of at most 1024 bytes"
    # The x keeps a last line end, and the byte count shows a NUL byte the shell would drop.
    _d=$(printf '%s' "$_b" | busybox base64 -d 2> /dev/null; echo x)
    _d=${_d%x}
    _n=$(printf '%s' "$_b" | busybox base64 -d 2> /dev/null | wc -c | tr -d ' ')
    [ -n "$_d" ] && [ "${#_d}" = "$_n" ] || web_fail "parameter $1 must be base64 of text"
    case $_d in *[![:print:]]*) web_fail "parameter $1 must be base64 of text without control characters" ;; esac
    PV=$_d
}

# url_action_params: a URL request's parameters: url (required), param (optional, base64 of a
# query string added to it) and user and password (both or neither), in ACT_URL, ACT_USER and
# ACT_PASSWORD. With ACT_KEEP_TAG set to a tag's id, a user given without a password takes the
# password that tag's URL action stores for the same user. Fails the request when one is bad.
url_action_params() {
    param url
    case $? in
    1) web_fail "parameter url is required: an http:// or https:// URL" ;;
    2) web_fail "parameter url must be an http:// or https:// URL" ;;
    esac
    ACT_URL=$PV
    [ -n "$ACT_URL" ] || web_fail "parameter url is required: an http:// or https:// URL"
    valid_url "$ACT_URL" || web_fail "parameter url must be an http:// or https:// URL"
    if param_base64 param; then
        _q=${PV#[?&]}
        valid_query "$_q" || web_fail "parameter param must be base64 of a query string"
        case $ACT_URL in
        *\?*) ACT_URL="$ACT_URL&$_q" ;;
        *) ACT_URL="$ACT_URL?$_q" ;;
        esac
        valid_url "$ACT_URL" || web_fail "the URL with param added isn't a valid URL of at most 1024 bytes"
    fi
    param user
    [ $? = 2 ] && web_fail "parameter user must be 1 to 64 printable characters other than :"
    ACT_USER=$PV
    param password
    [ $? = 2 ] && web_fail "parameter password must be 1 to 64 printable characters"
    ACT_PASSWORD=$PV
    # With ACT_KEEP_TAG set, a user without a password keeps the password that tag's URL action
    # has stored for the same user.
    if [ -n "$ACT_USER" ] && [ -z "$ACT_PASSWORD" ] && [ -n "${ACT_KEEP_TAG:-}" ] &&
        tag_get "$ACT_KEEP_TAG" && [ "$T_CMD" = 3 ] && [ "$T_A2" = "$ACT_USER" ]; then
        ACT_PASSWORD=$T_A3
    fi
    if [ -n "$ACT_USER" ] || [ -n "$ACT_PASSWORD" ]; then
        [ -n "$ACT_USER" ] && [ -n "$ACT_PASSWORD" ] || web_fail "give both user and password, or neither"
        case $ACT_USER in *[![:graph:]]* | *:*) web_fail "parameter user must be 1 to 64 printable characters other than :" ;; esac
        [ ${#ACT_USER} -le 64 ] || web_fail "parameter user must be 1 to 64 printable characters other than :"
        case $ACT_PASSWORD in *[![:graph:]]*) web_fail "parameter password must be 1 to 64 printable characters" ;; esac
        [ ${#ACT_PASSWORD} -le 64 ] || web_fail "parameter password must be 1 to 64 printable characters"
    fi
    return 0
}

# url_request URL USER PASSWORD OUT: an HTTP GET of URL with curl, the body into the file OUT:
# at most 3 redirects, to http(s) only; 15 s in all; certificates checked; basic authentication
# when USER is given, passed to curl on its standard input so it never shows in the process list;
# a body over 1 MB is cut off as an error. curl's status; UR_CODE the HTTP status, and UR_ERROR
# what went wrong, in words.
url_request() {
    mkdir -p "$WEB_RUN" || return 1
    _err=$WEB_RUN/curl.$$
    if [ -n "$2" ]; then
        _cred=$(printf '%s' "$2:$3" | sed 's/[\\"]/\\&/g')
        UR_CODE=$(printf 'user = "%s"\n' "$_cred" | timeout 20 curl -q -sS -K - -L --max-redirs 3 --max-time 15 \
            --proto =http,https --proto-redir =http,https --max-filesize 1048576 -o "$4" -w '%{http_code}' "$1" 2> "$_err")
    else
        UR_CODE=$(timeout 20 curl -q -sS -L --max-redirs 3 --max-time 15 --proto =http,https --proto-redir =http,https \
            --max-filesize 1048576 -o "$4" -w '%{http_code}' "$1" < /dev/null 2> "$_err")
    fi
    _s=$?
    _cred=""
    case $UR_CODE in [0-9][0-9][0-9]) ;; *) UR_CODE="" ;; esac
    UR_ERROR=$(head -n 1 "$_err" 2> /dev/null | tr -d '\r' | sed 's/^curl: ([0-9]*) //')
    rm -f "$_err"
    case $_s in
    0) UR_ERROR="" ;;
    124 | 143) UR_ERROR="no answer within 15 s" ;;
    *) [ -n "$UR_ERROR" ] || UR_ERROR="the request failed (curl status $_s)" ;;
    esac
    return $_s
}

# cgi_run ENDPOINT QUERY [check]: runs the API's program ENDPOINT on this rabbit with QUERY as its
# query string (data only: the program decodes and checks it as any request's), as the web user
# from 127.0.0.1, with no other environment, for at most 30 s; with "check" it only checks its
# parameters. Its answer's body in AR_BODY, and its message, when it failed, in AR_MSG. Status 0
# when it answered success.
cgi_run() {
    _chk=0
    [ "${3:-}" = check ] && _chk=1
    AR_BODY=$(timeout 30 env -i PATH="$PATH" REQUEST_METHOD=GET QUERY_STRING="$2" SCRIPT_NAME="/cgi-bin/$1" \
        REQUEST_URI="/cgi-bin/$1" REMOTE_USER="$WEB_USER" REMOTE_ADDR=127.0.0.1 GATEWAY_INTERFACE=CGI/1.1 \
        WEB_CHECK_ONLY=$_chk "$WEB_CGI/$1" < /dev/null 2> /dev/null | tr -d '\r' | sed '1,/^$/d')
    AR_MSG=""
    case $AR_BODY in *'"return":"0"'*) return 0 ;; esac
    AR_MSG=$(printf '%s\n' "$AR_BODY" | sed -n 's/.*"msg":"\(.*\)"}$/\1/p' | head -n 1)
    [ -n "$AR_MSG" ] || AR_MSG="it didn't answer"
    return 1
}

# api_run ENDPOINT QUERY [check]: cgi_run for an endpoint of WEB_TAG_ENDPOINTS only (status 2
# for any other).
api_run() {
    case " $WEB_TAG_ENDPOINTS " in *" $1 "*) ;; *) return 2 ;; esac
    cgi_run "$@"
}

# Reads the tag store and prints its valid tags, one line each in the store format, every field
# checked again: lines with a bad id or a repeated one are left out; bad numbers become -1; a bad
# name becomes empty; an action that fails its rules (the URL rule, the credentials, the
# endpoints allowed and the query characters) is dropped (cmd 0).
WEB_TAGS_READ_AWK='BEGIN {
    FS = "\t"
    OFS = "\t"
    ctl = "[" sprintf("%c", 1) "-" sprintf("%c", 31) sprintf("%c", 127) "]"
    n = split(ENVIRON["WEB_TAG_ENDPOINTS"], e, " ")
    for (i = 1; i <= n; i++) allowed[e[i]] = 1
    qre = "^([]A-Za-z0-9._~:/?@!$&()*+,;=[-]|%[0-9A-Fa-f][0-9A-Fa-f])*$"
    only = ENVIRON["TAGS_ONLY"]
}
function num(v) { return (v ~ /^-?[0-9]+$/ && length(v) <= 6) ? v + 0 : -1 }
function cred(u, p) {
    if (u == "" && p == "") return 1
    return u ~ /^[!-~]+$/ && !index(u, ":") && length(u) <= 64 && p ~ /^[!-~]+$/ && length(p) <= 64
}
/^#/ || NF == 0 { next }
{
    id = toupper($1)
    if (length(id) != 16 || id !~ /^[0-9A-F]+$/ || (id in seen)) next
    seen[id] = 1
    name = $6
    if (name ~ ctl || length(name) > 256) name = ""
    cmd = 0
    a1 = ""; a2 = ""; a3 = ""
    if ($4 == "3" && goodurl($7) && cred($8, $9)) { cmd = 3; a1 = $7; a2 = $8; a3 = $9 }
    else if ($4 == "5" && ($7 in allowed) && length($8) <= 1024 && $8 ~ qre && !index($8, "::")) { cmd = 5; a1 = $7; a2 = $8 }
    if (only == "" || id == only) print id, num($2), num($3), cmd, (cmd ? num($5) : -1), name, a1, a2, a3
    if (id == only) exit
}'

# Applies the change OP (add, rename, assign, unassign, delete, clear) to the valid tags on its
# input, with the values in TAG_ID, TAG_TYPE, TAG_COLOR (with TAG_SETCOLOR 1 to change it),
# TAG_NAME (with TAG_SETNAME 1, for assign), TAG_CMD and TAG_A1 to TAG_A3, and prints the new
# store. Exits 3 when the tag isn't known, 4 when it is known already (add).
WEB_TAGS_EDIT_AWK='BEGIN {
    FS = "\t"
    OFS = "\t"
    op = ENVIRON["OP"]
    t = ENVIRON["TAG_ID"]
    found = 0
    print "# LapinLibre tag store: id, type, color, cmd, var, name, then the action (tab-separated)"
}
op == "clear" { next }
$1 == t {
    found = 1
    if (op == "delete") next
    if (op == "rename") {
        if (ENVIRON["TAG_SETCOLOR"] == "1") $3 = ENVIRON["TAG_COLOR"]
        $6 = ENVIRON["TAG_NAME"]
    }
    if (op == "unassign") { $4 = 0; $5 = -1; $6 = ""; $7 = ""; $8 = ""; $9 = "" }
    if (op == "assign") {
        $4 = ENVIRON["TAG_CMD"]; $5 = -1
        $7 = ENVIRON["TAG_A1"]; $8 = ENVIRON["TAG_A2"]; $9 = ENVIRON["TAG_A3"]
        if (ENVIRON["TAG_SETNAME"] == "1") $6 = ENVIRON["TAG_NAME"]
    }
}
{ print }
END {
    if (op == "add" && !found) print t, ENVIRON["TAG_TYPE"], ENVIRON["TAG_COLOR"], 0, -1, "", "", "", ""
    if (op == "add" && found) exit 4
    if (op != "add" && op != "clear" && !found) exit 3
}'

# tags_read: the store's valid tags (WEB_TAGS_READ_AWK), one per line, or only the tag TAGS_ONLY
# when it is set; nothing without a store.
tags_read() {
    [ -e "$WEB_TAGS" ] || return 0
    WEB_TAG_ENDPOINTS="$WEB_TAG_ENDPOINTS" TAGS_ONLY="${TAGS_ONLY:-}" awk "$WEB_URL_AWK$WEB_TAGS_READ_AWK" "$WEB_TAGS"
}

# tags_change OP: the store changed by WEB_TAGS_EDIT_AWK, under the store's lock (waited for at
# most 5 s), and replaced whole (mode 0600, in a 0700 folder). Status 0 when it was written, 3 for
# an unknown tag, 4 for a tag known already, 1 when it couldn't be read or written.
tags_change() {
    mkdir -p "$WEB_RUN" "${WEB_TAGS_LOCK%/*}" "$WEB_DATA" && chmod 700 "$WEB_DATA" || return 1
    exec 8>> "$WEB_TAGS_LOCK" || return 1
    if ! timeout 5 flock -x 8; then
        exec 8>&-
        return 1
    fi
    _cur=$WEB_RUN/tags.$$
    _s=1
    if tags_read > "$_cur"; then
        OP=$1
        export OP TAG_ID TAG_TYPE TAG_COLOR TAG_SETCOLOR TAG_NAME TAG_SETNAME TAG_CMD TAG_A1 TAG_A2 TAG_A3
        awk "$WEB_TAGS_EDIT_AWK" < "$_cur" > "$WEB_TAGS.new"
        _s=$?
        if [ $_s = 0 ]; then
            chmod 600 "$WEB_TAGS.new" && mv -f "$WEB_TAGS.new" "$WEB_TAGS" || _s=1
        fi
    fi
    rm -f "$_cur" "$WEB_TAGS.new"
    flock -u 8
    exec 8>&-
    return $_s
}

# tag_fields LINE: a store line's fields in T_ID, T_TYPE, T_COLOR, T_CMD, T_VAR, T_NAME, T_A1,
# T_A2 and T_A3.
tag_fields() {
    _l=$1$TAB$TAB$TAB$TAB$TAB$TAB$TAB$TAB
    T_ID=${_l%%"$TAB"*}
    _l=${_l#*"$TAB"}
    T_TYPE=${_l%%"$TAB"*}
    _l=${_l#*"$TAB"}
    T_COLOR=${_l%%"$TAB"*}
    _l=${_l#*"$TAB"}
    T_CMD=${_l%%"$TAB"*}
    _l=${_l#*"$TAB"}
    T_VAR=${_l%%"$TAB"*}
    _l=${_l#*"$TAB"}
    T_NAME=${_l%%"$TAB"*}
    _l=${_l#*"$TAB"}
    T_A1=${_l%%"$TAB"*}
    _l=${_l#*"$TAB"}
    T_A2=${_l%%"$TAB"*}
    _l=${_l#*"$TAB"}
    T_A3=${_l%%"$TAB"*}
}

# tag_get ID: the stored tag ID's fields (tag_fields). False when it isn't stored.
tag_get() {
    TAGS_ONLY=$1
    _line=$(tags_read)
    TAGS_ONLY=""
    [ -n "$_line" ] || return 1
    tag_fields "$_line"
}

# param_tag: the required parameter tag, in TG. Fails the request when it is missing or bad.
param_tag() {
    param tag
    case $? in
    1) web_fail "parameter tag is required: a tag's id (16 hexadecimal digits)" ;;
    2) web_fail "parameter tag must be a tag's id: 16 hexadecimal digits" ;;
    esac
    [ -n "$PV" ] || web_fail "parameter tag is required: a tag's id (16 hexadecimal digits)"
    valid_tag "$PV" || web_fail "parameter tag must be a tag's id: 16 hexadecimal digits"
}

# param_tag_name: the parameter name, a tag's name (0 to 64 characters of UTF-8), in PV. Status 1
# when absent. Fails the request when it is bad.
param_tag_name() {
    param name
    case $? in
    1) return 1 ;;
    2) web_fail "parameter name must be text without control characters" ;;
    esac
    valid_utf8 "$PV" || web_fail "parameter name must be valid UTF-8"
    _chars=$(printf '%s' "$PV" | tr -d '\200-\277' | wc -c | tr -d ' ')
    [ "$_chars" -le 64 ] || web_fail "parameter name is longer than 64 characters"
    return 0
}

# tag_change_failed STATUS: fails the request for tags_change's STATUS.
tag_change_failed() {
    case $1 in
    3) web_fail "no tag $TG is known: learn it first (rfid_start_record)" ;;
    4) web_fail "the tag $TG is known already" ;;
    *) web_fail "the tag store couldn't be written: try again" ;;
    esac
}

# The names of the codes: tag types (TN), colours (CN) and action kinds (MN).
type_name() {
    case $1 in 1) TN=FLATANOZ ;; 2) TN=NANOZTAG ;; 3) TN=ZSTAMPS ;; 15) TN=KEYRING ;; *) TN=UNKNOWN ;; esac
}
color_name() {
    case $1 in
    1) CN=RED ;; 2) CN=BLUE ;; 3) CN=GREEN ;; 4) CN=YELLOW ;; 5) CN=PINK ;; 6) CN=BLACK ;; 7) CN=GREY ;;
    8) CN=ORANGE ;; 9) CN=PURPLE ;; 10) CN=WHITE ;; 51) CN=BROWN ;; *) CN=UNKNOWN ;;
    esac
}
cmd_name() {
    case $1 in 1) MN=VERA ;; 2) MN=EEDOMUS ;; 3) MN=URL ;; 4) MN=ZIBASE ;; 5) MN=KAROTZ ;; *) MN=Unknown ;; esac
}

# tags_json SHAPE: the stored tags as JSON list items, comma-separated: "list" (rfid_list's
# shape) or "ext" (rfid_list_ext's, with its second type key holding the action's name).
tags_json() {
    tags_read | {
        _sep=""
        while IFS= read -r _line; do
            tag_fields "$_line"
            json_str "$T_NAME"
            if [ "$1" = ext ]; then
                type_name "$T_TYPE"
                color_name "$T_COLOR"
                cmd_name "$T_CMD"
                printf '%s{"id":"%s","name":%s,"type":"%s","type_name":"%s","color":"%s","color_name":"%s","type":"%s","cmd":"%s","cmd_name":"%s"}' \
                    "$_sep" "$T_ID" "$J" "$T_TYPE" "$TN" "$T_COLOR" "$CN" "$MN" "$T_CMD" "$MN"
            else
                printf '%s{"id":"%s","type":"%s","color":"%s","cmd":"%s","var":"%s","name":%s}' \
                    "$_sep" "$T_ID" "$T_TYPE" "$T_COLOR" "$T_CMD" "$T_VAR" "$J"
            fi
            _sep=,
        done
    }
}

# learning: true while tag learning is on (its flag in RAM, set less than WEB_LEARN_TIME seconds
# ago); a flag older than that is removed.
learning() {
    first_line "$WEB_RUN/learning" || return 1
    case $FL in '' | *[!0-9]*) rm -f "$WEB_RUN/learning"; return 1 ;; esac
    _age=$(($(date +%s) - FL))
    if [ $_age -ge 0 ] && [ $_age -lt $WEB_LEARN_TIME ]; then
        return 0
    fi
    rm -f "$WEB_RUN/learning"
    return 1
}

# learning_left: the seconds of tag learning left, in LL_LEFT; 0 when learning is off.
learning_left() {
    LL_LEFT=0
    learning || return 0
    LL_LEFT=$((WEB_LEARN_TIME - _age))
}

# led_learning: the LED's pulse while tags are learned. dbus_call's status.
led_learning() {
    led_set FF8800 1 000000 400
}

# tone FILE: plays one of the core's tones through multimedia-daemon, unless the rabbit sleeps
# (needs read_api_state) or the file is missing. Needs WEB_ID.
tone() {
    [ "$SLEEP" = 1 ] && return 0
    [ -f "$1" ] || return 0
    dbus_call Multimedia play "string:$WEB_ID" "string:$1" int32:1 || web_log "a tone couldn't be played"
    return 0
}

# lang_sound KEY: the sound file of KEY in the rabbit's language file (/usr/karotz/res/lang/<lang>),
# or en-US's when that file has none, in LS; empty unless it is an MP3 in /usr/karotz/sounds that
# is there.
lang_sound() {
    LS=""
    rabbit_lang
    for _lf in "/usr/karotz/res/lang/$RL" /usr/karotz/res/lang/en-US; do
        ini_get "$_lf" lang "$1"
        [ -n "$IV" ] && break
    done
    case $IV in
    /usr/karotz/sounds/*.mp3) ;;
    *) return 0 ;;
    esac
    case ${IV#/usr/karotz/sounds/} in */* | *..*) return 0 ;; esac
    [ -f "$IV" ] && LS=$IV
    return 0
}

# learn_timer STAMP: ends tag learning WEB_LEARN_TIME seconds after it started at STAMP, as
# rfid_stop_record does, unless it was stopped or started again meanwhile. Run in the background:
# it lets go of the request's input, output and locks first, so the answer isn't held back.
learn_timer() {
    exec < /dev/null > /dev/null 2>&1 8>&- 9>&-
    sleep $WEB_LEARN_TIME
    first_line "$WEB_RUN/learning" || return 0
    [ "$FL" = "$1" ] || return 0
    cgi_run rfid_stop_record ""
}
