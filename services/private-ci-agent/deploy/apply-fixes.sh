#!/bin/bash
# 无人值守部署脚本：将本次 private CI 修复部署到 worker 并预热共享缓存。
#
# 背景（2026-08-06 一批修复）：
#   1. 取消监控/强制回收（cancel_event + kill_job 前缀回收）
#   2. Go setup 失败短路（mod_download 失败后不再跑 migrate）
#   3. node-chromium 受控运行时（浏览器 smoke 用含 Chromium 系统库的镜像）
#   4. Go 模块缓存跨 job 共享（CACHE_MAP["go"]，避免每次冷下载）
#   5. heartbeat 用 lease_token 续期（修复 lease 必然过期 bug）
#
# 用法（root）：
#   bash /path/to/deploy-private-ci-fixes.sh
# 或从仓库根：
#   bash services/private-ci-agent/deploy/apply-fixes.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
AGENT_DIR="/srv/private-ci/agent"
CIWORKER_UID=1500

log() { echo "[deploy] $*"; }
die()  { echo "[deploy] FAIL: $*" >&2; exit 1; }

# ── 1. 根文件系统需要可写（当前 ro）────────────────────────
if ! touch "${AGENT_DIR}/.wtest" 2>/dev/null; then
    log "Remounting / read-write (was read-only)"
    mount -o remount,rw / || die "cannot remount / rw"
    touch "${AGENT_DIR}/.wtest" 2>/dev/null || die "agent dir still not writable after remount"
    rm -f "${AGENT_DIR}/.wtest"
fi

# ── 2. 同步 worker 代码（private-ci-agent）────────────────
log "Syncing private-ci-agent source -> ${AGENT_DIR}"
for f in config.py executor.py main.py podman.py profiles.py controller_client.py; do
    src="${REPO_ROOT}/services/private-ci-agent/private_ci_agent/${f}"
    [ -f "${src}" ] || die "missing source ${src}"
    install -o nobody -g nogroup -m 664 "${src}" "${AGENT_DIR}/private_ci_agent/${f}"
    log "  updated ${f}"
done

# 同步 Worker 启动、预检和部署辅助脚本
install -o nobody -g nogroup -m 755 \
    "${REPO_ROOT}/services/private-ci-agent/run-agent-with-proxy.sh" \
    "${AGENT_DIR}/run-agent-with-proxy.sh"
install -o nobody -g nogroup -m 755 \
    "${REPO_ROOT}/services/private-ci-agent/deploy/private-ci-preflight" \
    "${AGENT_DIR}/bin/private-ci-preflight"

for f in prepare-node-chromium prepare-go-cache prepare-playwright-cache; do
    src="${REPO_ROOT}/services/private-ci-agent/deploy/${f}"
    [ -f "${src}" ] || continue
    install -o nobody -g nogroup -m 755 "${src}" "${AGENT_DIR}/deploy/${f}"
    log "  updated deploy/${f}"
done
install -o nobody -g nogroup -m 644 \
    "${REPO_ROOT}/services/private-ci-agent/deploy/Dockerfile.node-chromium" \
    "${AGENT_DIR}/deploy/Dockerfile.node-chromium"

# ── 4. 重建并重启 controller（github-action-service）────────
# 容器内代码通过镜像打包（build: .），heartbeat lease_token 修复需重建。
log "Rebuilding github-action-service controller"
cd "${REPO_ROOT}/services/github-action-service"
BUILD_SHA="$(git -C "${REPO_ROOT}" rev-parse HEAD)"
SERVICE_VERSION="$(sed -n 's/^SERVICE_VERSION = "\([0-9][0-9.]*\)"/\1/p' app/version.py)"
[ -n "${SERVICE_VERSION}" ] || die "SERVICE_VERSION is missing from app/version.py"
BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
export SOURCE_REPOSITORY="https://github.com/frankichen/github_mcp"
export VCS_REF="${BUILD_SHA}" SERVICE_VERSION BUILD_DATE
export MYGITHUB12_BUILD_SHA="${BUILD_SHA}" MYGITHUB10_BUILD_SHA="${BUILD_SHA}"
export MYGITHUB12_VERSION="${SERVICE_VERSION}" MYGITHUB10_VERSION="${SERVICE_VERSION}"
CONTROLLER_IMAGE="github-action-service:${BUILD_SHA}"
# Docker BuildKit runs its build steps in a separate network namespace. Use
# the host network so the build can reach the host-only proxy listener at
# 127.0.0.1:10808, and pass the proxy explicitly to apt/pip/npm layers.
DOCKER_BUILD_PROXY="${PRIVATE_CI_DOCKER_BUILD_PROXY:-http://127.0.0.1:10808}"
docker build \
    --network host \
    --build-arg "HTTP_PROXY=${DOCKER_BUILD_PROXY}" \
    --build-arg "HTTPS_PROXY=${DOCKER_BUILD_PROXY}" \
    --build-arg "ALL_PROXY=${DOCKER_BUILD_PROXY}" \
    --build-arg "http_proxy=${DOCKER_BUILD_PROXY}" \
    --build-arg "https_proxy=${DOCKER_BUILD_PROXY}" \
    --build-arg "all_proxy=${DOCKER_BUILD_PROXY}" \
    --build-arg "SOURCE_REPOSITORY=${SOURCE_REPOSITORY}" \
    --build-arg "VCS_REF=${VCS_REF}" \
    --build-arg "SERVICE_VERSION=${SERVICE_VERSION}" \
    --build-arg "BUILD_DATE=${BUILD_DATE}" \
    --tag "${CONTROLLER_IMAGE}" \
    --file Dockerfile . || die "controller build failed"

# The Controller is managed as a persistent Docker container by systemd, not
# by the historical de-hosted compose project. Preserve its env/secrets and
# data mounts, keep one exact rollback container, and bind only the local
# Tailscale address used by de Nginx.
ROLLBACK_CONTAINER="github-action-service-rollback-${BUILD_SHA:0:12}"
if ! docker inspect github-action-service >/dev/null 2>&1; then
    die "existing github-action-service container is missing"
fi
docker stop --time 30 github-action-service || die "controller stop failed"
docker rename github-action-service "${ROLLBACK_CONTAINER}" || die "controller rollback rename failed"
docker update --restart=no "${ROLLBACK_CONTAINER}" >/dev/null || true

mapfile -t CONTROLLER_MOUNT_ARGS < <(
    docker inspect "${ROLLBACK_CONTAINER}" | jq -r '
        .[0].Mounts[] |
        "-v", (.Source + ":" + .Destination + (if .RW then "" else ":ro" end))'
)
if ! docker run -d \
    --name github-action-service \
    --restart unless-stopped \
    --env-file <(docker inspect "${ROLLBACK_CONTAINER}" --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -E '/^(MYGITHUB12_BUILD_SHA|MYGITHUB10_BUILD_SHA|MYGITHUB12_VERSION|MYGITHUB10_VERSION|MYGITHUB12_RUNTIME_MODE|MYGITHUB10_RUNTIME_MODE)=/d') \
    --env "MYGITHUB12_RUNTIME_MODE=production" \
    --env "MYGITHUB12_BUILD_SHA=${BUILD_SHA}" \
    --env "MYGITHUB12_VERSION=${SERVICE_VERSION}" \
    --env "MYGITHUB10_RUNTIME_MODE=production" \
    --env "MYGITHUB10_BUILD_SHA=${BUILD_SHA}" \
    --env "MYGITHUB10_VERSION=${SERVICE_VERSION}" \
    --publish 127.0.0.1:8765:8000 \
    --publish 100.127.108.20:8765:8000 \
    "${CONTROLLER_MOUNT_ARGS[@]}" \
    "${CONTROLLER_IMAGE}"; then
    docker rm -f github-action-service >/dev/null 2>&1 || true
    docker rename "${ROLLBACK_CONTAINER}" github-action-service || true
    docker start github-action-service || true
    die "controller start failed; rollback started"
fi

controller_ready=0
for _ in $(seq 1 30); do
    if curl -fsS --noproxy '*' --max-time 3 http://127.0.0.1:8765/health >/dev/null 2>&1; then
        controller_ready=1
        break
    fi
    sleep 1
done
if [ "${controller_ready}" -ne 1 ]; then
    docker logs --tail 80 github-action-service >&2 || true
    docker rm -f github-action-service || true
    docker rename "${ROLLBACK_CONTAINER}" github-action-service || true
    docker start github-action-service || true
    die "controller health failed; rollback started"
fi
sleep 5

# ── 5. 预热本地共享 Node Chromium 镜像 ─────────────────────
log "Preheating shared local Node Chromium image"
runuser -u ciworker -- env HOME=/home/ciworker XDG_RUNTIME_DIR=/run/user/1500 \
    DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1500/bus \
    PYTHONPATH="${AGENT_DIR}" \
    "${AGENT_DIR}/deploy/prepare-node-chromium" || die "Node Chromium image preheat failed"

# ── 6. 预热共享 Go 缓存（goose 模块进 file:// 命中）────────
log "Preheating shared Go module cache (goose)"
mkdir -p /srv/private-ci/cache/go
chown "${CIWORKER_UID}:${CIWORKER_UID}" /srv/private-ci/cache/go
chmod 700 /srv/private-ci/cache/go
runuser -u ciworker -- env HOME=/home/ciworker XDG_RUNTIME_DIR=/run/user/1500 \
    DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1500/bus \
    PYTHONPATH="${AGENT_DIR}" \
    "${AGENT_DIR}/deploy/prepare-go-cache" || die "go cache preheat failed"

# ── 7. 预热共享 Playwright 浏览器缓存 ──────────────────────
log "Preheating shared Playwright browser cache"
runuser -u ciworker -- env HOME=/home/ciworker XDG_RUNTIME_DIR=/run/user/1500 \
    DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1500/bus \
    PYTHONPATH="${AGENT_DIR}" \
    "${AGENT_DIR}/deploy/prepare-playwright-cache" || die "Playwright cache preheat failed"

# ── 8. 重启 worker 加载新代码 ─────────────────────────────
log "Restarting private-ci-agent.service"
systemctl restart private-ci-agent.service
sleep 3
systemctl is-active --quiet private-ci-agent.service || die "worker did not restart"

log "DONE. Worker restarted with local shared image and caches preheated."
log "Verify: journalctl -u private-ci-agent.service -n 20"
