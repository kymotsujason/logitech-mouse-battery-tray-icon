#!/usr/bin/env bash
# runs inside a clean container: installs the package, checks what it put where and that it runs, then removes it
# and checks that none of its files stayed behind
set -u

kind=$1
package=$2
version=$3
name=logitech-mouse-battery
appDir=/usr/share/$name
failures=0
C54F_HIDPP="06 43 ff 0a 01 03 a1 01 85 10 95 06 75 08 15 00 26 ff 00 09 01 81 00 09 01 91 00 c0 06 43 ff 0a 02 03 a1 01 85 11 95 13 75 08 15 00 26 ff 00 09 02 81 00 09 02 91 00 c0"
GENERIC_DESKTOP="05 01 09 02 a1 01 09 01 a1 00 95 10 75 01 15 00 25 01 05 09 19 01 29 10 81 02 95 02 75 10 16 00 80 26 ff 7f 05 01 09 30 09 31 81 06 95 01 75 08 15 80 25 7f 09 38 81 06 05 0c 0a 38 02 81 06 c0 95 05 75 08 15 00 26 ff 00 06 00 ff 09 f1 81 00 c0"

check() {
    if [ "$2" = "$3" ]; then
        echo "PASS $1"
    else
        echo "FAIL $1: expected '$3', got '$2'"
        failures=$((failures + 1))
    fi
}

installPackage() {
    case "$kind" in
        deb) export DEBIAN_FRONTEND=noninteractive; apt-get update -qq && apt-get install -y -qq --no-install-recommends "$package" ;;
        rpm) dnf install -y -q "$package" ;;
        arch) pacman -Syu --noconfirm && pacman -U --noconfirm "$package" ;;
    esac
}

listFiles() {
    case "$kind" in
        deb) dpkg -L "$name" ;;
        rpm) rpm -ql "$name" ;;
        arch) pacman -Qlq "$name" ;;
    esac
}

removePackage() {
    # apt remove, not purge, since a purge would hide a file wrongly marked as a conffile
    case "$kind" in
        deb) apt-get remove -y -qq "$name" ;;
        rpm) dnf remove -y -q "$name" ;;
        arch) pacman -R --noconfirm "$name" ;;
    esac
}

# a fake sysfs node whose device link leads to a HID device holding the given descriptor, printed as its devpath
fakeNode() {
    local device="/tmp/sysfs/devices/fake/$1"
    mkdir -p "$device/hidraw/hidraw0"
    python3 -c "import sys; open(sys.argv[1], 'wb').write(bytes.fromhex(sys.argv[2]))" "$device/report_descriptor" "$2"
    ln -s ../.. "$device/hidraw/hidraw0/device"
    echo "/devices/fake/$1/hidraw/hidraw0"
}

# runs the installed rule's own PROGRAM command with the fake sysfs root in place of %S
ruleProgram() {
    local program
    program=$(sed -n 's/.*PROGRAM="\([^"]*\)".*/\1/p' /usr/lib/udev/rules.d/70-$name.rules)
    sh -c "$(echo "$program" | sed "s|%S%p|/tmp/sysfs$1|")"
    echo $?
}

installPackage > /tmp/install.log 2>&1
status=$?
check "the package installs with its dependencies" "$status" "0"
if [ "$status" -ne 0 ]; then
    tail -20 /tmp/install.log
    exit 1
fi

missing=""
for path in /usr/bin/$name $appDir/autostart.py $appDir/check.py $appDir/dbusCalls.py $appDir/hidDescriptor.py $appDir/hidpp.py $appDir/icon.py $appDir/installWatch.py $appDir/isHidpp.py $appDir/lowBattery.py $appDir/mice.py $appDir/notify.py $appDir/panelColor.py $appDir/tray.py $appDir/trayHost.py $appDir/upower.py $appDir/version.py $appDir/NotoSans-Medium.ttf $appDir/OFL.txt /usr/share/applications/$name.desktop /etc/xdg/autostart/$name.desktop /usr/lib/udev/rules.d/70-$name.rules /usr/share/icons/hicolor/scalable/apps/$name.svg /usr/share/metainfo/io.github.kymotsujason.LogitechMouseBattery.metainfo.xml; do
    if [ ! -f "$path" ]; then
        missing="$missing $path"
    fi
done
check "every file lands where the spec puts it" "$missing" ""

check "the launcher prints the version" "$($name --version)" "$version"

output=$($name check 2>&1)
status=$?
lastLine=$(echo "$output" | tail -1)
case "$lastLine" in
    "No Logitech mouse found."|"No mouse answered. Move your mouse to wake it, then run this again."|"Can't read the receiver. Unplug it and plug it back in.") known=yes ;;
    *) known="no ($lastLine)" ;;
esac
check "the terminal check exits 1" "$status" "1"
check "the terminal check ends with one of its known lines" "$known" "yes"

rendered=$(QT_QPA_PLATFORM=offscreen python3 -B -c '
import sys
sys.path.insert(0, "/usr/share/logitech-mouse-battery")
from PyQt6.QtGui import QFontDatabase
from PyQt6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
import icon
image = icon.renderImage(icon.IconState(percent=69), 22, "#fcfcfc")
opaque = sum(1 for y in range(22) for x in range(22) if image.pixelColor(x, y).alpha() == 255)
print("drawn" if QFontDatabase.addApplicationFont(icon.FONT_FILE) >= 0 and opaque > 0 else "failed")
' 2>/dev/null)
check "the icon renders offscreen with the bundled font" "$rendered" "drawn"

check "the rule's program accepts the C54F HID++ node" "$(ruleProgram "$(fakeNode 0003:046D:C54F.0001 "$C54F_HIDPP")")" "0"
check "the rule's program rejects a generic desktop node" "$(ruleProgram "$(fakeNode 0003:046D:C0A9.0002 "$GENERIC_DESKTOP")")" "1"

files=$(listFiles | while read -r path; do if [ -f "$path" ] || [ -L "$path" ]; then echo "$path"; fi; done)
removePackage > /tmp/remove.log 2>&1
check "the package removes" "$?" "0"

left=""
for path in $files $appDir; do
    if [ -e "$path" ]; then
        left="$left $path"
    fi
done
check "no file stays behind" "$left" ""

if [ "$failures" -gt 0 ]; then
    exit 1
fi
