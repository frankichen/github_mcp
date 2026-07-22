#!/usr/bin/env bash
set -euo pipefail

# Migration wrapper for isolated CI/deploy execution. It never skips a migration.
# The caller supplies a fixed command; arbitrary shell input is rejected by policy.
TOTAL_TIMEOUT_SECONDS="${MIGRATION_TOTAL_TIMEOUT_SECONDS:-600}"
STEP_TIMEOUT_SECONDS="${MIGRATION_STEP_TIMEOUT_SECONDS:-120}"
HEARTBEAT_SECONDS="${MIGRATION_HEARTBEAT_SECONDS:-10}"

if [[ "${1:-}" != "--" || "$#" -lt 2 ]]; then
  echo "usage: $0 -- migration-command [args...]" >&2
  exit 64
fi
shift

if command -v pg_isready >/dev/null 2>&1 && [[ -n "${DATABASE_URL:-}" ]]; then
  timeout "${STEP_TIMEOUT_SECONDS}s" pg_isready -d "$DATABASE_URL"
fi

heartbeat() {
  while true; do
    echo "migration-heartbeat ts=$(date -u +%Y-%m-%dT%H:%M:%SZ) command=${1}" >&2
    sleep "$HEARTBEAT_SECONDS"
  done
}

heartbeat "$1" &
HEARTBEAT_PID=$!
trap 'kill "$HEARTBEAT_PID" 2>/dev/null || true' EXIT

timeout --foreground "${TOTAL_TIMEOUT_SECONDS}s" "$@"
