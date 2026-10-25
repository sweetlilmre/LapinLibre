# SPDX-License-Identifier: MIT
# The web API's signal watches, the ears' reset and moves, and the camera (web.sh reads this file
# the first time one of its functions is called).

# signal_watch NAME MEMBER: starts watching the daemon's MEMBER signals (dbus-monitor, for at
# most 15 s, into a file), and waits up to 2 s for the watch to be in place (dbus-monitor writes
# its own first lines then).
signal_watch() {
    signal_stop
    [ -d "$WEB_RUN" ] || mkdir -p "$WEB_RUN" || return 1
    WEB_MONITOR_OUT=$WEB_RUN/signals.$$
    : > "$WEB_MONITOR_OUT"
    timeout 15 dbus-monitor --system \
        "type='signal',sender='com.mindscape.karotz.$1',interface='com.mindscape.karotz.KarotzInterface',member='$2'" \
        < /dev/null > "$WEB_MONITOR_OUT" 2> /dev/null 9>&- &
    WEB_MONITOR=$!
    _i=0
    while [ $_i -lt 10 ] && [ ! -s "$WEB_MONITOR_OUT" ]; do
        usleep 200000
        _i=$((_i + 1))
    done
}

# signal_seen STRING: true once a watched signal carried the string argument STRING.
signal_seen() {
    [ -n "$WEB_MONITOR_OUT" ] && grep -qF "string \"$1\"" "$WEB_MONITOR_OUT" 2> /dev/null
}

# signal_wait STRING SECONDS: waits at most SECONDS for signal_seen STRING.
signal_wait() {
    _n=$(($2 * 5))
    while ! signal_seen "$1"; do
        [ "$_n" -gt 0 ] || return 1
        usleep 200000
        _n=$((_n - 1))
    done
}

# ears_reset_wait: recalibrates both ears and waits for it to end (at most 10 s; then it is logged
# and the caller goes on). dbus_call's status.
ears_reset_wait() {
    signal_watch Ears reset_finished
    dbus_call Ears reset "string:$WEB_ID" string:B
    _r=$?
    if [ $_r = 0 ] && ! signal_wait "$WEB_ID" 10; then
        web_log "the ears' reset didn't end within 10 s: moving them anyway"
    fi
    signal_stop
    return $_r
}

# ears_move LEFT RIGHT: both ears to these positions (0 to 16), at the API's speed. dbus_call's
# status.
ears_move() {
    dbus_call Ears move "string:$WEB_ID" "int32:$1" "int32:$WEB_EAR_SPEED" "int32:$2" "int32:$WEB_EAR_SPEED"
}

# picture_ready: true when the camera's picture file is there and complete (it ends with the JPEG
# end marker).
picture_ready() {
    [ -s "$WEB_PICTURE" ] && [ "$(tail -c 2 "$WEB_PICTURE" | od -An -tx1 | tr -cd '0-9a-f')" = ffd9 ]
}

# take_picture: a new picture from webcam-daemon in WEB_PICTURE, waited for at most 10 s; then the
# API's LED state is restored (the daemon flashes the LED itself). Fails the request when no
# picture comes. Needs web_hardware first.
take_picture() {
    rm -f "$WEB_PICTURE"
    signal_watch Webcam event
    dbus_call Webcam takeSnapshot "string:$WEB_ID" string:file int32:1
    _r=$?
    case $_r in
    1 | 2)
        signal_stop
        [ $_r = 1 ] && led_restore
        dbus_failed Webcam $_r "the camera couldn't take a picture"
        ;;
    esac
    # A TRUE answer comes once the file is written; without an answer, the event with this
    # request's own id says the picture is this request's.
    _i=0
    while :; do
        if [ $_r = 0 ] || signal_seen "$WEB_CORR"; then
            picture_ready && break
        fi
        if [ $_i -ge 50 ]; then
            signal_stop
            led_restore
            web_fail "no picture came from the camera within 10 s"
        fi
        usleep 200000
        _i=$((_i + 1))
    done
    signal_stop
    led_restore
}

# snapshot_names: the stored snapshots' names, oldest first.
snapshot_names() {
    ls "$WEB_SNAPSHOTS" 2> /dev/null | grep -E '^snapshot_[0-9]{4}(_[0-9]{2}){5}\.jpg$'
}

# valid_snapshot_file NAME: a name snapshot gives a picture or its thumbnail.
valid_snapshot_file() {
    case $1 in
    snapshot_[0-9][0-9][0-9][0-9]_[0-9][0-9]_[0-9][0-9]_[0-9][0-9]_[0-9][0-9]_[0-9][0-9].jpg) return 0 ;;
    snapshot_[0-9][0-9][0-9][0-9]_[0-9][0-9]_[0-9][0-9]_[0-9][0-9]_[0-9][0-9]_[0-9][0-9].thumb.gif) return 0 ;;
    esac
    return 1
}

# store_picture: moves the picture into the stored snapshots, named by the local time (a second
# later when that name is taken), with a quarter-size GIF thumbnail when djpeg makes one; the
# oldest beyond WEB_SNAPSHOT_MAX go. SNAP_NAME, and SNAP_THUMB (empty without a thumbnail).
store_picture() {
    mkdir -p "$WEB_SNAPSHOTS" || web_fail "the snapshot couldn't be stored"
    _stamp=$(date +%Y_%m_%d_%H_%M_%S)
    while [ -e "$WEB_SNAPSHOTS/snapshot_$_stamp.jpg" ]; do
        sleep 1
        _stamp=$(date +%Y_%m_%d_%H_%M_%S)
    done
    SNAP_NAME=snapshot_$_stamp.jpg
    SNAP_THUMB=snapshot_$_stamp.thumb.gif
    mv -f "$WEB_PICTURE" "$WEB_SNAPSHOTS/$SNAP_NAME" || web_fail "the snapshot couldn't be stored"
    if ! timeout 10 djpeg -scale 1/4 -gif -outfile "$WEB_SNAPSHOTS/$SNAP_THUMB" "$WEB_SNAPSHOTS/$SNAP_NAME" \
        > /dev/null 2>&1 9>&- || [ ! -s "$WEB_SNAPSHOTS/$SNAP_THUMB" ]; then
        rm -f "$WEB_SNAPSHOTS/$SNAP_THUMB"
        SNAP_THUMB=""
    fi
    _all=$(snapshot_names)
    count_lines "$_all"
    _extra=$((N - WEB_SNAPSHOT_MAX))
    if [ $_extra -gt 0 ]; then
        printf '%s\n' "$_all" | head -n $_extra | while IFS= read -r _old; do
            rm -f "$WEB_SNAPSHOTS/$_old" "$WEB_SNAPSHOTS/${_old%.jpg}.thumb.gif"
        done
    fi
}
