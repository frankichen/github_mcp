import json
import os
from pathlib import Path

import pytest

from app import ci_mcp
from app.mcp_response import StructuredFastMCP
from app.mcp_server import get_mygithub_capabilities, mcp as service_mcp


def _structured_result(call_result):
    if isinstance(call_result, tuple):
        return call_result[1]
    structured = getattr(call_result, "structured_content", None)
    if structured is None:
        structured = getattr(call_result, "structuredContent", None)
    return structured


@pytest.mark.asyncio
async def test_production_canonical_hides_wait_but_keeps_start_and_get(monkeypatch):
    mcp = StructuredFastMCP("web-ci-wait-compatibility")
    ci_mcp.register_private_ci_mcp_tools(mcp)
    monkeypatch.setenv("MYGITHUB12_RUNTIME_MODE", "production")
    monkeypatch.delenv("MYGITHUB12_EXPOSE_DEPRECATED_TOOLS", raising=False)

    visible = await mcp.list_tools()
    tools = {tool.name: tool for tool in visible}

    assert "wait_private_ci_job" not in tools
    assert {"start_private_ci_job", "get_private_ci_job"} <= set(tools)
    assert "wait_private_ci_job" not in tools["start_private_ci_job"].description
    assert "never waits" in tools["get_private_ci_job"].description

    identity = await mcp.tool_schema_identity(visible)
    assert identity["hidden_deprecated_tools"] == ["wait_private_ci_job"]
    assert identity["hidden_deprecated_tool_count"] == 1
    assert identity["compatibility_tool_count"] == identity["tool_count"] + 1
    assert identity["deprecated_tools_exposed"] is False

    monkeypatch.setattr(
        ci_mcp,
        "wait_for_job_change",
        lambda job_id, timeout_seconds, last_known_status, last_known_step, last_known_revision: {
            "ok": True,
            "job_id": job_id,
            "status": "passed",
            "changed": True,
            "timed_out": False,
            "terminal": True,
        },
    )
    hidden_call = _structured_result(
        await mcp.call_tool("wait_private_ci_job", {"job_id": "job-hidden-compatibility"})
    )
    assert hidden_call["status"] == "passed"
    assert hidden_call["terminal"] is True


@pytest.mark.asyncio
async def test_explicit_compatibility_exposure_restores_wait_and_legacy_contract(monkeypatch):
    mcp = StructuredFastMCP("web-ci-wait-compatibility")
    calls = []

    def fake_wait(job_id, timeout_seconds, last_known_status, last_known_step, last_known_revision):
        calls.append((job_id, timeout_seconds, last_known_status, last_known_step, last_known_revision))
        return {
            "ok": True,
            "job_id": job_id,
            "status": "passed",
            "changed": True,
            "timed_out": False,
            "terminal": True,
        }

    monkeypatch.setattr(ci_mcp, "wait_for_job_change", fake_wait)
    ci_mcp.register_private_ci_mcp_tools(mcp)
    monkeypatch.setenv("MYGITHUB12_RUNTIME_MODE", "production")
    monkeypatch.setenv("MYGITHUB12_EXPOSE_DEPRECATED_TOOLS", "true")

    visible = await mcp.list_tools()
    tools = {tool.name: tool for tool in visible}
    wait_tool = tools["wait_private_ci_job"]
    wait_schema = wait_tool.inputSchema["properties"]

    assert wait_schema["timeout_seconds"]["default"] == 55
    assert {"job_id", "timeout_seconds", "last_known_status", "last_known_step", "last_known_revision"} <= set(wait_schema)
    assert "compatibility-only" in wait_tool.description
    assert "get_private_ci_job" in wait_tool.description

    result = _structured_result(
        await mcp.call_tool(
            "wait_private_ci_job",
            {
                "job_id": "job-legacy",
                "last_known_status": "running",
                "last_known_step": "pytest",
                "last_known_revision": 9,
            },
        )
    )

    assert calls == [("job-legacy", 55, "running", "pytest", 9)]
    assert result["status"] == "passed"
    assert result["terminal"] is True

    identity = await mcp.tool_schema_identity(visible)
    assert identity["hidden_deprecated_tools"] == []
    assert identity["hidden_deprecated_tool_count"] == 0
    assert identity["deprecated_tools_exposed"] is True


@pytest.mark.asyncio
async def test_capabilities_report_wait_deprecation_and_schema_visibility(monkeypatch):
    monkeypatch.setenv("MYGITHUB12_RUNTIME_MODE", "production")
    monkeypatch.setenv("MYGITHUB12_BUILD_SHA", "a" * 40)
    monkeypatch.setenv("MYGITHUB12_EXPOSE_DEPRECATED_TOOLS", "false")

    production = json.loads(await get_mygithub_capabilities())
    deprecated = {item["name"]: item for item in production["deprecated_tools"]}

    assert production["tool_count"] == 165
    assert production["tool_manifest_count"] == 165
    assert production["compatibility_tool_count"] == 176
    assert production["hidden_deprecated_tool_count"] == 11
    assert "wait_private_ci_job" in production["hidden_deprecated_tools"]
    assert deprecated["wait_private_ci_job"] == {
        "name": "wait_private_ci_job",
        "deprecated": True,
        "compatibility_only": True,
        "replacement": "get_private_ci_job",
        "guidance": (
            "Legacy wait is compatibility-only for existing/debug clients. Canonical Web CI uses "
            "start_private_ci_job then get_private_ci_job snapshots and must not loop wait to terminal. "
            "The 55-second bound is legacy behavior, not an OpenAI/ChatGPT Web timeout SLA. Fast feedback "
            "is not merge-eligible; the formal gate requires full CI plus a reusable attestation."
        ),
    }
    production_schema_sha = production["tool_schema_sha256"]

    monkeypatch.setenv("MYGITHUB12_EXPOSE_DEPRECATED_TOOLS", "true")
    compatibility = json.loads(await get_mygithub_capabilities())

    assert compatibility["tool_count"] == 176
    assert compatibility["tool_manifest_count"] == 176
    assert compatibility["compatibility_tool_count"] == 176
    assert compatibility["hidden_deprecated_tool_count"] == 0
    assert "wait_private_ci_job" not in compatibility["hidden_deprecated_tools"]
    assert compatibility["tool_schema_sha256"] != production_schema_sha


@pytest.mark.asyncio
async def test_static_manifests_keep_wait_as_hidden_compatibility_tool(monkeypatch):
    root = Path(os.environ.get("CI_REPOSITORY_ROOT", "") or Path(__file__).resolve().parents[3])
    legacy = json.loads((root / "docs" / "MYGITHUB10_TOOL_MANIFEST.json").read_text(encoding="utf-8"))
    canonical = json.loads((root / "docs" / "MYGITHUB12_TOOL_MANIFEST.json").read_text(encoding="utf-8"))
    legacy_wait = next(tool for tool in legacy["tools"] if tool["name"] == "wait_private_ci_job")

    monkeypatch.setenv("MYGITHUB12_EXPOSE_DEPRECATED_TOOLS", "true")
    registered = {tool.name: tool for tool in await service_mcp.list_tools()}

    assert legacy_wait["description"] == registered["wait_private_ci_job"].description
    assert "compatibility-only" in legacy_wait["description"]
    assert canonical["tool_count"] == 164
    assert canonical["compatibility_tool_count"] == 175
    assert "wait_private_ci_job" in canonical["hidden_deprecated_tools"]


def test_current_web_instructions_recommend_snapshot_continuation():
    root = Path(os.environ.get("CI_REPOSITORY_ROOT", "") or Path(__file__).resolve().parents[3])
    readme = (root / "README.md").read_text(encoding="utf-8")
    instructions = (root / "services" / "github-action-service" / "custom-gpt-instructions.md").read_text(encoding="utf-8")

    assert "`start_private_ci_job` → `get_private_ci_job` snapshot" in readme
    assert "wait_private_ci_job" not in instructions
