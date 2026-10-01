#!/bin/sh
# the group has to exist before the rule that names it lands, since a deb upgrade runs 1.0.1's postrm, which
# triggers udev, right after unpacking
set -e
echo 'u logitech-mouse-battery - "Mouse Battery service" - -' | systemd-sysusers --replace=/usr/lib/sysusers.d/logitech-mouse-battery.conf -
