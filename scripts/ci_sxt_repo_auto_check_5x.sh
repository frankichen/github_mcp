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
CONTROLLER_URL="$CONTROLLER_URL" ACTION_API_KEY="$ACTION_API_KEY" STABLE_SXT_SHA="$STABLE_SXT_SHA" STABLE_SXT_BASE_SHA="$STABLE_SXT_BASE_SHA" OUTPUT="$out" \
python3 - <<'PY'
import asyncio, json, os, re, statistics, time
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

SHA_RE = re.compile(r"^[0-9a-f]{40}$")

async def call(name, arguments):
    last = None
    for _ in range(3):
        try:
            async with httpx.AsyncClient(headers={"Authorization": "Bearer " + os.environ["ACTION_API_KEY"]}, trust_env=False, timeout=70) as client:
                async with streamable_http_client(os.environ["CONTROLLER_URL"].rstrip("/"), http_client=client) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await session.call_tool(name, arguments)
                        return json.loads(result.content[0].text)
        except Exception as exc:
            last = exc
            await asyncio.sleep(1)
    raise RuntimeError(f"MCP call failed: {name}: {type(last).__name__}")

async def main():
    if not SHA_RE.fullmatch(os.environ["STABLE_SXT_SHA"]) or not SHA_RE.fullmatch(os.environ["STABLE_SXT_BASE_SHA"]):
        raise SystemExit("STABLE_SXT_SHA and STABLE_SXT_BASE_SHA must be 40 lowercase hex characters")
    rows = []
    terminal = {"passed", "failed", "cancelled", "timed_out", "superseded", "worker_lost"}
    for run in range(1, 6):
        started = time.time()
        payload = await call("start_private_ci_job", {"repository": "frankichen/sxt", "branch": "main", "commit_sha": os.environ["STABLE_SXT_SHA"], "base_sha": os.environ["STABLE_SXT_BASE_SHA"], "changed_files_json": "[]", "profile": "repo-auto-check", "force_rerun": True, "supersede_previous": False})
        job_id = payload.get("job_id") or payload.get("job", {}).get("job_id")
        if not job_id: raise RuntimeError("private CI did not return job_id")
        state = await call("get_private_ci_job", {"job_id": job_id})
        while state.get("status") not in terminal:
            state = await call("wait_private_ci_job", {"job_id": job_id, "timeout_seconds": 55, "last_known_status": state.get("status", "")})
            if state.get("status") == "not_found": raise RuntimeError(f"job disappeared: {job_id}")
        summary = state.get("summary") or {}
        performance = summary.get("performance") or {}
        rows.append({"run": run, "job_id": job_id, "commit_sha": state.get("commit_sha"), "tree_sha": summary.get("git_tree_sha"), "status": state.get("status"), "duration_seconds": round(time.time() - started, 3), "queue_seconds": None, "source_prepare_seconds": None, "migration_seconds": next((s.get("duration_seconds") for s in summary.get("steps", []) if s.get("step_name", "").endswith(":migrate")), None), "go_test_seconds": performance.get("go_test_seconds"), "admin_test_seconds": performance.get("admin_test_seconds"), "console_test_seconds": performance.get("console_test_seconds"), "build_seconds": performance.get("build_seconds"), "total_seconds": performance.get("total_wall_seconds"), "container_image_digest": summary.get("image_digest"), "go_version": summary.get("go_version"), "node_version": summary.get("node_version"), "npm_version": summary.get("npm_version"), "test_counts": summary.get("test_counts")})
        with open(os.environ["OUTPUT"], "w", encoding="utf-8") as handle:
            for row in rows: handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    with open(os.environ["OUTPUT"], "w", encoding="utf-8") as handle:
        for row in rows: handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    if len(rows) != 5 or any(row["status"] != "passed" for row in rows): raise SystemExit(1)
    durations = sorted(row["duration_seconds"] for row in rows)
    print(json.dumps({"runs": rows, "median_seconds": statistics.median(durations), "p90_seconds": durations[min(4, int(len(durations) * .9)) - 1]}, ensure_ascii=False))

asyncio.run(main())
PY
