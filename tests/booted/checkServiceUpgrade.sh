#!/usr/bin/env bash
# runs on a booted GitHub hosted runner with passwordless sudo, after checkService.sh: installs 1.1.0, lets a fake receiver
# made through uhid start its service, upgrades to the built version while that service runs, and checks that the
# upgrade hands the service and its bus name to a new process running the built version, which reads the fake mouse
set -u

repo=$(cd "$(dirname "$0")/../.." && pwd)
version=$(sed -n 's/^VERSION = "\([^"]*\)"$/\1/p' "$repo/src/version.py")
name=logitech-mouse-battery
unit=$name.service
service=io.github.kymotsujason.LogitechMouseBattery
objectPath=/io/github/kymotsujason/LogitechMouseBattery
interface=io.github.kymotsujason.LogitechMouseBattery1
work=$(mktemp -d)
fakePid=""
node=""
startedAt=0
failures=0

check() {
    if [ "$2" = "$3" ]; then
        echo "PASS $1"
    else
        echo "FAIL $1: expected '$3', got '$2'"
        failures=$((failures + 1))
    fi
}

stop() {
    echo "FAIL $1"
    failures=$((failures + 1))
    exit 1
}

cleanup() {
    if [ "$failures" -gt 0 ]; then
        echo "--- journal for $unit"
        sudo -n journalctl -u "$unit" --no-pager 2>/dev/null | tail -50
        for log in "$work"/*.log; do
            if [ -f "$log" ]; then
                echo "--- $(basename "$log")"
                cat "$log"
            fi
        done
    fi
    if [ -n "$fakePid" ]; then
        sudo kill "$fakePid" 2>/dev/null
    fi
    rm -rf "$work"
}
trap cleanup EXIT

startFake() {
    sudo python3 -B "$repo/tests/booted/fakeReceiver.py" > "$work/fake.log" 2>&1 &
    fakePid=$!
    node=""
    for i in $(seq 100); do
        for entry in /sys/class/hidraw/hidraw*; do
            if grep -qs "HID_ID=0003:0000046D:0000C54F" "$entry/device/uevent"; then
                node=/dev/$(basename "$entry")
            fi
        done
        if [ -n "$node" ] && [ -e "$node" ]; then
            break
        fi
        sleep 0.1
    done
    if [ -z "$node" ]; then
        stop "the fake receiver never got a hidraw node"
    fi
    sudo udevadm settle
}

mouseState() {
    local state=""
    for i in $(seq 150); do
        state=$(busctl --system --timeout=2 call "$service" "$objectPath" "$interface" GetState 2>&1)
        case "$state" in
            *02bc524c*) break ;;
        esac
        sleep 0.1
    done
    echo "$state" > "$work/state.log"
    echo "$state" | grep -c '\\"percent\\": 81'
}

mainPid() {
    systemctl show -p MainPID --value "$unit"
}

[ "$(ps -o comm= -p 1)" = "systemd" ] || stop "systemd isn't PID 1 here"
sudo -n true 2>/dev/null || stop "sudo asks for a password here"

# checkService.sh removes its package without purging it, so this starts from a system with none of it, and dpkg,
# unlike apt, ignores a package it has never seen
sudo dpkg --purge "$name" > "$work/purge.log" 2>&1 || stop "the earlier install didn't purge"
curl -fsSL -o "$work/old.deb" "https://github.com/kymotsujason/logitech-mouse-battery-tray-icon/releases/download/v1.1.0/logitech-mouse-battery_1.1.0_all.deb" || stop "couldn't download the 1.1.0 package"
sudo apt-get update -qq > /dev/null
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "$work/old.deb" > "$work/install1.log" 2>&1 || stop "1.1.0 didn't install"
check "1.1.0 installed" "$(logitech-mouse-battery --version)" "1.1.0"

if ! sudo modprobe uhid 2>/dev/null; then
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "linux-modules-extra-$(uname -r)" > "$work/modules.log" 2>&1
    sudo modprobe uhid || stop "couldn't load uhid"
fi
[ -e /dev/uhid ] || stop "/dev/uhid is missing after loading uhid"

startFake
active=""
for i in $(seq 100); do
    active=$(systemctl is-active "$unit")
    if [ "$active" = "active" ]; then
        break
    fi
    sleep 0.1
done
[ "$active" = "active" ] || stop "plugging the receiver in didn't start the 1.1.0 service"
check "the 1.1.0 service lists the fake mouse at 81%" "$(mouseState)" "1"
oldPid=$(mainPid)
[ "$oldPid" -gt 0 ] 2>/dev/null || stop "the 1.1.0 service has no main process"
# 1.1.0 logs no start line, so the line checked after the upgrade can only come from the built version
check "the 1.1.0 service logged no start line" "$(sudo journalctl _PID="$oldPid" -o cat --no-pager | grep -c '^Mouse Battery service .* started$')" "0"

sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "$repo/dist/${name}_${version}_all.deb" > "$work/install2.log" 2>&1 || stop "the built version didn't install over 1.1.0"
check "$version installed" "$(logitech-mouse-battery --version)" "$version"
# apt runs dpkg under a pseudo terminal, which ends every line the install scripts print with a carriage return
check "afterInstall.sh asked for the restart" "$(tr -d '\r' < "$work/install2.log" | grep -o 'Mouse Battery: .*' | grep -cx 'Mouse Battery: restarting the service if it runs')" "1"
check "no step failed" "$(grep -c 'that step failed' "$work/install2.log")" "0"

# the restart is queued with --no-block, so the new process can show up after apt returns
newPid=$oldPid
active=""
for i in $(seq 150); do
    newPid=$(mainPid)
    active=$(systemctl is-active "$unit")
    if [ "$active" = "active" ] && [ "$newPid" != "$oldPid" ] && [ "$newPid" != "0" ]; then
        break
    fi
    sleep 0.1
done
check "the upgrade restarted the service" "$active $( [ "$newPid" != "$oldPid" ] && [ "$newPid" != "0" ] && echo "in a new process")" "active in a new process"
check "the 1.1.0 process exited" "$( [ -e "/proc/$oldPid" ] && echo running || echo gone)" "gone"
check "the bus name belongs to the new process" "$(busctl --system --timeout=2 call org.freedesktop.DBus /org/freedesktop/DBus org.freedesktop.DBus GetConnectionUnixProcessID s "$service" 2>&1)" "u $newPid"
started=0
for i in $(seq 50); do
    started=$(sudo journalctl _PID="$newPid" -o cat --no-pager | grep -cxF "Mouse Battery service $version started")
    if [ "$started" = "1" ]; then
        break
    fi
    sleep 0.1
done
check "the new process logged that $version started" "$started" "1"
lastPid=$newPid
check "the $version service lists the fake mouse at 81% again" "$(mouseState)" "1"

historyRows() {
    # read only, so the check never writes beside the service, and as root, since the folder is the service user's alone
    sudo python3 -c "import sqlite3; print(sqlite3.connect('file:/var/lib/$name/history.sqlite3?mode=ro', uri=True).execute('select count(*) from readings').fetchone()[0])" 2>/dev/null || echo 0
}

waitForRows() {
    local rows=0
    for i in $(seq 150); do
        rows=$(historyRows)
        if [ "$rows" -ge "$1" ] 2>/dev/null; then
            break
        fi
        sleep 0.1
    done
    echo "$rows"
}

# the first reading's time is something only the original file has, since a database made new starts after it
oldestReading() {
    sudo python3 -c "import sqlite3; print(sqlite3.connect('file:/var/lib/$name/history.sqlite3?mode=ro', uri=True).execute('select min(at) from readings').fetchone()[0])" 2>/dev/null
}

brokenFiles() {
    sudo find /var/lib/$name -maxdepth 1 -name 'history.sqlite3.broken-*' | wc -l
}

# a stopping service can still write read rows, since it processes every pending reading on SIGTERM, so a read row only
# proves the new process when its time is at or after the new process's start second, which waitForStart takes
readsSince() {
    sudo python3 -c "import sqlite3, sys; print(sqlite3.connect('file:/var/lib/$name/history.sqlite3?mode=ro', uri=True).execute('select count(*) from readings where origin = 1 and at >= ?', (int(sys.argv[1]),)).fetchone()[0])" "$1" 2>/dev/null || echo 0
}

waitForReadsSince() {
    local reads=0
    for i in $(seq 300); do
        reads=$(readsSince "$1")
        if [ "$reads" -ge 1 ] 2>/dev/null; then
            break
        fi
        sleep 0.1
    done
    echo "$reads"
}

# waits for a new process to log its start line, which gives the pid the next restart is told apart from
waitForStart() {
    local pid="" count=0
    for i in $(seq 150); do
        pid=$(mainPid)
        if [ "$pid" -gt 0 ] 2>/dev/null && [ "$pid" != "$lastPid" ]; then
            count=$(sudo journalctl _PID="$pid" -o cat --no-pager | grep -cxF "Mouse Battery service $version started")
            if [ "$count" = "1" ]; then
                break
            fi
        fi
        sleep 0.1
    done
    [ "$count" = "1" ] || stop "the restarted service never logged that $version started"
    lastPid=$pid
    startedAt=$(date -d "$(systemctl show -p ExecMainStartTimestamp --value "$unit")" +%s 2>/dev/null)
    [ "$startedAt" -gt 0 ] 2>/dev/null || stop "the restarted service has no start time"
}

check "the upgrade gave the service its state folder" "$(sudo stat -c '%U %a' /var/lib/$name 2>&1)" "$name 750"
rows=$(waitForRows 2)
check "the service saved the fake mouse's readings" "$( [ "$rows" -ge 2 ] 2>/dev/null && echo saved || echo "$rows rows")" "saved"
check "GetHistory answers for the fake mouse" "$(busctl --system --timeout=5 call "$service" "$objectPath" "$interface" GetHistory s 02bc524c 2>&1 | grep -c '\\"key\\": \\"02bc524c\\"')" "1"
check "GetLevels answers a range for the fake mouse" "$(busctl --system --timeout=5 call "$service" "$objectPath" "$interface" GetLevels ss 02bc524c year 2>&1 | grep -c '\\"range\\": \\"year\\"')" "1"
oldest=$(oldestReading)
[ -n "$oldest" ] || stop "the history has no first reading to compare against"
# stopped for a second before it starts, so nothing the old process writes can share a second with the new start
sudo systemctl stop "$unit"
sleep 1
sudo systemctl start "$unit"
waitForStart
reads=$(waitForReadsSince "$startedAt")
check "the history survived a service restart" "$( [ "$reads" -ge 1 ] 2>/dev/null && [ "$(oldestReading)" = "$oldest" ] && echo kept || echo "$reads reads since the restart, first reading $(oldestReading)")" "kept"
check "the restart set no broken history file aside" "$(brokenFiles)" "0"
sudo DEBIAN_FRONTEND=noninteractive apt-get install --reinstall -y "$repo/dist/${name}_${version}_all.deb" > "$work/install3.log" 2>&1 || stop "the reinstall failed"
waitForStart
reads=$(waitForReadsSince "$startedAt")
check "the history survived a reinstall of the same package" "$( [ "$reads" -ge 1 ] 2>/dev/null && [ "$(oldestReading)" = "$oldest" ] && echo kept || echo "$reads reads since the reinstall, first reading $(oldestReading)")" "kept"
check "the reinstall set no broken history file aside" "$(brokenFiles)" "0"

if [ "$failures" -gt 0 ]; then
    exit 1
fi
