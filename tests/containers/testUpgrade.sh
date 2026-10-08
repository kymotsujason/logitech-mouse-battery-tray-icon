#!/usr/bin/env bash
# upgrades a running 1.0.1 tray and a running 1.1.0 tray to the built version in Arch containers and checks the same
# process restarts into the built version each time
set -u

repo=$(cd "$(dirname "$0")/../.." && pwd)
version=$(sed -n 's/^VERSION = "\([^"]*\)"$/\1/p' "$repo/src/version.py")
old=$(mktemp -d)
trap 'rm -rf "$old"' EXIT
status=0
for oldVersion in 1.0.1 1.1.0; do
    oldPackage=logitech-mouse-battery-$oldVersion-1-any.pkg.tar.zst
    if ! curl -fsSL -o "$old/$oldPackage" "https://github.com/kymotsujason/logitech-mouse-battery-tray-icon/releases/download/v$oldVersion/$oldPackage"; then
        echo "FAIL couldn't download the $oldVersion package"
        exit 1
    fi
    echo "--- upgrading a running $oldVersion tray to $version"
    docker run --rm -v "$repo:/repo:ro" -v "$old:/old:ro" archlinux:latest bash /repo/tests/containers/upgradeInside.sh "/old/$oldPackage" "/repo/dist/logitech-mouse-battery-$version-1-any.pkg.tar.zst" "$version" || status=1
done
exit $status
