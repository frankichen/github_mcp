#!/usr/bin/env bash
set -euo pipefail

tmp="$(mktemp -d /tmp/mygithub10-local-parallel.XXXXXX)"
trap 'rm -rf -- "$tmp"' EXIT

run() {
  local name="$1"; shift
  ("$@") >"$tmp/$name.log" 2>&1
}

run controller bash -c 'cd services/github-action-service && PYTHONPATH=. TMPDIR="$1" GITHUB_TOKEN=test_token_value ACTION_API_KEY=test_api_key_32_bytes_long IDEMPOTENCY_DB_PATH="$1/idempotency.db" DEPLOYMENT_DB_PATH="$1/deployment.db" CI_DB_PATH="$1/ci.db" pytest -q tests' _ "$tmp" & p1=$!
run private-agent bash -c 'cd services/private-ci-agent && PYTHONPATH=. TMPDIR="$1" pytest -q tests' _ "$tmp" & p2=$!
run executor bash -c 'cd services/private-ci-deploy-executor && PYTHONPATH=. TMPDIR="$1" pytest -q tests' _ "$tmp" & p3=$!
status=0
for pid in "$p1" "$p2" "$p3"; do wait "$pid" || status=1; done
cat "$tmp"/*.log
exit "$status"
