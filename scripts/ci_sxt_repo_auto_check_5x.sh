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
import asyncio, json, math, os, re, statistics, time
from datetime import datetime
import httpx
from mcp import ClientSession
try:
    from mcp.client.streamable_http import streamable_http_client
except ImportError:
    from mcp.client.streamable_http import streamablehttp_client as streamable_http_client

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

async def validate_start_schema():
    async with httpx.AsyncClient(headers={"Authorization": "Bearer " + os.environ["ACTION_API_KEY"]}, trust_env=False, timeout=70) as client:
        async with streamable_http_client(os.environ["CONTROLLER_URL"].rstrip("/"), http_client=client) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                tool = next((item for item in tools if item.name == "start_private_ci_job"), None)
                if tool is None:
                    raise RuntimeError("start_private_ci_job is missing from MCP schema")
                properties = set((tool.inputSchema or {}).get("properties", {}))
                required = {"repository", "branch", "commit_sha", "base_sha", "profile", "force_rerun", "supersede_previous", "timeout_seconds", "priority"}
                if not required <= properties or "changed_files_json" in properties:
                    raise RuntimeError(f"start_private_ci_job schema mismatch: {sorted(properties)}")

def iso_seconds(value):
    if not value: return None
    if isinstance(value, (int, float)): return float(value)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()

def measurement(value, reason):
    return {"available": value is not None, "seconds": round(value, 3) if value is not None else None, "reason": "" if value is not None else reason}

def step_duration(steps, *needles):
    values = [s.get("duration_seconds") for s in steps if any(needle in s.get("step_name", "").lower() for needle in needles) and s.get("duration_seconds") is not None]
    return sum(values) if values else None

async def main():
    if not SHA_RE.fullmatch(os.environ["STABLE_SXT_SHA"]) or not SHA_RE.fullmatch(os.environ["STABLE_SXT_BASE_SHA"]):
        raise SystemExit("STABLE_SXT_SHA and STABLE_SXT_BASE_SHA must be 40 lowercase hex characters")
    try:
        await validate_start_schema()
    except Exception as exc:
        raise SystemExit(f"MCP start_private_ci_job schema validation failed: {type(exc).__name__}") from exc
    rows = []
    terminal = {"passed", "failed", "cancelled", "timed_out", "superseded", "worker_lost"}
    for run in range(1, 6):
        started = time.time()
        payload = await call("start_private_ci_job", {"repository": "frankichen/sxt", "branch": "main", "commit_sha": os.environ["STABLE_SXT_SHA"], "base_sha": os.environ["STABLE_SXT_BASE_SHA"], "profile": "repo-auto-check", "force_rerun": True, "supersede_previous": False, "timeout_seconds": 900, "priority": "normal"})
        job_id = payload.get("job_id") or payload.get("job", {}).get("job_id")
        if not job_id: raise RuntimeError("private CI did not return job_id")
        state = await call("get_private_ci_job", {"job_id": job_id})
        while state.get("status") not in terminal:
            state = await call("wait_private_ci_job", {"job_id": job_id, "timeout_seconds": 55, "last_known_status": state.get("status", "")})
            if state.get("status") == "not_found": raise RuntimeError(f"job disappeared: {job_id}")
        summary = state.get("summary") or {}; steps = summary.get("steps") or []
        performance = summary.get("performance") or {}
        created, leased, started_at, finished = (iso_seconds(state.get(key)) for key in ("created_at", "leased_at", "started_at", "finished_at"))
        total = performance.get("total_wall_seconds") or (finished - created if finished and created else None)
        row = {"run": run, "job_id": job_id, "commit_sha": state.get("commit_sha"), "tree_sha": summary.get("git_tree_sha"), "status": state.get("status"), "duration_seconds": round(time.time() - started, 3), "queue_seconds": measurement(leased - created if leased and created else None, "created_at or leased_at unavailable"), "source_prepare_seconds": measurement(step_duration(steps, "source", "prepare"), "source prepare step unavailable"), "migration_seconds": measurement(step_duration(steps, "migrate"), "migration step unavailable"), "go_test_seconds": measurement(performance.get("go_test_seconds"), "summary.performance.go_test_seconds unavailable"), "admin_test_seconds": measurement(performance.get("admin_test_seconds"), "summary.performance.admin_test_seconds unavailable"), "console_test_seconds": measurement(performance.get("console_test_seconds"), "summary.performance.console_test_seconds unavailable"), "build_seconds": measurement(performance.get("build_seconds"), "summary.performance.build_seconds unavailable"), "total_seconds": measurement(total, "finished_at and summary.performance.total_wall_seconds unavailable"), "container_image_digest": summary.get("image_digest") or {"available": False, "reason": "summary.image_digest unavailable"}, "go_version": summary.get("go_version") or {"available": False, "reason": "summary.go_version unavailable"}, "node_version": summary.get("node_version") or {"available": False, "reason": "summary.node_version unavailable"}, "npm_version": summary.get("npm_version") or {"available": False, "reason": "summary.npm_version unavailable"}, "test_counts": summary.get("test_counts") or {"available": False, "reason": "summary.test_counts unavailable"}}
        rows.append(row)
        with open(os.environ["OUTPUT"], "w", encoding="utf-8") as handle:
            for row in rows: handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    with open(os.environ["OUTPUT"], "w", encoding="utf-8") as handle:
        for row in rows: handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    if len(rows) != 5 or any(row["status"] != "passed" for row in rows): raise SystemExit(1)
    if any(row["commit_sha"] != os.environ["STABLE_SXT_SHA"] or not row["tree_sha"] for row in rows): raise SystemExit("stable SHA/tree validation failed")
    durations = sorted(row["duration_seconds"] for row in rows); nearest_rank = math.ceil(0.9 * len(durations)) - 1
    print(json.dumps({"runs": rows, "median_seconds": statistics.median(durations), "p90_seconds": durations[nearest_rank], "p90_method": "nearest-rank"}, ensure_ascii=False))

asyncio.run(main())
PY
