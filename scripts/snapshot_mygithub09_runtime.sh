#!/usr/bin/env bash
set -euo pipefail

COMPOSE_FILE="${MYGITHUB10_COMPOSE_FILE:-/opt/github-action-service/docker-compose.yml}"
CONTAINER="${MYGITHUB10_CONTAINER_NAME:-github-action-service}"
OUTPUT=""
if [[ "${1:-}" == "--output" && -n "${2:-}" ]]; then OUTPUT="$2"; fi
if [[ -z "$OUTPUT" ]]; then
  echo "SNAPSHOT_OUTPUT_REQUIRED" >&2
  exit 2
fi
if [[ ! -f "$COMPOSE_FILE" ]]; then echo "SNAPSHOT_COMPOSE_NOT_FOUND" >&2; exit 2; fi
if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then echo "SNAPSHOT_CONTAINER_NOT_FOUND" >&2; exit 2; fi

install -d -m 700 "$OUTPUT"
stamp="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
{
  echo "snapshot_id=$(basename "$OUTPUT")"
  echo "created_at=$stamp"
  echo "container_name=$CONTAINER"
  echo "compose_file=$COMPOSE_FILE"
  echo "compose_config_sha256=$(sha256sum "$COMPOSE_FILE" | awk '{print $1}')"
  echo "previous_image_name=$(docker inspect --format '{{.Config.Image}}' "$CONTAINER")"
  echo "previous_image_id=$(docker inspect --format '{{.Image}}' "$CONTAINER")"
  echo "restart_policy=$(docker inspect --format '{{json .HostConfig.RestartPolicy}}' "$CONTAINER")"
  echo "env_file_path=${MYGITHUB10_ENV_FILE_PATH:-/opt/github-action-service/.env}"
  echo "health_status=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$CONTAINER")"
} > "$OUTPUT/snapshot.meta"
docker inspect --format '{{json .NetworkSettings.Ports}}' "$CONTAINER" > "$OUTPUT/ports.json"
docker inspect --format '{{json .Mounts}}' "$CONTAINER" > "$OUTPUT/mounts.json"
docker inspect --format '{{json .NetworkSettings.Networks}}' "$CONTAINER" > "$OUTPUT/networks.json"
chmod 600 "$OUTPUT"/*
echo "SNAPSHOT_CREATED:$OUTPUT"
