#!/usr/bin/env bash
set -euo pipefail

# Read-only performance validation against an explicitly supplied stable sxt SHA.
# It never writes to frankichen/sxt and never starts a deployment.
: "${CONTROLLER_URL:?set CONTROLLER_URL to the private CI MCP endpoint}"
: "${ACTION_API_KEY:?set ACTION_API_KEY in the caller environment, never in a file}"
: "${STABLE_SXT_SHA:?set STABLE_SXT_SHA to a known 40-character sxt commit}"
: "${STABLE_SXT_BASE_SHA:?set STABLE_SXT_BASE_SHA to the stable commit parent}"
out="${1:-artifacts/sxt-repo-auto-check-5x.jsonl}"
mkdir -p "$(dirname "$out")"
CONTROLLER_URL="$CONTROLLER_URL" ACTION_API_KEY="$ACTION_API_KEY" STABLE_SXT_SHA="$STABLE_SXT_SHA" OUTPUT="$out" \
python3 - <<'PY'
import asyncio, json, os, time
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

async def main():
    headers = {"Authorization": "Bearer " + os.environ["ACTION_API_KEY"]}
    url = os.environ["CONTROLLER_URL"].rstrip("/")
    rows = []
    async with httpx.AsyncClient(headers=headers, trust_env=False) as http_client:
      async with streamable_http_client(url, http_client=http_client) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for run in range(1, 6):
                started = time.time()
                result = await session.call_tool("start_private_ci_job", {"repository": "frankichen/sxt", "branch": "main", "commit_sha": os.environ["STABLE_SXT_SHA"], "base_sha": os.environ["STABLE_SXT_BASE_SHA"], "changed_files_json": "[]", "profile": "repo-auto-check", "force_rerun": True, "supersede_previous": False})
                payload = json.loads(result.content[0].text)
                job_id = payload.get("job_id") or payload.get("job", {}).get("job_id")
                if not job_id: raise RuntimeError("private CI did not return job_id")
                final = await session.call_tool("wait_private_ci_job", {"job_id": job_id, "timeout_seconds": 55})
                state = json.loads(final.content[0].text)
                while state.get("status") not in {"passed", "failed", "cancelled", "timed_out", "superseded", "worker_lost"}:
                    final = await session.call_tool("wait_private_ci_job", {"job_id": job_id, "timeout_seconds": 55, "last_known_status": state.get("status", "")})
                    state = json.loads(final.content[0].text)
                rows.append({"run": run, "job_id": job_id, "status": state.get("status"), "duration_ms": round((time.time() - started) * 1000)})
    with open(os.environ["OUTPUT"], "w", encoding="utf-8") as handle:
        for row in rows: handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    if len(rows) != 5 or any(row["status"] != "passed" for row in rows): raise SystemExit(1)
    print(json.dumps(rows, ensure_ascii=False))

asyncio.run(main())
PY
