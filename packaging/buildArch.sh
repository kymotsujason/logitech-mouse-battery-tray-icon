#!/usr/bin/env bash
# builds the Arch package from the tarball in dist/ inside an Arch container as a plain user, since makepkg refuses
# root, and leaves the package in dist/ plus the checksummed PKGBUILD and .SRCINFO in dist/aur/
set -eu

repo=$(cd "$(dirname "$0")/.." && pwd)
version=$(sed -n 's/^VERSION = "\([^"]*\)"$/\1/p' "$repo/version.py")
tarball="logitech-mouse-battery-$version.tar.gz"
if [ ! -f "$repo/dist/$tarball" ]; then
    echo "dist/$tarball is missing, run packaging/tarball.sh first" >&2
    exit 1
fi
mkdir -p "$repo/dist/aur"
docker run --rm -v "$repo:/repo" -e TARBALL="$tarball" -e OWNER="$(id -u):$(id -g)" archlinux:latest bash -c '
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
cp /repo/packaging/aur/PKGBUILD "/repo/dist/$TARBALL" /build/
chown -R builder /build
su builder -c "cd /build && updpkgsums && makepkg --force --nodeps && makepkg --printsrcinfo > .SRCINFO"
cp /build/*.pkg.tar.zst /repo/dist/
cp /build/PKGBUILD /build/.SRCINFO /repo/dist/aur/
chown -R "$OWNER" /repo/dist
'
