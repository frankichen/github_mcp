#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

REMOTE_HOST="${REMOTE_HOST:-root@gongshi-test}"
REMOTE_RELEASE_ROOT="${REMOTE_RELEASE_ROOT:-/home/dly/releases}"
REMOTE_COMPOSE_DIR="${REMOTE_COMPOSE_DIR:-/home/dly/src/sxt}"
REMOTE_COMPOSE_ENV="${REMOTE_COMPOSE_ENV:-/home/dly/env/compose.env}"
REMOTE_COMPOSE_FILE="${REMOTE_COMPOSE_FILE:-/home/dly/src/sxt/deploy/compose/docker-compose.test.yml}"
REMOTE_HTTPS_OVERRIDE="${REMOTE_HTTPS_OVERRIDE:-/home/dly/src/sxt/deploy/compose/docker-compose.https.override.yml}"
HEALTH_URL="${HEALTH_URL:-http://gongshi-test/public/v1/health}"
ENVIRONMENT_URL="${ENVIRONMENT_URL:-http://gongshi-test}"
LOCAL_RELEASE_BASE="${LOCAL_RELEASE_BASE:-${ROOT_DIR}/dist/gongshi-test}"
LOCAL_TEST_DATABASE_URL="${LOCAL_TEST_DATABASE_URL:-${DATABASE_URL:?set DATABASE_URL for local verification}}"
GIT_CONNECT_TIMEOUT="${GIT_CONNECT_TIMEOUT:-20}"
GIT_LOW_SPEED_TIME="${GIT_LOW_SPEED_TIME:-30}"
GIT_LOW_SPEED_LIMIT="${GIT_LOW_SPEED_LIMIT:-1}"

DEPLOY_STARTED_AT="$(date +%s)"
DEPLOY_STEP=0
DEPLOY_TOTAL_STEPS=5

DRY_RUN=0
BUILD_ONLY=0
WITH_FRONTEND=0
EXPECTED_GIT_SHA="${EXPECTED_GIT_SHA:-}"
REPORT_JSON=""

usage() {
  cat <<'MSG'
Usage:
  bash scripts/deploy_gongshi_test.sh [options]

Options:
  --yes             Deprecated compatibility flag; deployment is non-interactive by default.
  --dry-run         Validate git state and print the release plan without building or deploying.
  --build-only      Build the release package locally, but do not upload or deploy.
  --expected-sha SHA Require exact 40-character main SHA after sync.
  --report-json PATH Write a redacted single JSON deployment result.
  --with-frontend   Include admin, foreground H5, tester, docs, and OpenAPI static assets.
  -h, --help        Show this help.

Environment overrides:
  REMOTE_HOST=root@gongshi-test
  REMOTE_RELEASE_ROOT=/home/dly/releases
  REMOTE_COMPOSE_DIR=/home/dly/src/sxt
  REMOTE_COMPOSE_ENV=/home/dly/env/compose.env
  REMOTE_COMPOSE_FILE=/home/dly/src/sxt/deploy/compose/docker-compose.test.yml
  REMOTE_HTTPS_OVERRIDE=/home/dly/src/sxt/deploy/compose/docker-compose.https.override.yml
  HEALTH_URL=http://gongshi-test/public/v1/health
  ENVIRONMENT_URL=http://gongshi-test
  LOCAL_RELEASE_BASE=dist/gongshi-test
  LOCAL_TEST_DATABASE_URL=${DATABASE_URL:?set DATABASE_URL for local verification}
  GIT_CONNECT_TIMEOUT=20       Git connection timeout in seconds
  GIT_LOW_SPEED_TIME=30        Abort when transfer is below 1 byte/s for this long
MSG
}

die() {
  echo "[deploy-gongshi-test] error: $*" >&2
  exit 1
}

log() {
  local elapsed
  elapsed=$(( $(date +%s) - DEPLOY_STARTED_AT ))
  echo "[deploy-gongshi-test +${elapsed}s] $*"
}

step() {
  DEPLOY_STEP=$((DEPLOY_STEP + 1))
  log "[$DEPLOY_STEP/$DEPLOY_TOTAL_STEPS] $*"
}

configure_sudo_environment() {
  if [[ -z "${SUDO_USER:-}" || "${SUDO_USER}" == "root" ]]; then
    return
  fi

  local invoking_home
  invoking_home="$(getent passwd "$SUDO_USER" | cut -d: -f6)"
  if [[ -n "$invoking_home" ]]; then
    # sudo normally switches HOME to /root and loses the user's Git proxy and
    # Go module cache. Keep the deployment's network settings from the caller.
    [[ -f "$invoking_home/.gitconfig" ]] && export GIT_CONFIG_GLOBAL="$invoking_home/.gitconfig"
    [[ -d "$invoking_home/go" ]] && export GOPATH="$invoking_home/go"
    log "sudo detected; using $SUDO_USER's Git config and Go cache"
  fi
}

git_remote() {
  GIT_TERMINAL_PROMPT=0 git \
    -c "http.connectTimeout=$GIT_CONNECT_TIMEOUT" \
    -c "http.lowSpeedLimit=$GIT_LOW_SPEED_LIMIT" \
    -c "http.lowSpeedTime=$GIT_LOW_SPEED_TIME" \
    "$@"
}

quote() {
  printf "%q" "$1"
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

# Go's default `GOPROXY=https://proxy.golang.org,direct` only falls back to
# direct for 404/410 responses.  A timeout therefore aborts the deployment,
# even when the module is already present in the local download cache.
go_proxy_for_build() {
  local configured_proxy module_cache
  configured_proxy="$(go env GOPROXY)"
  [[ "$configured_proxy" == "off" ]] && {
    printf '%s\n' "off"
    return
  }

  module_cache="$(go env GOMODCACHE)/cache/download"
  if [[ -d "$module_cache" ]]; then
    # Pipe separators make Go continue on transient errors (including timeouts).
    # The local file proxy makes cached modules independent of the network.
    configured_proxy="${configured_proxy//,/|}"
    printf 'file://%s|%s\n' "$module_cache" "$configured_proxy"
  else
    printf '%s\n' "${configured_proxy//,/|}"
  fi
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --yes) ;;
      --dry-run) DRY_RUN=1 ;;
      --build-only) BUILD_ONLY=1 ;;
      --skip-verify) die "--skip-verify is not supported for safe deployments" ;;
      --expected-sha) [[ $# -ge 2 ]] || die "--expected-sha requires a full SHA"; EXPECTED_GIT_SHA="$2"; shift ;;
      --report-json) [[ $# -ge 2 ]] || die "--report-json requires a path"; REPORT_JSON="$2"; shift ;;
      --with-frontend) WITH_FRONTEND=1 ;;
      -h|--help)
        usage
        exit 0
        ;;
      *) die "unknown option: $1" ;;
    esac
    shift
  done
}

assert_safe_paths() {
  case "$REMOTE_RELEASE_ROOT" in
    /home/dly/releases) ;;
    /home/dly/releases/*) ;;
    *) die "REMOTE_RELEASE_ROOT must stay under /home/dly/releases: $REMOTE_RELEASE_ROOT" ;;
  esac

  case "$REMOTE_COMPOSE_ENV" in
    /home/dly/env/*.env) ;;
    *) die "REMOTE_COMPOSE_ENV must be an env file under /home/dly/env: $REMOTE_COMPOSE_ENV" ;;
  esac
}

assert_clean_worktree_for_switch() {
  if [[ -n "$(git status --short)" ]]; then
    die "working tree is not clean; public test deployment only allows a clean main checkout"
  fi
}

planned_restart_services() {
  if [[ "$WITH_FRONTEND" == "1" ]]; then
    printf '%s\n' "api worker scheduler web"
  else
    printf '%s\n' "api worker scheduler"
  fi
}

frontend_included_label() {
  if [[ "$WITH_FRONTEND" == "1" ]]; then
    printf '%s\n' "true"
  else
    printf '%s\n' "false"
  fi
}

sync_main() {
  require_command git

  log "fetching origin/main (timeout=${GIT_CONNECT_TIMEOUT}s, low-speed timeout=${GIT_LOW_SPEED_TIME}s)"
  git_remote fetch origin main

  local current_branch
  current_branch="$(git branch --show-current)"
  if [[ "$current_branch" != "main" ]]; then
    assert_clean_worktree_for_switch
    git switch main
  fi

  git_remote pull --ff-only origin main

  local head_sha origin_sha status
  head_sha="$(git rev-parse HEAD)"
  origin_sha="$(git rev-parse origin/main)"
  status="$(git status --short)"

  [[ "$head_sha" == "$origin_sha" ]] || die "HEAD does not match origin/main"
  [[ -z "$status" ]] || die "working tree is not clean after syncing main"
  if [[ -n "$EXPECTED_GIT_SHA" ]]; then
    [[ "$EXPECTED_GIT_SHA" =~ ^[0-9a-f]{40}$ ]] || die "DEPLOY_SHA_MISMATCH: expected SHA must be 40 lowercase hex characters"
    [[ "$head_sha" == "$EXPECTED_GIT_SHA" && "$origin_sha" == "$EXPECTED_GIT_SHA" ]] || die "DEPLOY_SHA_MISMATCH: HEAD and origin/main do not equal expected SHA"
  fi

  GIT_SHA="$head_sha"
  GIT_SHA_SHORT="$(git rev-parse --short=12 HEAD)"
}

run_verification() {
  require_command go
  if [[ -z "${DATABASE_URL:-}" ]]; then
    export DATABASE_URL="$LOCAL_TEST_DATABASE_URL"
    log "DATABASE_URL not set; using LOCAL_TEST_DATABASE_URL for local verification"
  fi

  log "running: go test ./..."
  go test ./...

  log "running: go vet ./..."
  go vet ./...

  if [[ -f "${ROOT_DIR}/scripts/validate_repo_contracts.sh" ]]; then
    log "running: bash scripts/validate_repo_contracts.sh"
    bash "${ROOT_DIR}/scripts/validate_repo_contracts.sh"
    VERIFICATION_COMMANDS_JSON='["go test ./...","go vet ./...","bash scripts/validate_repo_contracts.sh"]'
  else
    log "scripts/validate_repo_contracts.sh not found; skipped"
    VERIFICATION_COMMANDS_JSON='["go test ./...","go vet ./..."]'
  fi
}

copy_dir_contents() {
  local source_dir="$1"
  local target_dir="$2"

  [[ -d "$source_dir" ]] || die "source directory not found: $source_dir"
  mkdir -p "$target_dir"
  cp -a "${source_dir}/." "$target_dir/"
}

copy_dir_no_images() {
  local source_dir="$1"
  local target_dir="$2"

  [[ -d "$source_dir" ]] || die "source directory not found: $source_dir"
  mkdir -p "$target_dir"
  rsync -a --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.svg' --exclude='*.webp' --exclude='*.ico' --exclude='*.bmp' "${source_dir}/" "$target_dir/"
}

build_frontend_assets() {
  if [[ "$WITH_FRONTEND" != "1" ]]; then
    return
  fi

  require_command npm
  require_command rsync

  local admin_dir="${ROOT_DIR}/h5/lenshub-admin"
  local console_dir="${ROOT_DIR}/h5/lenshub-console"
  local html_dir="${LOCAL_RELEASE_DIR}/frontend/html"

  log "building admin frontend"
  log "npm ci may take a few minutes; package-manager output is shown below"
  npm --prefix "$admin_dir" ci
  npm --prefix "$admin_dir" run build

  log "building new console frontend"
  npm --prefix "$console_dir" ci
  npm --prefix "$console_dir" run build

  rm -rf "$html_dir"
  mkdir -p "$html_dir"

  copy_dir_no_images "${ROOT_DIR}/h5/lenshub-app-simulator" "$html_dir"
  copy_dir_no_images "${ROOT_DIR}/h5/lenshub-app-simulator" "${html_dir}/app"
  copy_dir_no_images "${ROOT_DIR}/h5/lenshub-app-simulator" "${html_dir}/h5"
  copy_dir_no_images "${ROOT_DIR}/h5/lenshub-flow-tester" "${html_dir}/tester"
  copy_dir_contents "${ROOT_DIR}/h5/lenshub-api-docs" "${html_dir}/docs"
  copy_dir_contents "${ROOT_DIR}/api/openapi" "${html_dir}/openapi"
  copy_dir_contents "${admin_dir}/dist" "${html_dir}/admin"
  copy_dir_contents "${console_dir}/dist" "${html_dir}/newadmin"
  mkdir -p "${LOCAL_RELEASE_DIR}/deploy/nginx"
  cp "${ROOT_DIR}/deploy/nginx/lenshub-test.conf" "${LOCAL_RELEASE_DIR}/deploy/nginx/lenshub-test.conf"

  [[ -f "${html_dir}/index.html" ]] || die "missing frontend root index.html"
  [[ -f "${html_dir}/app/index.html" ]] || die "missing frontend app index.html"
  [[ -f "${html_dir}/h5/index.html" ]] || die "missing frontend h5 index.html"
  [[ -f "${html_dir}/admin/index.html" ]] || die "missing admin index.html"
  [[ -f "${html_dir}/newadmin/index.html" ]] || die "missing newadmin index.html"
  [[ -f "${LOCAL_RELEASE_DIR}/deploy/nginx/lenshub-test.conf" ]] || die "missing frontend nginx config"
  [[ -f "${html_dir}/tester/index.html" ]] || die "missing tester index.html"
  [[ -f "${html_dir}/docs/index.html" ]] || die "missing docs index.html"
}

build_release() {
  require_command go
  require_command sha256sum

  BUILD_TIME_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  RELEASE_ID="$(date -u +%Y%m%d-%H%M%S)-${GIT_SHA_SHORT}"
  LOCAL_RELEASE_DIR="${LOCAL_RELEASE_BASE}/${RELEASE_ID}"

  rm -rf "$LOCAL_RELEASE_DIR"
  mkdir -p "$LOCAL_RELEASE_DIR"

  log "building backend release artifact natively: $LOCAL_RELEASE_DIR"
  mkdir -p "${LOCAL_RELEASE_DIR}/bin" "${LOCAL_RELEASE_DIR}/db" "${LOCAL_RELEASE_DIR}/i18n"
  (
    cd "$ROOT_DIR"
    BUILD_GOPROXY="$(go_proxy_for_build)"
    GOPROXY="$BUILD_GOPROXY" CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags="-s -w" -o "${LOCAL_RELEASE_DIR}/bin/lenshub-api" ./cmd/api
    GOPROXY="$BUILD_GOPROXY" CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags="-s -w" -o "${LOCAL_RELEASE_DIR}/bin/lenshub-worker" ./cmd/worker
    GOPROXY="$BUILD_GOPROXY" CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags="-s -w" -o "${LOCAL_RELEASE_DIR}/bin/lenshub-scheduler" ./cmd/scheduler
    GOPROXY="$BUILD_GOPROXY" CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags="-s -w" -o "${LOCAL_RELEASE_DIR}/bin/lenshub-admin-bootstrap" ./cmd/admin-bootstrap
    # goose runs inside the minimal runtime image. Build it statically just
    # like the application binaries so it does not depend on the image's
    # system dynamic linker (for example /lib64/ld-linux-x86-64.so.2).
    GOPROXY="$BUILD_GOPROXY" CGO_ENABLED=0 GOBIN="${LOCAL_RELEASE_DIR}/bin" go install -tags='no_clickhouse no_libsql no_mssql no_mysql no_sqlite3 no_vertica no_ydb' github.com/pressly/goose/v3/cmd/goose@v3.22.1
  )
  copy_dir_contents "${ROOT_DIR}/db/migrations" "${LOCAL_RELEASE_DIR}/db/migrations"
  copy_dir_contents "${ROOT_DIR}/i18n/app" "${LOCAL_RELEASE_DIR}/i18n/app"

  [[ -x "${LOCAL_RELEASE_DIR}/bin/lenshub-api" ]] || die "missing bin/lenshub-api in release artifact"
  [[ -x "${LOCAL_RELEASE_DIR}/bin/lenshub-worker" ]] || die "missing bin/lenshub-worker in release artifact"
  [[ -x "${LOCAL_RELEASE_DIR}/bin/lenshub-scheduler" ]] || die "missing bin/lenshub-scheduler in release artifact"
  [[ -x "${LOCAL_RELEASE_DIR}/bin/lenshub-admin-bootstrap" ]] || die "missing bin/lenshub-admin-bootstrap in release artifact"
  [[ -x "${LOCAL_RELEASE_DIR}/bin/goose" ]] || die "missing bin/goose in release artifact"
  [[ -d "${LOCAL_RELEASE_DIR}/db/migrations" ]] || die "missing db/migrations in release artifact"

  build_frontend_assets

  local frontend_included frontend_reason restart_services
  restart_services="$(planned_restart_services)"
  if [[ "$WITH_FRONTEND" == "1" ]]; then
    frontend_included=true
    frontend_reason="admin and foreground h5 artifacts are included under frontend/html"
  else
    frontend_included=false
    frontend_reason="run with --with-frontend after the web service has the release frontend bind mount"
  fi

  cat > "${LOCAL_RELEASE_DIR}/release-manifest.json" <<JSON
{
  "project": "frankichen/sxt",
  "environment": "public_test",
  "release_id": "${RELEASE_ID}",
  "git_branch": "main",
  "git_sha": "${GIT_SHA}",
  "dirty_worktree": false,
  "build_time_utc": "${BUILD_TIME_UTC}",
  "migration_required": true,
  "compose_change_required": false,
  "env_change_required": false,
  "frontend_included": ${frontend_included},
  "frontend_reason": "${frontend_reason}",
  "frontend_root": "frontend/html",
  "restart_services": "${restart_services}",
  "verification_commands": ${VERIFICATION_COMMANDS_JSON},
  "operator_note": "artifact-only public test deployment from clean main"
}
JSON

  (
    cd "$LOCAL_RELEASE_DIR"
    checksum_paths=(bin db i18n release-manifest.json)
    if [[ -d deploy ]]; then
      checksum_paths+=(deploy)
    fi
    if [[ -d frontend ]]; then
      checksum_paths+=(frontend)
    fi
    find "${checksum_paths[@]}" -type f -print0 \
      | sort -z \
      | xargs -0 sha256sum > checksums.sha256
  )

  log "release package ready: $LOCAL_RELEASE_DIR"
}

print_plan() {
  cat <<MSG

Deployment plan:
  type: artifact-only public_test
  release_id: ${RELEASE_ID:-<not-built-yet>}
  branch: main
  git_sha: ${GIT_SHA}
  frontend_included: $(frontend_included_label)
  migration_required: true
  compose_change_required: false
  env_change_required: false
  local_release_dir: ${LOCAL_RELEASE_DIR:-<not-built-yet>}
  remote_incoming: ${REMOTE_RELEASE_ROOT}/incoming/${RELEASE_ID:-<release_id>}
  remote_release: ${REMOTE_RELEASE_ROOT}/${RELEASE_ID:-<release_id>}
  remote_current: ${REMOTE_RELEASE_ROOT}/current
  restart_services: $(planned_restart_services)
  health_url: ${HEALTH_URL}
  environment_url: ${ENVIRONMENT_URL}
  rollback: restore previous current symlink and recreate $(planned_restart_services)
MSG
}

upload_release() {
  require_command ssh
  require_command scp

  REMOTE_INCOMING="${REMOTE_RELEASE_ROOT}/incoming/${RELEASE_ID}"
  log "creating remote incoming directory: ${REMOTE_HOST}:${REMOTE_INCOMING}"
  ssh "$REMOTE_HOST" "mkdir -p $(quote "$REMOTE_INCOMING")"

  log "uploading release package"
  scp -pr "${LOCAL_RELEASE_DIR}/." "${REMOTE_HOST}:${REMOTE_INCOMING}/"
}

activate_remote_release() {
  require_command ssh

  local remote_cmd
  remote_cmd="REMOTE_RELEASE_ROOT=$(quote "$REMOTE_RELEASE_ROOT") REMOTE_COMPOSE_DIR=$(quote "$REMOTE_COMPOSE_DIR") REMOTE_COMPOSE_ENV=$(quote "$REMOTE_COMPOSE_ENV") REMOTE_COMPOSE_FILE=$(quote "$REMOTE_COMPOSE_FILE") REMOTE_HTTPS_OVERRIDE=$(quote "$REMOTE_HTTPS_OVERRIDE") HEALTH_URL=$(quote "$HEALTH_URL") ENVIRONMENT_URL=$(quote "$ENVIRONMENT_URL") RELEASE_ID=$(quote "$RELEASE_ID") GIT_SHA=$(quote "$GIT_SHA") WITH_FRONTEND=$(quote "$WITH_FRONTEND") bash -s"

  ssh "$REMOTE_HOST" "$remote_cmd" <<'REMOTE'
set -euo pipefail

LOCK="${REMOTE_RELEASE_ROOT}/deploy.lock"
INCOMING="${REMOTE_RELEASE_ROOT}/incoming/${RELEASE_ID}"
RELEASE_DIR="${REMOTE_RELEASE_ROOT}/${RELEASE_ID}"
CURRENT="${REMOTE_RELEASE_ROOT}/current"
previous_release="$(readlink -f "$CURRENT" || true)"

exec 9>"$LOCK"
flock -n 9 || { echo "DEPLOYMENT_ALREADY_ACTIVE" >&2; exit 1; }
printf '%s\n' "release_id=${RELEASE_ID} started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)" >&9

cd "$INCOMING"
test -f release-manifest.json
test -f checksums.sha256
sha256sum -c checksums.sha256
grep -q '"git_branch": "main"' release-manifest.json
grep -q "\"git_sha\": \"${GIT_SHA}\"" release-manifest.json
grep -q '"dirty_worktree": false' release-manifest.json
grep -q '"migration_required": true' release-manifest.json
grep -q '"compose_change_required": false' release-manifest.json
grep -q '"env_change_required": false' release-manifest.json
if [[ "$WITH_FRONTEND" == "1" ]]; then
  grep -q '"frontend_included": true' release-manifest.json
  test -f frontend/html/index.html
  test -f frontend/html/app/index.html
  test -f frontend/html/h5/index.html
  test -f frontend/html/admin/index.html
  test -f frontend/html/newadmin/index.html
  test -f deploy/nginx/lenshub-test.conf
fi

cd "$REMOTE_COMPOSE_DIR"
compose_args=(--env-file "$REMOTE_COMPOSE_ENV" -f "$REMOTE_COMPOSE_FILE")
if [[ -f "$REMOTE_HTTPS_OVERRIDE" ]]; then
  compose_args+=(-f "$REMOTE_HTTPS_OVERRIDE")
fi

services=(api worker scheduler)
if [[ "$WITH_FRONTEND" == "1" ]]; then
  services+=(web)
  if ! docker compose "${compose_args[@]}" config | grep -q 'target: /usr/share/nginx/html'; then
    echo "web service is not configured with the release frontend bind mount" >&2
    exit 1
  fi
fi

# Compose must use already-provisioned runtime images. A normal release only
# replaces the bind-mounted release directory and recreates containers.
while IFS= read -r image; do
  [[ -n "$image" ]] || continue
  if ! docker image inspect "$image" >/dev/null 2>&1; then
    echo "required runtime image is missing on server: $image" >&2
    echo "provision it once with the infrastructure image setup, then rerun this release" >&2
    exit 1
  fi
done < <(docker compose "${compose_args[@]}" config --images | sort -u)

rm -rf "$RELEASE_DIR"
mkdir -p "$RELEASE_DIR"
cp -a "$INCOMING"/. "$RELEASE_DIR"/
chmod 755 "$RELEASE_DIR"/bin/*

# 先对新 release 执行 migration；失败时 current 和业务服务均保持不变。
if ! LENSHUB_RELEASE_DIR="$RELEASE_DIR" docker compose "${compose_args[@]}" --profile ops run --rm -T --interactive=false migrate \
  sh -lc '/opt/lenshub/bin/goose -allow-missing -dir /opt/lenshub/db/migrations postgres "$DATABASE_URL" up' \
  </dev/null; then
  echo "MIGRATION_FAILED" >&2
  exit 1
fi

ln -sfnT "$RELEASE_DIR" "$CURRENT"
if [[ "$(readlink -f "$CURRENT")" != "$RELEASE_DIR" ]]; then
  echo "CURRENT_SWITCH_FAILED" >&2
  exit 1
fi

docker compose "${compose_args[@]}" ps

if ! docker compose "${compose_args[@]}" up -d --no-deps --force-recreate --no-build "${services[@]}"; then
  echo "service recreate failed; rolling back to ${previous_release}" >&2
  if [[ -n "$previous_release" ]]; then
    ln -sfnT "$previous_release" "$CURRENT"
    docker compose "${compose_args[@]}" up -d --no-deps --force-recreate --no-build "${services[@]}" || true
  fi
  exit 1
fi

health_ok=0
for _ in $(seq 1 30); do
  if curl -fsS -m 5 "$HEALTH_URL" >/dev/null; then
    health_ok=1
    break
  fi
  sleep 2
done

if [[ "$WITH_FRONTEND" == "1" && "$health_ok" == "1" ]]; then
  for endpoint in /nginx-health / /app/ /h5/ /admin/ /newadmin/ /tester/ /docs/; do
    curl -fsS -m 5 "${ENVIRONMENT_URL%/}${endpoint}" >/dev/null || { echo "FRONTEND_HEALTH_FAILED:${endpoint}" >&2; health_ok=0; break; }
  done
fi

if [[ "$health_ok" != "1" ]]; then
  echo "health check failed; rolling back to ${previous_release}" >&2
  if [[ -n "$previous_release" ]]; then
    ln -sfnT "$previous_release" "$CURRENT"
    docker compose "${compose_args[@]}" up -d --no-deps --force-recreate --no-build "${services[@]}" || true
  fi
  exit 1
fi

docker compose "${compose_args[@]}" ps --format 'table {{.Name}}\t{{.Service}}\t{{.State}}\t{{.Status}}'
docker compose "${compose_args[@]}" logs --tail=80 "${services[@]}"

cat <<REPORT
release_id=${RELEASE_ID}
git_sha=${GIT_SHA}
incoming=${INCOMING}
release_dir=${RELEASE_DIR}
current=$(readlink -f "$CURRENT")
previous_release=${previous_release}
health=passed
REPORT
REMOTE
}

write_report_json() {
  [[ -n "$REPORT_JSON" ]] || return 0
  mkdir -p "$(dirname "$REPORT_JSON")"
  local finished duration
  finished="$(date +%s)"
  duration=$((finished - DEPLOY_STARTED_AT))
  printf '{"release_id":"%s","git_sha":"%s","previous_release":null,"current_release":null,"frontend_included":%s,"migration_status":"not_run_locally","health_status":"not_run_locally","environment_url":"%s","started_at":"%s","finished_at":"%s","duration_seconds":%s}\n' \
    "${RELEASE_ID:-}" "$GIT_SHA" "$(frontend_included_label)" "$ENVIRONMENT_URL" \
    "$(date -u -d "@$DEPLOY_STARTED_AT" +%Y-%m-%dT%H:%M:%SZ)" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$duration" > "$REPORT_JSON"
}

main() {
  parse_args "$@"
  assert_safe_paths
  configure_sudo_environment

  cd "$ROOT_DIR"
  step "同步 main 分支"
  sync_main
  trap write_report_json EXIT
  print_plan

  if [[ "$DRY_RUN" == "1" ]]; then
    log "dry run complete; no build, upload, or remote change was performed"
    exit 0
  fi

  step "执行本地验证"
  run_verification
  step "构建发布包"
  build_release
  print_plan

  if [[ "$BUILD_ONLY" == "1" ]]; then
    log "build-only complete; no upload or remote change was performed"
    exit 0
  fi

  step "上传发布包"
  upload_release
  step "远程切换服务并健康检查"
  activate_remote_release
  log "deployment completed"
}

main "$@"
