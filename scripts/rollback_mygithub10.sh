#!/usr/bin/env bash
set -euo pipefail

# Safe rollback for the actual production Compose layout discovered on de.
# It never deletes databases/images/releases and never runs a down migration.
COMPOSE_FILE="${MYGITHUB10_COMPOSE_FILE:-/opt/github-action-service/docker-compose.yml}"
SERVICE="github-action-service"
CONTAINER="github-action-service"
BACKUP_ROOT="${MYGITHUB10_ROLLBACK_BACKUP_ROOT:-/var/backups/github-action-service}"
CONFIRM=false
[[ "${1:-}" == "--confirm" ]] && CONFIRM=true

if [[ ! -f "$COMPOSE_FILE" ]]; then
  echo "ROLLBACK_BLOCKED: compose file not found: $COMPOSE_FILE" >&2
  exit 2
fi

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup="$BACKUP_ROOT/mygithub10-rollback-$stamp"
old_image="$(docker inspect --format '{{.Config.Image}}' "$CONTAINER" 2>/dev/null || true)"
old_id="$(docker inspect --format '{{.Image}}' "$CONTAINER" 2>/dev/null || true)"

echo "Rollback plan (no changes will be made unless --confirm is supplied):"
echo "  compose_file=$COMPOSE_FILE"
echo "  service=$SERVICE container=$CONTAINER"
echo "  old_image=$old_image"
echo "  old_image_id=$old_id"
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
  echo "old_image=$old_image"
  echo "old_image_id=$old_id"
  echo "ports=127.0.0.1:8765->8000,100.118.124.97:8788->8000"
  echo "volumes=github_action_data:/data,/var/lib/private-ci:/data/private-ci"
  echo "restart_policy=unless-stopped"
  echo "env_file=/opt/github-action-service/.env"
} > "$backup/rollback-metadata.txt"
docker inspect --format '{{json .State}}' "$CONTAINER" > "$backup/old-container-state.json"
docker image inspect "$old_image" --format 'id={{.Id}} repo_tags={{json .RepoTags}} created={{.Created}}' > "$backup/old-image-metadata.txt"

docker compose -f "$COMPOSE_FILE" up -d --no-build --force-recreate "$SERVICE"
echo "Rollback requested successfully; backup=$backup"
