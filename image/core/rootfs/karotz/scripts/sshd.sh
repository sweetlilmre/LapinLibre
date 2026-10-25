#!/bin/sh
# SPDX-License-Identifier: MIT
# SSH, for init to keep running: Dropbear in the foreground, public-key logins only. Dropbear makes
# its host keys on first use in /etc/dropbear; that is linked into /usr/etc/conf, which a USB-key
# install keeps, so the rabbit's host keys survive installs. With no authorized_keys (no install has
# set lapinlibre.txt's SSH_PUBKEY; it links to /usr/etc/conf) nobody could log in, so
# it waits.
if [ ! -s /usr/karotz/.ssh/authorized_keys ]; then
    exec sleep 2147483647
fi
mkdir -p /usr/etc/conf/dropbear
if [ ! -L /usr/etc/dropbear ]; then
    rm -rf /usr/etc/dropbear
    ln -s conf/dropbear /usr/etc/dropbear
fi
chmod 700 /usr/karotz/.ssh
chmod 600 /usr/karotz/.ssh/authorized_keys
exec /sbin/dropbear -F -E -s -R -p 22
