#!/bin/sh
# SPDX-License-Identifier: MIT
# inetd, for init to keep running: /etc/inetd.conf's services (the web page), plus telnet when the
# key's install set TELNET=yes (/usr/etc/conf/telnet): a root shell with no
# password, for testing only. With no service at all, inetd would exit at once and init restart it
# without end, so this waits instead.
#
# First it writes the web server's configuration in RAM, which inetd.conf's httpd line names (the
# root file system is read-only, so it can't hold a rabbit's own password). The install leaves the
# web password from lapinlibre.txt's WEB_PASSWORD in /usr/etc/conf/web_password; it is hashed here
# (SHA-512 crypt) into web_password.hash and the plain file removed, so the flash is written once
# per new password. The configuration takes clients from the local network only, and with a hash
# asks every request for the user admin and the password; without one there is no login. If it
# can't be written, it refuses everything, or is missing, which stops httpd answering. httpd runs as
# the user web (inetd.conf), so the configuration is root's and readable by the group web only
# (0640); web-files.sh makes the web user's files first.
RUN=/var/run/inetd.conf
WEB_FILES=/karotz/scripts/web-files.sh
HTTPD_CONF=/var/run/httpd.conf
HTTPD_FIXED=/karotz/etc/httpd.conf
PASSWORD=/usr/etc/conf/web_password
HASH=/usr/etc/conf/web_password.hash
WEB_USER=admin

log() {
    logger -t inetd.sh "$*" 2> /dev/null
}

$WEB_FILES

# web_readable FILE: FILE for the group web to read (httpd's configuration).
web_readable() {
    chgrp web "$1" && chmod 640 "$1"
}

# A SHA-512 crypt hash, as cryptpw makes it.
is_hash() {
    case $1 in
    '$6$'*'$'*) ;;
    *) return 1 ;;
    esac
    case ${1#???} in *[!./0-9A-Za-z\$]*) return 1 ;; esac
    [ ${#1} -le 120 ]
}

# A new password from the install: its hash replaces any earlier one, then the plain file goes. A
# password that can't be hashed, or a hash that isn't one, locks the page: no password at all is
# only when neither file is there.
locked=""
if [ -s $PASSWORD ]; then
    hash=$(busybox cryptpw -m sha512 -P 0 < $PASSWORD 2> /dev/null)
    if is_hash "$hash" && (umask 077 && printf '%s\n' "$hash" > $HASH.new) && mv -f $HASH.new $HASH; then
        rm -f $PASSWORD
        log "the new web password is in place"
    else
        rm -f $HASH.new $HASH
        locked=1
        log "the web password couldn't be hashed: the web page stays locked"
    fi
fi

hash=""
if [ -e $HASH ]; then
    read -r hash < $HASH
    if ! is_hash "$hash"; then
        hash=""
        locked=1
        log "the web password's hash is damaged: the web page is locked"
    fi
fi

web_conf() {
    (
        umask 077
        {
            echo "# The web server's rules, made by inetd.sh at boot."
            if [ -n "$locked" ]; then
                echo "D:*"
                exit 0
            fi
            for range in 127.0.0.1 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16; do
                echo "A:$range"
            done
            echo "D:*"
            # The fixed part's index and type lines; never its allow, deny or proxy rules.
            grep -E '^[IE.]' $HTTPD_FIXED
            if [ -n "$hash" ]; then
                echo "/:$WEB_USER:$hash"
            fi
        } > $HTTPD_CONF.new
    ) && web_readable $HTTPD_CONF.new && mv -f $HTTPD_CONF.new $HTTPD_CONF
}
if ! web_conf; then
    rm -f $HTTPD_CONF.new
    (umask 077 && echo "D:*" > $HTTPD_CONF) && web_readable $HTTPD_CONF || rm -f $HTTPD_CONF
    log "the web server's configuration couldn't be written: the web page is locked"
elif [ -z "$hash" ] && [ -z "$locked" ]; then
    log "no web password is set (WEB_PASSWORD in lapinlibre.txt): the web page has no login"
fi

grep -h '^[^#[:space:]]' /etc/inetd.conf > $RUN 2> /dev/null
if [ "$(cat /usr/etc/conf/telnet 2> /dev/null)" = yes ]; then
    echo "23 stream tcp nowait root /bin/busybox telnetd -i -l /bin/sh" >> $RUN
fi
if [ ! -s $RUN ]; then
    exec sleep 2147483647
fi
exec /sbin/inetd -f $RUN
