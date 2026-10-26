#!/bin/sh
# SPDX-License-Identifier: MIT
# The web page and API (stages 1 to 3): behaviour tests on the rabbit, POSIX sh (busybox on the
# rabbit).
#
#   web-api.sh <new www dir> <new web.sh> <work dir> <test password> [hardware] [tag] [reboot]
#
# Runs from RAM, before any flashing: a second httpd on port 8099 (127.0.0.1 only) serves copies of
# the new files with a test configuration made like inetd.sh's; the installed web page, its
# configuration and its password are never touched. The copies keep the API's data (the tag store,
# the ears' switch) in the work dir, never on the flash. From the repository:
#   tar -C image/core/usr -cf - www share/lapinlibre scripts/tag_listener | \
#       ssh root@<rabbit> 'rm -rf /dev/shm/web && mkdir -p /dev/shm/web && tar -C /dev/shm/web -xf -'
#   ssh root@<rabbit> sh -s -- /dev/shm/web/www /dev/shm/web/share/lapinlibre/web.sh /dev/shm/webtest testpass123 \
#       < tests/device/web-api.sh
# The tag listener is taken from scripts/tag_listener beside the www dir.
# With "tag" (someone at the rabbit, with one RFID tag) it also runs a copy of the tag listener
# and asks for the tag to be laid on the nose twice: once while learning (the nose pulses orange,
# then flashes green), once to run the action given to it (the LED goes red; a request to the test
# httpd). The copy stops at the end; the tag is forgotten.
# With "hardware" it also drives the rabbit through the stage 2 endpoints (someone at the rabbit
# watches): the LED goes red, then pulses green, then flashes yellow (never blue); the
# ears recalibrate and move to positions from 0 to 16; a tone plays and stops; a short phrase is
# spoken when a speech server is set; pictures are taken (the nose flashes, the shutter sounds),
# listed, fetched and deleted (every picture the API holds in RAM goes); the rabbit sleeps and
# wakes up silently. It starts and ends awake with the green pulse, so it can be run again; the
# ears' on/off setting (kept on the flash) is left alone. With "reboot" it also checks the reboot
# endpoint: the rabbit then restarts about 10 s after the check.
#
# Checked: basic auth is required and the right password accepted; the status, version, free
# space, logs and SSH endpoints answer in their shape with values filled in; the policy refusals
# (no web user, an outside address, cross-site requests, methods, sizes, bad parameters); no
# cross-origin header; with "hardware", each stage 2 endpoint's answer and its effect on the
# status. Prints PASS/FAIL per check; exits 1 if any failed.
WWW=$1
LIB=$2
W=$3
PASS=$4
PORT=8099
if [ -z "$WWW" ] || [ -z "$LIB" ] || [ -z "$W" ] || [ -z "$PASS" ]; then
    echo "usage: $0 <www dir> <web.sh> <work dir> <test password> [hardware] [tag] [reboot]" >&2
    exit 2
fi
shift 4
HARDWARE=0
TAG=0
REBOOT=0
for option; do
    case $option in
    hardware) HARDWARE=1 ;;
    tag) TAG=1 ;;
    reboot) REBOOT=1 ;;
    *) echo "unknown option: $option" >&2; exit 2 ;;
    esac
done
command -v curl > /dev/null || { echo "no curl" >&2; exit 2; }
failed=0
pass() { echo "PASS  $1"; }
fail() { echo "FAIL  $1"; failed=1; }
check() { if [ "$2" = "$3" ]; then pass "$1"; else fail "$1: got '$2', expected '$3'"; fi; }
has() { case $2 in *"$3"*) pass "$1" ;; *) fail "$1: '$3' not in '$(printf '%s' "$2" | head -c 300)'" ;; esac; }
hasnt() { case $2 in *"$3"*) fail "$1: '$3' found" ;; *) pass "$1" ;; esac; }

# The copies, each program sourcing the copied library.
rm -rf "$W"
mkdir -p "$W" || exit 2
cp -r "$WWW" "$W/www" && cp "$LIB" "$W/web.sh" || exit 2
# The library's area files, from beside it.
for f in "${LIB%/*}"/web-*.sh; do
    [ -f "$f" ] && { cp "$f" "$W/" || exit 2; }
done
# The copies' data, programs and area files: the work dir's.
sed -i "s|^WEB_DATA=.*|WEB_DATA=$W/data|; s|^WEB_CGI=.*|WEB_CGI=$W/www/cgi-bin|; s|^WEB_LIB=.*|WEB_LIB=$W|; s|^WEB_HASH=.*|WEB_HASH=$W/web_password.hash|" "$W/web.sh"
LISTENER_SRC=${WWW%/www}/scripts/tag_listener
[ -f "$LISTENER_SRC" ] && cp "$LISTENER_SRC" "$W/tag_listener"
for f in $(find "$W/www/cgi-bin" -type f) $W/tag_listener; do
    [ -f "$f" ] || continue
    sed -i "s|^\. /usr/share/lapinlibre/web.sh\$|. $W/web.sh|" "$f"
    chmod 755 "$f"
done

# The test configuration: the local network, deny-all, the index, the password.
HASH=$(printf '%s\n' "$PASS" | busybox cryptpw -m sha512 -P 0)
{
    for r in 127.0.0.1 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16; do echo "A:$r"; done
    echo "D:*"
    echo "I:index.html"
    echo "/:admin:$HASH"
} > "$W/httpd.conf"
chmod 600 "$W/httpd.conf"
# The copies' password hash (the rabbit's own, if any, is left alone).
printf '%s\n' "$HASH" > "$W/web_password.hash"
busybox httpd -f -p 127.0.0.1:$PORT -c "$W/httpd.conf" -h "$W/www" &
HTTPD=$!
sleep 1
U=http://127.0.0.1:$PORT

# get PATH [curl options...]: the HTTP status, a line break, then the body.
get() {
    path=$1
    shift
    curl -s -m 30 -w '\n%{http_code}' "$@" "$U/$path" > "$W/out" 2> /dev/null
    CODE=$(tail -n 1 "$W/out")
    BODY=$(sed '$d' "$W/out")
}
auth="-u admin:$PASS"

# 1. Authentication.
get cgi-bin/lapinlibre/status
check "no credentials: 401" "$CODE" 401
get cgi-bin/lapinlibre/status -u admin:wrong-password
check "wrong password: 401" "$CODE" 401
get cgi-bin/lapinlibre/status -u root:"$PASS"
check "other user: 401" "$CODE" 401
# shellcheck disable=SC2086
get "" $auth
check "page with the password: 200" "$CODE" 200
has "page is LapinLibre's" "$BODY" "<title>LapinLibre</title>"

# 2. The endpoints.
# shellcheck disable=SC2086
get cgi-bin/lapinlibre/status $auth -D "$W/headers"
check "lapinlibre/status: 200" "$CODE" 200
has "lapinlibre/status: return 0" "$BODY" '"return":"0"'
for field in build build_id hostname ip mac time controller; do
    hasnt "lapinlibre/status: $field filled" "$BODY" "\"$field\":\"\""
done
has "lapinlibre/status: daemons" "$BODY" '"daemons":{"led":"1"'
has "lapinlibre/status: web user" "$BODY" '"web_user":"admin"'
headers=$(tr -d '\r' < "$W/headers")
has "content type text/plain" "$headers" "Content-Type: text/plain"
has "no caching" "$headers" "Cache-Control: no-store"
hasnt "no cross-origin header" "$headers" "Access-Control"
# shellcheck disable=SC2086
get cgi-bin/status $auth
has "status: OpenKarotz shape" "$BODY" '"sleep":"'
has "status: LapinLibre's part" "$BODY" '"lapinlibre":{'
# shellcheck disable=SC2086
get "cgi-bin/status?format=xml" $auth
has "status xml" "$BODY" "<OPENKAROTZ><VERSION>"
# shellcheck disable=SC2086
get cgi-bin/get_version $auth
has "get_version" "$BODY" '"patch_version":"N/A"'
# shellcheck disable=SC2086
get cgi-bin/get_free_space $auth
has "get_free_space" "$BODY" '"karotz_percent_used_space":"'
# shellcheck disable=SC2086
get cgi-bin/tools_log $auth
check "tools_log: 200" "$CODE" 200
# shellcheck disable=SC2086
get "cgi-bin/lapinlibre/logs?lines=3&source=kernel" $auth
check "logs: 3 kernel lines" "$(printf '%s\n' "$BODY" | wc -l | tr -d ' ')" 3
# shellcheck disable=SC2086
get cgi-bin/lapinlibre/ssh $auth
has "ssh: users" "$BODY" '"users":["root","karotz"]'
hasnt "ssh: no key material" "$BODY" "AAAA"

# 3. The refusals.
# shellcheck disable=SC2086
get cgi-bin/reboot $auth -H "Origin: http://evil.example"
check "reboot from another origin: 403" "$CODE" 403
# shellcheck disable=SC2086
get cgi-bin/reboot $auth -H "Sec-Fetch-Site: cross-site"
check "reboot cross-site: 403" "$CODE" 403
# shellcheck disable=SC2086
get cgi-bin/reboot $auth -e "http://evil.example/page"
check "reboot with another site's referer: 403" "$CODE" 403
# shellcheck disable=SC2086
get cgi-bin/get_version $auth -X PUT
check "PUT: 405" "$CODE" 405
long=$(head -c 5000 /dev/zero | tr '\0' 'a')
# shellcheck disable=SC2086
get "cgi-bin/get_version?x=$long" $auth
check "long query: 414" "$CODE" 414
# shellcheck disable=SC2086
get "cgi-bin/lapinlibre/logs?lines=%24(reboot)" $auth
has "bad parameter: error" "$BODY" '"return":"1"'
# The programs' own checks, without httpd: no web user, and an outside address.
out=$(REQUEST_METHOD=GET SCRIPT_NAME=/cgi-bin/get_version REMOTE_ADDR=127.0.0.1 sh "$W/www/cgi-bin/get_version")
has "no web user: 403" "$out" "Status: 403"
out=$(REQUEST_METHOD=GET SCRIPT_NAME=/cgi-bin/reboot REMOTE_ADDR=8.8.8.8 REMOTE_USER=admin sh "$W/www/cgi-bin/reboot")
has "outside address: 403" "$out" "Status: 403"

# 4. Stage 2's reads, which change nothing.
# shellcheck disable=SC2086
get cgi-bin/sound_list $auth
has "sound_list: the boot-ready tone" "$BODY" '{"id":"boot-ready"}'
# shellcheck disable=SC2086
get cgi-bin/voice_list $auth
has "voice_list" "$BODY" '"voices":['
# shellcheck disable=SC2086
get cgi-bin/display_cache $auth
check "display_cache" "$BODY" '{"count":"0","return":"0"}'
# shellcheck disable=SC2086
get cgi-bin/tools_ps $auth
has "tools_ps" "$BODY" "led-daemon"
# shellcheck disable=SC2086
get "cgi-bin/snapshot_get?filename=..%2Fled" $auth
check "snapshot_get, a bad name: 404" "$CODE" 404
# shellcheck disable=SC2086
get "cgi-bin/leds?color=FF0000" $auth -H "Origin: http://evil.example"
check "leds from another origin: 403" "$CODE" 403
get "cgi-bin/leds?color=FF0000"
check "leds without credentials: 401" "$CODE" 401

# field NAME: a string field of the last JSON answer.
field() {
    printf '%s' "$BODY" | sed -n "s/.*\"$1\":\"\\([^\"]*\\)\".*/\\1/p"
}

# 5. Stage 3 without the hardware: the lists, and the tag store (in the work dir) with a made-up tag.
FAKE=0123456789ABCDEF
# shellcheck disable=SC2086
get cgi-bin/rfid_list $auth
check "rfid_list: empty" "$BODY" '{"tags":[],"return":"0"}'
# shellcheck disable=SC2086
get cgi-bin/tag_list $auth
has "tag_list: points to rfid_list" "$BODY" 'rfid_list'
for list in moods_list:moods stories_list:stories radios_list:streams; do
    # shellcheck disable=SC2086
    get "cgi-bin/${list%%:*}" $auth
    has "${list%%:*}: its shape" "$BODY" "{\"${list#*:}\":["
done
# shellcheck disable=SC2086
get "cgi-bin/rfid_info?tag=$FAKE" $auth
has "rfid_info: unknown tag" "$BODY" '"return":"1"'
# shellcheck disable=SC2086
get "cgi-bin/rfid_assign_url?tag=$FAKE&url=http%3A%2F%2F127.0.0.1%2F" $auth
has "rfid_assign_url: unknown tag" "$BODY" 'no tag'
check "nothing stored yet" "$(ls "$W/data/tags" 2> /dev/null)" ""
mkdir -p "$W/data"
printf '%s\t1\t3\t0\t-1\t\t\t\t\n' "$FAKE" > "$W/data/tags"
# shellcheck disable=SC2086
get "cgi-bin/rfid_rename?tag=$FAKE&name=Test%20tag&color=4" $auth
check "rfid_rename" "$BODY" '{"return":"0"}'
check "the store's mode" "$(ls -l "$W/data/tags" | cut -c1-10)" "-rw-------"
# shellcheck disable=SC2086
get "cgi-bin/rfid_assign_karotz?tag=$FAKE&url=$(printf '%s' /cgi-bin/ears_reset | busybox base64)" $auth
check "rfid_assign_karotz: checked, not run" "$BODY" '{"return":"0"}'
# shellcheck disable=SC2086
get "cgi-bin/rfid_assign_karotz?tag=$FAKE&url=$(printf '%s' /cgi-bin/reboot | busybox base64)" $auth
has "rfid_assign_karotz: reboot refused" "$BODY" "can't run"
# shellcheck disable=SC2086
get "cgi-bin/rfid_list_ext?x=1" $auth
has "rfid_list_ext: the tag and its action" "$BODY" "\"id\":\"$FAKE\",\"name\":\"Test tag\",\"type\":\"1\",\"type_name\":\"FLATANOZ\",\"color\":\"4\",\"color_name\":\"YELLOW\",\"type\":\"KAROTZ\""
# A URL request with credentials, to the test httpd itself.
# shellcheck disable=SC2086
get "cgi-bin/rfid_test_url?url=http%3A%2F%2F127.0.0.1%3A$PORT%2Fcgi-bin%2Fget_version&user=admin&password=$PASS" $auth
has "rfid_test_url: the answer's body" "$BODY" 'patch_version'
# shellcheck disable=SC2086
get "cgi-bin/rfid_test_url?url=http%3A%2F%2F127.0.0.1%3A$PORT%2Fcgi-bin%2Fget_version" $auth
has "rfid_test_url: without credentials, the 401 page" "$BODY" '"return":"0"'
# shellcheck disable=SC2086
get "cgi-bin/rfid_assign_url?tag=$FAKE&url=http%3A%2F%2F127.0.0.1%3A$PORT%2Fcgi-bin%2Fget_version&user=admin&password=$PASS" $auth
check "rfid_assign_url" "$BODY" '{"return":"0"}'
# shellcheck disable=SC2086
get "cgi-bin/rfid_info?tag=$FAKE" $auth
has "rfid_info: a URL action" "$BODY" '"cmd_name":"URL"'
hasnt "rfid_info: no password" "$BODY" "$PASS"
# shellcheck disable=SC2086
get "cgi-bin/rfid_delete?tag=$FAKE" $auth
check "rfid_delete" "$BODY" "{\"return\":\"0\",\"tag\":\"$FAKE\"}"
# shellcheck disable=SC2086
get cgi-bin/clear_rfid $auth
check "clear_rfid" "$BODY" '{"return":"0"}'
hasnt "the password not logged" "$(tail -n 200 /var/log/messages 2> /dev/null)" "$PASS"

# 6. The hardware, only when asked.
if [ $HARDWARE = 1 ]; then
    # shellcheck disable=SC2086
    get "cgi-bin/wakeup?silent=1" $auth
    check "wakeup at the start" "$BODY" '{"return":"0","silent":"1"}'
    # shellcheck disable=SC2086
    get cgi-bin/status $auth
    check "status: awake" "$(field sleep)" 0

    # The LED: red, a green pulse, a momentary yellow; never blue.
    # shellcheck disable=SC2086
    get "cgi-bin/leds?color=FF0000" $auth
    has "leds red" "$BODY" '"color":"FF0000","secondary_color":"000000","pulse":"0"'
    # shellcheck disable=SC2086
    get cgi-bin/status $auth
    check "status: led_color red" "$(field led_color) $(field led_pulse)" "FF0000 0"
    sleep 2
    # shellcheck disable=SC2086
    get "cgi-bin/leds?color=00FF00&pulse=1&color2=000000&speed=700" $auth
    has "leds green pulse" "$BODY" '"pulse":"1"'
    # shellcheck disable=SC2086
    get "cgi-bin/leds?color=FFFF00&nomemory=1" $auth
    has "leds yellow, not remembered" "$BODY" '"no_memory":"1"'
    # shellcheck disable=SC2086
    get cgi-bin/status $auth
    check "status: still the green pulse" "$(field led_color) $(field led_pulse)" "00FF00 1"
    sleep 2
    # shellcheck disable=SC2086
    get "cgi-bin/leds?color=00FF00&pulse=1" $auth
    # shellcheck disable=SC2086
    get "cgi-bin/leds?color=red" $auth
    has "leds: a bad colour refused" "$BODY" '"return":"1"'

    # The ears, within one turn (0 to 16).
    # shellcheck disable=SC2086
    get "cgi-bin/ears?left=4&right=4" $auth
    check "ears to 4, after recalibrating" "$BODY" '{"left":"4","right":"4","return":"0"}'
    sleep 5
    # shellcheck disable=SC2086
    get "cgi-bin/ears?left=0&right=0&noreset=1" $auth
    check "ears to 0, without recalibrating" "$BODY" '{"left":"0","right":"0","return":"0"}'
    sleep 5
    # shellcheck disable=SC2086
    get "cgi-bin/ears?left=17&right=" $auth
    has "ears: a missing position refused" "$BODY" '"return":"1"'
    # shellcheck disable=SC2086
    get cgi-bin/ears_random $auth
    left=$(field left)
    right=$(field right)
    case "$left $right" in
    [0-9]\ [0-9] | [0-9]\ 1[0-6] | 1[0-6]\ [0-9] | 1[0-6]\ 1[0-6]) pass "ears_random: $left and $right" ;;
    *) fail "ears_random: '$BODY'" ;;
    esac
    sleep 6
    # shellcheck disable=SC2086
    get cgi-bin/ears_reset $auth
    check "ears_reset" "$BODY" '{"return":"0"}'
    sleep 6

    # A tone, then stop.
    # shellcheck disable=SC2086
    get "cgi-bin/sound?id=boot-ready" $auth
    check "sound: the boot-ready tone" "$BODY" '{"return":"0"}'
    sleep 1
    # shellcheck disable=SC2086
    get "cgi-bin/sound_control?cmd=stop" $auth
    check "sound_control stop" "$BODY" '{"return":"0","cmd":"stop"}'
    # shellcheck disable=SC2086
    get "cgi-bin/sound?url=http%3A%2F%2Fh%2F%3A%3Ax" $auth
    has "sound: a URL with :: refused" "$BODY" '"return":"1"'

    # Speech, when a speech server is set.
    # shellcheck disable=SC2086
    get cgi-bin/lapinlibre/status $auth
    speech=$(field speech_server)
    if [ -n "$speech" ]; then
        # shellcheck disable=SC2086
        get "cgi-bin/tts?text=Hello%20from%20LapinLibre" $auth
        has "tts: spoken" "$BODY" '"played":true'
        sleep 5
    else
        echo "SKIP  tts: no speech server is set"
    fi

    # The talking clock, when a speech server is set.
    if [ -n "$speech" ]; then
        # shellcheck disable=SC2086
        get "cgi-bin/apps/clock?hour=7" $auth
        check "apps/clock" "$BODY" '{"return":"0","hour":"7"}'
        sleep 5
    else
        echo "SKIP  apps/clock: no speech server is set"
    fi

    # Tag learning's start and stop: an orange pulse, then the green pulse again.
    # shellcheck disable=SC2086
    get cgi-bin/rfid_start_record $auth
    check "rfid_start_record" "$BODY" '{"return":"0"}'
    sleep 3
    # shellcheck disable=SC2086
    get cgi-bin/rfid_stop_record $auth
    check "rfid_stop_record" "$BODY" '{"return":"0"}'
    # shellcheck disable=SC2086
    get cgi-bin/status $auth
    check "status: the green pulse after learning" "$(field led_color) $(field led_pulse)" "00FF00 1"

    # The camera: a picture taken, listed, fetched, viewed live, then all deleted.
    # shellcheck disable=SC2086
    get cgi-bin/snapshot $auth
    name=$(field filename)
    thumb=$(field thumb)
    case $name in snapshot_*.jpg) pass "snapshot: $name" ;; *) fail "snapshot: '$BODY'" ;; esac
    # shellcheck disable=SC2086
    get cgi-bin/snapshot_list $auth
    has "snapshot_list" "$BODY" "{\"id\":\"$name\"}"
    curl -s -m 30 -u "admin:$PASS" -D "$W/headers" -o "$W/picture" "$U/cgi-bin/snapshot_get?filename=$name"
    check "snapshot_get: a JPEG" "$(head -c 2 "$W/picture" | od -An -tx1 | tr -d ' ')" ffd8
    has "snapshot_get: its type" "$(tr -d '\r' < "$W/headers")" "Content-Type: image/jpeg"
    if [ -n "$thumb" ]; then
        curl -s -m 30 -u "admin:$PASS" -o "$W/thumb" "$U/cgi-bin/snapshot_get?filename=$thumb"
        check "snapshot_get: a GIF thumbnail" "$(head -c 3 "$W/thumb")" GIF
    else
        fail "snapshot: no thumbnail"
    fi
    sleep 2
    curl -s -m 30 -u "admin:$PASS" -D "$W/headers" -o "$W/picture" "$U/cgi-bin/snapshot_view"
    check "snapshot_view: a JPEG" "$(head -c 2 "$W/picture" | od -An -tx1 | tr -d ' ')" ffd8
    # shellcheck disable=SC2086
    get "cgi-bin/lapinlibre/snapshot_delete?filename=$name" $auth
    check "lapinlibre/snapshot_delete" "$BODY" "{\"return\":\"0\",\"filename\":\"$name\"}"
    # shellcheck disable=SC2086
    get cgi-bin/snapshot_list $auth
    hasnt "snapshot_list: the deleted picture" "$BODY" "\"$name\""
    # shellcheck disable=SC2086
    get cgi-bin/clear_snapshots $auth
    check "clear_snapshots" "$BODY" '{"return":"0"}'
    # shellcheck disable=SC2086
    get cgi-bin/snapshot_list $auth
    check "snapshot_list: empty" "$BODY" '{"snapshots":[],"return":"0"}'

    # Sleep, what it refuses, and waking up.
    # shellcheck disable=SC2086
    get cgi-bin/sleep $auth
    check "sleep" "$BODY" '{"return":"0"}'
    # shellcheck disable=SC2086
    get cgi-bin/status $auth
    check "status: asleep" "$(field sleep)" 1
    # shellcheck disable=SC2086
    get "cgi-bin/leds?color=FF0000" $auth
    has "leds while asleep: refused" "$BODY" "asleep"
    # shellcheck disable=SC2086
    get cgi-bin/sleep $auth
    has "sleep while asleep: refused" "$BODY" "already asleep"
    sleep 3
    # shellcheck disable=SC2086
    get "cgi-bin/wakeup?silent=1" $auth
    check "wakeup" "$BODY" '{"return":"0","silent":"1"}'
    # shellcheck disable=SC2086
    get cgi-bin/status $auth
    check "status: awake again, green pulse" "$(field sleep) $(field led_color) $(field led_pulse)" "0 00FF00 1"
fi

# 7. A real tag, only when asked: someone lays it on the nose when told.
# wait_for TEXT SECONDS: waits for TEXT in the newest lines of the system log.
wait_for() {
    i=0
    while [ $i -lt "$2" ]; do
        tail -n 100 /var/log/messages 2> /dev/null | grep -qF "$1" && return 0
        sleep 1
        i=$((i + 1))
    done
    return 1
}
if [ $TAG = 1 ]; then
    if [ ! -x "$W/tag_listener" ]; then
        fail "tag: no scripts/tag_listener beside the www dir"
    else
        "$W/tag_listener" &
        LISTENER=$!
        sleep 2
        # shellcheck disable=SC2086
        get "cgi-bin/wakeup?silent=1" $auth
        # shellcheck disable=SC2086
        get cgi-bin/rfid_start_record $auth
        check "tag: learning started" "$BODY" '{"return":"0"}'
        echo "ACTION: lay the tag on the rabbit's nose now (within 60 s), then lift it off."
        tag=""
        i=0
        while [ $i -lt 60 ] && [ -z "$tag" ]; do
            sleep 1
            i=$((i + 1))
            # shellcheck disable=SC2086
            get cgi-bin/rfid_list $auth
            tag=$(printf '%s' "$BODY" | sed -n 's/.*"id":"\([0-9A-F]\{16\}\)".*/\1/p')
        done
        if [ -n "$tag" ]; then
            pass "tag: learned $tag"
        else
            fail "tag: no tag learned within 60 s"
        fi
        # shellcheck disable=SC2086
        get cgi-bin/rfid_stop_record $auth
        check "tag: learning stopped" "$BODY" '{"return":"0"}'
        if [ -n "$tag" ]; then
            # shellcheck disable=SC2086
            get "cgi-bin/rfid_assign_karotz?tag=$tag&name=Test&url=$(printf '%s' '/cgi-bin/leds?color=FF0000' | busybox base64)" $auth
            check "tag: a rabbit action given" "$BODY" '{"return":"0"}'
            sleep 3
            echo "ACTION: lay the tag on the nose again now (within 30 s): the LED should go red; then lift it off."
            if wait_for "tag $tag: leds done" 30; then pass "tag: its action ran"; else fail "tag: its action didn't run"; fi
            # shellcheck disable=SC2086
            get cgi-bin/status $auth
            check "tag: the LED is red" "$(field led_color)" FF0000
            # shellcheck disable=SC2086
            get "cgi-bin/rfid_assign_url?tag=$tag&url=http%3A%2F%2F127.0.0.1%3A$PORT%2Fcgi-bin%2Fget_version&user=admin&password=$PASS" $auth
            check "tag: a URL request given" "$BODY" '{"return":"0"}'
            sleep 3
            echo "ACTION: lay the tag on the nose a last time now (within 30 s), then lift it off."
            if wait_for "tag $tag: request to 127.0.0.1: HTTP 200" 30; then pass "tag: its request was sent"; else fail "tag: no request sent"; fi
            # shellcheck disable=SC2086
            get "cgi-bin/rfid_delete?tag=$tag" $auth
            check "tag: forgotten" "$BODY" "{\"return\":\"0\",\"tag\":\"$tag\"}"
        fi
        kill "$LISTENER" 2> /dev/null
        # shellcheck disable=SC2086
        get "cgi-bin/leds?color=00FF00&pulse=1&color2=000000&speed=700" $auth
    fi
fi

# 8. The reboot itself, only when asked.
if [ $REBOOT = 1 ]; then
    # shellcheck disable=SC2086
    get cgi-bin/reboot $auth
    check "reboot: answered" "$BODY" '{"return":"0"}'
    echo "The rabbit restarts in about 10 s."
fi

kill "$HTTPD" 2> /dev/null
rm -rf "$W"
exit $failed
