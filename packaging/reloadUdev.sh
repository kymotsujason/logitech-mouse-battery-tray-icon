#!/bin/sh
# a failing udevadm, as in a container or a chroot, must never fail the package operation
udevadm control --reload-rules || true
udevadm trigger --subsystem-match=hidraw --action=change || true
