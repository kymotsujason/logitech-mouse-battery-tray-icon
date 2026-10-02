#!/usr/bin/env bash
# builds the Arch package from the tarball in dist/ inside an Arch container as a plain user, since makepkg refuses
# root, and leaves the package in dist/ plus the checksummed PKGBUILD and .SRCINFO in dist/aur/
set -eu

repo=$(cd "$(dirname "$0")/.." && pwd)
version=$(sed -n 's/^VERSION = "\([^"]*\)"$/\1/p' "$repo/src/version.py")
tarball="logitech-mouse-battery-$version.tar.gz"
if [ ! -f "$repo/dist/$tarball" ]; then
    echo "dist/$tarball is missing, run packaging/tarball.sh first" >&2
    exit 1
fi
mkdir -p "$repo/dist/aur"
# archlinux:latest from 2026-09-21, named by its digest since a tag can be moved to a different image, while pacman checks the signature of every package -Syu brings in
image=archlinux@sha256:f3691b4dde62ba4c4b6f0ae2c1fbf28e8c0c8c4b9a35c7e06dc1f70e21aa29f6
docker run --rm -v "$repo:/repo" -e TARBALL="$tarball" -e OWNER="$(id -u):$(id -g)" "$image" bash -c '
set -eu
# the image turns on debug packages, which need debugedit even for a package with no binaries
if ! pacman -Syu --noconfirm --needed fakeroot debugedit pacman-contrib > /tmp/pacman.log 2>&1; then
    echo "pacman -Syu failed:"
    cat /tmp/pacman.log
    exit 1
fi
useradd -m builder
echo "PACKAGER=\"kymotsujason <kymotsujason@gmail.com>\"" > /home/builder/.makepkg.conf
chown builder /home/builder/.makepkg.conf
mkdir /build
cp /repo/packaging/aur/PKGBUILD /repo/packaging/aur/logitech-mouse-battery.install "/repo/dist/$TARBALL" /build/
chown -R builder /build
su builder -c "cd /build && updpkgsums && makepkg --force --nodeps && makepkg --printsrcinfo > .SRCINFO"
cp /build/*.pkg.tar.zst /repo/dist/
cp /build/PKGBUILD /build/.SRCINFO /build/logitech-mouse-battery.install /repo/dist/aur/
chown -R "$OWNER" /repo/dist
'
