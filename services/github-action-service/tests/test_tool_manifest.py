import json
from pathlib import Path

import pytest

from app.mcp_server import mcp


EXPECTED_TOOL_COUNT = 155
MYGITHUB12_TOOLS = {
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
    assert MYGITHUB12_TOOLS <= set(actual_names)
    assert len(MYGITHUB12_TOOLS) == 37
    assert tools["build_github_patch"].annotations.readOnlyHint is True
    assert tools["search_repository_text"].annotations.readOnlyHint is True
    assert "result" not in (tools["get_private_ci_job"].outputSchema.get("properties") or {})
    assert tools["get_private_ci_job"].inputSchema["properties"]["detail_level"]["default"] == "summary"
    templates = await mcp.list_resource_templates()
    assert any(str(template.uriTemplate) == "mygithub12://response/{resource_id}" for template in templates)
    for name in ("commit_github_files", "apply_github_patch", "edit_github_file_ranges", "commit_github_uploaded_files"):
        properties = tools[name].inputSchema["properties"]
        assert properties["workspace_id"]["default"] == ""
        assert properties["expected_workspace_revision"]["default"] == 0


def test_composed_mygithub12_manifest_matches_new_tools():
    root = Path(__file__).resolve().parents[3]
    manifest = json.loads((root / "docs" / "MYGITHUB12_TOOL_MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["service_name"] == "MyGithut12"
    assert manifest["service_version"] == "12.0.2"
    assert manifest["legacy_tool_count"] == 118
    assert manifest["new_tool_count"] == 37
    assert manifest["tool_count"] == EXPECTED_TOOL_COUNT
    assert set(manifest["new_tools"]) == MYGITHUB12_TOOLS
