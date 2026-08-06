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
for f in executor.py main.py podman.py profiles.py controller_client.py; do
    src="${REPO_ROOT}/services/private-ci-agent/private_ci_agent/${f}"
    [ -f "${src}" ] || die "missing source ${src}"
    install -o nobody -g nogroup -m 664 "${src}" "${AGENT_DIR}/private_ci_agent/${f}"
    log "  updated ${f}"
done

# 同步部署辅助脚本
for f in prepare-go-cache prepare-playwright-cache; do
    src="${REPO_ROOT}/services/private-ci-agent/deploy/${f}"
    [ -f "${src}" ] || continue
    install -o nobody -g nogroup -m 755 "${src}" "${AGENT_DIR}/deploy/${f}"
    log "  updated deploy/${f}"
done

# ── 4. 重建并重启 controller（github-action-service）────────
# 容器内代码通过镜像打包（build: .），heartbeat lease_token 修复需重建。
log "Rebuilding github-action-service controller"
cd "${REPO_ROOT}/services/github-action-service"
docker compose build github-action-service || die "controller build failed"
docker compose up -d github-action-service || die "controller restart failed"
sleep 5

# ── 5. 预热共享 Go 缓存（goose 模块进 file:// 命中）────────
log "Preheating shared Go module cache (goose)"
mkdir -p /srv/private-ci/cache/go
chown "${CIWORKER_UID}:${CIWORKER_UID}" /srv/private-ci/cache/go
chmod 700 /srv/private-ci/cache/go
runuser -u ciworker -- env HOME=/home/ciworker XDG_RUNTIME_DIR=/run/user/1500 \
    DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1500/bus \
    PYTHONPATH="${AGENT_DIR}" \
    /usr/bin/python3 "${AGENT_DIR}/deploy/prepare-go-cache" || die "go cache preheat failed"

# ── 6. 重启 worker 加载新代码 ─────────────────────────────
log "Restarting private-ci-agent.service"
systemctl restart private-ci-agent.service
sleep 3
systemctl is-active --quiet private-ci-agent.service || die "worker did not restart"

log "DONE. Worker restarted with fixes; goose cache preheated."
log "Verify: journalctl -u private-ci-agent.service -n 20"
