#!/usr/bin/env bash
# checks the desktop entries, the udev rule, the service unit, and the AppStream data with each format's own
# validator, inside an Arch container, which has all four tools
set -u

repo=$(cd "$(dirname "$0")/.." && pwd)
if [ "${1:-}" != "--inside" ]; then
    docker run --rm -v "$repo:/repo:ro" archlinux:latest bash /repo/packaging/validate.sh --inside
    exit $?
fi

pacman -Syu --noconfirm --needed desktop-file-utils appstream python > /tmp/pacman.log 2>&1
status=$?
if [ "$status" -ne 0 ]; then
    echo "FAIL pacman -Syu: expected exit 0, got $status"
    cat /tmp/pacman.log
    exit 1
fi
failures=0

validate() {
    "$@" > /tmp/validate.log 2>&1
    local status=$?
    if [ "$status" -eq 0 ]; then
        echo "PASS $*"
    else
        echo "FAIL $*: expected exit 0, got $status"
        cat /tmp/validate.log
        failures=$((failures + 1))
    fi
}

validate desktop-file-validate /repo/packaging/logitech-mouse-battery.desktop
validate desktop-file-validate /repo/packaging/autostart/logitech-mouse-battery.desktop
validate udevadm verify --resolve-names=never /repo/packaging/70-logitech-mouse-battery.rules
validate systemd-analyze verify /repo/packaging/systemd/logitech-mouse-battery.service
validate appstreamcli validate --no-net /repo/packaging/io.github.kymotsujason.LogitechMouseBattery.metainfo.xml

if [ "$failures" -gt 0 ]; then
    exit 1
fi
