# SPDX-License-Identifier: MIT
# The web API's less common helpers (web.sh reads this file the first time one is called): the
# decoding of parameters that need it, JSON escaping, the URL and UTF-8 checks, and the words for
# a daemon's failure.

# Escapes each input line for a JSON string (quotes, backslashes, control characters) and joins
# the lines with \n.
WEB_JSON_AWK='BEGIN {
    for (i = 1; i < 32; i++) esc[sprintf("%c", i)] = sprintf("\\u%04x", i)
    esc[sprintf("%c", 127)] = "\\u007f"
    esc["\t"] = "\\t"
    esc["\""] = "\\\""
    esc["\\"] = "\\\\"
}
{
    o = ""
    n = length($0)
    for (i = 1; i <= n; i++) {
        c = substr($0, i, 1)
        o = o ((c in esc) ? esc[c] : c)
    }
    printf "%s%s", (NR > 1 ? "\\n" : ""), o
}'

# json_escape VALUE: VALUE as a JSON string literal, in J, whatever it holds.
json_escape() {
    J="\"$(printf '%s' "$1" | awk "$WEB_JSON_AWK")\""
}

# Decodes WEB_QUERY (the query string, then the body: name=value pairs joined by &) into one
# line per parameter: "name=value" for a good value, "!name" for one that held a control
# character, a broken %-escape or a raw byte outside printable ASCII. Names other than
# [a-z0-9_]{1,32} are left out, and so is every repeat of a name.
WEB_DECODE_AWK='BEGIN {
    hex = "0123456789ABCDEF"
    ns = split(ENVIRON["WEB_SPACE_PARAMS"], sp, " ")
    for (i = 1; i <= ns; i++) space[sp[i]] = 1
    n = split(ENVIRON["WEB_QUERY"], pairs, "&")
    for (i = 1; i <= n; i++) {
        pair = pairs[i]
        if (pair == "") continue
        eq = index(pair, "=")
        if (eq) { name = substr(pair, 1, eq - 1); raw = substr(pair, eq + 1) }
        else { name = pair; raw = "" }
        if (length(name) > 32 || name !~ /^[a-z0-9_]+$/ || (name in seen)) continue
        seen[name] = 1
        bad = 0
        out = ""
        len = length(raw)
        for (j = 1; j <= len; j++) {
            c = substr(raw, j, 1)
            if (c == "+") { out = out " "; continue }
            if (c == "%") {
                h1 = index(hex, toupper(substr(raw, j + 1, 1)))
                h2 = index(hex, toupper(substr(raw, j + 2, 1)))
                if (h1 == 0 || h2 == 0 || j + 2 > len) { bad = 1; break }
                v = (h1 - 1) * 16 + h2 - 1
                j += 2
                if (v < 32 || v == 127) {
                    if ((name in space) && (v == 9 || v == 10 || v == 13)) { out = out " "; continue }
                    bad = 1
                    break
                }
                out = out sprintf("%c", v)
                continue
            }
            if (c < "!" || c > "~") { bad = 1; break }
            out = out c
        }
        print (bad ? "!" name : name "=" out)
    }
}'

# decode_params QUERY: WEB_PARAMS from the query and body (WEB_DECODE_AWK).
decode_params() {
    WEB_PARAMS=$(WEB_SPACE_PARAMS="$WEB_SPACE_PARAMS" WEB_QUERY="$1" awk "$WEB_DECODE_AWK")
}

# valid_url VALUE: an http(s) URL that is safe to hand to the player: at most 1024 bytes; a host
# name or IPv4 address; an optional port from 1 to 65535; then a path and query of URL
# characters and %XX escapes only (no quote, backslash, angle bracket, brace, space, control
# character or ', and never "::").
valid_url() {
    case $1 in
    http://* | https://*) ;;
    *) return 1 ;;
    esac
    case $1 in *::*) return 1 ;; esac
    [ ${#1} -le 1024 ] || return 1
    printf '%s\n' "$1" | grep -Eq '^https?://[A-Za-z0-9.-]{1,253}(:[0-9]{1,5})?([/?#]([]A-Za-z0-9._~:/?#@!$&()*+,;=[-]|%[0-9A-Fa-f]{2})*)?$' ||
        return 1
    _hp=${1#*://}
    _hp=${_hp%%[/?#]*}
    case $_hp in
    *:*)
        nozeros "${_hp#*:}"
        _port=$NZ
        [ -n "$_port" ] && [ "$_port" -ge 1 ] && [ "$_port" -le 65535 ] || return 1
        ;;
    esac
    return 0
}

# valid_utf8 VALUE: true when VALUE is valid UTF-8.
valid_utf8() {
    case $1 in *[![:print:]]*) ;; *) return 0 ;; esac
    printf '%s' "$1" | iconv -f UTF-8 -t UTF-8 > /dev/null 2>&1
}

# daemon_name NAME: the program behind a daemon's bus name, in DN.
daemon_name() {
    case $1 in
    Led) DN=led-daemon ;;
    Ears) DN=ears-daemon ;;
    Multimedia) DN=multimedia-daemon ;;
    Voice) DN=voice-daemon ;;
    Webcam) DN=webcam-daemon ;;
    *) DN=$1 ;;
    esac
}

# dbus_failed NAME STATUS MESSAGE: fails the request for dbus_call's STATUS: MESSAGE when the
# daemon answered FALSE, else that it isn't running or is busy.
dbus_failed() {
    daemon_name "$1"
    case $2 in
    1) web_fail "$3" ;;
    2) web_fail "$DN isn't running" ;;
    *) web_fail "$DN is busy: it didn't answer in time" ;;
    esac
}
