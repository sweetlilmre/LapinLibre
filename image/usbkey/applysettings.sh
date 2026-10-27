#!/bin/bash
# SPDX-License-Identifier: MIT
# The key's settings (lapinlibre.txt) onto the rabbit: the Wi-Fi network,
# a fixed address or DHCP, the SSH key, the telnet choice, the HTTPS checks, the web page's password, the app server
# and the language, all in /usr/etc/conf, which installs keep.
# A value left empty keeps what the rabbit has.
#
#   applysettings.sh --check    check the file only (the install runs this before erasing)
#   applysettings.sh            check, then apply
#
# Runs on whichever system the rabbit has when the key's install starts, so only bash and the
# common tools. The file may come from Windows (CRLF line ends, a UTF-8 byte-order mark) and may
# quote its values. Never logs the Wi-Fi password or the web password.

source /mnt/usbkey/install-lib.sh

FILE=/mnt/usbkey/lapinlibre.txt
CONF=/usr/etc/conf
BOM=$(printf '\357\273\277')

WIFI_SSID= WIFI_PASSWORD= IP= NETMASK= GATEWAY= DNS= SSH_PUBKEY= TELNET= HTTPS_CHECK= WEB_PASSWORD=
SERVER= TTS_SERVER= ASR_SERVER= LANGUAGE= HOSTNAME=
problems=""
problem() { problems="${problems}${problems:+; }$1"; }

if [ -f "${FILE}" ]; then
    n=0
    while IFS= read -r line || [ -n "${line}" ]; do
        n=$((n + 1))
        line=${line%$'\r'}
        [ ${n} -eq 1 ] && line=${line#${BOM}}
        # Trim, and skip blank lines and comments.
        line="${line#"${line%%[![:space:]]*}"}"
        line="${line%"${line##*[![:space:]]}"}"
        case "${line}" in ''|'#'*) continue ;; esac
        case "${line}" in *=*) ;; *) problem "line ${n} has no =" ; continue ;; esac
        key=${line%%=*}
        value=${line#*=}
        key="${key%"${key##*[![:space:]]}"}"
        value="${value#"${value%%[![:space:]]*}"}"
        case "${value}" in \"*\") value=${value#\"}; value=${value%\"} ;; \'*\') value=${value#\'}; value=${value%\'} ;; esac
        case "${key}" in
        WIFI_SSID|WIFI_PASSWORD|IP|NETMASK|GATEWAY|DNS|SSH_PUBKEY|TELNET|HTTPS_CHECK|WEB_PASSWORD|SERVER|TTS_SERVER|ASR_SERVER|LANGUAGE|HOSTNAME)
            printf -v "${key}" '%s' "${value}" ;;
        *) problem "line ${n}: unknown setting ${key}" ;;
        esac
    done < "${FILE}"
fi

is_address() { [[ "$1" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; }

# The Wi-Fi network: both or neither; the characters wpa_supplicant's quoted strings can hold.
if [ -n "${WIFI_SSID}${WIFI_PASSWORD}" ]; then
    [ -n "${WIFI_SSID}" ] || problem "WIFI_PASSWORD is set but WIFI_SSID is empty"
    [ -n "${WIFI_PASSWORD}" ] || problem "WIFI_SSID is set but WIFI_PASSWORD is empty"
    case "${WIFI_SSID}${WIFI_PASSWORD}" in *\"*|*\\*) problem "the Wi-Fi name and password can't contain \" or \\" ;; esac
    [ ${#WIFI_PASSWORD} -ge 8 ] && [ ${#WIFI_PASSWORD} -le 63 ] || [ -z "${WIFI_PASSWORD}" ] || problem "WIFI_PASSWORD must be 8 to 63 characters (WPA)"
elif [ ! -s "${CONF}/wpa.conf" ]; then
    problem "no Wi-Fi settings: fill in WIFI_SSID and WIFI_PASSWORD in lapinlibre.txt"
fi
if [ -n "${IP}" ]; then
    [ -n "${WIFI_SSID}" ] || problem "IP is set: give WIFI_SSID and WIFI_PASSWORD with it"
    for v in IP NETMASK GATEWAY DNS; do
        is_address "${!v}" || problem "${v} must be an address like 192.168.1.10 when IP is set"
    done
fi
if [ -n "${SSH_PUBKEY}" ] && ! [[ "${SSH_PUBKEY}" =~ ^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp[0-9]+)\ [A-Za-z0-9+/=]+(\ .*)?$ ]]; then
    problem "SSH_PUBKEY isn't one public key line (ssh-ed25519 AAAA... comment)"
fi
case "${TELNET}" in ''|yes|no) ;; *) problem "TELNET must be yes, no or empty" ;; esac
case "${HTTPS_CHECK}" in ''|yes|no) ;; *) problem "HTTPS_CHECK must be yes, no or empty" ;; esac
# The web password: printable ASCII without spaces (values are trimmed), and none of the
# characters the web server's configuration or the settings file give a meaning to.
web_password_ok() {
    local LC_ALL=C
    [ ${#1} -ge 8 ] && [ ${#1} -le 64 ] || return 1
    case "$1" in *[!!-~]*|*\"*|*\\*|*:*) return 1 ;; esac
    return 0
}
[ -z "${WEB_PASSWORD}" ] || web_password_ok "${WEB_PASSWORD}" ||
    problem "WEB_PASSWORD must be 8 to 64 characters, without spaces, \", \\ or :"
# The app server: a host name or address (SERVER), or a URL for speech and host:port for speech
# recognition when those run elsewhere.
is_host() { [[ "$1" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]]; }
[ -z "${SERVER}" ] || is_host "${SERVER}" || problem "SERVER must be a host name or address, like 192.168.1.10"
[ -z "${TTS_SERVER}" ] || [[ "${TTS_SERVER}" =~ ^https?://[A-Za-z0-9.-]+(:[0-9]+)?(/[A-Za-z0-9._/-]*)?$ ]] ||
    problem "TTS_SERVER must be a URL like http://192.168.1.10:8081"
[ -z "${ASR_SERVER}" ] || [[ "${ASR_SERVER}" =~ ^[A-Za-z0-9.-]+(:[0-9]+)?$ ]] ||
    problem "ASR_SERVER must be host or host:port, like 192.168.1.10:8443"
[ -z "${LANGUAGE}" ] || [[ "${LANGUAGE}" =~ ^[a-z][a-z]-[A-Z][A-Z]$ ]] || problem "LANGUAGE must look like en-US or fr-FR"
[ -z "${HOSTNAME}" ] || [[ "${HOSTNAME}" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]] ||
    problem "HOSTNAME must be lower-case letters, digits and dashes, like lapinlibre or kitchen-rabbit"

[ -z "${problems}" ] || ERROR "lapinlibre.txt: ${problems}"
[ "$1" = "--check" ] && { LOG "Settings: lapinlibre.txt is fine"; exit 0; }

mkdir -p "${CONF}"
if [ -n "${WIFI_SSID}" ]; then
    printf 'network={\nssid="%s"\npsk="%s"\nkey_mgmt=WPA-PSK\nscan_ssid=1\nproto=WPA RSN\n}\n' \
        "${WIFI_SSID}" "${WIFI_PASSWORD}" > "${CONF}/wpa.conf"
    chmod 600 "${CONF}/wpa.conf"
    # The address settings, read by /usr/scripts/waitfornetwork.sh: empty IP means DHCP.
    printf 'IP="%s"\nNETMASK="%s"\nGATEWAY="%s"\nDNS="%s"\n' "${IP}" "${NETMASK}" "${GATEWAY}" "${DNS}" > "${CONF}/network.conf"
    LOG "Settings: Wi-Fi network ${WIFI_SSID}, $([ -n "${IP}" ] && echo "address ${IP}" || echo DHCP)"
else
    LOG "Settings: Wi-Fi kept"
fi
if [ -n "${SSH_PUBKEY}" ]; then
    printf '%s\n' "${SSH_PUBKEY}" > "${CONF}/authorized_keys"
    chmod 600 "${CONF}/authorized_keys"
    LOG "Settings: SSH key ${SSH_PUBKEY%% *} ${SSH_PUBKEY##* }"
fi
if [ -n "${TELNET}" ]; then
    echo "${TELNET}" > "${CONF}/telnet"
    LOG "Settings: telnet ${TELNET}"
fi
# Read by karotzVM before each HTTPS request: "no" turns the certificate checks off.
if [ -n "${HTTPS_CHECK}" ]; then
    echo "${HTTPS_CHECK}" > "${CONF}/https_check"
    LOG "Settings: HTTPS checks ${HTTPS_CHECK}"
fi
# The web password as given; the rabbit hashes it at its next start (inetd.sh) and removes this.
# None given: no password, so any earlier one goes.
if [ -n "${WEB_PASSWORD}" ]; then
    rm -f "${CONF}/web_password.new"
    (umask 077 && printf '%s\n' "${WEB_PASSWORD}" > "${CONF}/web_password.new") &&
        chmod 600 "${CONF}/web_password.new" && mv -f "${CONF}/web_password.new" "${CONF}/web_password" ||
        ERROR "can't write the web password"
    LOG "Settings: web password set"
else
    rm -f "${CONF}/web_password" "${CONF}/web_password.new" "${CONF}/web_password.hash"
    LOG "Settings: no web password"
fi

# KEY=VALUE in [SECTION] of an ini file, replacing the key's line or adding it (and the section).
set_ini() { # file section key value
    [ -f "$1" ] || : > "$1"
    awk -v s="[$2]" -v k="$3" -v v="$4" '
        $0 == s { print; in_s = 1; next }
        /^\[/ { if (in_s && !done) { print k "=" v; done = 1 } in_s = 0 }
        in_s && index($0, k "=") == 1 { if (!done) { print k "=" v; done = 1 } next }
        { print }
        END { if (!done) { if (!in_s) print s; print k "=" v } }' "$1" > "$1.new" && mv -f "$1.new" "$1"
}
# The app server (tools/app-server: the platform on 9123, apps on 8080, speech on 8081, speech
# recognition on 8443), read by the controller (karotz.conf) and voice-daemon (voice.conf).
if [ -n "${SERVER}" ]; then
    set_ini "${CONF}/karotz.conf" karotz ip_server "${SERVER}"
    set_ini "${CONF}/karotz.conf" karotz port_server 9123
    set_ini "${CONF}/karotz.conf" karotz url_app "http://${SERVER}:8080"
    set_ini "${CONF}/karotz.conf" karotz domain_app "${SERVER}"
    # The controller reboots the rabbit when the update server offers newer versions: a closed
    # port on the rabbit itself makes that check fail at once.
    set_ini "${CONF}/karotz.conf" karotz url_update "http://127.0.0.1:1"
    LOG "Settings: app server ${SERVER}"
fi
# The controller's platform account, whatever the app server: any values, but both set.
[ -s "${CONF}/account.conf" ] || printf '[account]\nid=lapinlibre\npwd=lapinlibre\n' > "${CONF}/account.conf"
TTS_URL=${TTS_SERVER:-${SERVER:+http://${SERVER}:8081}}
if [ -n "${TTS_URL}" ]; then
    # voice-daemon looks a language's server up by its first two letters.
    for l in en fr de es it nl pt; do set_ini "${CONF}/voice.conf" tts "${l}" "${TTS_URL%/}/${l}"; done
    LOG "Settings: speech ${TTS_URL}"
fi
ASR=${ASR_SERVER:-${SERVER:+${SERVER}:8443}}
if [ -n "${ASR}" ]; then
    host=${ASR%%:*}; port=8443
    case "${ASR}" in *:*) port=${ASR##*:} ;; esac
    set_ini "${CONF}/voice.conf" asr nbserver 1
    set_ini "${CONF}/voice.conf" asr server0 "${host}"
    set_ini "${CONF}/voice.conf" asr port "${port}"
    LOG "Settings: speech recognition ${host}:${port}"
fi
# The rabbit's name, which linuxrc sets and mDNS answers to (<name>.local).
if [ -n "${HOSTNAME}" ]; then
    echo "${HOSTNAME}" > "${CONF}/hostname"
    LOG "Settings: name ${HOSTNAME}"
fi
if [ -n "${LANGUAGE}" ]; then
    set_ini "${CONF}/karotz.conf" karotz lang "${LANGUAGE}"
    LOG "Settings: language ${LANGUAGE}"
fi
exit 0
