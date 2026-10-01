#!/usr/bin/env bash
# runs inside archlinux:latest with the repo at /repo: installs the 1.0.1 package in $1, starts its tray against a fake
# tray host on a private bus, upgrades to the package in $2 with pacman, and checks the same process restarts into
# 1.1.0. The tooltip tells the versions apart, since only 1.1.0 reads through the service, which the private bus lacks
set -u

failures=0

check() {
    if [ "$2" = "$3" ]; then
        echo "PASS $1"
    else
        echo "FAIL $1: expected '$3', got '$2'"
        failures=$((failures + 1))
    fi
}

if ! pacman -Syu --noconfirm --needed python python-pyqt6 python-gobject dbus xorg-server-xvfb > /tmp/pacman.log 2>&1; then
    echo "FAIL the test's own packages install"
    tail -20 /tmp/pacman.log
    exit 1
fi
if ! pacman -U --noconfirm "$1" > /tmp/install1.log 2>&1; then
    echo "FAIL 1.0.1 installs"
    cat /tmp/install1.log
    exit 1
fi
echo "installed $(logitech-mouse-battery --version)"

Xvfb :99 -nolisten tcp > /tmp/xvfb.log 2>&1 &
sleep 1
mkdir -p /tmp/runtime /tmp/config
chmod 700 /tmp/runtime
cat > /tmp/bus.conf <<EOF
<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:path=/tmp/bus</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
EOF
dbus-daemon --config-file=/tmp/bus.conf --fork --print-address=1 --print-pid=1 > /tmp/busInfo
address=$(sed -n 1p /tmp/busInfo)
python3 /repo/tests/fakes/fakeWatcher.py "$address" A > /tmp/watcher.log 2>&1 &
sleep 1

env DISPLAY=:99 QT_QPA_PLATFORM=xcb DBUS_SESSION_BUS_ADDRESS="$address" DBUS_SYSTEM_BUS_ADDRESS="$address" XDG_RUNTIME_DIR=/tmp/runtime XDG_CONFIG_HOME=/tmp/config logitech-mouse-battery > /tmp/tray.log 2>&1 &
trayPid=$!

registered() {
    grep -c " registered " /tmp/watcher.log
}

state() {
    if kill -0 "$trayPid" 2>/dev/null; then
        echo running
    else
        echo gone
    fi
}

tooltip() {
    local item
    item=$(grep " registered " /tmp/watcher.log | tail -1 | awk '{print $4}')
    python3 /repo/tests/live/itemTool.py "$address" "$item" tooltip 2>&1
}

for i in $(seq 1 30); do
    [ "$(registered)" -ge 1 ] && break
    sleep 1
done
sleep 2
before=$(tooltip)
echo "before upgrade: $(registered) registered, pid $trayPid $(state), tooltip: $before"
check "1.0.1 doesn't show the text only 1.1.0 has" "$( [ "$before" != "Can't reach the battery service." ] && echo yes)" "yes"

if ! pacman -U --noconfirm "$2" > /tmp/install2.log 2>&1; then
    echo "FAIL the built package installs over 1.0.1"
    cat /tmp/install2.log
    exit 1
fi
echo "installed $(logitech-mouse-battery --version)"

for i in $(seq 1 30); do
    [ "$(registered)" -ge 2 ] && break
    sleep 1
done
sleep 2
check "the tray registered again after the upgrade" "$( [ "$(registered)" -ge 2 ] && echo yes)" "yes"
check "the same tray process kept running" "$(state)" "running"
check "the tray now shows a text only 1.1.0 has" "$(tooltip)" "Can't reach the battery service."
check "the upgrade made the service's user" "$(getent passwd logitech-mouse-battery > /dev/null && echo yes)" "yes"
check "afterInstall.sh skipped in the container and said so" "$(grep -c "Mouse Battery: skipped setting up the service, since this system isn't booted with systemd" /tmp/install2.log)" "1"

if [ "$failures" -gt 0 ]; then
    echo "--- tray output"
    cat /tmp/tray.log
    echo "--- watcher log"
    cat /tmp/watcher.log
    echo "--- upgrade output"
    cat /tmp/install2.log
    exit 1
fi
