#!/bin/sh
# deb passes remove or purge and rpm passes 0 on a real removal, while both run this on an upgrade too
case "$1" in
    remove|purge|0) ;;
    *) exit 0 ;;
esac
if systemd-detect-virt --quiet --chroot || ! systemd-notify --booted; then
    exit 0
fi
systemctl daemon-reload || true
systemctl reload dbus.service || true
udevadm control --reload || true
udevadm trigger --subsystem-match=hidraw --action=change || true
exit 0
