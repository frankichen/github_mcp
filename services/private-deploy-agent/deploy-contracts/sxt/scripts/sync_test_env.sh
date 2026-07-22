#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

REMOTE_HOST="${REMOTE_HOST:-root@gongshi-test}"
REMOTE_ENV_FILE="${REMOTE_ENV_FILE:-/home/dly/env/lenshub.env}"
LOCAL_ENV_FILE="${LOCAL_ENV_FILE:-${ROOT_DIR}/.env.test/lenshub.env}"
REMOTE_COMPOSE_DIR="${REMOTE_COMPOSE_DIR:-/home/dly/src/sxt}"
REMOTE_COMPOSE_ENV="${REMOTE_COMPOSE_ENV:-/home/dly/env/compose.env}"
REMOTE_COMPOSE_FILE="${REMOTE_COMPOSE_FILE:-/home/dly/src/sxt/deploy/compose/docker-compose.test.yml}"
REMOTE_HTTPS_OVERRIDE="${REMOTE_HTTPS_OVERRIDE:-${REMOTE_COMPOSE_DIR}/deploy/compose/docker-compose.https.override.yml}"
HEALTH_URL="${HEALTH_URL:-https://server.winpozo.com/public/v1/health}"

usage() {
  cat <<'MSG'
Usage:
  scripts/sync_test_env.sh pull
  scripts/sync_test_env.sh edit
  scripts/sync_test_env.sh push
  scripts/sync_test_env.sh restart
  scripts/sync_test_env.sh status

Default mapping:
  remote: root@gongshi-test:/home/dly/env/lenshub.env
  local:  .env.test/lenshub.env

Commands:
  pull     Download the remote env file to the local ignored path.
  edit     Pull, open $EDITOR, then ask before upload/restart.
  push     Upload the local env file, back up the remote file, restart services.
  restart  Recreate api/worker/scheduler so env changes take effect.
  status   Show remote env metadata and key names only, not values.

Overrides:
  REMOTE_HOST=root@gongshi-test
  REMOTE_ENV_FILE=/home/dly/env/lenshub.env
  LOCAL_ENV_FILE=/absolute/path/to/lenshub.env
  HEALTH_URL=https://server.winpozo.com/public/v1/health
  ALLOW_LOCAL_ENDPOINTS=1  # allow localhost/127.0.0.1 endpoint values on push
  REMOTE_HTTPS_OVERRIDE=/home/dly/src/sxt/deploy/compose/docker-compose.https.override.yml
MSG
}

die() {
  echo "[sync-test-env] error: $*" >&2
  exit 1
}

log() {
  echo "[sync-test-env] $*"
}

quote() {
  printf "%q" "$1"
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

assert_safe_remote_env_file() {
  case "$REMOTE_ENV_FILE" in
    /home/dly/env/*.env) ;;
    *) die "REMOTE_ENV_FILE must be under /home/dly/env and end with .env: $REMOTE_ENV_FILE" ;;
  esac
}

local_backup_if_exists() {
  if [[ -f "$LOCAL_ENV_FILE" ]]; then
    local backup="${LOCAL_ENV_FILE}.bak-$(date +%Y%m%d%H%M%S)"
    cp -p "$LOCAL_ENV_FILE" "$backup"
    chmod 600 "$backup"
    log "local backup created: $backup"
  fi
}

pull_env() {
  require_command ssh
  require_command scp
  assert_safe_remote_env_file

  mkdir -p "$(dirname "$LOCAL_ENV_FILE")"
  local_backup_if_exists
  scp -p "${REMOTE_HOST}:${REMOTE_ENV_FILE}" "$LOCAL_ENV_FILE"
  chmod 600 "$LOCAL_ENV_FILE"
  log "downloaded remote env to: $LOCAL_ENV_FILE"
}

require_local_env() {
  [[ -f "$LOCAL_ENV_FILE" ]] || die "local env file not found: $LOCAL_ENV_FILE; run pull first"
}

env_has_key() {
  local key="$1"
  grep -Eq "^${key}=" "$LOCAL_ENV_FILE"
}

validate_local_env() {
  require_local_env

  local required=(
    APP_ENV
    DATABASE_URL
    PUBLIC_API_BASE_URL
    AUTH_BASE_URL
    JWT_SIGNING_KEY
    DEVICE_SECRET_ENCRYPTION_KEY
  )
  local key
  for key in "${required[@]}"; do
    env_has_key "$key" || die "missing required key in local env: $key"
  done

  if [[ "${ALLOW_LOCAL_ENDPOINTS:-}" != "1" ]]; then
    if grep -Eq '^(DATABASE_URL|REDIS_ADDR|RABBITMQ_URL)=.*(localhost|127[.]0[.]0[.]1|\[::1\])' "$LOCAL_ENV_FILE"; then
      die "refusing to upload localhost endpoints; set ALLOW_LOCAL_ENDPOINTS=1 to override"
    fi
  fi
}

restart_services() {
  require_command ssh
  local remote_cmd
  remote_cmd="REMOTE_COMPOSE_DIR=$(quote "$REMOTE_COMPOSE_DIR") REMOTE_COMPOSE_ENV=$(quote "$REMOTE_COMPOSE_ENV") REMOTE_COMPOSE_FILE=$(quote "$REMOTE_COMPOSE_FILE") REMOTE_HTTPS_OVERRIDE=$(quote "$REMOTE_HTTPS_OVERRIDE") HEALTH_URL=$(quote "$HEALTH_URL") bash -s"

  ssh "$REMOTE_HOST" "$remote_cmd" <<'REMOTE'
set -euo pipefail
cd "$REMOTE_COMPOSE_DIR"
docker compose --env-file "$REMOTE_COMPOSE_ENV" \
  -f "$REMOTE_COMPOSE_FILE" \
  -f "$REMOTE_HTTPS_OVERRIDE" \
  up -d --no-deps --force-recreate api worker scheduler

for _ in $(seq 1 30); do
  if curl -fsS -m 5 "$HEALTH_URL" >/dev/null; then
    docker compose --env-file "$REMOTE_COMPOSE_ENV" \
      -f "$REMOTE_COMPOSE_FILE" \
      -f "$REMOTE_HTTPS_OVERRIDE" \
      ps --format 'table {{.Name}}\t{{.Service}}\t{{.State}}\t{{.Status}}'
    exit 0
  fi
  sleep 2
done

echo "health check failed after restart: $HEALTH_URL" >&2
exit 1
REMOTE
  log "services restarted and health check passed"
}

push_env() {
  require_command ssh
  require_command scp
  assert_safe_remote_env_file
  validate_local_env

  local remote_tmp="/tmp/lenshub-env-upload-$(date +%Y%m%d%H%M%S)-$$.env"
  scp -p "$LOCAL_ENV_FILE" "${REMOTE_HOST}:${remote_tmp}"

  local remote_cmd
  remote_cmd="REMOTE_ENV_FILE=$(quote "$REMOTE_ENV_FILE") REMOTE_TMP=$(quote "$remote_tmp") bash -s"
  ssh "$REMOTE_HOST" "$remote_cmd" <<'REMOTE'
set -euo pipefail
backup="${REMOTE_ENV_FILE}.bak-$(date +%Y%m%d%H%M%S)-sync"
cp -a "$REMOTE_ENV_FILE" "$backup"
install -m 600 -o root -g root "$REMOTE_TMP" "$REMOTE_ENV_FILE"
rm -f "$REMOTE_TMP"
echo "remote backup created: $backup"
REMOTE

  restart_services
}

status_env() {
  require_command ssh
  assert_safe_remote_env_file
  local remote_cmd
  remote_cmd="REMOTE_ENV_FILE=$(quote "$REMOTE_ENV_FILE") bash -s"
  ssh "$REMOTE_HOST" "$remote_cmd" <<'REMOTE'
set -euo pipefail
find "$(dirname "$REMOTE_ENV_FILE")" -maxdepth 1 -type f -name '*.env' -printf '%m %u %g %s %p\n' | sort
echo
echo "keys in $REMOTE_ENV_FILE:"
awk -F= 'NF && $1 !~ /^#/ {print $1}' "$REMOTE_ENV_FILE" | sort
REMOTE
}

edit_env() {
  pull_env
  "${EDITOR:-vi}" "$LOCAL_ENV_FILE"
  echo
  read -r -p "Upload edited env to ${REMOTE_HOST}:${REMOTE_ENV_FILE} and restart api/worker/scheduler? [y/N] " answer
  case "$answer" in
    y|Y|yes|YES) push_env ;;
    *) log "upload skipped; local file remains at: $LOCAL_ENV_FILE" ;;
  esac
}

main() {
  case "${1:-}" in
    pull) pull_env ;;
    edit) edit_env ;;
    push) push_env ;;
    restart) restart_services ;;
    status) status_env ;;
    -h|--help|help|"") usage ;;
    *) usage >&2; die "unknown command: $1" ;;
  esac
}

main "$@"
