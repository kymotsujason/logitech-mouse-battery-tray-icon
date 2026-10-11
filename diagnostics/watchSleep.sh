#!/usr/bin/env bash
# watches a Logitech mouse's kernel battery while the mouse sits untouched long enough to sleep, and then while it wakes,
# to see whether the kernel marks the battery unknown when the mouse drops its link. It only reads, needs no root, and
# changes nothing. The first argument is how many minutes to leave the mouse alone, 12 by default since the G703 sleeps after 10
set -u

minutes="${1:-12}"
report="sleep-report-$(date +%Y%m%d-%H%M%S).txt"
psRoot="${PS_ROOT:-/sys/class/power_supply}"
work=$(mktemp -d /tmp/watchSleep.XXXXXX)
events="$work/events"
pids=()

cleanup() {
    for pid in "${pids[@]}"; do
        pkill -P "$pid" 2>/dev/null
        kill "$pid" 2>/dev/null
    done
    rm -rf "$work"
}
trap cleanup EXIT

case "$minutes" in
    ''|*[!0-9]*) echo "Usage: bash watchSleep.sh [MINUTES]"; exit 2 ;;
esac

stamp() {
    date +%H:%M:%S
}

readValue() {
    cat "$1" 2>/dev/null || echo "-"
}

batteryLine() {
    echo "status=$(readValue "$1/status") online=$(readValue "$1/online") capacity=$(readValue "$1/capacity") voltage=$(readValue "$1/voltage_now")"
}

watchSysfs() {
    # reading these files returns what the kernel cached, so it never wakes the mouse
    local supply name line
    declare -A last
    while true; do
        for name in "${!last[@]}"; do
            if [ ! -e "$psRoot/$name" ]; then
                echo "$(stamp) [sysfs] $name is gone"
                unset "last[$name]"
            fi
        done
        for supply in "$psRoot"/hidpp_battery_*; do
            [ -e "$supply" ] || continue
            name=$(basename "$supply")
            line=$(batteryLine "$supply")
            if [ "${last[$name]:-}" != "$line" ]; then
                echo "$(stamp) [sysfs] $name $line"
                last[$name]=$line
            fi
        done
        sleep 1
    done
}

watchUevents() {
    # each change event is one power_supply_changed call in the kernel, even when the values stay the same
    local ours=0
    udevadm monitor --kernel --property --subsystem-match=power_supply 2>&1 | while IFS= read -r line; do
        case "$line" in
            KERNEL*hidpp_battery*)
                ours=1
                echo "$(stamp) [udev] ${line#* }"
                ;;
            KERNEL*) ours=0 ;;
            POWER_SUPPLY_STATUS=*|POWER_SUPPLY_ONLINE=*|POWER_SUPPLY_CAPACITY=*|POWER_SUPPLY_VOLTAGE_NOW=*)
                [ "$ours" = 1 ] && echo "$(stamp) [udev]     $line"
                ;;
        esac
    done
}

watchKernel() {
    journalctl -k -f -n 0 --no-pager 2>/dev/null | grep --line-buffered -iE "logitech|hidpp|046d" | while IFS= read -r line; do
        echo "$(stamp) [kernel] $line"
    done
}

mark() {
    echo "$(stamp) ---- $1 ----" >> "$events"
}

waitOrEnter() {
    # waits $1 seconds, printing $2 with the seconds left every minute, and Enter ends the wait early
    local left=$1
    while [ "$left" -gt 0 ]; do
        printf "%s\n" "$(printf "$2" "$((left / 60))" "$((left % 60))")"
        if read -r -t "$((left < 60 ? left : 60))" < /dev/tty; then
            return 0
        fi
        left=$((left - 60))
    done
}

phaseCount() {
    # how many lines with tag $2 sit between marker $1 and the next marker
    awk -v start="---- $1 ----" -v tag="$2" 'index($0, "---- ") { inside = index($0, start) > 0; next } inside && index($0, tag) { n++ } END { print n + 0 }' "$events"
}

phaseUnknown() {
    awk -v start="---- $1 ----" 'index($0, "---- ") { inside = index($0, start) > 0; next } inside && /\[sysfs\]/ && (/status=Unknown/ || /online=0/ || / is gone/) { print "    " $0 }' "$events"
}

supplies=("$psRoot"/hidpp_battery_*)
if [ ! -e "${supplies[0]}" ]; then
    echo "The kernel shows no Logitech battery. Unplug the mouse's cable, plug in its receiver, move the mouse, and run this again."
    exit 1
fi
: > "$events"

echo "This watches what the kernel says about the mouse's battery while the mouse sleeps and wakes."
echo "It only reads, and the report goes to $report."
echo
echo "Unplug the mouse's cable so it runs on its receiver, and move the mouse once."
read -r -p "Then press Enter and don't touch the mouse until this asks you to. " < /dev/tty

watchSysfs >> "$events" &
pids+=($!)
watchUevents >> "$events" &
pids+=($!)
watchKernel >> "$events" &
pids+=($!)
upower --monitor-detail > "$work/upower" 2>&1 &
pids+=($!)

mark "mouse left alone"
echo
echo "Leave the mouse alone for $minutes minutes so it falls asleep. Press Enter to stop early."
waitOrEnter "$((minutes * 60))" "  %d min %d s left"

mark "mouse moved"
printf "\a"
echo
echo "Move the mouse now for a few seconds. This keeps watching for 30 s more, or press Enter to finish."
waitOrEnter 30 "  %d min %d s left"
mark "done"

for pid in "${pids[@]}"; do
    pkill -P "$pid" 2>/dev/null
    kill "$pid" 2>/dev/null
done
pids=()

alone=$(phaseUnknown "mouse left alone")
moved=$(phaseUnknown "mouse moved")
{
    echo "Sleep report"
    echo "kernel: $(uname -r)"
    echo "upower: $(upower --version 2>/dev/null | tr '\n' ' ')"
    echo "mouse battery: $(logitech-mouse-battery --version 2>/dev/null || echo "not installed")"
    echo "minutes left alone: $minutes"
    echo
    echo "==== summary ===="
    echo "kernel battery updates while left alone: $(phaseCount "mouse left alone" "[udev] change")"
    echo "kernel battery updates after moving it: $(phaseCount "mouse moved" "[udev] change")"
    if [ -n "$alone" ]; then
        echo "the battery went unknown or offline while left alone:"
        echo "$alone"
    else
        echo "the battery never went unknown or offline while left alone"
    fi
    if [ -n "$moved" ]; then
        echo "unknown or offline after moving it:"
        echo "$moved"
    fi
    echo
    echo "==== events ===="
    cat "$events"
    echo
    echo "==== upower --monitor-detail ===="
    cat "$work/upower"
} > "$report"

echo
sed -n '/^==== summary ====/,/^$/p' "$report"
echo "Done. Please send $report back."
