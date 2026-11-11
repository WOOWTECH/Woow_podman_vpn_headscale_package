#!/usr/bin/env bash
# Rebuild the Headplane 0.7.1 UI from the pinned upstream source and one ACL SSR patch.
set -euo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
IMAGE=${1:-localhost/woow-headplane:0.7.1-ssr1}
SOURCE_COMMIT=30c842ed8dbf011e78596e3a474133ec4069ba79
NODE_IMAGE=docker.io/library/node:24-bookworm@sha256:8fa96264f302f58bbb13aa7339c541547b56975766ce956c875da149452a3e40
UPSTREAM_IMAGE=ghcr.io/tale/headplane:0.7.1@sha256:394483fd2336ce6b15df360c3e118ddfd45eb8140fe21df9f2611077f5d79d22
WORK=$(mktemp -d "${TMPDIR:-/tmp}/headplane-build.XXXXXX")
trap 'rm -rf "$WORK"' EXIT

git clone --quiet --depth 1 --branch v0.7.1 https://github.com/tale/headplane.git "$WORK/source"
[[ $(git -C "$WORK/source" rev-parse HEAD) == "$SOURCE_COMMIT" ]] || {
  echo "Headplane v0.7.1 source commit changed" >&2
  exit 1
}
git -C "$WORK/source" apply --unidiff-zero --check "$REPO/patches/headplane-acl-ssr.patch"
git -C "$WORK/source" apply --unidiff-zero "$REPO/patches/headplane-acl-ssr.patch"

podman image exists "$NODE_IMAGE" || podman pull "$NODE_IMAGE" >/dev/null
podman image exists "$UPSTREAM_IMAGE" || podman pull "$UPSTREAM_IMAGE" >/dev/null
podman run --rm --pull=never --volume "$WORK/source:/src:Z" --workdir /src \
  --entrypoint /bin/sh "$NODE_IMAGE" -c \
  'corepack pnpm install --frozen-lockfile && corepack pnpm run build'

mkdir -p "$WORK/image"
cp -a "$WORK/source/build" "$WORK/image/build"
cp "$REPO/Containerfile.headplane" "$WORK/image/Containerfile"
podman build --format docker --timestamp 0 --pull=never --tag "$IMAGE" \
  --file "$WORK/image/Containerfile" "$WORK/image"
