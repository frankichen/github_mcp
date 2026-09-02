import json
import os
from pathlib import Path

import pytest

from app.mcp_server import mcp


EXPECTED_REGISTERED_TOOL_COUNT = 175
EXPECTED_CANONICAL_TOOL_COUNT = 165
DX1_TOOLS = [
    "prepare_development_task",
    "resume_development_task",
    "recover_drifted_development_task",
    "recover_base_synced_development_task",
    "apply_development_change_set",
    "validate_development_task",
    "converge_development_task",
    "finalize_development_task",
]
INFRASTRUCTURE_DEPLOY_TOOLS = [
    "plan_infrastructure_deployment",
    "start_infrastructure_deployment",
    "get_infrastructure_deployment",
]
HIGH_LEVEL_PUT_TOOLS = [
    "put_generated_files",
    "put_github_file",
    "put_github_files",
    "put_github_file_from_local_candidate",
]
MYGITHUB12_BASE_TOOLS = {
    "get_repository_index_status", "request_repository_index_build",
    "get_repository_index_job", "wait_repository_index_job",
    "cancel_repository_index_job", "list_repository_indexes",
    "plan_private_ci_job",
    "create_development_workspace", "get_development_workspace",
    "list_development_workspaces", "renew_development_workspace_lease",
    "resume_development_workspace",
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
HIDDEN_DEPRECATED_TOOLS = {
    "get_github_file", "commit_github_files", "get_test_deployment_logs",
    "begin_github_file_upload", "append_github_file_upload_chunk", "finalize_github_file_upload",
    "commit_github_uploaded_files", "put_github_file", "put_github_files",
    "put_github_file_from_local_candidate",
}


@pytest.mark.asyncio
async def test_registered_tool_manifest_is_stable_and_unique(monkeypatch):
    monkeypatch.setenv("MYGITHUB12_EXPOSE_DEPRECATED_TOOLS", "true")
    actual = await mcp.list_tools()
    actual_names = [tool.name for tool in actual]

    assert len(actual_names) == EXPECTED_REGISTERED_TOOL_COUNT
    assert len(actual_names) == len(set(actual_names))
    assert all(name and name.strip() == name for name in actual_names)
    tools = {tool.name: tool for tool in actual}
    assert MYGITHUB12_BASE_TOOLS <= set(actual_names)
    assert len(MYGITHUB12_BASE_TOOLS) == 40
    assert actual_names[-15:-11] == HIGH_LEVEL_PUT_TOOLS
    assert actual_names[-11:-3] == DX1_TOOLS
    assert actual_names[-3:] == INFRASTRUCTURE_DEPLOY_TOOLS
    for name in DX1_TOOLS:
        assert tools[name].annotations.readOnlyHint is False
        assert tools[name].annotations.destructiveHint is False
        assert tools[name].annotations.idempotentHint is False
    resume_schema = tools["resume_development_task"].inputSchema["properties"]
    assert resume_schema["recover_stale_session"]["default"] is True
    assert resume_schema["renew_lease"]["default"] is False
    assert resume_schema["lease_seconds"]["default"] == 7200
    recovery_schema = tools["recover_drifted_development_task"].inputSchema
    assert {"repository", "branch", "workspace_id", "development_session_id", "expected_workspace_revision", "expected_session_revision", "expected_current_head_sha", "expected_current_tree_sha", "expected_base_branch", "expected_base_sha", "idempotency_key"} <= set(recovery_schema["required"])
    assert recovery_schema["properties"]["lease_seconds"]["default"] == 7200
    base_sync_schema = tools["recover_base_synced_development_task"].inputSchema
    assert {
        "repository", "branch", "workspace_id", "development_session_id",
        "expected_workspace_revision", "expected_session_revision",
        "expected_old_base_sha", "expected_new_base_sha", "expected_base_branch",
        "expected_old_session_head_sha", "expected_current_head_sha",
        "expected_current_tree_sha", "idempotency_key",
    } <= set(base_sync_schema["required"])
    assert base_sync_schema["properties"]["lease_seconds"]["default"] == 7200
    change_set_tool = tools["apply_development_change_set"]
    change_set_schema = change_set_tool.inputSchema
    assert set(change_set_schema["required"]) == {
        "development_session_id", "expected_session_revision",
        "expected_workspace_revision", "expected_head_sha", "commit_message",
    }
    assert {
        "change_set_json", "change_set_file", "bundle_file", "prepared_change_set_id",
        "expected_change_set_size_bytes", "expected_change_set_sha256",
        "expected_change_set_git_blob_sha",
    } <= set(change_set_schema["properties"])
    change_set_meta = getattr(change_set_tool, "meta", None) or getattr(change_set_tool, "_meta", None) or {}
    assert change_set_meta["openai/fileParams"] == ["change_set_file", "bundle_file"]
    assert "large or exact-byte-sensitive" in change_set_tool.description
    assert "bundle_file as a transport alias" in change_set_tool.description
    assert tools["plan_infrastructure_deployment"].annotations.readOnlyHint is True
    assert tools["start_infrastructure_deployment"].annotations.readOnlyHint is False
    assert tools["start_infrastructure_deployment"].annotations.destructiveHint is True
    assert tools["get_infrastructure_deployment"].annotations.readOnlyHint is True
    infrastructure_get_input = tools["get_infrastructure_deployment"].inputSchema
    assert infrastructure_get_input["required"] == ["deployment_id"]
    infrastructure_get_schema = infrastructure_get_input["properties"]
    assert infrastructure_get_schema["wait_seconds"]["default"] == 0
    assert infrastructure_get_schema["last_known_revision"]["default"] == 0
    assert infrastructure_get_schema["last_known_status"]["default"] == ""
    assert infrastructure_get_schema["last_known_step"]["default"] == ""
    assert infrastructure_get_schema["include_log_tail"]["default"] is False
    assert infrastructure_get_schema["log_tail_lines"]["default"] == 40
    assert tools["plan_private_ci_job"].annotations.readOnlyHint is True
    assert tools["build_github_patch"].annotations.readOnlyHint is True
    builder_schema = tools["build_github_patch"].inputSchema
    assert builder_schema["properties"]["operation"]["default"] == "modify"
    assert builder_schema["properties"]["operation"]["enum"] == ["modify", "add", "delete"]
    assert "operation" not in builder_schema["required"]
    for lease_tool_name in ("create_development_workspace", "renew_development_workspace_lease", "resume_development_workspace", "prepare_development_task"):
        lease_schema = tools[lease_tool_name].inputSchema["properties"]["lease_seconds"]
        assert lease_schema["default"] == 7200
    assert tools["resume_development_workspace"].annotations.readOnlyHint is False
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
    for name in ("commit_github_files", "apply_github_patch", "apply_github_patch_from_ref", "replace_github_text_once", "edit_github_file_ranges", "commit_github_uploaded_files", *HIGH_LEVEL_PUT_TOOLS[1:]):
        properties = tools[name].inputSchema["properties"]
        assert properties["workspace_id"]["default"] == ""
        assert properties["expected_workspace_revision"]["default"] == 0

    monkeypatch.setenv("MYGITHUB12_EXPOSE_DEPRECATED_TOOLS", "false")
    canonical_names = [tool.name for tool in await mcp.list_tools()]
    assert len(canonical_names) == EXPECTED_CANONICAL_TOOL_COUNT
    assert HIDDEN_DEPRECATED_TOOLS.isdisjoint(canonical_names)
    assert canonical_names == [name for name in actual_names if name not in HIDDEN_DEPRECATED_TOOLS]
    assert "put_generated_files" in canonical_names
    assert {"edit_github_file_ranges", "replace_github_text_once", "apply_github_patch"} <= set(canonical_names)


def test_composed_mygithub12_manifest_matches_new_tools():
    root = Path(os.environ.get("CI_REPOSITORY_ROOT", "") or Path(__file__).resolve().parents[3])
    manifest = json.loads((root / "docs" / "MYGITHUB12_TOOL_MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["service_name"] == "MyGithut12"
    assert manifest["service_version"] == "12.9.2"
    assert manifest["manifest_format"] == "composed-v2"
    assert manifest["legacy_tool_count"] == 120
    assert manifest["new_tool_count"] == 55
    assert manifest["tool_count"] == EXPECTED_CANONICAL_TOOL_COUNT
    assert manifest["compatibility_tool_count"] == EXPECTED_REGISTERED_TOOL_COUNT
    assert set(manifest["hidden_deprecated_tools"]) == HIDDEN_DEPRECATED_TOOLS
    assert manifest["schema_identity_algorithm"] == "sha256-canonical-mcp-tools-v1"
    assert manifest["new_tools"][-15:-11] == HIGH_LEVEL_PUT_TOOLS
    assert manifest["new_tools"][-11:-3] == DX1_TOOLS
    assert manifest["new_tools"][-3:] == INFRASTRUCTURE_DEPLOY_TOOLS
    assert set(manifest["new_tools"][:-15]) == MYGITHUB12_BASE_TOOLS
    legacy = json.loads((root / "docs" / "MYGITHUB10_TOOL_MANIFEST.json").read_text(encoding="utf-8"))
    assert [tool["name"] for tool in legacy["tools"]].count("apply_github_patch_from_ref") == 1
    apply_tool = next(tool for tool in legacy["tools"] if tool["name"] == "apply_github_patch_from_ref")
    assert apply_tool["read_only"] is False
    assert apply_tool["consequential"] is True
    assert {"patch_repository", "patch_ref", "patch_path", "expected_patch_blob_sha", "expected_patch_sha256", "expected_patch_size_bytes"} <= set(apply_tool["input_schema"]["required"])
    assert manifest["new_tools"].count("analyze_repository_patch_from_ref") == 1
