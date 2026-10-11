#!/usr/bin/env bash
# walks a tester through using a Logitech mouse wirelessly, letting it sleep, and plugging in its cable, and saves what
# the kernel, udev, UPower, and Mouse Battery see at each step to one file in their home folder. It only reads, needs no
# root, and changes nothing. The first argument picks another report path
set -u

report=$(realpath -m "${1:-$HOME/mouse-report-$(date +%Y%m%d-%H%M%S).txt}")
appDir=/usr/share/logitech-mouse-battery
psRoot="${PS_ROOT:-/sys/class/power_supply}"
usbRoot="${USB_ROOT:-/sys/bus/usb/devices}"
udevData="${UDEV_DATA:-/run/udev/data}"
sleepLimit=$((12 * 60))
work=$(mktemp -d /tmp/mouseReport.XXXXXX)
pids=()
finished=0
keepWork=0

if [ -t 1 ]; then
    bold=$(tput bold 2>/dev/null)
    plain=$(tput sgr0 2>/dev/null)
else
    bold=""
    plain=""
fi

stopWatchers() {
    # each watcher is a pipeline inside a subshell, so its children go first or they'd outlive the script, and the wait
    # takes the notice bash prints for a killed job off the screen
    local pid
    for pid in ${pids[@]+"${pids[@]}"}; do
        pkill -P "$pid" 2>/dev/null
        kill "$pid" 2>/dev/null
        wait "$pid" 2>/dev/null
    done
    pids=()
}

cleanup() {
    stopWatchers
    { stty echo < /dev/tty; } 2>/dev/null
    if [ "$keepWork" = 0 ]; then
        rm -rf "$work"
    fi
    if [ "$finished" = 0 ]; then
        printf "\n\nStopped before the end, so no report was saved.\n"
    fi
}
trap cleanup EXIT
trap 'exit 130' INT TERM

if ! { : < /dev/tty; } 2>/dev/null; then
    echo "Run this in a terminal, since it asks you to do a few things with the mouse."
    finished=1
    exit 1
fi

title() {
    printf "\n%s%s%s\n\n" "$bold" "$1" "$plain"
}

clearLine() {
    printf "\r\033[K"
}

drainInput() {
    # a key pressed while nothing was asking would otherwise answer the next question unseen, and reading one key at a
    # time takes keys typed without Enter too
    while read -r -s -n 1 -t 0.05 < /dev/tty; do
        :
    done
}

quiet() {
    { stty -echo < /dev/tty; } 2>/dev/null
}

loud() {
    { stty echo < /dev/tty; } 2>/dev/null
}

ask() {
    # $1 is the prompt, and the answer lands in $answer
    drainInput
    read -r -p "$1" answer < /dev/tty
}

readValue() {
    cat "$1" 2>/dev/null || echo "-"
}

udevFlag() {
    # udev keeps each input's properties in its database as E:NAME=VALUE lines
    sed -n "s/^E:$2=//p" "$udevData/+input:$(basename "$1")" 2>/dev/null | head -n 1
}

inputsUnder() {
    # $2 is how deep to look, and the LED folders named like input50::capslock aren't inputs
    local input
    for input in $(find "$(readlink -f "$1")" -maxdepth "$2" -type d -name "input[0-9]*" -path "*/input/input*" 2>/dev/null | sort -u); do
        case "$(basename "$input")" in
            *:*) continue ;;
        esac
        echo "$input"
    done
}

hasMouseInput() {
    local input
    for input in $(inputsUnder "$1" "$2"); do
        if [ "$(udevFlag "$input" ID_INPUT_MOUSE)" = 1 ]; then
            return 0
        fi
    done
    return 1
}

isReceiver() {
    local driver
    case "$(readValue "$1/product")" in
        *[Rr]eceiver*) return 0 ;;
    esac
    for driver in "$1"/*/*/driver; do
        [ -e "$driver" ] || continue
        if [ "$(basename "$(readlink -f "$driver")")" = "logitech-djreceiver" ]; then
            return 0
        fi
    done
    return 1
}

logitechDevices() {
    local dev
    for dev in "$usbRoot"/*; do
        [ -f "$dev/idVendor" ] || continue
        [ "$(readValue "$dev/idVendor")" = "046d" ] || continue
        echo "$dev"
    done
}

wiredMice() {
    # a Logitech USB device that isn't a receiver and has an input udev calls a mouse is a mouse on its cable
    local dev
    for dev in $(logitechDevices); do
        if ! isReceiver "$dev" && hasMouseInput "$dev" 4; then
            basename "$dev"
        fi
    done
}

plainText() {
    # a device chooses its own name strings, so the lines shown on screen keep only printable ASCII and newlines, and
    # every other byte shows as a question mark (so a multibyte character shows as several), while the report file keeps
    # the raw values
    LC_ALL=C tr -c '\012\040-\176' '?'
}

wiredNames() {
    local name
    for name in $(wiredMice); do
        readValue "$usbRoot/$name/product"
    done | plainText | sed 's/^-$/mouse/' | paste -sd "," | sed 's/,/, /g'
}

receiverPresent() {
    local dev
    for dev in $(logitechDevices); do
        if isReceiver "$dev"; then
            return 0
        fi
    done
    return 1
}

noWiredMouse() {
    [ -z "$(wiredMice)" ]
}

cableConnected() {
    local dev
    for dev in $(wiredMice); do
        case " $wiredBefore " in
            *" $dev "*) ;;
            *) return 0 ;;
        esac
    done
    return 1
}

isAsleep() {
    [ "$(readValue "$1/status")" = "Unknown" ] || [ "$(readValue "$1/online")" = "0" ]
}

batteryIsMouse() {
    # on a receiver the mouse's own HID device holds its input, while on a cable the battery sits on an interface of its
    # own and the mouse input is on another interface of the same USB device
    local hid usb
    if hasMouseInput "$1/device" 2; then
        return 0
    fi
    hid=$(readlink -f "$1/device")
    [ -f "$(dirname "$hid")/bInterfaceNumber" ] || return 1
    usb=$(dirname "$(dirname "$hid")")
    hasMouseInput "$usb" 4
}

mouseBatteries() {
    # a receiver can hold a keyboard's battery too, and only a battery with a mouse input beside it is the mouse's,
    # unless udev's database can't be read at all
    local supply mice="" every=""
    for supply in "$psRoot"/hidpp_battery_*; do
        [ -e "$supply" ] || continue
        every="$every $(basename "$supply")"
        if batteryIsMouse "$supply"; then
            mice="$mice $(basename "$supply")"
        fi
    done
    if [ -r "$udevData" ]; then
        echo $mice
    else
        echo $every
    fi
}

watchedAwake() {
    local name
    for name in $watched; do
        if isAsleep "$psRoot/$name"; then
            return 1
        fi
    done
    return 0
}

watchedAsleep() {
    local name
    for name in $watched; do
        if [ ! -e "$psRoot/$name" ] || isAsleep "$psRoot/$name"; then
            sleptBattery=$name
            return 0
        fi
    done
    return 1
}

sleptAwake() {
    local supply
    if [ -e "$psRoot/$sleptBattery" ]; then
        ! isAsleep "$psRoot/$sleptBattery"
        return
    fi
    # a battery that went away comes back under a new number
    for supply in "$psRoot"/hidpp_battery_*; do
        [ -e "$supply" ] || continue
        case " $watched " in
            *" $(basename "$supply") "*) continue ;;
        esac
        if ! isAsleep "$supply"; then
            return 0
        fi
    done
    return 1
}

waitFor() {
    # waits until the check in $2 passes, for up to $1 seconds or with no limit when $1 is 0, showing $3 on one line and
    # $4 once after a minute, and gives 0 when the check passed, 1 when the time ran out, and 2 when Enter skipped it
    local limit=$1 check=$2 label=$3 hint=${4:-} waited=0 left status
    drainInput
    while true; do
        if "$check"; then
            clearLine
            return 0
        fi
        if [ "$limit" -gt 0 ] && [ "$waited" -ge "$limit" ]; then
            clearLine
            return 1
        fi
        if [ "$waited" = 60 ] && [ -n "$hint" ]; then
            clearLine
            echo "  $hint"
        fi
        if [ "$limit" -gt 0 ]; then
            left=$((limit - waited))
            printf "\r  %s %d:%02d left " "$label" "$((left / 60))" "$((left % 60))"
        else
            printf "\r  %s " "$label"
        fi
        read -r -s -t 1 < /dev/tty
        status=$?
        # a status above 128 is the second running out, while 0 is Enter and anything else is a terminal that's gone
        if [ "$status" -le 128 ]; then
            clearLine
            return 2
        fi
        waited=$((waited + 1))
    done
}

pause() {
    # shows $2 for $1 seconds, so a device that just showed up has time to settle
    local left=$1
    quiet
    while [ "$left" -gt 0 ]; do
        printf "\r  %s %d " "$2" "$left"
        sleep 1
        left=$((left - 1))
    done
    loud
    clearLine
}

duration() {
    if [ "$1" -ge 60 ]; then
        echo "$(($1 / 60)) min $(($1 % 60)) s"
    else
        echo "$1 s"
    fi
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
    local input
    for input in $(inputsUnder "$1" "$2"); do
        echo "      input $(basename "$input") \"$(readValue "$input/name")\" mouse=$(udevFlag "$input" ID_INPUT_MOUSE) keyboard=$(udevFlag "$input" ID_INPUT_KEYBOARD)"
    done
}

runCheck() {
    if command -v logitech-mouse-battery >/dev/null; then
        timeout --foreground 40 logitech-mouse-battery check 2>&1
    else
        echo "Mouse Battery isn't installed."
    fi
}

snapshot() {
    # $1 names the step, and $2 holds what Mouse Battery's own check printed during it
    local dev hid driver node passes supply supplyDev usb path native
    echo
    echo "==== $1 ===="
    echo "time: $(date)"

    echo
    echo "-- Logitech USB devices --"
    for dev in "$usbRoot"/*; do
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
    for supply in "$psRoot"/hidpp_battery_*; do
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
    cat "$2"

    echo
    echo "-- Recent kernel lines about Logitech devices --"
    journalctl -k -b --no-pager 2>/dev/null | grep -iE "logitech|hidpp|046d" | tail -30
}

checkLines() {
    # joins each mouse's name with the reading line under it, so "G703 LS (UPower)" and "94%, discharging" read as one
    awk 'function flush() { if (pending != "") print pending; pending = "" }
        /^  / { if (pending != "") { print pending ", " substr($0, 3); pending = "" } else print substr($0, 3); next }
        { flush(); pending = $0 }
        END { flush() }' "$1"
}

keyboardNotes() {
    local path native type model
    for path in $(upower -e 2>/dev/null); do
        native=$(upower -i "$path" 2>/dev/null | sed -n 's/^ *native-path: *//p')
        case "$native" in
            hidpp_battery*) ;;
            *) continue ;;
        esac
        batteryIsMouse "$psRoot/$native" || continue
        model=$(readValue "$psRoot/$native/model_name" | sed 's/^-$/mouse/')
        type=$(upowerType "$path")
        case "$type" in
            mouse*) ;;
            keyboard*) echo "UPower calls the battery of the $model a keyboard, which Mouse Battery 1.2.0 and older don't show." ;;
            *) echo "UPower calls the battery of the $model $type, which Mouse Battery doesn't show." ;;
        esac
    done
}

summaryLines() {
    # $1 is the label, then one line per thing to say, with the label only on the first
    local label=$1 line first=1
    shift
    for line in "$@"; do
        if [ "$first" = 1 ]; then
            printf "%-10s %s\n" "$label:" "$line"
            first=0
        else
            printf "%-10s %s\n" "" "$line"
        fi
    done
}

readStep() {
    # $1 is the step's key, $2 its title, and $3 a note for the summary, and it saves the check output, the snapshot, and
    # the summary lines for that step
    quiet
    printf "  Reading the mouse..."
    runCheck > "$work/$1-check"
    snapshot "$2" "$work/$1-check" > "$work/$1-snapshot"
    { if [ -n "${3:-}" ]; then echo "$3"; fi; checkLines "$work/$1-check"; keyboardNotes; } > "$work/$1-summary"
    loud
    clearLine
    echo "  Done."
}

watchSysfs() {
    # reading these files returns what the kernel cached, so it never wakes the mouse
    local supply name line
    declare -A last
    while true; do
        for name in "${!last[@]}"; do
            if [ ! -e "$psRoot/$name" ]; then
                echo "$(date +%H:%M:%S) [sysfs] $name is gone"
                unset "last[$name]"
            fi
        done
        for supply in "$psRoot"/hidpp_battery_*; do
            [ -e "$supply" ] || continue
            name=$(basename "$supply")
            line="status=$(readValue "$supply/status") online=$(readValue "$supply/online") capacity=$(readValue "$supply/capacity") voltage=$(readValue "$supply/voltage_now")"
            if [ "${last[$name]:-}" != "$line" ]; then
                echo "$(date +%H:%M:%S) [sysfs] $name $line"
                last[$name]=$line
            fi
        done
        sleep 1
    done
}

watchUevents() {
    # each change event is one power_supply_changed call in the kernel, even when the values stay the same
    local ours=0 line
    udevadm monitor --kernel --property --subsystem-match=power_supply 2>&1 | while IFS= read -r line; do
        case "$line" in
            KERNEL*hidpp_battery*)
                ours=1
                echo "$(date +%H:%M:%S) [udev] ${line#* }"
                ;;
            KERNEL*) ours=0 ;;
            POWER_SUPPLY_STATUS=*|POWER_SUPPLY_ONLINE=*|POWER_SUPPLY_CAPACITY=*|POWER_SUPPLY_VOLTAGE_NOW=*)
                [ "$ours" = 1 ] && echo "$(date +%H:%M:%S) [udev]     $line"
                ;;
        esac
    done
}

watchKernel() {
    local line
    journalctl -k -f -n 0 --no-pager 2>/dev/null | grep --line-buffered -iE "logitech|hidpp|046d" | while IFS= read -r line; do
        echo "$(date +%H:%M:%S) [kernel] $line"
    done
}

mark() {
    echo "$(date +%H:%M:%S) ---- $1 ----" >> "$work/sleep-events"
}

sleepStep() {
    local result started slept woke
    watched=$(mouseBatteries)
    if [ -z "$watched" ]; then
        echo "  Skipped. Linux shows no battery for this mouse, so there's no sleep to watch."
        sleepSummary="Skipped, since Linux shows no battery for this mouse."
        return
    fi
    ask "  Also check what happens when the mouse sleeps? It takes up to 12 minutes. (y/N) "
    case "$answer" in
        [Yy]*) ;;
        *)
            echo "  Skipped."
            sleepSummary="Skipped."
            return
            ;;
    esac
    if ! watchedAwake; then
        echo
        waitFor 0 watchedAwake "Move the mouse a little to wake it first..." "Still waiting. Press Enter to skip the sleep check."
        if ! watchedAwake; then
            echo "  Skipped, since the mouse didn't wake."
            sleepSummary="Skipped, since the mouse was already asleep and didn't wake."
            return
        fi
    fi

    : > "$work/sleep-events"
    watchSysfs >> "$work/sleep-events" 2>/dev/null &
    pids+=($!)
    watchUevents >> "$work/sleep-events" 2>/dev/null &
    pids+=($!)
    watchKernel >> "$work/sleep-events" 2>/dev/null &
    pids+=($!)
    upower --monitor-detail > "$work/sleep-upower" 2>&1 &
    pids+=($!)

    echo
    echo "  Put the mouse down and don't touch it. Keep the computer on. It's fine if the screen turns off."
    echo "  You'll hear a beep when it falls asleep. Press Enter to stop early."
    echo
    mark "mouse left alone"
    started=$SECONDS
    waitFor "$sleepLimit" watchedAsleep "Waiting for the mouse to fall asleep..."
    result=$?
    slept=$((SECONDS - started))
    if [ "$result" = 0 ]; then
        mark "mouse fell asleep"
        printf "\a"
        echo "  The mouse fell asleep after $(duration "$slept"). Move it now to wake it."
        started=$SECONDS
        waitFor 0 sleptAwake "Waiting for the mouse to wake..." "Still waiting. Move the mouse, or press Enter to go on."
        result=$?
        woke=$((SECONDS - started))
        if [ "$result" = 0 ]; then
            echo "  Awake again."
            # the kernel sends a few battery updates as the mouse comes back, and the log should hold them
            pause 5 "Finishing the sleep check"
            mark "done"
            sleepSummary="The mouse fell asleep after $(duration "$slept") and woke $(duration "$woke") after the beep."
        else
            mark "done"
            echo "  Going on without seeing it wake."
            sleepSummary="The mouse fell asleep after $(duration "$slept"), and its wake wasn't seen."
        fi
    elif [ "$result" = 1 ]; then
        mark "done"
        echo "  The mouse didn't fall asleep within 12 minutes. That's fine, carrying on."
        sleepSummary="The mouse didn't fall asleep within 12 minutes."
    else
        mark "done"
        echo "  Stopped early."
        sleepSummary="Stopped after $(duration "$slept") without seeing a sleep."
    fi
    stopWatchers
}

wiredBefore=""
sleepSummary="Skipped."
if [ -d "$report" ] || { [ -e "$report" ] && [ ! -w "$report" ]; } || [ ! -d "$(dirname "$report")" ] || [ ! -w "$(dirname "$report")" ]; then
    echo "Can't save a report at $report."
    finished=1
    exit 1
fi

title "Mouse Report"
echo "This checks how your computer sees your Logitech mouse, wirelessly and on its cable,"
echo "and saves what it finds to a file you can send back. It only looks. It doesn't change"
echo "anything, and nothing is sent anywhere."
echo
echo "It takes about a minute, or up to 12 more if you also check sleep."
echo
ask "Press Enter to start, or Ctrl+C to stop at any time. "

title "Step 1 of 3: Wireless"
if ! noWiredMouse; then
    echo "  It looks like your $(wiredNames) is plugged in with its cable. Unplug the cable to use it wirelessly."
    waitFor 0 noWiredMouse "Waiting for the cable to come out..." "Still waiting. Press Enter if the mouse is already wireless."
fi
if ! receiverPresent; then
    echo "  Plug the mouse's receiver into the computer. If it fits into the end of the cable, plug it in there."
    echo "  If your mouse has no receiver, press Enter to go on."
    waitFor 0 receiverPresent "Waiting for the receiver..." "Still waiting. Press Enter if the mouse has no receiver."
fi
echo "  Move the mouse a little so it's awake."
pause 5 "Reading in"
wirelessNote=""
if ! noWiredMouse; then
    wirelessNote="The cable was still plugged in, so this may be a wired reading."
fi
readStep wireless "wireless" "$wirelessNote"
wiredBefore=$(wiredMice | tr '\n' ' ')

title "Step 2 of 3: Sleep (optional)"
sleepStep

title "Step 3 of 3: Cable"
echo "  Plug the mouse in with its USB cable."
echo "  If your mouse has no cable, or its cable only charges it, press Enter to skip."
waitFor 0 cableConnected "Waiting for the cable..." "Still waiting. Press Enter if it's already plugged in."
if [ $? = 0 ]; then
    echo "  Cable connected."
    pause 5 "Letting it settle"
    readStep cable "cable"
else
    echo "  Going on without seeing the cable."
    readStep cable "cable" "The cable wasn't seen, so this may be a wireless reading."
fi

# the subshell stops at its first failed command, so any capture that can't be read fails the save and keeps the folder
(
    set -e
    echo "Mouse report"
    echo "kernel: $(uname -r)"
    echo "system: $(. /etc/os-release 2>/dev/null && echo "${PRETTY_NAME:-unknown}")"
    echo "upower: $(upower --version 2>/dev/null | tr '\n' ' ')"
    echo "mouse battery: $(logitech-mouse-battery --version 2>/dev/null || echo "not installed")"
    echo
    echo "==== summary ===="
    mapfile -t lines < "$work/wireless-summary"
    summaryLines "Wireless" "${lines[@]:-Nothing found.}"
    summaryLines "Sleep" "$sleepSummary"
    mapfile -t lines < "$work/cable-summary"
    summaryLines "Cable" "${lines[@]:-Nothing found.}"
    cat "$work/wireless-snapshot"
    if [ -f "$work/sleep-events" ]; then
        echo
        echo "==== sleep ===="
        cat "$work/sleep-events"
        echo
        echo "-- upower --monitor-detail --"
        cat "$work/sleep-upower"
    fi
    cat "$work/cable-snapshot"
) > "$report"
if [ $? != 0 ]; then
    echo
    echo "Couldn't save the whole report at $report."
    echo "Your readings are still saved in $work, so copy that folder before you restart or turn off the computer."
    keepWork=1
    finished=1
    exit 1
fi
finished=1

title "All done"
sed -n '/^==== summary ====/,/^====/p' "$report" | sed '1d;$d' | sed '/^$/d' | plainText
echo
echo "Attach this file to your GitHub issue, or send it to whoever asked you to run this:"
echo "$bold$report$plain"
