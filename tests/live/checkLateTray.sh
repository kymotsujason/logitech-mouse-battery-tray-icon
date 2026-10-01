#!/usr/bin/env bash
# checks on Qt 6.4 that the tray icon registers when the StatusNotifier host shows up late,
# with a control that builds the tray icon early, the way Qt 6.4's cached tray check breaks it
set -u

here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
if [ "${1:-}" != "--inside" ]; then
    docker run --rm -v "$repo:/repo:ro" ubuntu:24.04 bash /repo/tests/live/checkLateTray.sh --inside
    exit $?
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq > /dev/null
apt-get install -y -qq --no-install-recommends python3-pyqt6 python3-gi gir1.2-glib-2.0 dbus-daemon xvfb xauth > /dev/null 2>&1
failures=0

check() {
    if [ "$2" = "$3" ]; then
        echo "PASS $1"
    else
        echo "FAIL $1: expected '$3', got '$2'"
        failures=$((failures + 1))
    fi
}

cp -r /repo /tmp/app
Xvfb :99 -nolisten tcp > /tmp/xvfb.log 2>&1 &
sleep 1

runCase() {
    local name=$1
    local early=$2
    mkdir -p "/tmp/$name/runtime" "/tmp/$name/config"
    chmod 700 "/tmp/$name/runtime"
    cat > "/tmp/$name/bus.conf" <<EOF
<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:path=/tmp/$name/bus</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
EOF
    dbus-daemon --config-file="/tmp/$name/bus.conf" --fork --print-address=1 --print-pid=1 > "/tmp/$name/busInfo"
    local address
    address=$(sed -n 1p "/tmp/$name/busInfo")
    python3 /tmp/app/tests/fakes/fakeWatcher.py "$address" A 5 > "/tmp/$name/watcher.log" 2>&1 &
    local watcherPid=$!
    sleep 1
    env -u QT_QPA_PLATFORMTHEME DISPLAY=:99 QT_QPA_PLATFORM=xcb DBUS_SESSION_BUS_ADDRESS="$address" DBUS_SYSTEM_BUS_ADDRESS="$address" XDG_RUNTIME_DIR="/tmp/$name/runtime" XDG_CONFIG_HOME="/tmp/$name/config" LIVE_EARLY_TRAY="$early" python3 -B /tmp/app/tests/live/runTray.py > "/tmp/$name/app.log" 2>&1 &
    local appPid=$!
    sleep 9
    # a crashed app never registers either, so both cases also report whether the app is still running
    local state=exited
    if kill -0 "$appPid" 2>/dev/null; then
        state=running
    fi
    kill "$appPid" "$watcherPid" "$(sed -n 2p "/tmp/$name/busInfo")" 2>/dev/null
    echo "$(grep -c " registered " "/tmp/$name/watcher.log") $state"
}

check "on Qt 6.4 the icon registers after a host that comes 5 s late" "$(runCase late "")" "1 running"
check "the control, a tray icon built before the host, never registers" "$(runCase early 1)" "0 running"

if [ "$failures" -gt 0 ]; then
    cat /tmp/late/app.log /tmp/early/app.log
    exit 1
fi
