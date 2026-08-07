import json

import pytest

from app.ci_mcp import build_private_ci_job_response
from app.mcp_response import (
    MAX_SAFE_INLINE_BYTES,
    StructuredFastMCP,
    json_bytes,
    prepare_tool_response,
    read_response_resource_chunk,
    response_size_bytes,
)


def _large_ci_job(step_count: int = 36, changed_count: int = 140) -> tuple[dict, list[dict]]:
    worker_steps = []
    persisted_steps = []
    for index in range(step_count):
        name = f"node:workspace-{index}:test"
        worker_steps.append(
            {
                "step_name": name,
                "command": "npm run test -- " + ("very-long-argument " * 80),
                "status": "passed",
                "exit_code": 0,
                "duration_seconds": index + 0.25,
                "step_id": 1000 + index,
            }
        )
        persisted_steps.append(
            {
                "step_name": name,
                "status": "passed",
                "exit_code": 0,
                "started_at": f"2026-08-07T10:{index % 60:02d}:00+00:00",
                "finished_at": f"2026-08-07T10:{index % 60:02d}:01+00:00",
                "duration_seconds": index + 0.3,
                "log_start_offset": index * 10000,
                "log_end_offset": (index + 1) * 10000,
            }
        )

    changed_files = [f"src/generated/file_{index:03d}.tsx" for index in range(changed_count)]
    workspaces = [
        {"path": ".", "stack": "go"},
        {"path": "h5/admin", "stack": "node", "framework": "vue", "package_manager": "npm"},
        {"path": "h5/console", "stack": "node", "framework": "react", "package_manager": "npm"},
    ]
    summary = {
        "status": "passed",
        "exit_code": 0,
        "git_tree_sha": "b" * 40,
        "detected_stacks": ["go", "node", "react", "vue"],
        "selected_profiles": ["go-check", "node-check"],
        "workspaces": workspaces,
        "steps": worker_steps,
        "evidence": {"changed_files": changed_files, "lock_sha256": "c" * 64},
        "performance": {"total_wall_seconds": 123.456, "samples": list(range(200))},
        "warnings": ["bounded warning"],
    }
    job = {
        "job_id": "job-large-001",
        "repository": "frankichen/sxt",
        "branch": "ai/large-ci-response",
        "commit_sha": "a" * 40,
        "base_sha": "d" * 40,
        "changed_files": changed_files,
        "changed_files_total": changed_count,
        "changed_files_truncated": False,
        "profile": "repo-auto-check",
        "status": "passed",
        "priority": 100,
        "worker_id": "wsl-ci-01",
        "queue_position": None,
        "exit_code": 0,
        "created_at": "2026-08-07T10:00:00+00:00",
        "started_at": "2026-08-07T10:00:01+00:00",
        "finished_at": "2026-08-07T10:02:00+00:00",
        "duration_seconds": 119.0,
        "summary": summary,
        "cancel_requested": False,
        "superseded_by_job_id": None,
        "detected_stacks": summary["detected_stacks"],
        "selected_profiles": summary["selected_profiles"],
        "workspaces": workspaces,
    }
    return job, persisted_steps


def test_get_private_ci_job_compact_summary_is_gate_complete_and_small():
    job, persisted_steps = _large_ci_job()
    result = build_private_ci_job_response(job, persisted_steps, "summary")

    assert result["repository"] == "frankichen/sxt"
    assert result["branch"] == "ai/large-ci-response"
    assert result["commit_sha"] == "a" * 40
    assert result["profile"] == "repo-auto-check"
    assert result["status"] == "passed"
    assert result["exit_code"] == 0
    assert result["git_tree_sha"] == "b" * 40
    assert result["detected_stacks"] == ["go", "node", "react", "vue"]
    assert result["selected_profiles"] == ["go-check", "node-check"]
    assert result["workspaces_total"] == 3
    assert result["steps_total"] == 36
    assert not result["steps_truncated"]
    assert all(set(step) == {"step_name", "status", "exit_code", "duration_seconds"} for step in result["steps"])
    assert all("command" not in step and "log_start_offset" not in step and "log_end_offset" not in step for step in result["steps"])
    assert "changed_files" not in result
    assert "summary" not in result
    assert "evidence" not in result
    assert "performance" not in result
    assert response_size_bytes(result) < MAX_SAFE_INLINE_BYTES


def test_get_private_ci_job_full_keeps_detail_without_duplicate_steps():
    job, persisted_steps = _large_ci_job()
    result = build_private_ci_job_response(job, persisted_steps, "full")

    assert result["changed_files"] == job["changed_files"]
    assert result["summary"]["evidence"] == job["summary"]["evidence"]
    assert result["summary"]["performance"] == job["summary"]["performance"]
    assert result["git_tree_sha"] == "b" * 40
    for duplicate_key in ("steps", "status", "exit_code", "detected_stacks", "selected_profiles", "workspaces", "git_tree_sha"):
        assert duplicate_key not in result["summary"]
    assert result["steps_total"] == 36
    assert result["steps"][0]["command"].startswith("npm run test")
    assert result["steps"][0]["log_start_offset"] == 0
    assert result["steps"][0]["log_end_offset"] == 10000


def test_oversized_response_falls_back_to_integrity_checked_resource(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_RESPONSE_RESOURCE_DIR", str(tmp_path))
    payload = {
        "ok": True,
        "repository": "frankichen/sxt",
        "commit_sha": "a" * 40,
        "status": "passed",
        "evidence": {"blob": "资源-content-" * 10000},
    }
    expected = json_bytes(payload)
    assert len(expected) > 64 * 1024

    result = prepare_tool_response(payload)
    meta = result["response_meta"]
    assert meta["mode"] == "resource"
    assert meta["truncated"] is True
    assert meta["total_bytes"] == len(expected)
    assert meta["inline_bytes"] < MAX_SAFE_INLINE_BYTES
    assert meta["resource_uri"].startswith("mygithub12://response/")

    parts = []
    offset = 0
    while True:
        page = read_response_resource_chunk(meta["resource_uri"], offset_bytes=offset, limit_bytes=4096)
        parts.append(page["content"])
        if not page["has_more"]:
            assert page["content_sha256"] == meta["content_sha256"]
            break
        offset = page["next_offset"]
    reconstructed = "".join(parts).encode("utf-8")
    assert reconstructed == expected


@pytest.mark.asyncio
async def test_structured_fastmcp_removes_json_string_in_json_envelope(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_RESPONSE_RESOURCE_DIR", str(tmp_path))
    mcp = StructuredFastMCP("protocol-shape-test")

    @mcp.tool(name="legacy_json_tool")
    async def legacy_json_tool() -> str:
        return json.dumps({"ok": True, "job_id": "job-1", "status": "passed"})

    # Direct Python compatibility is intentionally unchanged.
    assert isinstance(await legacy_json_tool(), str)

    tool = next(item for item in await mcp.list_tools() if item.name == "legacy_json_tool")
    assert "result" not in (tool.outputSchema.get("properties") or {})

    call_result = await mcp.call_tool("legacy_json_tool", {})
    if isinstance(call_result, tuple):
        structured = call_result[1]
    else:
        structured = getattr(call_result, "structured_content", None)
        if structured is None:
            structured = getattr(call_result, "structuredContent", None)
    assert structured["ok"] is True
    assert structured["job_id"] == "job-1"
    assert structured["status"] == "passed"
    assert "result" not in structured
    assert structured["response_meta"]["truncated"] is False
