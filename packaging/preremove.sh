#!/bin/sh
# deb passes remove and rpm passes 0 on a real removal, while both run this on an upgrade too
case "$1" in
    remove|0)
        systemctl stop logitech-mouse-battery.service 2>/dev/null || true
        ;;
esac
exit 0
