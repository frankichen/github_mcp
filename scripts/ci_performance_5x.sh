#!/usr/bin/env bash
set -euo pipefail

out="${1:-artifacts/ci-performance-5x.jsonl}"
mkdir -p "$(dirname "$out")"
: > "$out"
for run in 1 2 3 4 5; do
  started="$(python3 -c 'import time; print(time.time_ns() // 1000000)')"
  scripts/test_local_parallel.sh >/tmp/mygithub10-ci-performance-$run.log 2>&1
  finished="$(python3 -c 'import time; print(time.time_ns() // 1000000)')"
  duration=$((finished - started))
  printf '{"run":%d,"status":"passed","duration_ms":%d}\n' "$run" "$duration" >> "$out"
done
cat "$out"
