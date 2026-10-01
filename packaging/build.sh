#!/usr/bin/env bash
# builds the .deb and .rpm into dist/ with nfpm's own image, which avoids installing nfpm, and runs it as the
# calling user, thus the packages in dist/ aren't owned by root
set -eu

repo=$(cd "$(dirname "$0")/.." && pwd)
version=$(sed -n 's/^VERSION = "\([^"]*\)"$/\1/p' "$repo/src/version.py")
mkdir -p "$repo/dist"
for packager in deb rpm; do
    docker run --rm --user "$(id -u):$(id -g)" -e VERSION="$version" -v "$repo:/work" -w /work goreleaser/nfpm:v2.47.0 package --config packaging/nfpm.yaml --packager "$packager" --target dist/
done
