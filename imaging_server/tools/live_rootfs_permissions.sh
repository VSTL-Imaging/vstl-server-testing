#!/usr/bin/env bash

repair_live_rootfs_permissions() {
    local rootfs="$1"
    [[ -d "$rootfs" ]] || {
        echo "ERROR: rootfs not found for permission repair: $rootfs" >&2
        return 1
    }

    chown root:root "$rootfs"

    local path
    for path in bin sbin lib lib64 etc usr opt root var dev proc run sys tmp; do
        if [[ -e "$rootfs/$path" ]]; then
            chown -R root:root "$rootfs/$path"
        fi
    done

    for path in bin sbin lib lib64 etc usr usr/bin opt root var dev proc run sys; do
        if [[ -d "$rootfs/$path" ]]; then
            chmod 0755 "$rootfs/$path"
        fi
    done

    if [[ -d "$rootfs/tmp" ]]; then
        chmod 1777 "$rootfs/tmp"
    fi
    if [[ -d "$rootfs/var/tmp" ]]; then
        chmod 1777 "$rootfs/var/tmp"
    fi

    if [[ -e "$rootfs/etc/sudo.conf" ]]; then
        chown root:root "$rootfs/etc/sudo.conf"
        chmod 0644 "$rootfs/etc/sudo.conf"
    fi
    if [[ -e "$rootfs/etc/sudo_logsrvd.conf" ]]; then
        chown root:root "$rootfs/etc/sudo_logsrvd.conf"
        chmod 0644 "$rootfs/etc/sudo_logsrvd.conf"
    fi
    if [[ -e "$rootfs/etc/sudoers" ]]; then
        chown root:root "$rootfs/etc/sudoers"
        chmod 0440 "$rootfs/etc/sudoers"
    fi
    if [[ -d "$rootfs/etc/sudoers.d" ]]; then
        chown root:root "$rootfs/etc/sudoers.d"
        chmod 0750 "$rootfs/etc/sudoers.d"
        find "$rootfs/etc/sudoers.d" -type f -exec chown root:root {} + -exec chmod 0440 {} +
    fi
    if [[ -e "$rootfs/usr/bin/sudo" ]]; then
        chown root:root "$rootfs/usr/bin/sudo"
        chmod 4755 "$rootfs/usr/bin/sudo"
    fi

    if [[ -d "$rootfs/home/user" ]]; then
        chown -R 1000:1000 "$rootfs/home/user"
    fi
}
