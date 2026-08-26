"""Register the MyGithut12 MCP tools."""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Callable

from mcp.types import ToolAnnotations

from app import mygithub12

logger = logging.getLogger(__name__)

_READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
_CACHE_WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
_WORKSPACE_WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)


def _error(exc: Exception) -> str:
    if isinstance(exc, mygithub12.MyGithub12Error):
        return json.dumps(
            {
                "ok": False,
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                    "trace_id": exc.trace_id,
                },
            },
            ensure_ascii=False,
        )
    logger.exception("MyGithut12 tool failed")
    return json.dumps(
        {
            "ok": False,
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "MyGithut12 operation failed",
                "details": {"type": type(exc).__name__},
                "trace_id": str(uuid.uuid4()),
            },
        },
        ensure_ascii=False,
    )


async def _call(github_call: Callable[..., Any], function: Callable[..., Any], *args: Any, **kwargs: Any) -> str:
    try:
        result = await github_call(function, *args, **kwargs)
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        return _error(exc)


def register_mygithub12_tools(mcp, github_call, service) -> None:
    # A. Index lifecycle (6)
    @mcp.tool(name="get_repository_index_status", description="Get immutable repository index status for an exact commit/tree identity.", annotations=_READ_ONLY)
    async def get_repository_index_status(repository: str, commit_sha: str = "", ref: str = "") -> str:
        return await _call(github_call, mygithub12.get_index_status, service, repository, commit_sha, ref)

    @mcp.tool(name="request_repository_index_build", description="Build or reuse a full or incremental index for an exact commit. The cache is rebuildable and never changes GitHub.", annotations=_CACHE_WRITE)
    async def request_repository_index_build(repository: str, commit_sha: str, strategy: str = "auto", base_commit_sha: str = "", priority: str = "interactive", idempotency_key: str = "", force: bool = False) -> str:
        return await _call(github_call, mygithub12.request_index_build, service, repository, commit_sha, strategy, base_commit_sha, priority, idempotency_key, force)

    @mcp.tool(name="get_repository_index_job", description="Get one repository index build job.", annotations=_READ_ONLY)
    async def get_repository_index_job(job_id: str) -> str:
        return await _call(github_call, mygithub12.get_index_job, job_id)

    @mcp.tool(name="wait_repository_index_job", description="Long-poll one repository index job for up to 55 seconds.", annotations=_READ_ONLY)
    async def wait_repository_index_job(job_id: str, timeout_seconds: int = 55, last_known_revision: int = 0, last_known_status: str = "", last_known_step: str = "") -> str:
        return await _call(github_call, mygithub12.wait_index_job, job_id, timeout_seconds, last_known_revision, last_known_status, last_known_step)

    @mcp.tool(name="cancel_repository_index_job", description="Request safe cancellation of a queued or running repository index job. Completed immutable indexes are not removed.", annotations=_CACHE_WRITE)
    async def cancel_repository_index_job(job_id: str) -> str:
        return await _call(github_call, mygithub12.cancel_index_job, job_id)

    @mcp.tool(name="list_repository_indexes", description="List bounded repository index metadata and active workspace pins without returning source content.", annotations=_READ_ONLY)
    async def list_repository_indexes(repository: str, limit: int = 50, offset: int = 0) -> str:
        return await _call(github_call, mygithub12.list_indexes, service, repository, limit, offset)

    # A2. Private CI applicability planning (1)
    @mcp.tool(name="plan_private_ci_job", description="Plan whether a private CI profile is applicable to one exact commit using repository policy, manifests and fixed profile entrypoints. Never queues CI.", annotations=_READ_ONLY)
    async def plan_private_ci_job(repository: str, commit_sha: str, profile: str = "repo-auto-check") -> str:
        return await _call(github_call, mygithub12.plan_private_ci_job, service, repository, commit_sha, profile)

    # B. Development workspaces (9)
    @mcp.tool(name="create_development_workspace", description="Create an isolated development workspace and unique ai/ branch or bind an existing ai/ branch.", annotations=_WORKSPACE_WRITE)
    async def create_development_workspace(repository: str, task_name: str, base_ref: str = "main", branch: str = "", owner: str = "chatgpt", create_branch: bool = True, lease_seconds: int = mygithub12.DEFAULT_LEASE_SECONDS) -> str:
        return await _call(github_call, mygithub12.create_workspace, service, repository, task_name, base_ref, branch, owner, create_branch, lease_seconds)

    @mcp.tool(name="get_development_workspace", description="Get current workspace revision, branch identity, lease, scope and drift status.", annotations=_READ_ONLY)
    async def get_development_workspace(workspace_id: str) -> str:
        return await _call(github_call, mygithub12.get_workspace, service, workspace_id)

    @mcp.tool(name="list_development_workspaces", description="List shared development workspaces for multi-window coordination.", annotations=_READ_ONLY)
    async def list_development_workspaces(repository: str = "", status: str = "", branch: str = "", owner: str = "", limit: int = 50, offset: int = 0) -> str:
        return await _call(github_call, mygithub12.list_workspaces, service, repository, status, branch, owner, limit, offset)

    @mcp.tool(name="renew_development_workspace_lease", description="Renew an active workspace write lease using workspace revision CAS.", annotations=_WORKSPACE_WRITE)
    async def renew_development_workspace_lease(workspace_id: str, expected_workspace_revision: int, lease_seconds: int = mygithub12.DEFAULT_LEASE_SECONDS) -> str:
        return await _call(github_call, mygithub12.renew_workspace_lease, service, workspace_id, expected_workspace_revision, lease_seconds)

    @mcp.tool(name="refresh_development_workspace", description="Read the GitHub branch again and mark the workspace drifted if its branch moved externally.", annotations=_WORKSPACE_WRITE)
    async def refresh_development_workspace(workspace_id: str, expected_workspace_revision: int) -> str:
        return await _call(github_call, mygithub12.refresh_workspace, service, workspace_id, expected_workspace_revision)

    @mcp.tool(name="close_development_workspace", description="Close a workspace and release its lease/index pin without deleting its branch or PR.", annotations=_WORKSPACE_WRITE)
    async def close_development_workspace(workspace_id: str, expected_workspace_revision: int) -> str:
        return await _call(github_call, mygithub12.close_workspace, service, workspace_id, expected_workspace_revision)

    @mcp.tool(name="declare_development_scope", description="Declare planned paths, symbols, APIs, tables, migrations and configuration scope for overlap analysis.", annotations=_WORKSPACE_WRITE)
    async def declare_development_scope(workspace_id: str, expected_workspace_revision: int, paths_json: str = "[]", symbols_json: str = "[]", apis_json: str = "[]", tables_json: str = "[]", migrations_json: str = "[]", configs_json: str = "[]", exclusive: bool = False) -> str:
        return await _call(github_call, mygithub12.declare_workspace_scope, service, workspace_id, expected_workspace_revision, paths_json, symbols_json, apis_json, tables_json, migrations_json, configs_json, exclusive)

    @mcp.tool(name="analyze_development_workspace_overlap", description="Compare active workspace declared scopes and actual branch changes, returning evidence-backed conflict levels.", annotations=_READ_ONLY)
    async def analyze_development_workspace_overlap(workspace_id: str, other_workspace_ids_json: str = "[]") -> str:
        return await _call(github_call, mygithub12.workspace_overlap, service, workspace_id, other_workspace_ids_json)

    @mcp.tool(name="plan_development_workspace_sync", description="Plan base-branch synchronization risk without rebasing, merging or moving any branch.", annotations=_READ_ONLY)
    async def plan_development_workspace_sync(workspace_id: str, base_branch: str = "") -> str:
        return await _call(github_call, mygithub12.workspace_sync_plan, service, workspace_id, base_branch)

    # C. Repository navigation and search (5)
    @mcp.tool(name="list_repository_tree", description="List a bounded recursive Git tree for an exact commit.", annotations=_READ_ONLY)
    async def list_repository_tree(repository: str, commit_sha: str, path: str = "", max_depth: int = 5, include_globs_json: str = "[]", exclude_globs_json: str = "[]", limit: int = 500, cursor: str = "") -> str:
        return await _call(github_call, mygithub12.list_repository_tree, service, repository, commit_sha, path, max_depth, include_globs_json, exclude_globs_json, limit, cursor)

    @mcp.tool(name="search_repository_files", description="Search exact-commit file paths and names without searching file contents.", annotations=_READ_ONLY)
    async def search_repository_files(repository: str, commit_sha: str, query: str, path_prefix: str = "", extensions_json: str = "[]", limit: int = 100, cursor: str = "") -> str:
        return await _call(github_call, mygithub12.search_repository_files, service, repository, commit_sha, query, path_prefix, extensions_json, limit, cursor)

    @mcp.tool(name="get_github_files_batch", description="Read a bounded batch of explicit paths from one exact commit with per-file SHA evidence.", annotations=_READ_ONLY)
    async def get_github_files_batch(repository: str, commit_sha: str, paths_json: str, include_content: bool = True, max_total_bytes: int = mygithub12.MAX_BATCH_BYTES) -> str:
        return await _call(github_call, mygithub12.get_files_batch, service, repository, commit_sha, paths_json, include_content, max_total_bytes)

    @mcp.tool(name="search_repository_text", description="Search indexed exact-commit text using literal or bounded regular-expression matching.", annotations=_READ_ONLY)
    async def search_repository_text(repository: str, commit_sha: str, query: str, regex: bool = False, case_sensitive: bool = False, path_globs_json: str = "[]", context_lines: int = 2, limit: int = 100, cursor: str = "") -> str:
        return await _call(github_call, mygithub12.search_text, service, repository, commit_sha, query, regex, case_sensitive, path_globs_json, context_lines, limit, cursor)

    @mcp.tool(name="search_repository_semantic", description="Return non-authoritative natural-language candidate code chunks with path, blob, line and score evidence.", annotations=_READ_ONLY)
    async def search_repository_semantic(repository: str, commit_sha: str, query: str, path_globs_json: str = "[]", limit: int = 20, cursor: str = "") -> str:
        return await _call(github_call, mygithub12.search_semantic, service, repository, commit_sha, query, path_globs_json, limit, cursor)

    # D. Symbols and language intelligence (8)
    @mcp.tool(name="search_repository_symbols", description="Search indexed exact-commit symbols with stable symbol IDs.", annotations=_READ_ONLY)
    async def search_repository_symbols(repository: str, commit_sha: str, query: str, kinds_json: str = "[]", languages_json: str = "[]", path_prefix: str = "", limit: int = 100, cursor: str = "") -> str:
        return await _call(github_call, mygithub12.search_symbols, service, repository, commit_sha, query, kinds_json, languages_json, path_prefix, limit, cursor)

    @mcp.tool(name="get_symbol_definition", description="Get an indexed symbol definition by symbol ID or file position.", annotations=_READ_ONLY)
    async def get_symbol_definition(repository: str, commit_sha: str, symbol_id: str = "", path: str = "", line: int = 0, column: int = 0) -> str:
        return await _call(github_call, mygithub12.get_symbol_definition, service, repository, commit_sha, symbol_id, path, line, column)

    @mcp.tool(name="find_symbol_references", description="Find exact lexical symbol references with explicit reliability labels; never presents heuristics as compiler facts.", annotations=_READ_ONLY)
    async def find_symbol_references(repository: str, commit_sha: str, symbol_id: str, include_definition: bool = False, limit: int = 100, cursor: str = "") -> str:
        return await _call(github_call, mygithub12.find_references, service, repository, commit_sha, symbol_id, include_definition, limit, cursor)

    @mcp.tool(name="get_symbol_call_hierarchy", description="Return evidence-labelled lexical caller/callee relationships for an indexed symbol.", annotations=_READ_ONLY)
    async def get_symbol_call_hierarchy(repository: str, commit_sha: str, symbol_id: str, direction: str = "both", depth: int = 2, limit: int = 200) -> str:
        return await _call(github_call, mygithub12.call_hierarchy, service, repository, commit_sha, symbol_id, direction, depth, limit)

    @mcp.tool(name="get_symbol_implementations", description="Find declarations that explicitly name a selected base type or interface.", annotations=_READ_ONLY)
    async def get_symbol_implementations(repository: str, commit_sha: str, symbol_id: str) -> str:
        return await _call(github_call, mygithub12.symbol_implementations, service, repository, commit_sha, symbol_id)

    @mcp.tool(name="get_symbol_type_hierarchy", description="Return parent and child type relations from indexed declaration evidence.", annotations=_READ_ONLY)
    async def get_symbol_type_hierarchy(repository: str, commit_sha: str, symbol_id: str, direction: str = "both") -> str:
        return await _call(github_call, mygithub12.symbol_type_hierarchy, service, repository, commit_sha, symbol_id, direction)

    @mcp.tool(name="get_symbol_diagnostics", description="Return bounded parser diagnostics for an exact indexed file or symbol without running caller commands.", annotations=_READ_ONLY)
    async def get_symbol_diagnostics(repository: str, commit_sha: str, symbol_id: str = "", path: str = "") -> str:
        return await _call(github_call, mygithub12.symbol_diagnostics, service, repository, commit_sha, symbol_id, path)

    @mcp.tool(name="get_symbol_history", description="Return bounded Git path history for a selected indexed symbol with commit evidence.", annotations=_READ_ONLY)
    async def get_symbol_history(repository: str, commit_sha: str, symbol_id: str, limit: int = 30) -> str:
        return await _call(github_call, mygithub12.symbol_history, service, repository, commit_sha, symbol_id, limit)

    # E. Architecture, context and change analysis (8)
    @mcp.tool(name="get_repository_dependency_graph", description="Build an evidence-labelled import dependency graph from one exact indexed commit.", annotations=_READ_ONLY)
    async def get_repository_dependency_graph(repository: str, commit_sha: str, path_prefix: str = "", symbol_id: str = "", depth: int = 2, limit: int = 500) -> str:
        return await _call(github_call, mygithub12.dependency_graph, service, repository, commit_sha, path_prefix, symbol_id, depth, limit)

    @mcp.tool(name="get_repository_agent_instructions", description="Resolve repository and path-level AI instructions from known instruction files at an exact commit.", annotations=_READ_ONLY)
    async def get_repository_agent_instructions(repository: str, commit_sha: str, target_paths_json: str = "[]") -> str:
        return await _call(github_call, mygithub12.agent_instructions, service, repository, commit_sha, target_paths_json)

    @mcp.tool(name="build_repository_context_pack", description="Build an auditable bounded context pack from task, path and symbol seeds.", annotations=_READ_ONLY)
    async def build_repository_context_pack(repository: str, commit_sha: str, task: str, seed_paths_json: str = "[]", seed_symbols_json: str = "[]", max_files: int = 30, max_total_bytes: int = 512000, include_tests: bool = True, include_docs: bool = True) -> str:
        return await _call(github_call, mygithub12.repository_context_pack, service, repository, commit_sha, task, seed_paths_json, seed_symbols_json, max_files, max_total_bytes, include_tests, include_docs)

    @mcp.tool(name="build_change_context_pack", description="Build a bounded change context pack for two exact commits.", annotations=_READ_ONLY)
    async def build_change_context_pack(repository: str, base_commit_sha: str, head_commit_sha: str, task: str = "", max_files: int = 50, max_total_bytes: int = 1048576) -> str:
        return await _call(github_call, mygithub12.change_context_pack, service, repository, base_commit_sha, head_commit_sha, task, max_files, max_total_bytes)

    @mcp.tool(name="analyze_repository_change_impact", description="Analyze changed files, modules, tests and contracts between two exact commits.", annotations=_READ_ONLY)
    async def analyze_repository_change_impact(repository: str, base_commit_sha: str, head_commit_sha: str) -> str:
        return await _call(github_call, mygithub12.change_impact, service, repository, base_commit_sha, head_commit_sha)

    @mcp.tool(name="analyze_repository_patch", description="Analyze a bounded unified diff against an exact commit without writing GitHub.", annotations=_READ_ONLY)
    async def analyze_repository_patch(repository: str, base_commit_sha: str, patch: str) -> str:
        return await _call(github_call, mygithub12.analyze_patch, service, repository, base_commit_sha, patch)

    @mcp.tool(name="analyze_repository_patch_from_ref", description="Analyze a strict unified diff stored in an exact GitHub blob against an exact repository commit without writing GitHub.", annotations=_READ_ONLY)
    async def analyze_repository_patch_from_ref(repository: str, base_commit_sha: str, patch_repository: str, patch_ref: str, patch_path: str, expected_patch_blob_sha: str, expected_patch_sha256: str, expected_patch_size_bytes: int) -> str:
        return await _call(github_call, mygithub12.analyze_patch_from_ref, service, repository, base_commit_sha, patch_repository, patch_ref, patch_path, expected_patch_blob_sha, expected_patch_sha256, expected_patch_size_bytes)

    @mcp.tool(name="get_affected_tests", description="Select evidence-labelled test candidates from changed commits, paths, symbols or a patch.", annotations=_READ_ONLY)
    async def get_affected_tests(repository: str, head_commit_sha: str, base_commit_sha: str = "", paths_json: str = "[]", symbol_ids_json: str = "[]", patch: str = "") -> str:
        return await _call(github_call, mygithub12.affected_tests, service, repository, head_commit_sha, base_commit_sha, paths_json, symbol_ids_json, patch)

    @mcp.tool(name="detect_repository_contract_changes", description="Detect API, database, configuration, authorization and schema contract candidates between exact commits.", annotations=_READ_ONLY)
    async def detect_repository_contract_changes(repository: str, base_commit_sha: str, head_commit_sha: str) -> str:
        return await _call(github_call, mygithub12.contract_changes, service, repository, base_commit_sha, head_commit_sha)
