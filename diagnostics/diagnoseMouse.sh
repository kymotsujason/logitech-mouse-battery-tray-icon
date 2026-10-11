#!/usr/bin/env bash
# saves what the kernel, udev, UPower, and Mouse Battery see of a Logitech mouse, once on its receiver and once on its
# cable, so a tester can send one file back. It only reads, needs no root, and changes nothing
set -u

report="${1:-mouse-report-$(date +%Y%m%d-%H%M%S).txt}"
appDir=/usr/share/logitech-mouse-battery

readValue() {
    cat "$1" 2>/dev/null || echo "-"
}

udevFlag() {
    udevadm info -q property -p "$1" 2>/dev/null | sed -n "s/^$2=//p"
}

upowerType() {
    local value
    value=$(busctl get-property org.freedesktop.UPower "$1" org.freedesktop.UPower.Device Type 2>/dev/null | awk '{print $2}')
    case "$value" in
        5) echo "mouse (5)" ;;
        6) echo "keyboard (6)" ;;
        "") echo "unknown (busctl couldn't read it)" ;;
        *) echo "other ($value)" ;;
    esac
}

usbParent() {
    # walks up from a sysfs device to the whole USB device, the level UPower looks at when it guesses a type
    local dir
    dir=$(readlink -f "$1")
    while [ "$dir" != "/" ] && [ -n "$dir" ]; do
        if [ -f "$dir/idVendor" ]; then
            echo "$dir"
            return 0
        fi
        dir=$(dirname "$dir")
    done
    return 1
}

listInputs() {
    # $2 is how deep to look, since a receiver's own HID device also holds the devices of the mice paired to it
    local input name
    for input in $(find "$(readlink -f "$1")" -maxdepth "$2" -type d -name "input[0-9]*" -path "*/input/input*" 2>/dev/null | sort -u); do
        name=$(readValue "$input/name")
        echo "      input $(basename "$input") \"$name\" mouse=$(udevFlag "$input" ID_INPUT_MOUSE) keyboard=$(udevFlag "$input" ID_INPUT_KEYBOARD)"
    done
}

snapshot() {
    local dev hid driver node passes supply supplyDev usb path native
    echo
    echo "==== $1 ===="
    echo "time: $(date)"

    echo
    echo "-- Logitech USB devices --"
    for dev in /sys/bus/usb/devices/*; do
        [ -f "$dev/idVendor" ] || continue
        [ "$(readValue "$dev/idVendor")" = "046d" ] || continue
        echo "$(basename "$dev") 046d:$(readValue "$dev/idProduct") \"$(readValue "$dev/product")\""
    done

    echo
    echo "-- Logitech HID devices --"
    for hid in /sys/bus/hid/devices/*:046D:*; do
        [ -e "$hid" ] || continue
        driver=$(basename "$(readlink -f "$hid/driver" 2>/dev/null)" 2>/dev/null)
        echo "$(basename "$hid") driver=${driver:--}"
        for node in "$hid"/hidraw/hidraw*; do
            [ -e "$node" ] || continue
            passes="app not installed"
            if [ -f "$appDir/isHidpp.py" ]; then
                if python3 -I -B "$appDir/isHidpp.py" "$node" 2>/dev/null; then
                    passes="passes the app's check"
                else
                    passes="fails the app's check"
                fi
            fi
            echo "    $(ls -l "/dev/$(basename "$node")" 2>/dev/null | awk '{print $1, $3, $4, $NF}'), $passes"
        done
        listInputs "$hid" 2
    done

    echo
    echo "-- Kernel batteries --"
    for supply in /sys/class/power_supply/hidpp_battery_*; do
        [ -e "$supply" ] || continue
        supplyDev=$(readlink -f "$supply/device")
        echo "$(basename "$supply") model=\"$(readValue "$supply/model_name")\" online=$(readValue "$supply/online") status=$(readValue "$supply/status") capacity=$(readValue "$supply/capacity") level=$(readValue "$supply/capacity_level") voltage=$(readValue "$supply/voltage_now")"
        echo "    device: $supplyDev"
        if usb=$(usbParent "$supplyDev"); then
            echo "    usb device: $(basename "$usb") 046d:$(readValue "$usb/idProduct"), the inputs UPower weighs for its type:"
            listInputs "$usb" 8
        fi
    done

    echo
    echo "-- UPower devices --"
    for path in $(upower -e 2>/dev/null); do
        case "$path" in
            *DisplayDevice) continue ;;
        esac
        native=$(upower -i "$path" 2>/dev/null | sed -n 's/^ *native-path: *//p')
        echo "$path native-path=${native:--} type=$(upowerType "$path")"
        upower -i "$path" 2>/dev/null | grep -E "^ *(model|vendor|serial|present|state|percentage|battery-level|icon-name):" | sed 's/^ */    /'
    done

    echo
    echo "-- Mouse Battery check --"
    if command -v logitech-mouse-battery >/dev/null; then
        timeout 40 logitech-mouse-battery check 2>&1
    else
        echo "logitech-mouse-battery isn't installed"
    fi

    echo
    echo "-- Recent kernel lines about Logitech devices --"
    journalctl -k -b --no-pager 2>/dev/null | grep -iE "logitech|hidpp|046d" | tail -30
}

verdict() {
    # one line per kernel battery that UPower lists, saying whether Mouse Battery would keep it
    local found=0 path native type
    for path in $(upower -e 2>/dev/null); do
        native=$(upower -i "$path" 2>/dev/null | sed -n 's/^ *native-path: *//p')
        case "$native" in
            hidpp_battery*) ;;
            *) continue ;;
        esac
        found=1
        type=$(upowerType "$path")
        case "$type" in
            mouse*) echo "  $native: UPower calls it a mouse, so Mouse Battery shows it" ;;
            *) echo "  $native: UPower calls it $type, so Mouse Battery skips it" ;;
        esac
    done
    if [ "$found" = 0 ]; then
        echo "  UPower lists no Logitech battery from the kernel"
    fi
}

waitForEnter() {
    echo
    echo "$1"
    read -r -p "Press Enter when ready. " < /dev/tty
}

: > "$report" || { echo "Can't write $report"; exit 1; }
{
    echo "Mouse report"
    echo "kernel: $(uname -r)"
    echo "system: $(. /etc/os-release 2>/dev/null && echo "${PRETTY_NAME:-unknown}")"
    echo "upower: $(upower --version 2>/dev/null | tr '\n' ' ')"
    echo "mouse battery: $(logitech-mouse-battery --version 2>/dev/null || echo "not installed")"
} >> "$report"

echo "This saves what your computer sees of the mouse, once on its receiver and once on its cable."
echo "It only reads, and the report goes to $report."

waitForEnter "Step 1 of 2. Unplug the mouse's cable, keep the receiver plugged in, and move the mouse."
snapshot "on the receiver" >> "$report"
echo "On the receiver:" | tee -a "$report"
verdict | tee -a "$report"

waitForEnter "Step 2 of 2. Plug the cable into the mouse and the computer (keep the receiver plugged in if it has its own port), wait about 5 seconds, and move the mouse."
snapshot "on the cable" >> "$report"
echo "On the cable:" | tee -a "$report"
verdict | tee -a "$report"

echo
echo "Done. Please send $report back."
