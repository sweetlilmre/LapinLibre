#!/bin/sh
# SPDX-License-Identifier: MIT
# The USB stick, for mdev's rule (/karotz/etc/mdev.conf): $MDEV is the disk or partition (uba,
# uba1, ...), $ACTION add or remove (add when unset, as during `mdev -s`).
# On add: mount the stick vfat on /mnt/usbkey, its first partition when the disk has one, else the
# whole disk; never twice. On remove: unmount it, only if /mnt/usbkey is mounted from this device.
# The stick's hooks (/usr/etc/usbkey.d/*, executable, in name order) run with `add` after a mount,
# with `remove` before an unmount and `removed` after it; each gets at most 10 s and a failing one
# is ignored. With no such directory nothing more is done.
PATH=/bin:/sbin:/usr/bin:/usr/sbin
export PATH
MNT=/mnt/usbkey
HOOKS=/usr/etc/usbkey.d

log() {
    echo "usbkey: $*"
    logger -t usbkey "$*" 2> /dev/null
}

hooks() { # hooks add|remove|removed
    [ -d "$HOOKS" ] || return 0
    for hook in "$HOOKS"/*; do
        [ -f "$hook" ] && [ -x "$hook" ] || continue
        timeout 10 "$hook" "$1" < /dev/null > /dev/null 2>&1
    done
    return 0
}

# The device /mnt/usbkey is mounted from, or nothing.
mounted_from() {
    while read -r dev dir rest; do
        [ "$dir" = "$MNT" ] && { echo "$dev"; return 0; }
    done < /proc/mounts
    return 0
}

[ -n "$MDEV" ] || exit 0
case "${ACTION:-add}" in
add)
    [ -n "$(mounted_from)" ] && exit 0
    disk=${MDEV%%[0-9]*}
    if [ -e "/sys/block/$disk/${disk}1" ]; then
        # The disk has a partition table: the first partition is the one to mount. Until its
        # node exists, its own event (which comes later) does it.
        dev=${disk}1
        [ -b "/dev/$dev" ] || exit 0
    else
        dev=$disk
    fi
    if mount -t vfat "/dev/$dev" "$MNT"; then
        log "mounted /dev/$dev on $MNT"
        hooks add
    else
        log "can't mount /dev/$dev on $MNT"
    fi
    ;;
remove)
    [ "$(mounted_from)" = "/dev/$MDEV" ] || exit 0
    hooks remove
    # The stick is gone: a busy mount point is detached lazily rather than left behind.
    umount "$MNT" 2> /dev/null || umount -l "$MNT"
    log "unmounted /dev/$MDEV from $MNT"
    hooks removed
    ;;
esac
exit 0
