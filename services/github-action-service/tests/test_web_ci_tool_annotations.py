import json
import os
from pathlib import Path

import pytest

from app import ci_mcp
from app.mcp_response import StructuredFastMCP
from app.mcp_server import get_mygithub_capabilities, mcp as service_mcp


TARGET_TOOL_NAMES = (
    "list_private_ci_workers",
    "list_private_ci_profiles",
    "list_private_ci_jobs",
    "start_private_ci_job",
    "get_private_ci_job",
    "wait_private_ci_job",
    "get_private_ci_logs",
    "get_private_ci_log_tail",
    "cancel_private_ci_job",
    "plan_private_ci_job",
    "validate_development_task",
    "converge_development_task",
)

EXPECTED_ANNOTATIONS = {
    "list_private_ci_workers": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
        "consequential": False,
    },
    "list_private_ci_profiles": {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
        "consequential": False,
    },
    "list_private_ci_jobs": {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
        "consequential": False,
    },
    "start_private_ci_job": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
        "consequential": True,
    },
    "get_private_ci_job": {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
        "consequential": False,
    },
    "wait_private_ci_job": {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
        "consequential": False,
    },
    "get_private_ci_logs": {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
        "consequential": False,
    },
    "get_private_ci_log_tail": {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
        "consequential": False,
    },
    "cancel_private_ci_job": {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
        "consequential": True,
    },
    "plan_private_ci_job": {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
        "consequential": False,
    },
    "validate_development_task": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
        "consequential": True,
    },
    "converge_development_task": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
        "consequential": True,
    },
}

ANNOTATION_FIELDS = (
    "readOnlyHint",
    "destructiveHint",
    "idempotentHint",
    "openWorldHint",
)
IDENTITY_FIELDS = (
    "tool_count",
    "tool_manifest_count",
    "compatibility_tool_count",
    "hidden_deprecated_tool_count",
    "tool_schema_sha256",
    "schema_generation_id",
)


def _root() -> Path:
    return Path(os.environ.get("CI_REPOSITORY_ROOT", "") or Path(__file__).resolve().parents[3])


def _manifest() -> dict:
    return json.loads((_root() / "docs" / "MYGITHUB12_TOOL_MANIFEST.json").read_text(encoding="utf-8"))


def _tool_annotations(tool) -> dict[str, bool]:
    assert tool.annotations is not None
    return {field: getattr(tool.annotations, field) for field in ANNOTATION_FIELDS}


def _runtime_annotation_snapshot(tools) -> dict[str, dict[str, bool]]:
    by_name = {tool.name: tool for tool in tools}
    assert set(TARGET_TOOL_NAMES) <= set(by_name)
    return {name: _tool_annotations(by_name[name]) for name in TARGET_TOOL_NAMES}


def _private_ci_surface_names(names: list[str]) -> set[str]:
    prefixes = (
        "list_private_ci_",
        "start_private_ci_",
        "get_private_ci_",
        "wait_private_ci_",
        "cancel_private_ci_",
        "plan_private_ci_",
    )
    return {
        name for name in names
        if name.startswith(prefixes) or name in {"validate_development_task", "converge_development_task"}
    }


@pytest.mark.asyncio
async def test_runtime_registration_enumerates_and_matches_private_ci_annotation_matrix(monkeypatch):
    monkeypatch.setenv("MYGITHUB12_EXPOSE_DEPRECATED_TOOLS", "true")
    registered = await service_mcp.list_tools()
    registered_names = [tool.name for tool in registered]

    assert _private_ci_surface_names(registered_names) == set(TARGET_TOOL_NAMES)
    assert _runtime_annotation_snapshot(registered) == {
        name: {field: EXPECTED_ANNOTATIONS[name][field] for field in ANNOTATION_FIELDS}
        for name in TARGET_TOOL_NAMES
    }

    monkeypatch.setenv("MYGITHUB12_EXPOSE_DEPRECATED_TOOLS", "false")
    canonical = await service_mcp.list_tools()
    canonical_names = {tool.name for tool in canonical}
    assert "wait_private_ci_job" not in canonical_names
    assert set(TARGET_TOOL_NAMES) - {"wait_private_ci_job"} <= canonical_names
    assert _tool_annotations(next(tool for tool in registered if tool.name == "wait_private_ci_job")) == {
        field: EXPECTED_ANNOTATIONS["wait_private_ci_job"][field] for field in ANNOTATION_FIELDS
    }


@pytest.mark.asyncio
async def test_list_workers_annotation_covers_real_reconciliation_call(monkeypatch):
    reconcile_calls = []

    def fake_reconcile():
        reconcile_calls.append(True)
        return 0

    monkeypatch.setattr("app.ci_database.reconcile_stale_workers", fake_reconcile)
    monkeypatch.setattr(ci_mcp, "db_get_workers", lambda: [{"worker_id": "wsl-ci-01"}])
    local_mcp = StructuredFastMCP("web-ci-tool-annotations")
    ci_mcp.register_private_ci_mcp_tools(local_mcp)

    tool = next(tool for tool in await local_mcp.list_tools() if tool.name == "list_private_ci_workers")
    assert _tool_annotations(tool) == {
        field: EXPECTED_ANNOTATIONS["list_private_ci_workers"][field] for field in ANNOTATION_FIELDS
    }
    await local_mcp.call_tool("list_private_ci_workers", {})
    assert reconcile_calls == [True]


@pytest.mark.asyncio
async def test_repeated_running_cancel_is_idempotent_after_first_request(monkeypatch):
    cancel_requests = []
    monkeypatch.setattr(
        ci_mcp,
        "get_job",
        lambda job_id: {"job_id": job_id, "status": "running"},
    )
    monkeypatch.setattr(
        ci_mcp,
        "request_cancel_job",
        lambda job_id: cancel_requests.append(job_id) or True,
    )
    local_mcp = StructuredFastMCP("web-ci-tool-annotations")
    ci_mcp.register_private_ci_mcp_tools(local_mcp)

    for _ in range(2):
        await local_mcp.call_tool("cancel_private_ci_job", {"job_id": "job-running"})

    assert cancel_requests == ["job-running", "job-running"]
    tool = next(tool for tool in await local_mcp.list_tools() if tool.name == "cancel_private_ci_job")
    assert _tool_annotations(tool) == {
        field: EXPECTED_ANNOTATIONS["cancel_private_ci_job"][field] for field in ANNOTATION_FIELDS
    }


def test_manifest_contains_explicit_annotation_matrix_and_reasons():
    manifest = _manifest()
    snapshot = manifest["tool_annotation_snapshot"]
    assert set(snapshot) == set(TARGET_TOOL_NAMES)
    legacy = json.loads(
        (_root() / "docs" / "MYGITHUB10_TOOL_MANIFEST.json").read_text(encoding="utf-8")
    )
    legacy_by_name = {tool["name"]: tool for tool in legacy["tools"]}
    assert legacy_by_name["list_private_ci_workers"]["read_only"] is False
    assert legacy_by_name["wait_private_ci_job"]["read_only"] is True
    assert legacy_by_name["wait_private_ci_job"]["consequential"] is False

    for name in TARGET_TOOL_NAMES:
        expected = EXPECTED_ANNOTATIONS[name]
        assert {field: snapshot[name][field] for field in ANNOTATION_FIELDS} == {
            field: expected[field] for field in ANNOTATION_FIELDS
        }
        assert snapshot[name]["consequential"] is expected["consequential"]
        assert snapshot[name]["reason"].strip()


@pytest.mark.asyncio
async def test_manifest_and_capability_schema_snapshots_match_runtime(monkeypatch):
    manifest = _manifest()

    monkeypatch.setenv("MYGITHUB12_RUNTIME_MODE", "production")
    monkeypatch.setenv("MYGITHUB12_BUILD_SHA", "a" * 40)
    monkeypatch.setenv("MYGITHUB12_EXPOSE_DEPRECATED_TOOLS", "false")
    canonical = await service_mcp.list_tools()
    canonical_identity = await service_mcp.tool_schema_identity(canonical)
    canonical_capabilities = json.loads(await get_mygithub_capabilities())
    assert {field: canonical_capabilities[field] for field in IDENTITY_FIELDS} == {
        field: canonical_identity[field] for field in IDENTITY_FIELDS
    }
    assert {field: canonical_identity[field] for field in IDENTITY_FIELDS} == manifest["schema_snapshots"]["canonical"]
    assert "wait_private_ci_job" in canonical_identity["hidden_deprecated_tools"]

    monkeypatch.setenv("MYGITHUB12_EXPOSE_DEPRECATED_TOOLS", "true")
    compatibility = await service_mcp.list_tools()
    compatibility_identity = await service_mcp.tool_schema_identity(compatibility)
    compatibility_capabilities = json.loads(await get_mygithub_capabilities())
    assert {field: compatibility_capabilities[field] for field in IDENTITY_FIELDS} == {
        field: compatibility_identity[field] for field in IDENTITY_FIELDS
    }
    assert {field: compatibility_identity[field] for field in IDENTITY_FIELDS} == manifest["schema_snapshots"]["compatibility"]
    assert "wait_private_ci_job" in {tool.name for tool in compatibility}
    assert canonical_identity["tool_schema_sha256"] != compatibility_identity["tool_schema_sha256"]


def test_no_non_read_tool_is_claimed_idempotent_without_behavior_evidence():
    assert not any(
        name != "cancel_private_ci_job"
        and not spec["readOnlyHint"]
        and spec["idempotentHint"]
        for name, spec in EXPECTED_ANNOTATIONS.items()
    )
