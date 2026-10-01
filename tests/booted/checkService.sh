#!/usr/bin/env bash
# runs on a booted GitHub hosted runner with passwordless sudo: installs 1.0.1, makes a fake receiver through uhid, gives
# the runner's user the ACL 1.0.x's uaccess gives a seat user, upgrades to the built 1.1.0, and checks that only the
# service can open the receiver and that it reads the fake mouse through the real system bus, udev, and systemd
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

canOpen() {
    python3 -c 'import os, sys; os.close(os.open(sys.argv[1], os.O_RDWR)); print("opened")' "$1" 2>&1 | tail -1
}

namedAclEntries() {
    getfacl -cp "$1" 2>/dev/null | grep -c '^user:[^:]'
}

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

stopFake() {
    sudo kill "$fakePid"
    wait "$fakePid" 2>/dev/null
    fakePid=""
    sudo udevadm settle
}

[ "$(ps -o comm= -p 1)" = "systemd" ] || stop "systemd isn't PID 1 here"
sudo -n true 2>/dev/null || stop "sudo asks for a password here"

curl -fsSL -o "$work/old.deb" "https://github.com/kymotsujason/logitech-mouse-battery-tray-icon/releases/download/v1.0.1/logitech-mouse-battery_1.0.1_all.deb" || stop "couldn't download the 1.0.1 package"
sudo apt-get update -qq > /dev/null
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$work/old.deb" acl > "$work/install1.log" 2>&1 || stop "1.0.1 didn't install"

if ! sudo modprobe uhid 2>/dev/null; then
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "linux-modules-extra-$(uname -r)" > "$work/modules.log" 2>&1
    sudo modprobe uhid || stop "couldn't load uhid"
fi
[ -e /dev/uhid ] || stop "/dev/uhid is missing after loading uhid"

startFake
check "the fake receiver landed on hid-generic" "$(grep '^DRIVER=' "/sys/class/hidraw/$(basename "$node")/device/uevent")" "DRIVER=hid-generic"
sudo setfacl -m "u:$(id -un):rw" "$node"
check "with 1.0.x's ACL the runner's user can open the receiver" "$(canOpen "$node")" "opened"

sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "$repo/dist/${name}_${version}_all.deb" > "$work/install2.log" 2>&1 || stop "1.1.0 didn't install"
expected="Mouse Battery: reloading systemd
Mouse Battery: reloading the system bus
Mouse Battery: reloading the udev rules
Mouse Battery: applying the rule to plugged in receivers and cables
Mouse Battery: waiting for udev
Mouse Battery: closing the access 1.0.x gave
Mouse Battery: clearing the service's failed starts
Mouse Battery: restarting the service if it runs"
check "afterInstall.sh ran its steps in order" "$(grep -o 'Mouse Battery: .*' "$work/install2.log" | grep -v 'that step failed')" "$expected"
check "no step failed" "$(grep -c 'that step failed' "$work/install2.log")" "0"
check "clearAccess.py closed the fake receiver" "$(grep -c "Closed access to $node" "$work/install2.log")" "1"
check "the node belongs to root and the group, mode 0660" "$(stat -c '%U %G %a' "$node")" "root $name 660"
check "the node has no named ACL entry left" "$(namedAclEntries "$node")" "0"
check "the runner's user can't open the receiver now" "$(canOpen "$node" | grep -c 'Permission denied')" "1"
# the runner has no seat session to show the uaccess builtin putting an ACL back, so this checks the tag that would
sudo udevadm trigger --action=change "$node"
sudo udevadm settle
tags=$(udevadm info "$node" | grep '^E: CURRENT_TAGS=')
check "the change event applied the new rule's systemd tag" "$(echo "$tags" | grep -c ':systemd:')" "1"
check "the node no longer carries the uaccess tag" "$(echo "$tags" | grep -c ':uaccess:')" "0"
check "a change event leaves the ACL off" "$(namedAclEntries "$node")" "0"

# systemd acts on SYSTEMD_WANTS only when a device first becomes active, so a fresh plug tests the start
sudo systemctl stop "$unit"
stopFake
startFake
active=""
for i in $(seq 100); do
    active=$(systemctl is-active "$unit")
    if [ "$active" = "active" ]; then
        break
    fi
    sleep 0.1
done
check "plugging the receiver in started the service" "$active" "active"
check "the service runs as its own user" "$(stat -c '%U' "/proc/$(systemctl show -p MainPID --value "$unit")")" "$name"

state=""
for i in $(seq 150); do
    state=$(busctl --system --timeout=2 call "$service" "$objectPath" "$interface" GetState 2>&1)
    case "$state" in
        *02bc524c*) break ;;
    esac
    sleep 0.1
done
check "GetState as the runner's user lists the fake mouse at 81%" "$(echo "$state" | grep -c '\\"percent\\": 81')" "1"
check "Properties.GetAll is denied" "$(busctl --system --timeout=2 call "$service" "$objectPath" org.freedesktop.DBus.Properties GetAll s "$interface" 2>&1 | grep -c 'Access denied')" "1"
check "a member the interface doesn't have is denied" "$(busctl --system --timeout=2 call "$service" "$objectPath" "$interface" Missing 2>&1 | grep -c 'Access denied')" "1"
check "taking the service's name is denied" "$(busctl --system --timeout=2 call org.freedesktop.DBus /org/freedesktop/DBus org.freedesktop.DBus RequestName su "$service" 0 2>&1 | grep -c 'Access denied')" "1"
check "check prints the fake mouse" "$(logitech-mouse-battery check 2>&1 | grep -c "PRO X3 SUPERSTRIKE ($node)")" "1"

sudo DEBIAN_FRONTEND=noninteractive apt-get remove -y "$name" > "$work/remove.log" 2>&1 || stop "1.1.0 didn't remove"
check "removing the package stopped the service" "$(systemctl is-active "$unit")" "inactive"

if [ "$failures" -gt 0 ]; then
    exit 1
fi
