#!/usr/bin/env bash
# builds the release tarball from the last commit, which the Arch package and the AUR recipe build from, and which
# comes out the same bytes on every run, thus its checksum holds
set -eu

repo=$(cd "$(dirname "$0")/.." && pwd)
version=$(sed -n 's/^VERSION = "\([^"]*\)"$/\1/p' "$repo/src/version.py")
mkdir -p "$repo/dist"
cd "$repo"
git archive --format=tar.gz --prefix="logitech-mouse-battery-$version/" -o "dist/logitech-mouse-battery-$version.tar.gz" HEAD
