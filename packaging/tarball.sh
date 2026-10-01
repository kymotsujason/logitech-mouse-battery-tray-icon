#!/usr/bin/env bash
# builds the release tarball from the last commit, which the Arch package and the AUR recipe build from. The AUR
# recipe's checksum holds since buildArch.sh hashes this same file and the release publishes it
set -eu

repo=$(cd "$(dirname "$0")/.." && pwd)
version=$(sed -n 's/^VERSION = "\([^"]*\)"$/\1/p' "$repo/src/version.py")
mkdir -p "$repo/dist"
cd "$repo"
git archive --format=tar.gz --prefix="logitech-mouse-battery-$version/" -o "dist/logitech-mouse-battery-$version.tar.gz" HEAD
