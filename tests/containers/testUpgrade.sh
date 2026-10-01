#!/usr/bin/env bash
# upgrades a running 1.0.1 tray to the built 1.1.0 in an Arch container and checks the same process restarts into 1.1.0
set -u

repo=$(cd "$(dirname "$0")/../.." && pwd)
version=$(sed -n 's/^VERSION = "\([^"]*\)"$/\1/p' "$repo/src/version.py")
old=$(mktemp -d)
trap 'rm -rf "$old"' EXIT
oldPackage=logitech-mouse-battery-1.0.1-1-any.pkg.tar.zst
if ! curl -fsSL -o "$old/$oldPackage" "https://github.com/kymotsujason/logitech-mouse-battery-tray-icon/releases/download/v1.0.1/$oldPackage"; then
    echo "FAIL couldn't download the 1.0.1 package"
    exit 1
fi
docker run --rm -v "$repo:/repo:ro" -v "$old:/old:ro" archlinux:latest bash /repo/tests/containers/upgradeInside.sh "/old/$oldPackage" "/repo/dist/logitech-mouse-battery-$version-1-any.pkg.tar.zst"
