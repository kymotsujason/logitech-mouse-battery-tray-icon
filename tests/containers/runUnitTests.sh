#!/usr/bin/env bash
# runs the unit suite on each target distro's own Python and PyQt6 through runSuite.sh, which puts Python 3.11 and
# Qt 6.4 under test and prints the versions each one ran
set -u

repo=$(cd "$(dirname "$0")/../.." && pwd)
failures=0

runIn() {
    local image=$1
    local install=$2
    echo "== $image"
    docker run --rm -v "$repo:/repo:ro" "$image" bash /repo/tests/containers/runSuite.sh "$install"
    local status=$?
    if [ "$status" -ne 0 ]; then
        echo "FAIL $image (status $status)"
        failures=$((failures + 1))
    else
        echo "PASS $image"
    fi
}

aptInstall="export DEBIAN_FRONTEND=noninteractive; apt-get update -qq && apt-get install -y -qq --no-install-recommends python3 python3-pyqt6"
runIn debian:12 "$aptInstall"
runIn ubuntu:24.04 "$aptInstall"
runIn archlinux:latest "pacman -Syu --noconfirm --needed python python-pyqt6"

if [ "$failures" -gt 0 ]; then
    exit 1
fi
