# SPDX-License-Identifier: MIT
# The web API's sounds, voices and the user's own lists on the stick (web.sh reads this file the
# first time one of its functions is called).

# media_ids DIR...: the names (without .mp3 or .wav) of the playable files in the folders, each
# once, sorted. Only names that pass valid_id count.
media_ids() {
    for _d; do
        for _f in "$_d"/*.mp3 "$_d"/*.wav; do
            [ -f "$_f" ] || continue
            _n=${_f##*/}
            _n=${_n%.*}
            valid_id "$_n" && printf '%s\n' "$_n"
        done
    done | sort -u
}

# The sound folders: the stick's sounds first, when it is mounted, then the core's own.
sound_dirs() {
    SOUND_DIRS="/usr/karotz/sounds"
    usb_mounted && SOUND_DIRS="/mnt/usbkey/sounds $SOUND_DIRS"
    return 0
}

# mp3_ids DIR: the names (without .mp3) of the MP3 files in the folder, sorted; only names that
# pass valid_id count.
mp3_ids() {
    mp3_names "$1" | sort -u
}

# mp3_names DIR: mp3_ids in the folder's own order (each name is there once).
mp3_names() {
    for _f in "$1/"*.mp3; do
        [ -f "$_f" ] || continue
        _n=${_f##*/}
        _n=${_n%.mp3}
        valid_id "$_n" && printf '%s\n' "$_n"
    done
}

# list_text FILE: the start of a list file on the stick as text: NUL bytes and invalid UTF-8 left
# out. Nothing when there is no such file.
list_text() {
    [ -f "$1" ] || return 0
    head -c $WEB_LIST_BYTES "$1" 2> /dev/null | tr -d '\000' | iconv -c -f UTF-8 -t UTF-8 2> /dev/null
    return 0
}

# Gives each id (in IDS, one per line) its label from "id:label" lines (the first for an id;
# spaces around both trimmed, control characters left out, at most 200 bytes), else the id
# itself; prints "key<TAB>id<TAB>label", the key ordering numbers first, by value, then the rest.
WEB_LABELS_AWK='BEGIN {
    ctl = "[" sprintf("%c", 1) "-" sprintf("%c", 31) sprintf("%c", 127) "]"
    zeros = "0000000000000000000000000000000000000000000000000000000000000000"
}
{
    sub(/\r$/, "")
    p = index($0, ":")
    if (p < 2) next
    id = substr($0, 1, p - 1)
    label = substr($0, p + 1)
    gsub(ctl, "", id)
    gsub(ctl, "", label)
    sub(/^ +/, "", id); sub(/ +$/, "", id)
    sub(/^ +/, "", label); sub(/ +$/, "", label)
    if (length(label) > 200) label = substr(label, 1, 200)
    if (id != "" && label != "" && !(id in labels)) labels[id] = label
}
END {
    n = split(ENVIRON["IDS"], ids, "\n")
    for (i = 1; i <= n; i++) {
        id = ids[i]
        if (id == "") continue
        if (id ~ /^[0-9]+$/) key = "0" substr(zeros, 1, 64 - length(id)) id
        else key = "1" id
        printf "%s\t%s\t%s\n", key, id, ((id in labels) ? labels[id] : id)
    }
}'

# labelled_ids DIR LABELS: one line per MP3 file in DIR, "key<TAB>id<TAB>label", numbers first
# in their order, then the other names; the labels from the file LABELS, else the ids.
labelled_ids() {
    _ids=$(mp3_names "$1")
    [ -n "$_ids" ] || return 0
    if [ -f "$2" ]; then
        # The labels' text may have lost bytes of a character cut at the size limit: iconv drops them.
        list_text "$2" | IDS="$_ids" awk "$WEB_LABELS_AWK" | sort | iconv -c -f UTF-8 -t UTF-8 2> /dev/null
    else
        IDS="$_ids" awk "$WEB_LABELS_AWK" < /dev/null | sort
    fi
    return 0
}

# labelled_json DIR LABELS KEY: labelled_ids as JSON list items {"id":..,"KEY":..}, comma-separated.
labelled_json() {
    labelled_ids "$1" "$2" | {
        _sep=""
        while IFS= read -r _line; do
            _line=${_line#*"$TAB"}
            json_str "${_line%%"$TAB"*}"
            _id=$J
            json_str "${_line#*"$TAB"}"
            printf '%s{"id":%s,"%s":%s}' "$_sep" "$_id" "$3" "$J"
            _sep=,
        done
    }
}

# sound_path ID: the file of the sound called ID, in SP: the stick's sounds first, then the core's,
# MP3 before WAV. False when there is none.
sound_path() {
    sound_dirs
    for _d in $SOUND_DIRS; do
        for _e in mp3 wav; do
            if [ -f "$_d/$1.$_e" ]; then
                SP=$_d/$1.$_e
                return 0
            fi
        done
    done
    return 1
}

# voice_of NUMBER: the language (VL) and gender (VG) of a voice number from 1 to 88.
voice_of() {
    _v=$1
    set -f
    # shellcheck disable=SC2086
    set -- $VOICE_LANGS
    set +f
    shift $(((_v - 1) / 2))
    VL=$1
    if [ $((_v % 2)) = 1 ]; then VG=male; else VG=female; fi
}

# lang_name CODE: the language's name in English, in LN.
lang_name() {
    case $1 in
    fr) LN=French ;; fr-CA) LN="French (Canada)" ;; en-US) LN="English (US)" ;; en-GB) LN="English (UK)" ;;
    de) LN=German ;; it) LN=Italian ;; es) LN=Spanish ;; nl) LN=Dutch ;; af) LN=Afrikaans ;; sq) LN=Albanian ;;
    ar) LN=Arabic ;; hy) LN=Armenian ;; bs) LN=Bosnian ;; pt-BR) LN="Portuguese (Brazil)" ;; hr) LN=Croatian ;;
    cs) LN=Czech ;; da) LN=Danish ;; en-AU) LN="English (Australia)" ;; eo) LN=Esperanto ;; fi) LN=Finnish ;;
    el) LN=Greek ;; ht) LN="Haitian Creole" ;; hi) LN=Hindi ;; hu) LN=Hungarian ;; is) LN=Icelandic ;;
    id) LN=Indonesian ;; ja) LN=Japanese ;; ko) LN=Korean ;; la) LN=Latin ;; no) LN=Norwegian ;; pl) LN=Polish ;;
    pt-PT) LN="Portuguese (Portugal)" ;; ro) LN=Romanian ;; ru) LN=Russian ;; sr) LN=Serbian ;;
    sh) LN=Serbo-Croatian ;; sk) LN=Slovak ;; sw) LN=Swahili ;; sv) LN=Swedish ;; ta) LN=Tamil ;; th) LN=Thai ;;
    tr) LN=Turkish ;; vi) LN=Vietnamese ;; cy) LN=Welsh ;; *) LN=$1 ;;
    esac
}

# read_tts_servers: the languages (first two letters, lower case) voice.conf has a speech server
# for, in TTS_LANGS (space-separated, with a space at each end).
read_tts_servers() {
    TTS_LANGS=" "
    [ -f /usr/etc/conf/voice.conf ] && [ -r /usr/etc/conf/voice.conf ] || return 0
    _in=0
    while IFS= read -r _l || [ -n "$_l" ]; do
        _l=${_l%"$CR"}
        case $_l in
        \[*) if [ "$_l" = "[tts]" ]; then _in=1; else _in=0; fi ;;
        [A-Za-z][A-Za-z]=?*)
            if [ $_in = 1 ]; then
                lower "${_l%"${_l#??}"}"
                TTS_LANGS="$TTS_LANGS$LO "
            fi
            ;;
        esac
    done < /usr/etc/conf/voice.conf
    return 0
}

# has_tts_server LANG: true when voice.conf has a speech server for LANG's first two letters.
# Needs read_tts_servers.
has_tts_server() {
    lower "${1%"${1#??}"}"
    case $TTS_LANGS in *" $LO "*) return 0 ;; esac
    return 1
}

# rabbit_lang: the rabbit's language (karotz.conf's lang, en-US when it has none), in RL.
rabbit_lang() {
    ini_get /usr/etc/conf/karotz.conf karotz lang
    RL=$IV
    case $RL in '' | *[!A-Za-z_-]*) RL=en-US ;; esac
}
