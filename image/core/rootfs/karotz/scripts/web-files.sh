#!/bin/sh
# SPDX-License-Identifier: MIT
# The web user's files, made by root before httpd (inetd.sh) and the tag listener (S40lapinlibre)
# start: both run as the user web, which can't make them itself. Each is made only when missing,
# so this runs any number of times; the two may run at the same time.
# - the web API's state in RAM (/var/run/lapinlibre-web) and its data on the flash
#   (/usr/etc/conf/web), owned by web, mode 0700; data that an earlier build left owned by root is
#   given to web (once: only when the folder isn't web's);
# - the API's two locks in /var/lock, owned by web, mode 0600.
USER=web
RUN=/var/run/lapinlibre-web
DATA=/usr/etc/conf/web
LOCKS="/var/lock/lapinlibre-web.lock /var/lock/lapinlibre-tags.lock"

log() {
    logger -t web-files.sh "$*" 2> /dev/null
}

uid=$(id -u $USER 2> /dev/null)
if [ -z "$uid" ]; then
    log "there is no user $USER: the web API's files aren't made"
    exit 1
fi

# owned_by_web PATH: true when PATH's owner is web.
owned_by_web() {
    [ "$(ls -ldn "$1" 2> /dev/null | awk '{ print $3 }')" = "$uid" ]
}

for dir in $RUN $DATA; do
    mkdir -p $dir || log "$dir couldn't be made"
    if ! owned_by_web $dir; then
        chown -R -h $USER:$USER $dir && chmod 700 $dir || log "$dir couldn't be given to $USER"
    fi
done
for lock in $LOCKS; do
    [ -e $lock ] || : >> $lock
    if ! owned_by_web $lock; then
        chown $USER:$USER $lock && chmod 600 $lock || log "$lock couldn't be given to $USER"
    fi
done
exit 0
