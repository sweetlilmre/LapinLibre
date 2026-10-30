#!/bin/sh
# SPDX-License-Identifier: MIT
# SSH, for init to keep running: Dropbear in the foreground, public-key logins only. Its host keys
# are in /etc/dropbear; that is linked into /usr/etc/conf, which a USB-key install keeps, so the
# rabbit's host keys survive installs. Missing host keys are made here, before Dropbear starts. With
# no authorized_keys (no install has set lapinlibre.txt's SSH_PUBKEY; it links to /usr/etc/conf)
# nobody could log in, so it waits.
#
# The keys' public parts go to /var/run/ssh for the web API (which runs as the user web and can't
# read the key files): authorized_keys, a copy of the authorised keys, and host_keys, each host key's
# public line. Both are made again at each start.
AUTHORIZED=/usr/karotz/.ssh/authorized_keys
KEYS=/usr/etc/conf/dropbear
PUBLIC=/var/run/ssh
KEY_TYPES="rsa ecdsa ed25519"

# public_keys: the public parts in PUBLIC, readable by everyone.
public_keys() {
    mkdir -p $PUBLIC && chmod 755 $PUBLIC || return 1
    if [ -s $AUTHORIZED ]; then
        cat $AUTHORIZED > $PUBLIC/authorized_keys.new
    else
        : > $PUBLIC/authorized_keys.new
    fi
    : > $PUBLIC/host_keys.new
    for f in $KEYS/dropbear_*_host_key; do
        [ -f "$f" ] || continue
        dropbearkey -y -f "$f" 2> /dev/null | grep -E '^(ssh-|ecdsa-)' >> $PUBLIC/host_keys.new
    done
    for name in authorized_keys host_keys; do
        chmod 644 $PUBLIC/$name.new && mv -f $PUBLIC/$name.new $PUBLIC/$name
    done
}

public_keys
if [ ! -s $AUTHORIZED ]; then
    exec sleep 2147483647
fi
mkdir -p $KEYS
if [ ! -L /usr/etc/dropbear ]; then
    rm -rf /usr/etc/dropbear
    ln -s conf/dropbear /usr/etc/dropbear
fi
made=""
for type in $KEY_TYPES; do
    if [ ! -f $KEYS/dropbear_${type}_host_key ]; then
        dropbearkey -t $type -f $KEYS/dropbear_${type}_host_key > /dev/null 2>&1 && made=1
    fi
done
[ -n "$made" ] && public_keys
chmod 700 /usr/karotz/.ssh
chmod 600 $AUTHORIZED
exec /sbin/dropbear -F -E -s -R -p 22
