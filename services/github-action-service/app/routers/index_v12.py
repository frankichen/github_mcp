"""Internal MyGithut12 repository-index HTTP contract.

These routes are authenticated by the controller API key and do not accept
arbitrary Git URLs, host paths or shell commands.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app import mygithub12
from app.auth import verify_api_key
from app.github_client import GitHubClient
from app.services.github_service import GitHubService

router = APIRouter()
_service = GitHubService(GitHubClient())


def _error(exc: Exception) -> JSONResponse:
    if isinstance(exc, mygithub12.MyGithub12Error):
        return JSONResponse(
            status_code=409 if exc.code.startswith("WORKSPACE_") or exc.code.startswith("INDEX_") else 400,
            content={"ok": False, "error": {"code": exc.code, "message": exc.message, "details": exc.details, "trace_id": exc.trace_id}},
        )
    return JSONResponse(status_code=500, content={"ok": False, "error": {"code": "INTERNAL_ERROR", "message": "MyGithut12 internal API failed"}})


async def _run(request: Request, function, *args, **kwargs):
    verify_api_key(request)
    try:
        return await run_in_threadpool(function, *args, **kwargs)
    except Exception as exc:
        return _error(exc)


@router.post("/v1/index/builds")
async def create_index_build(request: Request, body: dict[str, Any] = Body(...)):
    return await _run(request, mygithub12.request_index_build, _service, body["repository"], body["commit_sha"], body.get("strategy", "auto"), body.get("base_commit_sha", ""), body.get("priority", "interactive"), body.get("idempotency_key", ""), bool(body.get("force", False)))


@router.get("/v1/index/builds/{job_id}")
async def get_index_build(request: Request, job_id: str):
    return await _run(request, mygithub12.get_index_job, job_id)


@router.get("/v1/index/builds/{job_id}/wait")
async def wait_index_build(request: Request, job_id: str, timeout_seconds: int = Query(55, ge=0, le=55), last_known_revision: int = 0, last_known_status: str = "", last_known_step: str = ""):
    return await _run(request, mygithub12.wait_index_job, job_id, timeout_seconds, last_known_revision, last_known_status, last_known_step)


@router.post("/v1/index/builds/{job_id}/cancel")
async def cancel_index_build(request: Request, job_id: str):
    return await _run(request, mygithub12.cancel_index_job, job_id)


@router.get("/v1/index/repositories/{repository:path}/indexes")
async def list_indexes(request: Request, repository: str, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)):
    return await _run(request, mygithub12.list_indexes, _service, repository, limit, offset)


@router.get("/v1/index/repositories/{repository:path}/commits/{commit_sha}/status")
async def index_status(request: Request, repository: str, commit_sha: str):
    return await _run(request, mygithub12.get_index_status, _service, repository, commit_sha, "")


@router.post("/v1/search/text")
async def search_text(request: Request, body: dict[str, Any] = Body(...)):
    return await _run(request, mygithub12.search_text, _service, body["repository"], body["commit_sha"], body["query"], bool(body.get("regex", False)), bool(body.get("case_sensitive", False)), body.get("path_globs_json", "[]"), int(body.get("context_lines", 2)), int(body.get("limit", 100)), body.get("cursor", ""))


@router.post("/v1/search/semantic")
async def search_semantic(request: Request, body: dict[str, Any] = Body(...)):
    return await _run(request, mygithub12.search_semantic, _service, body["repository"], body["commit_sha"], body["query"], body.get("path_globs_json", "[]"), int(body.get("limit", 20)), body.get("cursor", ""))


@router.post("/v1/search/symbols")
async def search_symbols(request: Request, body: dict[str, Any] = Body(...)):
    return await _run(request, mygithub12.search_symbols, _service, body["repository"], body["commit_sha"], body["query"], body.get("kinds_json", "[]"), body.get("languages_json", "[]"), body.get("path_prefix", ""), int(body.get("limit", 100)), body.get("cursor", ""))


@router.post("/v1/symbols/definition")
async def symbol_definition(request: Request, body: dict[str, Any] = Body(...)):
    return await _run(request, mygithub12.get_symbol_definition, _service, body["repository"], body["commit_sha"], body.get("symbol_id", ""), body.get("path", ""), int(body.get("line", 0)), int(body.get("column", 0)))


@router.post("/v1/symbols/references")
async def symbol_references(request: Request, body: dict[str, Any] = Body(...)):
    return await _run(request, mygithub12.find_references, _service, body["repository"], body["commit_sha"], body["symbol_id"], bool(body.get("include_definition", False)), int(body.get("limit", 100)), body.get("cursor", ""))


@router.post("/v1/symbols/call-hierarchy")
async def symbol_call_hierarchy(request: Request, body: dict[str, Any] = Body(...)):
    return await _run(request, mygithub12.call_hierarchy, _service, body["repository"], body["commit_sha"], body["symbol_id"], body.get("direction", "both"), int(body.get("depth", 2)), int(body.get("limit", 200)))


@router.post("/v1/symbols/implementations")
async def symbol_implementations(request: Request, body: dict[str, Any] = Body(...)):
    return await _run(request, mygithub12.symbol_implementations, _service, body["repository"], body["commit_sha"], body["symbol_id"])


@router.post("/v1/symbols/type-hierarchy")
async def symbol_type_hierarchy(request: Request, body: dict[str, Any] = Body(...)):
    return await _run(request, mygithub12.symbol_type_hierarchy, _service, body["repository"], body["commit_sha"], body["symbol_id"], body.get("direction", "both"))


@router.post("/v1/symbols/diagnostics")
async def symbol_diagnostics(request: Request, body: dict[str, Any] = Body(...)):
    return await _run(request, mygithub12.symbol_diagnostics, _service, body["repository"], body["commit_sha"], body.get("symbol_id", ""), body.get("path", ""))


@router.post("/v1/symbols/history")
async def symbol_history(request: Request, body: dict[str, Any] = Body(...)):
    return await _run(request, mygithub12.symbol_history, _service, body["repository"], body["commit_sha"], body["symbol_id"], int(body.get("limit", 30)))


@router.post("/v1/graphs/dependencies")
async def dependency_graph(request: Request, body: dict[str, Any] = Body(...)):
    return await _run(request, mygithub12.dependency_graph, _service, body["repository"], body["commit_sha"], body.get("path_prefix", ""), body.get("symbol_id", ""), int(body.get("depth", 2)), int(body.get("limit", 500)))


@router.post("/v1/instructions/resolve")
async def resolve_instructions(request: Request, body: dict[str, Any] = Body(...)):
    return await _run(request, mygithub12.agent_instructions, _service, body["repository"], body["commit_sha"], body.get("target_paths_json", "[]"))


@router.post("/v1/context-packs/repository")
async def repository_context(request: Request, body: dict[str, Any] = Body(...)):
    return await _run(request, mygithub12.repository_context_pack, _service, body["repository"], body["commit_sha"], body["task"], body.get("seed_paths_json", "[]"), body.get("seed_symbols_json", "[]"), int(body.get("max_files", 30)), int(body.get("max_total_bytes", 512000)), bool(body.get("include_tests", True)), bool(body.get("include_docs", True)))


@router.post("/v1/context-packs/change")
async def change_context(request: Request, body: dict[str, Any] = Body(...)):
    return await _run(request, mygithub12.change_context_pack, _service, body["repository"], body["base_commit_sha"], body["head_commit_sha"], body.get("task", ""), int(body.get("max_files", 50)), int(body.get("max_total_bytes", 1048576)))


@router.post("/v1/analysis/change-impact")
async def change_impact(request: Request, body: dict[str, Any] = Body(...)):
    return await _run(request, mygithub12.change_impact, _service, body["repository"], body["base_commit_sha"], body["head_commit_sha"])


@router.post("/v1/analysis/patch")
async def patch_analysis(request: Request, body: dict[str, Any] = Body(...)):
    return await _run(request, mygithub12.analyze_patch, _service, body["repository"], body["base_commit_sha"], body["patch"])


@router.post("/v1/analysis/affected-tests")
async def affected_tests(request: Request, body: dict[str, Any] = Body(...)):
    return await _run(request, mygithub12.affected_tests, _service, body["repository"], body["head_commit_sha"], body.get("base_commit_sha", ""), body.get("paths_json", "[]"), body.get("symbol_ids_json", "[]"), body.get("patch", ""))


@router.post("/v1/analysis/contract-changes")
async def contract_changes(request: Request, body: dict[str, Any] = Body(...)):
    return await _run(request, mygithub12.contract_changes, _service, body["repository"], body["base_commit_sha"], body["head_commit_sha"])
