#!/usr/bin/env bash
# runs inside a clean container with the repo at /repo: installs the package, starts the image's own system bus with the
# installed policy, runs the service as its own user against a fake receiver, and checks what another user may do
set -u

kind=$1
package=$2
name=logitech-mouse-battery
service=io.github.kymotsujason.LogitechMouseBattery
objectPath=/io/github/kymotsujason/LogitechMouseBattery
interface=io.github.kymotsujason.LogitechMouseBattery1
socket=/run/dbus/system_bus_socket
failures=0

check() {
    if [ "$2" = "$3" ]; then
        echo "PASS $1"
    else
        echo "FAIL $1: expected '$3', got '$2'"
        failures=$((failures + 1))
    fi
}

# Debian's dbus holds the system bus config and the dbus.socket the unit needs, and Fedora keeps runuser in util-linux
installPackage() {
    case "$kind" in
        deb) export DEBIAN_FRONTEND=noninteractive; apt-get update -qq && apt-get install -y -qq --no-install-recommends "$package" dbus ;;
        rpm) dnf install -y -q "$package" dbus-broker util-linux ;;
        arch) pacman -Syu --noconfirm && pacman -U --noconfirm "$package" ;;
    esac
}

startBus() {
    systemd-machine-id-setup > /dev/null 2>&1
    mkdir -p /run/dbus
    if [ "$kind" = "deb" ]; then
        dbus-daemon --system --fork
        expected=dbus-daemon
    else
        # dbus-broker logs to the journal and takes its socket only from its parent
        mkdir -p /run/systemd/journal
        python3 -c '
import os, socket
s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
s.bind("/run/systemd/journal/socket")
os.chmod("/run/systemd/journal/socket", 0o666)
while True:
    s.recv(65536)
' &
        sleep 0.5
        systemd-socket-activate -l "$socket" -- /usr/bin/dbus-broker-launch --scope system > /tmp/broker.log 2>&1 &
        sleep 0.5
        chmod 0666 "$socket"
        expected=dbus-broker-lau
    fi
    for i in $(seq 50); do
        busctl --system call org.freedesktop.DBus /org/freedesktop/DBus org.freedesktop.DBus GetId > /dev/null 2>&1 && break
        sleep 0.1
    done
    local pid
    pid=$(busctl --system call org.freedesktop.DBus /org/freedesktop/DBus org.freedesktop.DBus GetConnectionUnixProcessID s org.freedesktop.DBus | awk '{print $2}')
    check "the bus answering is the image's own daemon" "$(cat /proc/$pid/comm 2>/dev/null)" "$expected"
}

asOther() {
    runuser -u probeuser -- "$@" 2>&1
}

if ! installPackage > /tmp/install.log 2>&1; then
    echo "FAIL the package installs"
    tail -20 /tmp/install.log
    exit 1
fi
check "the install made the service's user and group" "$(getent passwd $name > /dev/null && getent group $name > /dev/null && echo yes)" "yes"
if command -v systemd-analyze > /dev/null; then
    systemd-analyze verify /usr/lib/systemd/system/$name.service > /tmp/verify.log 2>&1
    check "systemd-analyze accepts the unit" "$?" "0"
fi
startBus
useradd -M probeuser

runuser -u $name -- env MOUSE_BATTERY_APP_DIR=/usr/share/$name QT_QPA_PLATFORM=offscreen python3 -B /repo/tests/fakes/runService.py "unix:path=$socket" > /tmp/service.log 2>&1 &
state=""
for i in $(seq 100); do
    state=$(asOther busctl --system call $service $objectPath $interface GetState)
    case "$state" in
        *02bc524c*) break ;;
    esac
    sleep 0.1
done
check "another user gets the state with the fake mouse" "$(echo "$state" | grep -c '\\"percent\\": 81')" "1"
asOther busctl --system call $service $objectPath $interface ReadAll > /dev/null
check "another user may ask for a read" "$?" "0"
check "another user can't call Properties.GetAll" "$(asOther busctl --system call $service $objectPath org.freedesktop.DBus.Properties GetAll s $interface | grep -c 'Access denied')" "1"
check "another user can't call a member the interface doesn't have" "$(asOther busctl --system call $service $objectPath $interface Missing | grep -c 'Access denied')" "1"
check "another user can't take the service's name" "$(asOther busctl --system call org.freedesktop.DBus /org/freedesktop/DBus org.freedesktop.DBus RequestName su $service 0 | grep -c 'Access denied')" "1"

if [ "$failures" -gt 0 ]; then
    echo "--- service output"
    cat /tmp/service.log
    exit 1
fi
