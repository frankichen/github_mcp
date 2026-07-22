#!/usr/bin/env bash
set -euo pipefail

# Safe rollback for the actual production Compose layout discovered on de.
# It never deletes databases/images/releases and never runs a down migration.
COMPOSE_FILE="${MYGITHUB10_COMPOSE_FILE:-/opt/github-action-service/docker-compose.yml}"
SERVICE="github-action-service"
CONTAINER="github-action-service"
BACKUP_ROOT="${MYGITHUB10_ROLLBACK_BACKUP_ROOT:-/var/backups/github-action-service}"
CONFIRM=false
SNAPSHOT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --snapshot) SNAPSHOT="${2:-}"; shift 2;;
    --confirm) CONFIRM=true; shift;;
    *) echo "ROLLBACK_ARGUMENT_INVALID" >&2; exit 2;;
  esac
done

if [[ -z "$SNAPSHOT" || ! -d "$SNAPSHOT" || ! -f "$SNAPSHOT/snapshot.meta" ]]; then
  echo "ROLLBACK_SNAPSHOT_REQUIRED" >&2
  exit 2
fi
previous_image_id="$(awk -F= '$1=="previous_image_id" {print substr($0,index($0,"=")+1)}' "$SNAPSHOT/snapshot.meta")"
snapshot_compose="$(awk -F= '$1=="compose_file" {print substr($0,index($0,"=")+1)}' "$SNAPSHOT/snapshot.meta")"
COMPOSE_FILE="${snapshot_compose:-$COMPOSE_FILE}"
if [[ -z "$previous_image_id" ]]; then echo "ROLLBACK_SNAPSHOT_INVALID" >&2; exit 2; fi

if [[ ! -f "$COMPOSE_FILE" ]]; then
  echo "ROLLBACK_BLOCKED: compose file not found: $COMPOSE_FILE" >&2
  exit 2
fi

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup="$BACKUP_ROOT/mygithub10-rollback-$stamp"

echo "Rollback plan (no changes will be made unless --confirm is supplied):"
echo "  compose_file=$COMPOSE_FILE"
echo "  service=$SERVICE container=$CONTAINER"
echo "  previous_image_id=$previous_image_id"
echo "  ports=127.0.0.1:8765->8000,100.118.124.97:8788->8000"
echo "  volumes=github_action_data:/data,/var/lib/private-ci:/data/private-ci"
echo "  restart_policy=unless-stopped"
echo "  env_file=/opt/github-action-service/.env (contents are not copied or printed)"
echo "  backup_dir=$backup"
if ! $CONFIRM; then exit 0; fi

install -d -m 700 "$backup"
{
  echo "compose_file=$COMPOSE_FILE"
  echo "service=$SERVICE"
  echo "container=$CONTAINER"
  echo "previous_image_id=$previous_image_id"
  echo "ports=127.0.0.1:8765->8000,100.118.124.97:8788->8000"
  echo "volumes=github_action_data:/data,/var/lib/private-ci:/data/private-ci"
  echo "restart_policy=unless-stopped"
  echo "env_file=/opt/github-action-service/.env"
} > "$backup/rollback-metadata.txt"
docker image inspect "$previous_image_id" >/dev/null 2>&1 || { echo "ROLLBACK_IMAGE_NOT_FOUND" >&2; exit 3; }
override="$backup/compose.override.yml"
cat > "$override" <<EOF
services:
  $SERVICE:
    image: $previous_image_id
EOF
chmod 600 "$override"
docker compose -f "$COMPOSE_FILE" -f "$override" up -d --no-build --force-recreate "$SERVICE"
current_id="$(docker inspect --format '{{.Image}}' "$CONTAINER" 2>/dev/null || true)"
if [[ "$current_id" != "$previous_image_id" ]]; then echo "ROLLBACK_IMAGE_MISMATCH" >&2; exit 4; fi
health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$CONTAINER" 2>/dev/null || true)"
if [[ "$health" == "unhealthy" ]]; then echo "ROLLBACK_HEALTH_FAILED" >&2; exit 5; fi
echo "Rollback completed; backup=$backup health=$health"
