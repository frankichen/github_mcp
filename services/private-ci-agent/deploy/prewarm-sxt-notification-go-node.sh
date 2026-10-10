#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
IMAGE="localhost/sxt-notification-go-node:1.26.4-22"
podman image exists docker.io/library/golang:1.26.4
podman image exists docker.io/library/node:22
podman build --pull=never -f "$DIR/Dockerfile.sxt-notification-go-node" -t "$IMAGE" "$DIR"
podman run --rm --pull=never "$IMAGE" sh -c 'go version && node --version'
podman image inspect --format '{{.Id}}' "$IMAGE"
