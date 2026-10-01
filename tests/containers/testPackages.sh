#!/usr/bin/env bash
# installs each built package from dist/ in a clean container of its distro and runs checkPackage.sh there
set -u

repo=$(cd "$(dirname "$0")/../.." && pwd)
version=$(sed -n 's/^VERSION = "\([^"]*\)"$/\1/p' "$repo/src/version.py")
failures=0

runIn() {
    local image=$1
    local kind=$2
    local package=$3
    echo "== $image"
    if [ ! -f "$repo/dist/$package" ]; then
        echo "FAIL $image: dist/$package is missing"
        failures=$((failures + 1))
        return
    fi
    docker run --rm -v "$repo/dist:/dist:ro" -v "$repo/tests/containers/checkPackage.sh:/checkPackage.sh:ro" "$image" bash /checkPackage.sh "$kind" "/dist/$package" "$version"
    if [ $? -ne 0 ]; then
        failures=$((failures + 1))
    fi
}

runIn debian:12 deb "logitech-mouse-battery_${version}_all.deb"
runIn ubuntu:24.04 deb "logitech-mouse-battery_${version}_all.deb"
runIn fedora:latest rpm "logitech-mouse-battery-${version}-1.noarch.rpm"
runIn archlinux:latest arch "logitech-mouse-battery-${version}-1-any.pkg.tar.zst"

if [ "$failures" -gt 0 ]; then
    exit 1
fi
