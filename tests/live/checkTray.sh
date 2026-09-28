#!/usr/bin/env bash
# runs the real tray code against fake devices and services on private D-Bus buses with its own folders,
# which checks the theme, watcher, menu, UPower, notification, lock, and trayless paths without touching the session
set -u

here=$(cd "$(dirname "$0")" && pwd)
work=$(mktemp -d /tmp/trayLive.XXXXXX)
unit="tray-live-check-$$"
pids=()
failures=0

cleanup() {
    for name in "$unit" "$unit-second" "$unit-trayless"; do
        systemctl --user stop "$name" 2>/dev/null
        systemctl --user reset-failed "$name" 2>/dev/null
    done
    for pid in "${pids[@]}"; do
        kill "$pid" 2>/dev/null
    done
    rm -rf "$work"
}
trap cleanup EXIT

check() {
    if [ "$2" = "$3" ]; then
        echo "PASS $1"
    else
        echo "FAIL $1: expected '$3', got '$2'"
        failures=$((failures + 1))
    fi
}

waitForLine() {
    for i in $(seq 50); do
        if grep -q "$2" "$1"; then
            grep "$2" "$1" | tail -1
            return 0
        fi
        sleep 0.2
    done
    return 1
}

waitForMore() {
    # waits for a line matching $2 beyond the $3 already in $1, since earlier matches are stale
    for i in $(seq 50); do
        if [ "$(grep -c "$2" "$1")" -gt "$3" ]; then
            grep "$2" "$1" | tail -1
            return 0
        fi
        sleep 0.2
    done
    return 1
}

waitForIconColor() {
    # right after a start the item can still be the empty startup outline, thus this waits past a registered
    # or newIcon line for the one whose fill actually matches, instead of trusting whichever line comes first
    for i in $(seq 50); do
        line=$(grep -E " registered | newIcon " "$1" | awk -v c="$2" '$6 == c {print; exit}')
        if [ -n "$line" ]; then
            echo "$line"
            return 0
        fi
        sleep 0.2
    done
    grep -E " registered | newIcon " "$1" | tail -1 >&2
    return 1
}

waitForOutput() {
    local expected=$1
    shift
    local output=""
    for i in $(seq 50); do
        output=$("$@" 2>/dev/null)
        if [ "$output" = "$expected" ]; then
            break
        fi
        sleep 0.2
    done
    printf '%s' "$output"
}

textColor() {
    kreadconfig6 --file "/usr/share/color-schemes/$1.colors" --group Colors:Window --key ForegroundNormal
}

# read once up front, thus an empty read fails the color checks instead of matching an empty result
darkColor=$(textColor BreezeDark)
lightColor=$(textColor BreezeLight)

startBus() {
    # no servicedir, thus nothing gets started on demand on these buses, such as a second kded6 or portal
    cat > "$work/$1.conf" <<EOF
<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:path=$work/$1</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
EOF
    dbus-daemon --config-file="$work/$1.conf" --fork --print-address=1 --print-pid=1 > "$work/$1.info"
    pids+=("$(sed -n 2p "$work/$1.info")")
}

startWatcher() {
    python3 "$here/fakeWatcher.py" "$session" "$1" > "$work/watcher$1.log" 2>&1 &
    watcherPid=$!
    pids+=("$watcherPid")
    waitForLine "$work/watcher$1.log" "owns the watcher name" > /dev/null
}

mkdir -p "$work/config" "$work/cache" "$work/state" "$work/data" "$work/runtime"
chmod 700 "$work/runtime"
startBus session
startBus system
session=$(sed -n 1p "$work/session.info")
system=$(sed -n 1p "$work/system.info")
echo 81 > "$work/percent"
echo 55 > "$work/upowerPercent"
# the installed app holds its lock in the real runtime folder, thus the test copy gets its own
privateEnv=(-E "DBUS_SESSION_BUS_ADDRESS=$session" -E "DBUS_SYSTEM_BUS_ADDRESS=$system" -E "XDG_CONFIG_HOME=$work/config" -E "XDG_CACHE_HOME=$work/cache" -E "XDG_STATE_HOME=$work/state" -E "XDG_DATA_HOME=$work/data" -E "XDG_RUNTIME_DIR=$work/runtime")
appEnv=("${privateEnv[@]}" -E QT_QPA_PLATFORM=offscreen -E QT_QPA_PLATFORMTHEME=kde -E "LIVE_PERCENT_FILE=$work/percent")

applyScheme() {
    systemd-run --user --collect --wait --quiet "${privateEnv[@]}" plasma-apply-colorscheme "$1" > /dev/null
}

python3 "$here/fakeNotifications.py" "$session" dontOpen > "$work/notifications.log" 2>&1 &
pids+=($!)
python3 "$here/fakeUPower.py" "$system" "$work/upowerPercent" > "$work/upower.log" 2>&1 &
pids+=($!)
waitForLine "$work/notifications.log" "owns the notifications name" > /dev/null
waitForLine "$work/upower.log" "owns the upower name" > /dev/null

applyScheme BreezeDark
startWatcher A
systemd-run --user --quiet --unit="$unit" "${appEnv[@]}" python3 "$here/runTray.py"

check "the icon shows the dark theme's color soon after startup" "$(waitForIconColor "$work/watcherA.log" "$darkColor" | awk '{print $6}')" "${darkColor:-no color read}"
line=$(waitForLine "$work/watcherA.log" " registered ")
item=$(echo "$line" | awk '{print $4}')

check "the tooltip lists the HID++ mouse and the UPower mouse" "$(waitForOutput "MX Master 3, 55%|PRO X3 SUPERSTRIKE, 81%" python3 "$here/itemTool.py" "$session" "$item" tooltip)" "MX Master 3, 55%|PRO X3 SUPERSTRIKE, 81%"

echo 54 > "$work/upowerPercent"
check "a UPower property change reaches the tooltip" "$(waitForOutput "MX Master 3, 54%|PRO X3 SUPERSTRIKE, 81%" python3 "$here/itemTool.py" "$session" "$item" tooltip)" "MX Master 3, 54%|PRO X3 SUPERSTRIKE, 81%"

echo 10 > "$work/percent"
python3 "$here/itemTool.py" "$session" "$item" menuOpened
check "opening the menu reads the HID++ mouse" "$(waitForOutput "MX Master 3, 54%|PRO X3 SUPERSTRIKE, 10%" python3 "$here/itemTool.py" "$session" "$item" tooltip)" "MX Master 3, 54%|PRO X3 SUPERSTRIKE, 10%"
check "a reading at 10% sends a low battery notice" "$(waitForLine "$work/notifications.log" "Mouse battery low")" "notify Mouse battery low|PRO X3 SUPERSTRIKE has 10% left.|"
check "the low battery notice is sent once" "$(grep -c "Mouse battery low" "$work/notifications.log")" "1"

systemd-run --user --quiet --unit="$unit-second" "${appEnv[@]}" python3 "$here/runTray.py"
check "a second copy says it's already running" "$(waitForLine "$work/notifications.log" "already running")" "notify Mouse Battery|Mouse Battery is already running.|"
sleep 1
check "the second copy exits" "$(systemctl --user show "$unit-second" -p SubState --value)" "dead"

iconsBefore=$(grep -c " newIcon " "$work/watcherA.log")
applyScheme BreezeLight
line=$(waitForMore "$work/watcherA.log" " newIcon " "$iconsBefore")
check "a theme switch pushes an icon in the light theme's color" "$(echo "$line" | awk '{print $6}')" "${lightColor:-no color read}"

# this is what a kded6 restart does to the tray watcher
kill "$watcherPid"
wait "$watcherPid" 2>/dev/null
startWatcher B
line=$(waitForLine "$work/watcherB.log" " registered ")
check "the icon registers with a new watcher" "$(echo "$line" | awk '{print $6}')" "${lightColor:-no color read}"
sleep 2
check "the app keeps running after the watcher comes back" "$(systemctl --user show "$unit" -p SubState --value)" "running"

systemctl --user stop "$unit"
kill "$watcherPid"
wait "$watcherPid" 2>/dev/null
systemd-run --user --quiet --unit="$unit-trayless" "${appEnv[@]}" -E LIVE_NOTICE_MS=1000 python3 "$here/runTray.py"
check "a session with no tray gets the notice with its way out" "$(waitForLine "$work/notifications.log" "needs a system tray")" "notify Mouse Battery|Mouse Battery needs a system tray with StatusNotifierItem support to show its icon.|dontOpen:Don't Open at Login"
waitForLine "$work/notifications.log" "pressed dontOpen" > /dev/null
sleep 1
check "Don't Open at Login writes the override" "$(grep -c "Hidden=true" "$work/config/autostart/logitech-mouse-battery.desktop" 2>/dev/null)" "1"
check "Don't Open at Login quits the app" "$(systemctl --user show "$unit-trayless" -p SubState --value)" "dead"

if [ "$failures" -gt 0 ]; then
    exit 1
fi
