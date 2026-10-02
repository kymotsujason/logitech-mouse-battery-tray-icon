#!/usr/bin/env bash
# builds the .deb and .rpm into dist/ with nfpm's own image, which avoids installing nfpm, and runs it as the
# calling user, so the packages in dist/ aren't owned by root
set -eu

repo=$(cd "$(dirname "$0")/.." && pwd)
version=$(sed -n 's/^VERSION = "\([^"]*\)"$/\1/p' "$repo/src/version.py")
mkdir -p "$repo/dist"
# a tag can be moved to a different image, so the one the packages are built in is named by its digest, and a new nfpm comes in as a commit
image=goreleaser/nfpm:v2.47.0@sha256:a662cb167d7b6d3a83920c83d76b12d02b8ac5dd2c13e5c62c15270b23f6df0c
for packager in deb rpm; do
    docker run --rm --user "$(id -u):$(id -g)" -e VERSION="$version" -v "$repo:/work" -w /work "$image" package --config packaging/nfpm.yaml --packager "$packager" --target dist/
done
