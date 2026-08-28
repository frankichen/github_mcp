#!/bin/bash
# Dynamic proxy resolution and agent startup.
# Resolves the Windows host proxy address fresh every startup.

set -u

AGENT_DIR="/srv/private-ci/agent"
VENV_PYTHON="${AGENT_DIR}/venv/bin/python"
WORKER_ID="${PRIVATE_CI_WORKER_ID:-wsl-ci-01}"
case "${WORKER_ID}" in
    wsl-ci-01|wsl-ci-02) ;;
    *) echo "[proxy-launcher] ERROR: CI_WORKER_ID_INVALID" >&2; exit 2 ;;
esac
export PRIVATE_CI_WORKER_ID="${WORKER_ID}"
# Rootless Podman reaches the Worker host proxy through the WSL slirp gateway.
# Keep an explicit operator override, but give every Worker the same safe default.
export PRIVATE_CI_CONTAINER_PROXY_HOST="${PRIVATE_CI_CONTAINER_PROXY_HOST:-10.0.2.2}"
WORKER_ROOT="/srv/private-ci/workers/${WORKER_ID}"
# proxy.conf is a static, root-owned input. Runtime values are worker-scoped
# so both Agent processes can run concurrently under ProtectSystem=strict.
PROXY_CONF="/etc/private-ci/proxy.conf"
RUNTIME_PROXY_CONF="${WORKER_ROOT}/run/proxy.runtime.conf"
LOG_DIR="${WORKER_ROOT}/logs"
LOCKFILE="${WORKER_ROOT}/run/private-ci-agent.lock"
PODMAN_TMPDIR="${WORKER_ROOT}/run/tmp"
CONTROLLER_HOST="${PRIVATE_CI_CONTROLLER_HOST:-100.127.108.20}"
mkdir -p "${WORKER_ROOT}/run" "${LOG_DIR}" "${PODMAN_TMPDIR}"
chmod 700 "${WORKER_ROOT}" "${WORKER_ROOT}/run" "${LOG_DIR}" "${PODMAN_TMPDIR}"

log() {
    local level="$1"; shift
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [proxy-launcher] ${level}: $*" | tee -a "${LOG_DIR}/proxy-launcher.log" >&2
}

sanitize_log() {
    sed -E 's/(token[=:][^ ]+)/token=***/gi; s/(Bearer )[^ ]+/\1***/gi; s/(CI_WORKER_TOKEN=)[^ ]+/\1***/gi'
}

# ── Single-instance lock ──────────────────────────────────────
exec 200>"${LOCKFILE}"
if ! flock -n 200; then
    log "WARNING" "Another agent instance is already running (lock held on ${LOCKFILE}). Exiting."
    exit 0
fi

# ── Resolve Windows proxy address ─────────────────────────────
resolve_proxy() {
    local proxy_host=""
    local proxy_port=""

    # Source proxy config for possible fixed settings
    if [ -f "${PROXY_CONF}" ]; then
        set -a
        # shellcheck source=/dev/null
        source "${PROXY_CONF}" 2>/dev/null || true
        set +a
    fi

    local gateway resolver
    gateway=$(ip route show default 2>/dev/null | awk '{print $3}' | head -1)
    resolver=$(awk '/^nameserver /{print $2; exit}' /etc/resolv.conf 2>/dev/null)

    # Attempt to resolve Windows host from WSL gateway or resolver
    # WSL mirrored mode: use localhost
    if [ "${PROXY_HOST_MODE:-auto}" = "local" ] || grep -Eiq '^[[:space:]]*networkingMode[[:space:]]*=[[:space:]]*mirrored' /mnt/c/Users/*/.wslconfig 2>/dev/null; then
        proxy_host="127.0.0.1"
        log "INFO" "Detected WSL mirrored network mode, using localhost"
    else
        if [ -n "${gateway}" ]; then
            proxy_host="${gateway}"
            log "INFO" "Detected WSL NAT gateway: ${gateway}"
        else
            # Fallback: try WSL resolver
            proxy_host="${resolver}"
            if [ -n "${proxy_host}" ]; then
                log "INFO" "Using WSL DNS resolver as proxy host: ${proxy_host}"
            else
                proxy_host="127.0.0.1"
                log "WARNING" "Could not determine Windows host IP, falling back to ${proxy_host}"
            fi
        fi
    fi

    proxy_port="${PROXY_PORT:-10808}"
    log "INFO" "Resolved proxy target: ${proxy_host}:${proxy_port}"
    echo "${proxy_host}" "${proxy_port}" "${gateway}" "${resolver}"
    return 0
}

# ── Wait for proxy port to be reachable ───────────────────────
wait_for_proxy() {
    local host="$1"
    local port="$2"
    local max_wait="${3:-120}"
    local waited=0
    local delay=2
    local max_delay=15

    log "INFO" "Waiting up to ${max_wait}s for proxy ${host}:${port} ..."

    while [ "${waited}" -lt "${max_wait}" ]; do
        if timeout 2 bash -c "echo >/dev/tcp/${host}/${port}" 2>/dev/null; then
            log "INFO" "Proxy ${host}:${port} is reachable after ${waited}s"
            return 0
        fi
        local sleep_for="${delay}"
        local remaining=$(( max_wait - waited ))
        [ "${sleep_for}" -gt "${remaining}" ] && sleep_for="${remaining}"
        sleep "${sleep_for}"
        waited=$(( waited + sleep_for ))
        if [ "${delay}" -lt "${max_delay}" ]; then
            delay=$(( delay + 1 ))
        fi
    done

    log "WARNING" "Proxy ${host}:${port} not reachable after ${max_wait}s"
    return 1
}

# ── Detect protocol and construct proxy environment variables ──
detect_protocol() {
    local host="$1" port="$2" code socks_ok=0 http_ok=0
    code=$(curl -sS -o /dev/null --noproxy '' --proxy "socks5h://${host}:${port}" --connect-timeout 5 --max-time 15 -w '%{http_code}' https://api.github.com 2>/dev/null || true)
    [[ "${code}" =~ ^[2345][0-9][0-9]$ ]] && socks_ok=1
    code=$(curl -sS -o /dev/null --noproxy '' --proxy "http://${host}:${port}" --connect-timeout 5 --max-time 15 -w '%{http_code}' https://api.github.com 2>/dev/null || true)
    [[ "${code}" =~ ^[2345][0-9][0-9]$ ]] && http_ok=1
    [ "${socks_ok}" = 1 ] && [ "${http_ok}" = 1 ] && { echo mixed; return 0; }
    [ "${http_ok}" = 1 ] && { echo http; return 0; }
    [ "${socks_ok}" = 1 ] && { echo socks5; return 0; }
    return 1
}

build_proxy_env() {
    local host="$1" port="$2" protocol="$3" gateway="$4" resolver="$5"

    local proxy_url="http://${host}:${port}"
    local all_proxy_url="${proxy_url}"
    [ "${protocol}" = socks5 ] && proxy_url="socks5h://${host}:${port}"
    [ "${protocol}" = socks5 ] && all_proxy_url="${proxy_url}"
    [ "${protocol}" = mixed ] && all_proxy_url="socks5h://${host}:${port}"

    # NO_PROXY: internal services that must bypass proxy
    local no_proxy="localhost,127.0.0.1,::1,${CONTROLLER_HOST},de,.de,.local,.internal,${host}"
    [ -n "${gateway}" ] && no_proxy="${no_proxy},${gateway}"
    [ -n "${resolver}" ] && no_proxy="${no_proxy},${resolver}"

    # Worker API address (from config)
    local controller_host
    controller_host="${CONTROLLER_HOST}"
    if [ -n "${controller_host}" ] && [[ "${no_proxy}" != *"${controller_host}"* ]]; then
        no_proxy="${no_proxy},${controller_host}"
    fi

    log "INFO" "Selected ${protocol} proxy at ${host}:${port}; internal bypass configured"

    cat > "${RUNTIME_PROXY_CONF}" <<CONFEOF
ALL_PROXY=${all_proxy_url}
HTTP_PROXY=${proxy_url}
HTTPS_PROXY=${proxy_url}
all_proxy=${all_proxy_url}
http_proxy=${proxy_url}
https_proxy=${proxy_url}
NO_PROXY=${no_proxy}
no_proxy=${no_proxy}
PROXY_AVAILABLE=1
PROXY_PROTOCOL=${protocol}
PROXY_HOST=${host}
PROXY_PORT=${port}
CONFEOF
}

# ── Main ──────────────────────────────────────────────────────
log "INFO" "Starting CI Agent proxy launcher"

read -r PROXY_HOST PROXY_PORT PROXY_GATEWAY PROXY_RESOLVER < <(resolve_proxy)

if wait_for_proxy "${PROXY_HOST}" "${PROXY_PORT}" "${PROXY_WAIT_SECONDS:-120}" && PROXY_PROTOCOL_DETECTED=$(detect_protocol "${PROXY_HOST}" "${PROXY_PORT}"); then
    build_proxy_env "${PROXY_HOST}" "${PROXY_PORT}" "${PROXY_PROTOCOL_DETECTED}" "${PROXY_GATEWAY}" "${PROXY_RESOLVER}"
    log "INFO" "Proxy is available"
else
    # Proxy unavailable but we still start the agent
    cat > "${RUNTIME_PROXY_CONF}" <<CONFEOF
ALL_PROXY=
HTTP_PROXY=
HTTPS_PROXY=
all_proxy=
http_proxy=
https_proxy=
NO_PROXY=localhost,127.0.0.1,::1,${CONTROLLER_HOST},de,.de,.local,.internal,${PROXY_HOST}
no_proxy=localhost,127.0.0.1,::1,${CONTROLLER_HOST},de,.de,.local,.internal,${PROXY_HOST}
PROXY_AVAILABLE=0
PROXY_PROTOCOL=none
PROXY_HOST=${PROXY_HOST}
PROXY_PORT=${PROXY_PORT}
CONFEOF
    log "WARNING" "Proxy unavailable, starting agent without proxy"
fi

# Ensure perms
chmod 640 "${RUNTIME_PROXY_CONF}"
mkdir -p "${PODMAN_TMPDIR}"
chmod 700 "${PODMAN_TMPDIR}"

# Source the final proxy config for current shell
set -a
# shellcheck source=/dev/null
source "${RUNTIME_PROXY_CONF}"
set +a
export TMPDIR="${PODMAN_TMPDIR}"

log "INFO" "Launching agent: ${VENV_PYTHON} -m private_ci_agent.main"

exec "${VENV_PYTHON}" -m private_ci_agent.main
