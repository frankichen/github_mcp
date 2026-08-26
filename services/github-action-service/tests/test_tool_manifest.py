import json
import os
from pathlib import Path

import pytest

from app.mcp_server import mcp


EXPECTED_TOOL_COUNT = 162
DX1_TOOLS = [
    "prepare_development_task",
    "apply_development_change_set",
    "validate_development_task",
    "finalize_development_task",
]
MYGITHUB12_BASE_TOOLS = {
    "get_repository_index_status", "request_repository_index_build",
    "get_repository_index_job", "wait_repository_index_job",
    "cancel_repository_index_job", "list_repository_indexes",
    "create_development_workspace", "get_development_workspace",
    "list_development_workspaces", "renew_development_workspace_lease",
    "refresh_development_workspace", "close_development_workspace",
    "declare_development_scope", "analyze_development_workspace_overlap",
    "plan_development_workspace_sync", "list_repository_tree",
    "search_repository_files", "get_github_files_batch",
    "search_repository_text", "search_repository_semantic",
    "search_repository_symbols", "get_symbol_definition",
    "find_symbol_references", "get_symbol_call_hierarchy",
    "get_symbol_implementations", "get_symbol_type_hierarchy",
    "get_symbol_diagnostics", "get_symbol_history",
    "get_repository_dependency_graph", "get_repository_agent_instructions",
    "build_repository_context_pack", "build_change_context_pack",
    "analyze_repository_change_impact", "analyze_repository_patch",
    "analyze_repository_patch_from_ref",
    "get_affected_tests", "detect_repository_contract_changes",
    "read_mcp_response_resource",
}


@pytest.mark.asyncio
async def test_registered_tool_manifest_is_stable_and_unique():
    actual = await mcp.list_tools()
    actual_names = [tool.name for tool in actual]

    assert len(actual_names) == EXPECTED_TOOL_COUNT
    assert len(actual_names) == len(set(actual_names))
    assert all(name and name.strip() == name for name in actual_names)
    tools = {tool.name: tool for tool in actual}
    assert MYGITHUB12_BASE_TOOLS <= set(actual_names)
    assert len(MYGITHUB12_BASE_TOOLS) == 38
    assert actual_names[-4:] == DX1_TOOLS
    for name in DX1_TOOLS:
        assert tools[name].annotations.readOnlyHint is False
        assert tools[name].annotations.destructiveHint is False
        assert tools[name].annotations.idempotentHint is False
    assert tools["build_github_patch"].annotations.readOnlyHint is True
    assert tools["search_repository_text"].annotations.readOnlyHint is True
    assert tools["analyze_repository_patch_from_ref"].annotations.readOnlyHint is True
    assert tools["analyze_repository_patch_from_ref"].inputSchema["required"][-3:] == [
        "expected_patch_blob_sha", "expected_patch_sha256", "expected_patch_size_bytes"
    ]
    assert [tool.name for tool in actual].count("analyze_repository_patch_from_ref") == 1
    assert [tool.name for tool in actual].count("apply_github_patch_from_ref") == 1
    assert tools["apply_github_patch_from_ref"].annotations.readOnlyHint is False
    assert tools["apply_github_patch_from_ref"].annotations.destructiveHint is True
    assert tools["apply_github_patch_from_ref"].annotations.idempotentHint is False
    reference_required = {
        "patch_repository", "patch_ref", "patch_path", "expected_patch_blob_sha",
        "expected_patch_sha256", "expected_patch_size_bytes",
    }
    assert reference_required <= set(tools["apply_github_patch_from_ref"].inputSchema["required"])
    assert reference_required <= set(tools["analyze_repository_patch_from_ref"].inputSchema["required"])
    assert "result" not in (tools["get_private_ci_job"].outputSchema.get("properties") or {})
    assert tools["get_private_ci_job"].inputSchema["properties"]["detail_level"]["default"] == "summary"
    templates = await mcp.list_resource_templates()
    assert any(str(template.uriTemplate) == "mygithub12://response/{resource_id}" for template in templates)
    for name in ("commit_github_files", "apply_github_patch", "apply_github_patch_from_ref", "replace_github_text_once", "edit_github_file_ranges", "commit_github_uploaded_files"):
        properties = tools[name].inputSchema["properties"]
        assert properties["workspace_id"]["default"] == ""
        assert properties["expected_workspace_revision"]["default"] == 0


def test_composed_mygithub12_manifest_matches_new_tools():
    root = Path(os.environ.get("CI_REPOSITORY_ROOT", "") or Path(__file__).resolve().parents[3])
    manifest = json.loads((root / "docs" / "MYGITHUB12_TOOL_MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["service_name"] == "MyGithut12"
    assert manifest["service_version"] == "12.1.3"
    assert manifest["legacy_tool_count"] == 120
    assert manifest["new_tool_count"] == 42
    assert manifest["tool_count"] == EXPECTED_TOOL_COUNT
    assert manifest["new_tools"][-4:] == DX1_TOOLS
    assert set(manifest["new_tools"][:-4]) == MYGITHUB12_BASE_TOOLS
    legacy = json.loads((root / "docs" / "MYGITHUB10_TOOL_MANIFEST.json").read_text(encoding="utf-8"))
    assert [tool["name"] for tool in legacy["tools"]].count("apply_github_patch_from_ref") == 1
    apply_tool = next(tool for tool in legacy["tools"] if tool["name"] == "apply_github_patch_from_ref")
    assert apply_tool["read_only"] is False
    assert apply_tool["consequential"] is True
    assert {"patch_repository", "patch_ref", "patch_path", "expected_patch_blob_sha", "expected_patch_sha256", "expected_patch_size_bytes"} <= set(apply_tool["input_schema"]["required"])
    assert manifest["new_tools"].count("analyze_repository_patch_from_ref") == 1
