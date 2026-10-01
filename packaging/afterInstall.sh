#!/bin/sh
# runs after every install and upgrade, and always exits 0, since a failing deb postinst leaves the package half
# configured and every later apt run stops on it
UNIT=logitech-mouse-battery.service
APP_DIR=/usr/share/logitech-mouse-battery
REPLUG="Unplug the receiver or cable and plug it back in, or reboot."

# the same two checks Arch's own systemd hook makes, since udev applies the new rule from scratch when such a root boots
if systemd-detect-virt --quiet --chroot; then
    echo "Mouse Battery: skipped setting up the service, since this is a chroot"
    exit 0
fi
if ! systemd-notify --booted; then
    echo "Mouse Battery: skipped setting up the service, since this system isn't booted with systemd"
    exit 0
fi

step() {
    echo "Mouse Battery: $1"
    shift
    if ! "$@"; then
        echo "Mouse Battery: that step failed. $REPLUG"
    fi
}

# a unit nothing has started yet isn't loaded, and reset-failed fails on it with nothing to clear
clearFailedStarts() {
    systemctl reset-failed "$UNIT" 2>/dev/null || true
}

step "reloading systemd" systemctl daemon-reload
# the trigger below can start the service, so this waits until the bus holds the new policy
step "reloading the system bus" systemctl reload dbus.service
step "reloading the udev rules" udevadm control --reload
step "applying the rule to plugged in receivers and cables" udevadm trigger --subsystem-match=hidraw --action=change
step "waiting for udev" udevadm settle
# until the trigger settles a node can still carry the old uaccess tag, which would put its ACL back
step "closing the access 1.0.x gave" python3 -I -B "$APP_DIR/clearAccess.py"
step "clearing the service's failed starts" clearFailedStarts
# nothing after this depends on it, and a service that fails to start would fail a call that waits
step "restarting the service if it runs" systemctl try-restart --no-block "$UNIT"
exit 0
