#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
IMAGE="localhost/sxt-notification-go-node:1.26.4-22"
podman image exists docker.io/library/golang:1.26.4
podman image exists docker.io/library/node:22
TMPDIR=/tmp BUILDAH_TMPDIR=/tmp buildah bud --pull=never --network=none -f "$DIR/Dockerfile.sxt-notification-go-node" -t "$IMAGE" "$DIR"
podman run --rm --pull=never --network=none "$IMAGE" sh -c 'go version && node --version'
podman image inspect --format '{{.Id}}' "$IMAGE"
