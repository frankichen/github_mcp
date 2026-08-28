import hmac
import hashlib
import json
import logging
import base64
import binascii
import asyncio
import os
import re
import subprocess
import inspect
import uuid
from pathlib import Path
from typing import Annotated, Literal, Optional
from typing_extensions import TypedDict

import httpx
from pydantic import Field

from mcp.server.fastmcp.server import AuthSettings
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from app.config import settings as app_settings
from app.github_client import GitHubClient
from app.exceptions import AppError
from app.services.github_service import GitHubService
from app.services.ci_service import get_ci_service
from app.ci_mcp import register_private_ci_mcp_tools
from app.github_extended_mcp import register_github_extended_tools
from app.github_policy import ensure_repository_allowed, repository_is_allowed
from app import github_utils
from app import mygithub10
from app import mygithub12
from app import attestation_registry
from app.version import runtime_build_sha
from app.observability import current_request_id
from app.mygithub12_mcp import register_mygithub12_tools
from app.mygithub12_dx_mcp import register_dx_tools
from app.infrastructure_deployment_mcp import register_infrastructure_deployment_tools
from app.mcp_response import (
    MAX_RESPONSE_RESOURCE_CHUNK_BYTES,
    StructuredFastMCP,
    read_response_resource_chunk,
    read_response_resource_text,
)

logger = logging.getLogger(__name__)


class GeneratedTextFile(TypedDict):
    path: str
    content: str


_client = GitHubClient()
_service = GitHubService(_client)
_ci_service = get_ci_service()


async def _github_call(function, *args, **kwargs):
    """Keep synchronous PyGithub/requests work off the MCP event loop."""
    if getattr(function, "__module__", "") == "app.github_utils":
        bound = inspect.signature(function).bind_partial(*args, **kwargs).arguments
        repository = bound.get("repository")
        if repository:
            ensure_repository_allowed(repository)
        repositories = bound.get("repositories")
        if repositories:
            if not isinstance(repositories, (list, tuple)):
                raise ValueError("repositories must be an array")
            for item in repositories:
                ensure_repository_allowed(item)
    return await asyncio.to_thread(function, *args, **kwargs)


def _write_audit_context(workspace_id: str, workspace_revision: int) -> dict:
    return {
        "request_trace_id": current_request_id(),
        "workspace_id": workspace_id or "",
        "workspace_revision": int(workspace_revision or 0),
    }


def _safe_write_failure(exc: Exception, *, failed_stage: str = "") -> dict:
    if isinstance(exc, mygithub12.MyGithub12Error):
        details = dict(exc.details or {})
        code = exc.code
        message = exc.message
    elif isinstance(exc, mygithub10.MyGithub10Error):
        details = dict(exc.details or {})
        code = exc.code
        message = exc.message
    elif isinstance(exc, AppError):
        details = dict(exc.details or {})
        code = "WRITE_VERIFY_FAILED" if exc.error == "write_verify_failed" else str(exc.error).upper()
        message = exc.message
    else:
        details = {"cause_type": type(exc).__name__}
        code = "INTERNAL_ERROR"
        message = "write operation failed"
    if failed_stage and not details.get("failed_stage"):
        details["failed_stage"] = failed_stage
    return {"code": code, "message": message, "details": details}


async def _finalize_durable_write(
    result: dict,
    workspace_id: str,
    expected_workspace_revision: int,
) -> dict:
    """Finalize a write only after GitHub durable verification and Workspace CAS."""
    if result.get("replayed"):
        # A replay is allowed only for an already success_verified operation.
        return result
    operation_id = str(result.pop("_operation_id", "") or "")
    cleanup_upload_id = str(result.pop("_cleanup_upload_id", "") or "")
    raw_cleanup_upload_ids = result.pop("_cleanup_upload_ids", [])
    cleanup_upload_ids = [str(item) for item in raw_cleanup_upload_ids if str(item)] if isinstance(raw_cleanup_upload_ids, list) else []
    if cleanup_upload_id:
        cleanup_upload_ids.insert(0, cleanup_upload_id)
    if result.get("write_verified") is not True:
        exc = mygithub10.MyGithub10Error(
            "WRITE_VERIFY_FAILED",
            "write result is missing durable GitHub verification evidence",
            {
                "repository": result.get("repository", ""),
                "branch": result.get("branch", ""),
                "new_commit_sha": result.get("commit_sha", ""),
                "failed_stage": "durable_verify_required",
            },
        )
        if operation_id:
            await _github_call(
                mygithub10._idempotent_finish,
                operation_id, "failed", result.get("commit_sha"), exc.code,
                {**result, "failed_stage": "durable_verify_required", "error": {"code": exc.code, "details": exc.details}},
            )
        raise exc

    if workspace_id:
        result["workspace_id"] = workspace_id
        result["workspace_revision_before"] = expected_workspace_revision
        try:
            workspace = await _github_call(
                mygithub12.workspace_write_complete,
                workspace_id, expected_workspace_revision,
                result["commit_sha"], result["tree_sha"], result,
            )
            result["workspace"] = workspace
            result["workspace_revision_after"] = workspace["revision"]
            result["workspace_head_sha"] = workspace["head_sha"]
        except Exception as exc:
            failure = _safe_write_failure(exc, failed_stage="workspace_finalize")
            failure["details"].update({
                "github_write_verified": True,
                "github_branch_head": result.get("verified_branch_head_sha", ""),
                "new_commit_sha": result.get("commit_sha", ""),
                "recovery_required": True,
                "recommended_action": "refresh_workspace",
            })
            if operation_id:
                await _github_call(
                    mygithub10._idempotent_finish,
                    operation_id, "indeterminate", result.get("commit_sha"), failure["code"],
                    {**result, "failed_stage": "workspace_finalize", "error": failure},
                )
            if isinstance(exc, mygithub12.MyGithub12Error):
                exc.details.update(failure["details"])
                raise
            raise mygithub10.MyGithub10Error(failure["code"], failure["message"], failure["details"]) from exc

    if operation_id:
        result["operation_id"] = operation_id
        try:
            await _github_call(
                mygithub10._idempotent_finish,
                operation_id, "success_verified", result.get("commit_sha"), None, result,
            )
        except Exception as exc:
            raise mygithub10.MyGithub10Error(
                "IDEMPOTENCY_FINALIZE_FAILED",
                "GitHub write is verified but durable idempotency finalization failed",
                {
                    "operation_id": operation_id,
                    "repository": result.get("repository", ""),
                    "branch": result.get("branch", ""),
                    "new_commit_sha": result.get("commit_sha", ""),
                    "observed_branch_head": result.get("verified_branch_head_sha", ""),
                    "expected_tree_sha": result.get("tree_sha", ""),
                    "observed_tree_sha": result.get("verified_tree_sha", ""),
                    "failed_stage": "idempotency_finalize",
                    "github_write_verified": True,
                    "workspace_finalized": bool(result.get("workspace_revision_after")),
                    "recovery_required": True,
                    "cause_type": type(exc).__name__,
                },
            ) from exc
    for cleanup_id in dict.fromkeys(cleanup_upload_ids):
        try:
            await _github_call(mygithub10.abort_upload, cleanup_id)
        except Exception:
            logger.warning("verified upload cleanup failed upload_id=%s", cleanup_id)
    return result


class ApiKeyVerifier:
    async def verify_token(self, token: str) -> Optional[AccessToken]:
        if not token or not app_settings.ACTION_API_KEY.get_secret_value():
            return None
        if not hmac.compare_digest(token, app_settings.ACTION_API_KEY.get_secret_value()):
            return None
        return AccessToken(
            token=token,
            client_id="github-action-service",
            scopes=[],
            resource=app_settings.SERVICE_URL,
        )


mcp = StructuredFastMCP(
    "GitHub Action Service",
    instructions="""\
Use this MCP server to read and write code on GitHub repositories, and manage CI/CD workflows.

GitHub Tools:
- get_github_file: Read file content from a GitHub repository
- list_github_directory: List directory contents
- create_github_branch: Create a new branch
- commit_github_files: Commit files to a repository
- create_github_pull_request: Create a pull request

GitHub Actions CI Tools:
- list_ci_workers: List GitHub Actions self-hosted runners
- list_ci_profiles: List GitHub Actions workflows
- list_ci_jobs: List GitHub Actions workflow runs
- start_ci_job: Trigger a GitHub Actions workflow_dispatch
- get_ci_job: Get details of a GitHub Actions run
- get_ci_logs: Get logs from a GitHub Actions run
- cancel_ci_job: Cancel a GitHub Actions run

Private CI Tools (German-controller + WSL-Podman):
- list_private_ci_workers: List private CI workers (wsl-ci-01)
- list_private_ci_profiles: List private CI profiles (repo-auto-check)
- list_private_ci_jobs: List private CI jobs
- start_private_ci_job: Start a private CI job for an exact commit SHA
- get_private_ci_job: Get private CI job details
- get_private_ci_logs: Get private CI job logs
- cancel_private_ci_job: Cancel a private CI job

Development History & Reports:
- list_github_commits: Query commit history by time range
- search_github_pull_request_history: Search PR activity
- list_github_review_history: List code reviews
- list_github_issue_history: List issue activity
- get_github_development_history: Aggregated development data
- get_github_weekly_report_data: Structured weekly report data
""",
    token_verifier=ApiKeyVerifier(),
    auth=AuthSettings(
        issuer_url=app_settings.SERVICE_URL,
        resource_server_url=app_settings.SERVICE_URL,
    ),
    # The application listens on localhost behind nginx, so FastMCP would
    # otherwise auto-enable localhost-only DNS rebinding protection.  The
    # public MCP endpoint is intentionally served through this exact domain.
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            "github.555044.xyz",
            "github.555044.xyz:*",
            "127.0.0.1:*",
            "localhost:*",
        ],
        allowed_origins=[
            "https://github.555044.xyz",
            "https://github.555044.xyz:*",
            "http://127.0.0.1:*",
            "http://localhost:*",
        ],
    ),
    stateless_http=True,
)


@mcp.resource("mygithub12://response/{resource_id}", mime_type="application/json")
def get_mcp_response_resource(resource_id: str) -> str:
    """Read one short-lived oversized tool response through MCP resources/read."""
    return read_response_resource_text(f"mygithub12://response/{resource_id}")


async def read_mcp_response_resource(
    resource_uri: str,
    offset_bytes: int = 0,
    limit_bytes: int = MAX_RESPONSE_RESOURCE_CHUNK_BYTES,
) -> dict[str, object]:
    try:
        return read_response_resource_chunk(resource_uri, offset_bytes, limit_bytes)
    except FileNotFoundError as exc:
        return {"ok": False, "error": {"code": "MCP_RESPONSE_RESOURCE_NOT_FOUND", "message": str(exc)}}
    except ValueError as exc:
        return {"ok": False, "error": {"code": "INVALID_MCP_RESPONSE_RESOURCE", "message": str(exc)}}


# ========================================================================
# Compatibility tools from the MyGithub09 baseline. Keep their public names stable.
# ========================================================================

@mcp.tool(
    name="get_github_file",
    description="Get file content from a GitHub repository. Returns the file content with metadata including SHA, size, and line range.",
)
async def get_github_file(
    repository: str, path: str, ref: str = "", start_line: int = 0, end_line: int = 0,
) -> str:
    try:
        sl = start_line if start_line > 0 else None
        el = end_line if end_line > 0 else None
        result = await _github_call(
            _service.get_file,
            repository=repository,
            path=path,
            ref=ref,
            start_line=sl,
            end_line=el,
        )
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": type(e).__name__, "message": str(e)})


@mcp.tool(
    name="list_github_directory",
    description="List directory contents in a GitHub repository. Returns files and subdirectories with their metadata.",
)
async def list_github_directory(repository: str, path: str, ref: str = "") -> str:
    try:
        result = await _github_call(
            _service.list_directory, repository=repository, path=path, ref=ref
        )
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": type(e).__name__, "message": str(e)})


@mcp.tool(
    name="create_github_branch",
    description="Create a new branch in a GitHub repository. Fails if the branch already exists.",
)
async def create_github_branch(repository: str, branch: str, base_branch: str = "main") -> str:
    try:
        result = await _github_call(
            _service.create_branch,
            repository=repository,
            branch=branch,
            base_branch=base_branch,
        )
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": type(e).__name__, "message": str(e)})


@mcp.tool(
    name="diagnose_text_payload",
    description="Diagnostic: reports metadata of a received text payload (character_count, utf8_byte_count, sha256, marker checks). Use this to verify ChatGPT is not truncating tool call arguments when committing large files.",
)
async def diagnose_text_payload(
    payload: str, label: str = "", start_marker: str = "", end_marker: str = "",
) -> str:
    import hashlib
    utf8_bytes = payload.encode("utf-8")
    return json.dumps({
        "label": label,
        "character_count": len(payload),
        "utf8_byte_count": len(utf8_bytes),
        "sha256": hashlib.sha256(utf8_bytes).hexdigest(),
        "start_marker_found": start_marker in payload if start_marker else None,
        "end_marker_found": end_marker in payload if end_marker else None,
        "preview_first_100": payload[:100],
        "preview_last_100": payload[-100:] if len(payload) >= 100 else "",
        "ends_with_newline": payload.endswith("\n") if payload else None,
        "null_bytes": "\x00" in payload,
    }, ensure_ascii=False)


@mcp.tool(
    name="commit_github_files",
    description="""Commit one or more files to a GitHub repository. All files go into a single commit.
Each file must have: path, operation (upsert/delete), content, optional expected_sha.
Optional pull_request object: create, base_branch, title, body.""",
)
async def commit_github_files(
    repository: str, branch: str, commit_message: str, files_json: str,
    base_branch: str = "main", create_branch_if_missing: bool = False,
    expected_head_sha: str = "", pull_request_json: str = "{}",
    workspace_id: str = "", expected_workspace_revision: int = 0,
) -> str:
    from app.models import CommitRequest, FileOperation, PullRequestConfig
    operation_id = ""
    try:
        logger.info("MCP commit_github_files: repo=%s branch=%s files_json_len=%d", repository, branch, len(files_json))
        files_data = json.loads(files_json)
        pr_data = json.loads(pull_request_json) if pull_request_json else {}
        file_ops = [FileOperation(**f) for f in files_data]
        pr_config = PullRequestConfig(**pr_data) if pr_data.get("create") else None
        audit_request = {
            "tool_name": "commit_github_files",
            "repository": repository,
            "branch": branch,
            "base_branch": base_branch,
            "create_branch_if_missing": create_branch_if_missing,
            "expected_head_sha": expected_head_sha,
            "commit_message": commit_message,
            "files": [
                {
                    "path": f.path,
                    "operation": f.operation,
                    "expected_sha": f.expected_sha or "",
                    "content_sha256": hashlib.sha256((f.content or "").encode("utf-8")).hexdigest() if f.operation == "upsert" else None,
                    "size_bytes": len((f.content or "").encode("utf-8")) if f.operation == "upsert" else 0,
                }
                for f in file_ops
            ],
        }
        operation_id, _ = await _github_call(
            mygithub10._idempotent_start,
            "commit_github_files", "", audit_request,
            _write_audit_context(workspace_id, expected_workspace_revision),
        )
        await _github_call(
            mygithub12.workspace_write_preflight,
            _service, repository, branch, expected_head_sha, workspace_id, expected_workspace_revision,
        )
        request = CommitRequest(
            repository=repository, branch=branch, base_branch=base_branch,
            create_branch_if_missing=create_branch_if_missing,
            commit_message=commit_message, expected_head_sha=expected_head_sha if expected_head_sha else None,
            files=file_ops, pull_request=pr_config,
        )
        try:
            result = await _github_call(_service.commit_files, request)
        except Exception as exc:
            failure = _safe_write_failure(exc)
            if operation_id:
                await _github_call(
                    mygithub10._idempotent_finish,
                    operation_id, "failed", None, failure["code"],
                    {"failed_stage": failure["details"].get("failed_stage", "github_write"), "error": failure},
                )
            if isinstance(exc, mygithub12.MyGithub12Error):
                return _mygithub10_error(exc)
            if isinstance(exc, AppError):
                code = "HEAD_CHANGED" if exc.error == "head_sha_conflict" else ("WRITE_VERIFY_FAILED" if exc.error == "write_verify_failed" else str(exc.error).upper())
                return _mygithub10_error(mygithub10.MyGithub10Error(code, exc.message, dict(exc.details or {})))
            return _mygithub10_error(exc)

        if result.get("write_verified") is not True:
            raise mygithub10.MyGithub10Error(
                "WRITE_VERIFY_FAILED",
                "commit_github_files writer returned without durable GitHub verification",
                {"repository": repository, "branch": branch, "new_commit_sha": result.get("commit_sha", ""), "failed_stage": "durable_verify_required"},
            )
        await _github_call(mygithub10._idempotent_mark_git_verified, operation_id, result)
        result["_operation_id"] = operation_id
        result = await _finalize_durable_write(result, workspace_id, expected_workspace_revision)
        return json.dumps(result, ensure_ascii=False)
    except json.JSONDecodeError as e:
        return json.dumps({"error": "json_parse_error", "message": f"files_json is not valid JSON (pos {e.pos}: {e.msg})", "received_len": len(files_json), "preview": files_json[:200]})
    except Exception as exc:
        # _finalize_durable_write owns git_verified -> indeterminate transitions;
        # only pre-Git failures are marked failed here.
        if operation_id:
            try:
                row = await _github_call(mygithub10._idempotent_existing_by_operation, operation_id)
                if row and row.get("status") == "in_progress":
                    failure = _safe_write_failure(exc)
                    await _github_call(
                        mygithub10._idempotent_finish,
                        operation_id, "failed", None, failure["code"],
                        {"failed_stage": failure["details"].get("failed_stage", "before_write"), "error": failure},
                    )
            except Exception:
                logger.exception("commit_github_files audit finalization failed operation_id=%s", operation_id)
        if isinstance(exc, mygithub12.MyGithub12Error) or isinstance(exc, mygithub10.MyGithub10Error):
            return _mygithub10_error(exc)
        if isinstance(exc, AppError):
            code = "HEAD_CHANGED" if exc.error == "head_sha_conflict" else ("WRITE_VERIFY_FAILED" if exc.error == "write_verify_failed" else str(exc.error).upper())
            return _mygithub10_error(mygithub10.MyGithub10Error(code, exc.message, dict(exc.details or {})))
        return _mygithub10_error(exc)


@mcp.tool(
    name="create_github_pull_request",
    description="Create a pull request on GitHub between two branches.",
)
async def create_github_pull_request(
    repository: str, head_branch: str, base_branch: str = "main",
    title: str = "", body: str = "", draft: bool = True,
) -> str:
    try:
        result = await _github_call(
            _service.create_pull_request,
            repository=repository,
            head_branch=head_branch,
            base_branch=base_branch,
            title=title,
            body=body,
            draft=draft,
        )
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": type(e).__name__, "message": str(e)})


# ========================================================================
# EXISTING GitHub Actions CI TOOLS (7) - MUST NOT BE MODIFIED
# ========================================================================

@mcp.tool(
    name="list_ci_workers",
    description="List GitHub repository self-hosted Actions runners. Returns runner name, status, OS, and labels. NOT for private CI workers (use list_private_ci_workers).",
)
async def list_ci_workers(repository: str) -> str:
    try:
        result = await _ci_service.list_ci_workers(repository)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": type(e).__name__, "message": str(e)})


@mcp.tool(
    name="list_ci_profiles",
    description="List GitHub Actions workflow definitions. Returns workflow name, state, and URL. NOT for private CI profiles (use list_private_ci_profiles).",
)
async def list_ci_profiles(repository: str) -> str:
    try:
        result = await _ci_service.list_ci_profiles(repository)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": type(e).__name__, "message": str(e)})


@mcp.tool(
    name="list_ci_jobs",
    description="List GitHub Actions workflow runs. Can filter by workflow_id, branch, or status. NOT for private CI jobs (use list_private_ci_jobs).",
)
async def list_ci_jobs(repository: str, workflow_id: str = "", branch: str = "", status: str = "", limit: int = 20) -> str:
    try:
        result = await _ci_service.list_ci_jobs(repository=repository, workflow_id=workflow_id, branch=branch, status=status, limit=limit)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": type(e).__name__, "message": str(e)})


@mcp.tool(
    name="start_ci_job",
    description="Trigger a GitHub Actions workflow_dispatch workflow. Requires workflow_id and a ref (branch/tag). NOT for private CI (use start_private_ci_job).",
)
async def start_ci_job(repository: str, workflow_id: str, ref: str = "main", inputs_json: str = "{}") -> str:
    try:
        inputs = json.loads(inputs_json) if inputs_json else {}
    except json.JSONDecodeError as e:
        return json.dumps({"error": "json_parse_error", "message": f"inputs_json is not valid JSON: {e.msg}"})
    if not isinstance(inputs, dict):
        return json.dumps({"error": "validation_error", "message": "inputs_json must decode to a JSON object"})
    try:
        result = await _ci_service.start_ci_job(repository=repository, workflow_id=workflow_id, ref=ref, inputs=inputs)
        return json.dumps(result, ensure_ascii=False)
    except httpx.TimeoutException as e:
        return json.dumps({"error": "timeout", "message": f"request timed out: {e}"})
    except httpx.HTTPStatusError as e:
        return json.dumps({"error": "http_error", "status_code": e.response.status_code, "message": e.response.text})
    except Exception as e:
        return json.dumps({"error": type(e).__name__, "message": str(e)})


@mcp.tool(
    name="get_ci_job",
    description="Get details of a GitHub Actions workflow run by run_id. NOT for private CI jobs (use get_private_ci_job).",
)
async def get_ci_job(repository: str, run_id: str) -> str:
    try:
        result = await _ci_service.get_ci_job(repository, run_id)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": type(e).__name__, "message": str(e)})


@mcp.tool(
    name="get_ci_logs",
    description="Get GitHub Actions workflow run logs. NOT for private CI logs (use get_private_ci_logs).",
)
async def get_ci_logs(repository: str, run_id: str, job_id: str = "") -> str:
    try:
        result = await _ci_service.get_ci_logs(repository, run_id, job_id)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": type(e).__name__, "message": str(e)})


@mcp.tool(
    name="cancel_ci_job",
    description="Cancel a GitHub Actions workflow run by run_id. NOT for private CI jobs (use cancel_private_ci_job).",
)
async def cancel_ci_job(repository: str, run_id: str) -> str:
    try:
        result = await _ci_service.cancel_ci_job(repository, run_id)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": type(e).__name__, "message": str(e)})


# ========================================================================
# NEW GITHUB UTILITY TOOLS (12)
# ========================================================================

@mcp.tool(
    name="get_github_repository",
    description="Get metadata about a GitHub repository including default branch, language, stars, and merge settings.",
)
async def get_github_repository(repository: str) -> str:
    try:
        return json.dumps(await _github_call(github_utils.get_github_repository, repository), ensure_ascii=False)
    except Exception as e:
        return json.dumps(github_utils._error_response("INTERNAL_ERROR", str(e)))


@mcp.tool(
    name="list_github_branches",
    description="List branches in a GitHub repository with optional protected_only filter, pagination support.",
)
async def list_github_branches(
    repository: str, protected_only: bool = False, limit: int = 100, page: int = 1,
) -> str:
    try:
        return json.dumps(await _github_call(github_utils.list_github_branches, repository, protected_only, limit, page), ensure_ascii=False)
    except Exception as e:
        return json.dumps(github_utils._error_response("INTERNAL_ERROR", str(e)))


@mcp.tool(
    name="get_github_branch",
    description="Get details of a specific branch including commit SHA and optional comparison with base branch.",
)
async def get_github_branch(repository: str, branch: str, base_branch: str = "") -> str:
    try:
        return json.dumps(await _github_call(github_utils.get_github_branch, repository, branch, base_branch), ensure_ascii=False)
    except Exception as e:
        return json.dumps(github_utils._error_response("INTERNAL_ERROR", str(e)))


@mcp.tool(
    name="get_github_commit",
    description="Get details of a specific commit by SHA including author, stats, changed files, and verification status.",
)
async def get_github_commit(repository: str, commit_sha: str, file_limit: int = 100) -> str:
    try:
        return json.dumps(await _github_call(github_utils.get_github_commit, repository, commit_sha, file_limit), ensure_ascii=False)
    except Exception as e:
        return json.dumps(github_utils._error_response("INTERNAL_ERROR", str(e)))


@mcp.tool(
    name="compare_github_commits",
    description="Compare two commits, branches, or tags showing ahead/behind, changed files, and commit list.",
)
async def compare_github_commits(
    repository: str, base: str, head: str, file_limit: int = 100,
) -> str:
    try:
        return json.dumps(await _github_call(github_utils.compare_github_commits, repository, base, head, file_limit), ensure_ascii=False)
    except Exception as e:
        return json.dumps(github_utils._error_response("INTERNAL_ERROR", str(e)))


@mcp.tool(
    name="list_github_pull_requests",
    description="List pull requests in a repository with filters for state, branch, sort order, and pagination.",
)
async def list_github_pull_requests(
    repository: str, state: str = "open", head_branch: str = "", base_branch: str = "",
    sort: str = "updated", direction: str = "desc", limit: int = 30, page: int = 1,
) -> str:
    try:
        return json.dumps(await _github_call(github_utils.list_github_pull_requests, repository, state, head_branch, base_branch, sort, direction, limit, page), ensure_ascii=False)
    except Exception as e:
        return json.dumps(github_utils._error_response("INTERNAL_ERROR", str(e)))


@mcp.tool(
    name="get_github_pull_request",
    description="Get detailed information about a specific pull request including mergeability, reviews, and stats.",
)
async def get_github_pull_request(repository: str, pull_number: int) -> str:
    try:
        return json.dumps(await _github_call(github_utils.get_github_pull_request, repository, pull_number), ensure_ascii=False)
    except Exception as e:
        return json.dumps(github_utils._error_response("INTERNAL_ERROR", str(e)))


@mcp.tool(
    name="list_github_pull_request_files",
    description="List files changed in a pull request with optional patch content and pagination.",
)
async def list_github_pull_request_files(
    repository: str, pull_number: int, limit: int = 100, page: int = 1, include_patch: bool = True,
) -> str:
    try:
        return json.dumps(await _github_call(github_utils.list_github_pull_request_files, repository, pull_number, limit, page, include_patch), ensure_ascii=False)
    except Exception as e:
        return json.dumps(github_utils._error_response("INTERNAL_ERROR", str(e)))


@mcp.tool(
    name="update_github_pull_request",
    description="Update a PR's title, body, state, or base branch. Supports expected_head_sha for concurrency protection. Cannot merge PRs.",
)
async def update_github_pull_request(
    repository: str, pull_number: int, title: str = "", body: str = "",
    state: str = "", base_branch: str = "", expected_head_sha: str = "",
) -> str:
    try:
        kwargs = {}
        if title: kwargs["title"] = title
        if body: kwargs["body"] = body
        if state: kwargs["state"] = state
        if base_branch: kwargs["base_branch"] = base_branch
        if expected_head_sha: kwargs["expected_head_sha"] = expected_head_sha
        return json.dumps(await _github_call(github_utils.update_github_pull_request, repository, pull_number, **kwargs), ensure_ascii=False)
    except Exception as e:
        return json.dumps(github_utils._error_response("INTERNAL_ERROR", str(e)))


@mcp.tool(
    name="get_github_pull_request_checks",
    description="Get check runs and commit statuses for the PR's current head SHA. Returns overall conclusion and per-check details.",
)
async def get_github_pull_request_checks(repository: str, pull_number: int) -> str:
    try:
        return json.dumps(await _github_call(github_utils.get_github_pull_request_checks, repository, pull_number), ensure_ascii=False)
    except Exception as e:
        return json.dumps(github_utils._error_response("INTERNAL_ERROR", str(e)))


@mcp.tool(
    name="list_github_pull_request_comments",
    description="List comments on a pull request (issue comments and review comments) with pagination.",
)
async def list_github_pull_request_comments(
    repository: str, pull_number: int, comment_type: str = "all", limit: int = 100, page: int = 1,
) -> str:
    try:
        return json.dumps(await _github_call(github_utils.list_github_pull_request_comments, repository, pull_number, comment_type, limit, page), ensure_ascii=False)
    except Exception as e:
        return json.dumps(github_utils._error_response("INTERNAL_ERROR", str(e)))


@mcp.tool(
    name="create_github_pull_request_comment",
    description="Create a comment on a pull request. Supports expected_head_sha for concurrency protection.",
)
async def create_github_pull_request_comment(
    repository: str, pull_number: int, body: str, expected_head_sha: str = "",
) -> str:
    try:
        return json.dumps(await _github_call(github_utils.create_github_pull_request_comment, repository, pull_number, body, expected_head_sha), ensure_ascii=False)
    except Exception as e:
        return json.dumps(github_utils._error_response("INTERNAL_ERROR", str(e)))


@mcp.tool(name="get_github_pull_request_merge_readiness", description="Aggregate safe PR merge readiness: exact head SHA, reviews, GitHub Checks, repository merge methods, and private CI gate.")
async def get_github_pull_request_merge_readiness(repository: str, pull_number: int, expected_head_sha: str = "", required_private_ci_job_id: str = "", expected_base_branch: str = "main") -> str:
    try:
        return json.dumps(await _github_call(github_utils.get_github_pull_request_merge_readiness, repository, pull_number, expected_head_sha, required_private_ci_job_id, expected_base_branch), ensure_ascii=False)
    except Exception as e:
        return json.dumps(github_utils._error_response("INTERNAL_ERROR", str(e)))


@mcp.tool(name="get_github_pull_request_conflicts", description="Read-only PR conflict diagnosis. Never updates the branch or force-pushes.")
async def get_github_pull_request_conflicts(repository: str, pull_number: int, expected_head_sha: str = "") -> str:
    try:
        return json.dumps(await _github_call(github_utils.get_github_pull_request_conflicts, repository, pull_number, expected_head_sha), ensure_ascii=False)
    except Exception as e:
        return json.dumps(github_utils._error_response("INTERNAL_ERROR", str(e)))


@mcp.tool(name="plan_github_pull_request_merge", description="Read-only merge preflight with exact SHA, review, Checks, private CI, and merge-method gates. Never merges.")
async def plan_github_pull_request_merge(repository: str, pull_number: int, merge_method: str = "squash", expected_head_sha: str = "", required_private_ci_job_id: str = "", expected_base_branch: str = "main") -> str:
    try:
        return json.dumps(await _github_call(github_utils.plan_github_pull_request_merge, repository, pull_number, merge_method, expected_head_sha, required_private_ci_job_id, expected_base_branch), ensure_ascii=False)
    except Exception as e:
        return json.dumps(github_utils._error_response("INTERNAL_ERROR", str(e)))


@mcp.tool(name="merge_github_pull_request", description="Safely merge a PR only after readiness gates, exact SHA, passed private CI, and explicit confirm=true. Never deploys. A confirmed merge also queues exact-SHA repository index bootstrap for the new base head.")
async def merge_github_pull_request(repository: str, pull_number: int, merge_method: str = "squash", expected_head_sha: str = "", required_private_ci_job_id: str = "", expected_base_branch: str = "main", commit_title: str = "", commit_message: str = "", delete_head_branch: bool = False, confirm: bool = False) -> str:
    try:
        if delete_head_branch:
            return json.dumps(github_utils._error_response("HEAD_BRANCH_DELETE_REQUIRES_SEPARATE_AUTHORIZATION", "Automatic head branch deletion is disabled; use delete_github_branch separately."))
        result = await _github_call(github_utils.merge_github_pull_request, repository, pull_number, merge_method, expected_head_sha, required_private_ci_job_id, expected_base_branch, commit_title, commit_message, delete_head_branch, confirm)
        if result.get("ok") and result.get("merged"):
            base_after = str(result.get("base_head_after") or "")
            base_before = str(result.get("base_head_before") or "")
            if re.fullmatch(r"[0-9a-f]{40}", base_after):
                try:
                    index_result = await _github_call(
                        mygithub12.request_index_build,
                        _service,
                        repository,
                        base_after,
                        "auto",
                        base_before if re.fullmatch(r"[0-9a-f]{40}", base_before) else "",
                        "interactive",
                        f"post-merge-index:{repository}:{base_after}",
                        False,
                    )
                    result["post_merge_index"] = {
                        key: index_result.get(key)
                        for key in (
                            "ok", "job_id", "commit_sha", "tree_sha", "version", "strategy",
                            "base_commit_sha", "status", "step", "deduplicated",
                        )
                        if key in index_result
                    }
                except Exception as index_exc:
                    error_code = getattr(index_exc, "code", type(index_exc).__name__)
                    result["post_merge_index"] = {
                        "ok": False,
                        "status": "bootstrap_failed",
                        "error_code": error_code,
                    }
                    result.setdefault("warnings", []).append("POST_MERGE_INDEX_BOOTSTRAP_FAILED")
            else:
                result["post_merge_index"] = {
                    "ok": False,
                    "status": "bootstrap_skipped",
                    "error_code": "POST_MERGE_BASE_SHA_INVALID",
                }
                result.setdefault("warnings", []).append("POST_MERGE_INDEX_BOOTSTRAP_SKIPPED")
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps(github_utils._error_response("INTERNAL_ERROR", str(e)))


@mcp.tool(name="mark_github_pull_request_ready", description="Convert a draft PR to ready-for-review with exact head SHA protection.")
async def mark_github_pull_request_ready(repository: str, pull_number: int, expected_head_sha: str) -> str:
    try: return json.dumps(await _github_call(github_utils.mark_github_pull_request_ready, repository, pull_number, expected_head_sha), ensure_ascii=False)
    except Exception as e: return json.dumps(github_utils._error_response("INTERNAL_ERROR", str(e)))


@mcp.tool(name="convert_github_pull_request_to_draft", description="Convert an open PR to draft with exact head SHA protection.")
async def convert_github_pull_request_to_draft(repository: str, pull_number: int, expected_head_sha: str) -> str:
    try: return json.dumps(await _github_call(github_utils.convert_github_pull_request_to_draft, repository, pull_number, expected_head_sha), ensure_ascii=False)
    except Exception as e: return json.dumps(github_utils._error_response("INTERNAL_ERROR", str(e)))


@mcp.tool(name="update_github_pull_request_branch", description="Request GitHub's official update-branch operation; never force-pushes locally.")
async def update_github_pull_request_branch(repository: str, pull_number: int, expected_head_sha: str) -> str:
    try: return json.dumps(await _github_call(github_utils.update_github_pull_request_branch, repository, pull_number, expected_head_sha), ensure_ascii=False)
    except Exception as e: return json.dumps(github_utils._error_response("INTERNAL_ERROR", str(e)))


@mcp.tool(name="list_github_pull_request_reviews", description="List submitted PR reviews, distinct from requested reviewers.")
async def list_github_pull_request_reviews(repository: str, pull_number: int, limit: int = 100, page: int = 1) -> str:
    try: return json.dumps(await _github_call(github_utils.list_github_pull_request_reviews, repository, pull_number, limit, page), ensure_ascii=False)
    except Exception as e: return json.dumps(github_utils._error_response("INTERNAL_ERROR", str(e)))


@mcp.tool(name="request_github_pull_request_reviewers", description="Request user and team reviewers with exact head SHA protection.")
async def request_github_pull_request_reviewers(repository: str, pull_number: int, reviewers_json: str = "[]", team_reviewers_json: str = "[]", expected_head_sha: str = "") -> str:
    try: return json.dumps(await _github_call(github_utils.request_github_pull_request_reviewers, repository, pull_number, reviewers_json, team_reviewers_json, expected_head_sha), ensure_ascii=False)
    except Exception as e: return json.dumps(github_utils._error_response("INTERNAL_ERROR", str(e)))


@mcp.tool(name="remove_github_pull_request_reviewers", description="Remove requested user and team reviewers with exact head SHA protection.")
async def remove_github_pull_request_reviewers(repository: str, pull_number: int, reviewers_json: str = "[]", team_reviewers_json: str = "[]", expected_head_sha: str = "") -> str:
    try: return json.dumps(await _github_call(github_utils.remove_github_pull_request_reviewers, repository, pull_number, reviewers_json, team_reviewers_json, expected_head_sha), ensure_ascii=False)
    except Exception as e: return json.dumps(github_utils._error_response("INTERNAL_ERROR", str(e)))


@mcp.tool(name="delete_github_branch", description="Delete a non-default, non-protected branch only with exact SHA and confirm=true. Never follows merge automatically.")
async def delete_github_branch(repository: str, branch: str, expected_head_sha: str, confirm: bool = False) -> str:
    try: return json.dumps(await _github_call(github_utils.delete_github_branch, repository, branch, expected_head_sha, confirm), ensure_ascii=False)
    except Exception as e: return json.dumps(github_utils._error_response("INTERNAL_ERROR", str(e)))


def _deployment_tool_error(exc):
    return json.dumps({"ok": False, "error": {"code": "INTERNAL_ERROR", "message": str(exc), "details": {}}}, ensure_ascii=False)


@mcp.tool(name="plan_test_deployment", description="Plan a whitelist-only repository test deployment from its fixed deployment contract; validates exact main SHA, private CI policy, changed files, and deployment infrastructure changes. Never accepts host or shell input.")
async def plan_test_deployment(repository: str, environment: str, commit_sha: str, private_ci_job_id: str = "", scope: str = "fullstack", expected_current_release_id: str = "", allow_deploy_infrastructure_changes: bool = False, artifact_id: str = "") -> str:
    try:
        if artifact_id and os.environ.get("MYGITHUB10_ARTIFACT_DEPLOY_ENABLED", "false").lower() not in {"1", "true", "yes", "on"}: return json.dumps({"ok": False, "error_code": "FEATURE_DISABLED"})
        from app import deployment_service
        return json.dumps(await _github_call(deployment_service.plan_test_deployment, repository, environment, commit_sha, private_ci_job_id, scope, expected_current_release_id, allow_deploy_infrastructure_changes, artifact_id), ensure_ascii=False)
    except Exception as e: return _deployment_tool_error(e)


@mcp.tool(name="start_test_deployment", description="Queue a whitelist-only repository test deployment from its fixed deployment contract after exact main SHA and required private CI gates. Requires confirm=true; never accepts host or shell input.")
async def start_test_deployment(repository: str, environment: str, commit_sha: str, private_ci_job_id: str = "", scope: str = "fullstack", expected_current_release_id: str = "", allow_deploy_infrastructure_changes: bool = False, force_redeploy: bool = False, confirm: bool = False, artifact_id: str = "") -> str:
    try:
        if artifact_id and os.environ.get("MYGITHUB10_ARTIFACT_DEPLOY_ENABLED", "false").lower() not in {"1", "true", "yes", "on"}: return json.dumps({"ok": False, "error_code": "FEATURE_DISABLED"})
        from app import deployment_service
        return json.dumps(await _github_call(deployment_service.start_test_deployment, repository, environment, commit_sha, private_ci_job_id, scope, expected_current_release_id, allow_deploy_infrastructure_changes, force_redeploy, confirm, "mcp", artifact_id), ensure_ascii=False)
    except Exception as e: return _deployment_tool_error(e)


@mcp.tool(name="get_test_deployment", description="Get a deployment status by deployment_id; deployment_id is distinct from CI job_id and GitHub Actions run_id.")
async def get_test_deployment(deployment_id: str) -> str:
    try:
        from app import deployment_service
        return json.dumps(await _github_call(deployment_service.get_test_deployment, deployment_id), ensure_ascii=False)
    except Exception as e: return _deployment_tool_error(e)


@mcp.tool(name="get_test_deployment_logs", description="Read redacted paginated deployment logs by deployment_id.")
async def get_test_deployment_logs(deployment_id: str, offset: int = 0, limit: int = 200) -> str:
    try:
        from app import deployment_service
        return json.dumps(await _github_call(deployment_service.get_test_deployment_logs, deployment_id, offset, limit), ensure_ascii=False)
    except Exception as e: return _deployment_tool_error(e)


@mcp.tool(name="wait_test_deployment", description="Long-poll deployment metadata for up to 55 seconds without returning logs or lease tokens.")
async def wait_test_deployment(deployment_id: str, timeout_seconds: int = 55, last_known_status: str = "", last_known_step: str = "", last_known_revision: int = 0) -> str:
    try:
        from app import deployment_service
        return json.dumps(await _github_call(deployment_service.wait_test_deployment, deployment_id, timeout_seconds, last_known_status, last_known_step, last_known_revision), ensure_ascii=False)
    except Exception as e: return _deployment_tool_error(e)


@mcp.tool(name="get_test_deployment_log_tail", description="Read a redacted deployment log tail without returning lease tokens.")
async def get_test_deployment_log_tail(deployment_id: str, lines: int = 100) -> str:
    try:
        from app import deployment_service
        return json.dumps(await _github_call(deployment_service.get_test_deployment_log_tail, deployment_id, lines), ensure_ascii=False)
    except Exception as e: return _deployment_tool_error(e)


@mcp.tool(name="list_test_deployments", description="List whitelist-only repository test deployments with filters and pagination.")
async def list_test_deployments(repository: str = "", environment: str = "", commit_sha: str = "", status: str = "", limit: int = 20, offset: int = 0) -> str:
    try:
        from app import deployment_service
        return json.dumps(await _github_call(deployment_service.list_test_deployments, repository, environment, commit_sha, status, limit, offset), ensure_ascii=False)
    except Exception as e: return _deployment_tool_error(e)


@mcp.tool(name="cancel_test_deployment", description="Cancel a queued deployment immediately or request safe cancellation at a worker boundary.")
async def cancel_test_deployment(deployment_id: str) -> str:
    try:
        from app import deployment_service
        return json.dumps(await _github_call(deployment_service.cancel_test_deployment, deployment_id), ensure_ascii=False)
    except Exception as e: return _deployment_tool_error(e)


@mcp.tool(name="get_test_environment_status", description="Get redacted fixed-contract test environment and health summary; never returns env values or credentials.")
async def get_test_environment_status(repository: str, environment: str) -> str:
    try:
        from app import deployment_service
        return json.dumps(await _github_call(deployment_service.get_test_environment_status, repository, environment), ensure_ascii=False)
    except Exception as e: return _deployment_tool_error(e)


@mcp.tool(name="list_test_releases", description="List verified fixed-contract test release summaries without reading env files.")
async def list_test_releases(repository: str, environment: str, limit: int = 20) -> str:
    try:
        from app import deployment_service
        return json.dumps(await _github_call(deployment_service.list_test_releases, repository, environment, limit), ensure_ascii=False)
    except Exception as e: return _deployment_tool_error(e)


@mcp.tool(name="rollback_test_deployment", description="Queue a whitelist-only rollback after current-release and checksum gates. Never runs goose down or deletes data/releases.")
async def rollback_test_deployment(repository: str, environment: str, target_release_id: str, expected_current_release_id: str, confirm: bool = False) -> str:
    try:
        from app import deployment_service
        return json.dumps(await _github_call(deployment_service.rollback_test_deployment, repository, environment, target_release_id, expected_current_release_id, confirm), ensure_ascii=False)
    except Exception as e: return _deployment_tool_error(e)


# ========================================================================
# DEVELOPMENT HISTORY TOOLS (6)
# ========================================================================

@mcp.tool(
    name="list_github_commits",
    description="Query commit history in a repository by time range. Filters by author/identity, excludes bots and merge commits by default. Supports pagination.",
)
async def list_github_commits_tool(
    repository: str, branch: str = "", author: str = "", identity: str = "",
    since: str = "", until: str = "", path: str = "",
    include_merge_commits: bool = False, limit: int = 100, page: int = 1,
) -> str:
    try:
        return json.dumps(await _github_call(github_utils.list_github_commits, repository, branch, author, identity, since, until, path, include_merge_commits, limit, page), ensure_ascii=False)
    except Exception as e:
        return json.dumps(github_utils._error_response("INTERNAL_ERROR", str(e)))


@mcp.tool(
    name="search_github_pull_request_history",
    description="Search PR history across repositories by identity, time range, and activity type (authored/updated/merged/reviewed/commented).",
)
async def search_github_pull_request_history_tool(
    repositories_json: str, identity: str, activity: str = "all",
    since: str = "", until: str = "", state: str = "all",
    include_drafts: bool = True, limit: int = 100, offset: int = 0,
) -> str:
    try:
        repos = json.loads(repositories_json) if repositories_json else []
        return json.dumps(await _github_call(github_utils.search_github_pull_request_history, repos, identity, activity, since, until, state, include_drafts, limit, offset), ensure_ascii=False)
    except Exception as e:
        return json.dumps(github_utils._error_response("INTERNAL_ERROR", str(e)))


@mcp.tool(
    name="list_github_review_history",
    description="List code reviews performed by an identity across repositories in a time range.",
)
async def list_github_review_history_tool(
    repositories_json: str, identity: str, since: str = "", until: str = "",
    states_json: str = '["APPROVED","CHANGES_REQUESTED","COMMENTED"]', limit: int = 100, offset: int = 0,
) -> str:
    try:
        repos = json.loads(repositories_json) if repositories_json else []
        states = json.loads(states_json) if states_json else None
        return json.dumps(await _github_call(github_utils.list_github_review_history, repos, identity, since, until, states, limit, offset), ensure_ascii=False)
    except Exception as e:
        return json.dumps(github_utils._error_response("INTERNAL_ERROR", str(e)))


@mcp.tool(
    name="list_github_issue_history",
    description="List issues an identity participated in across repositories. Excludes pull requests from results.",
)
async def list_github_issue_history_tool(
    repositories_json: str, identity: str, activity: str = "all",
    since: str = "", until: str = "", state: str = "all",
    limit: int = 100, offset: int = 0,
) -> str:
    try:
        repos = json.loads(repositories_json) if repositories_json else []
        return json.dumps(await _github_call(github_utils.list_github_issue_history, repos, identity, activity, since, until, state, limit, offset), ensure_ascii=False)
    except Exception as e:
        return json.dumps(github_utils._error_response("INTERNAL_ERROR", str(e)))


@mcp.tool(
    name="get_github_development_history",
    description="Get aggregated development history (commits, PRs, reviews, issues, CI runs, open work) for an identity across repositories in a time range. Ideal for daily/weekly/monthly reports.",
)
async def get_github_development_history_tool(
    identity: str, repositories_json: str = "[]", since: str = "", until: str = "",
    include_json: str = '["commits","pull_requests","reviews","issues","github_actions","private_ci","open_work"]',
    include_details: bool = True, max_items_per_section: int = 100,
) -> str:
    try:
        repos = json.loads(repositories_json) if repositories_json else None
        include = json.loads(include_json) if include_json else None
        result = await _github_call(github_utils.get_github_development_history, identity, repos, since, until, include, include_details, max_items_per_section)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"ok": False, "error": {"code": "INTERNAL_ERROR", "message": str(e)}}, ensure_ascii=False)


@mcp.tool(
    name="get_github_weekly_report_data",
    description="Get structured data for a weekly development report. Includes completed work, work in progress, code reviews, CI quality, risks, and next-week candidates.",
)
async def get_github_weekly_report_data_tool(
    identity: str, repositories_json: str = "[]", week: str = "current",
    week_start: str = "monday", timezone: str = "Asia/Shanghai",
    include_weekend: bool = True, include_open_work: bool = True,
    include_ci_failures: bool = True, include_code_statistics: bool = True,
    since: str = "", until: str = "",
) -> str:
    try:
        repos = json.loads(repositories_json) if repositories_json else None
        result = await _github_call(
            github_utils.get_github_weekly_report_data,
            identity, repos, week, week_start, timezone,
            include_weekend, include_open_work, include_ci_failures, include_code_statistics, since, until
        )
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"ok": False, "error": {"code": "INTERNAL_ERROR", "message": str(e)}}, ensure_ascii=False)


# ========================================================================
# Register Private CI MCP Tools
# ========================================================================


def _mygithub10_error(exc: Exception) -> str:
    if isinstance(exc, mygithub12.MyGithub12Error):
        return json.dumps({"ok": False, "error": {"code": exc.code, "message": exc.message, "details": exc.details, "trace_id": exc.trace_id}}, ensure_ascii=False)
    if isinstance(exc, mygithub10.MyGithub10Error):
        code = {"PATCH_HEAD_CHANGED": "HEAD_CHANGED", "FILE_BINARY_UNSUPPORTED": "BINARY_FILE_UNSUPPORTED", "PATCH_UNSAFE_PATH": "INVALID_REPOSITORY_PATH"}.get(exc.code, exc.code)
        return json.dumps({"ok": False, "error": {"code": code, "message": exc.message, "details": exc.details, "trace_id": exc.trace_id}}, ensure_ascii=False)
    logger.exception("MyGithut write tool failed")
    return json.dumps({"ok": False, "error": {"code": "INTERNAL_ERROR", "message": "MyGithut operation failed", "details": {}, "trace_id": str(uuid.uuid4())}}, ensure_ascii=False)


@mcp.tool(name="get_mygithub_capabilities", description="Return the explicit MyGithut12 capability and canonical connector schema identity.")
async def get_mygithub_capabilities() -> str:
    capabilities = mygithub10.capabilities(runtime_build_sha())
    visible_tools = await mcp.list_tools()
    capabilities.update(await mcp.tool_schema_identity(visible_tools))
    return json.dumps(capabilities, ensure_ascii=False)


@mcp.tool(name="get_github_file_manifest", description="Return exact Git Blob metadata for a file without returning file content.")
async def get_github_file_manifest(repository: str, path: str, ref: str = "") -> str:
    try:
        return json.dumps(await _github_call(mygithub10.file_manifest, _service, repository, path, ref), ensure_ascii=False)
    except Exception as exc:
        return _mygithub10_error(exc)


@mcp.tool(name="read_github_file_chunk", description="Read an exact UTF-8 byte chunk with SHA and continuation metadata.")
async def read_github_file_chunk(repository: str, path: str, ref: str = "", offset_bytes: int = 0, limit_bytes: int = mygithub10.MAX_FILE_CHUNK_BYTES, expected_blob_sha: str = "") -> str:
    try:
        return json.dumps(await _github_call(mygithub10.file_chunk, _service, repository, path, ref, offset_bytes, limit_bytes, expected_blob_sha), ensure_ascii=False)
    except Exception as exc:
        return _mygithub10_error(exc)


@mcp.tool(name="open_github_file_resource", description="Open a file resource handle for paginated reads instead of returning a large JSON body.")
async def open_github_file_resource(repository: str, path: str, ref: str = "") -> str:
    try:
        manifest = await _github_call(mygithub10.file_manifest, _service, repository, path, ref)
        token = base64.urlsafe_b64encode(json.dumps({"repository": repository, "path": path, "commit": manifest["resolved_commit_sha"]}, separators=(",", ":")).encode()).decode().rstrip("=")
        return json.dumps({"resource_uri": f"mygithub10://blob/{token}", **{key: manifest[key] for key in ("repository", "path", "resolved_commit_sha", "blob_sha", "size_bytes", "content_sha256")}}, ensure_ascii=False)
    except Exception as exc:
        return _mygithub10_error(exc)


@mcp.tool(name="read_github_file_resource", description="Read a bounded page from an opened MyGithut12 file resource.")
async def read_github_file_resource(resource_uri: str, offset_bytes: int = 0, limit_bytes: int = mygithub10.MAX_FILE_CHUNK_BYTES) -> str:
    try:
        token = resource_uri.rsplit("/", 1)[-1]
        token += "=" * (-len(token) % 4)
        item = json.loads(base64.urlsafe_b64decode(token).decode())
        return json.dumps(await _github_call(mygithub10.file_chunk, _service, item["repository"], item["path"], item["commit"], offset_bytes, limit_bytes), ensure_ascii=False)
    except Exception as exc:
        return _mygithub10_error(exc)


@mcp.tool(name="apply_github_patch", description="Apply a strict unified diff atomically with exact HEAD/blob checks, optional workspace CAS, dry-run and idempotency.")
async def apply_github_patch(repository: str, branch: str, expected_head_sha: str, expected_blob_shas_json: str, patch: str, commit_message: str, dry_run: bool = True, idempotency_key: str = "", create_pull_request: bool = False, pull_request_json: str = "{}", workspace_id: str = "", expected_workspace_revision: int = 0) -> str:
    try:
        await _github_call(mygithub12.workspace_write_preflight, _service, repository, branch, expected_head_sha, workspace_id, expected_workspace_revision)
        result = await _github_call(
            mygithub10.apply_patch, _service, repository, branch, expected_head_sha,
            expected_blob_shas_json, patch, commit_message, dry_run, idempotency_key,
            _write_audit_context(workspace_id, expected_workspace_revision),
        )
        if not dry_run:
            result = await _finalize_durable_write(result, workspace_id, expected_workspace_revision)
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        return _mygithub10_error(exc)


@mcp.tool(
    name="apply_github_patch_from_ref",
    description="Apply a strict unified diff stored in an exact GitHub blob, with source identity verification, exact target HEAD/blob checks, optional workspace CAS, dry-run and idempotency.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False,
    ),
)
async def apply_github_patch_from_ref(
    repository: str, branch: str, expected_head_sha: str, expected_blob_shas_json: str,
    patch_repository: str, patch_ref: str, patch_path: str, expected_patch_blob_sha: str,
    expected_patch_sha256: str, expected_patch_size_bytes: int, commit_message: str,
    dry_run: bool = True, idempotency_key: str = "", create_pull_request: bool = False,
    pull_request_json: str = "{}", workspace_id: str = "", expected_workspace_revision: int = 0,
) -> str:
    try:
        await _github_call(mygithub12.workspace_write_preflight, _service, repository, branch, expected_head_sha, workspace_id, expected_workspace_revision)
        result = await _github_call(
            mygithub10.apply_patch_from_ref, _service, repository, branch, expected_head_sha,
            expected_blob_shas_json, patch_repository, patch_ref, patch_path,
            expected_patch_blob_sha, expected_patch_sha256, expected_patch_size_bytes,
            commit_message, dry_run, idempotency_key,
            _write_audit_context(workspace_id, expected_workspace_revision),
        )
        if not dry_run:
            result = await _finalize_durable_write(result, workspace_id, expected_workspace_revision)
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        return _mygithub10_error(exc)


@mcp.tool(name="edit_github_file_ranges", description="Apply non-overlapping exact-text line range edits as one atomic commit. Each item uses expected_blob_sha and replacement_text; replace/delete must explicitly provide expected_old_text or compatibility field expected_old_text_sha256. start_line/end_line are 1-based inclusive. Supports optional workspace CAS. Dry-run and commit use identical computed UTF-8 bytes.")
async def edit_github_file_ranges(
    repository: str,
    branch: str,
    expected_head_sha: str,
    operations_json: Annotated[
        str,
        Field(
            description=(
                "JSON array of exact range edits. Each item includes path, operation, "
                "start_line/end_line, expected_blob_sha, replacement_text, and either "
                "expected_old_text or the explicit compatibility field "
                "expected_old_text_sha256."
            )
        ),
    ],
    commit_message: str,
    dry_run: bool = True,
    idempotency_key: str = "",
    workspace_id: str = "",
    expected_workspace_revision: int = 0,
) -> str:
    try:
        await _github_call(mygithub12.workspace_write_preflight, _service, repository, branch, expected_head_sha, workspace_id, expected_workspace_revision)
        result = await _github_call(
            mygithub10.edit_ranges, _service, repository, branch, expected_head_sha,
            operations_json, commit_message, dry_run, idempotency_key,
            _write_audit_context(workspace_id, expected_workspace_revision),
        )
        if not dry_run:
            result = await _finalize_durable_write(result, workspace_id, expected_workspace_revision)
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        return _mygithub10_error(exc)


@mcp.tool(name="replace_github_text_once", description="Replace exactly one UTF-8 text block without caller-supplied line numbers. Requires exact HEAD/blob identity and expected_match_count=1; no newline, whitespace, or Unicode normalization is performed. Supports dry-run, idempotency, Workspace CAS, and durable read-back.")
async def replace_github_text_once(
    repository: str,
    branch: str,
    expected_head_sha: str,
    path: str,
    expected_blob_sha: str,
    old_text: str,
    new_text: str,
    commit_message: str,
    expected_match_count: int = 1,
    dry_run: bool = True,
    idempotency_key: str = "",
    workspace_id: str = "",
    expected_workspace_revision: int = 0,
) -> str:
    try:
        await _github_call(mygithub12.workspace_write_preflight, _service, repository, branch, expected_head_sha, workspace_id, expected_workspace_revision)
        result = await _github_call(
            mygithub10.replace_text_once, _service, repository, branch, expected_head_sha,
            path, expected_blob_sha, old_text, new_text, commit_message, expected_match_count,
            dry_run, idempotency_key, _write_audit_context(workspace_id, expected_workspace_revision),
        )
        if not dry_run:
            result = await _finalize_durable_write(result, workspace_id, expected_workspace_revision)
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        return _mygithub10_error(exc)


@mcp.tool(
    name="build_github_patch",
    description="Build a deterministic strict unified diff from complete UTF-8 file text. operation defaults to modify; explicit add/delete emit /dev/null semantics. Pure dry-run: never reads or writes GitHub.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
async def build_github_patch(
    path: str,
    expected_blob_sha: str,
    original_text: str,
    replacement_text: str,
    operation: Literal["modify", "add", "delete"] = "modify",
) -> str:
    try:
        return json.dumps(
            mygithub10.build_patch(path, expected_blob_sha, original_text, replacement_text, operation),
            ensure_ascii=False,
        )
    except Exception as exc:
        return _mygithub10_error(exc)


async def _claim_high_level_put(
    tool_name: str,
    request: dict,
    dry_run: bool,
    idempotency_key: str,
    workspace_id: str,
    expected_workspace_revision: int,
) -> tuple[str, dict | None]:
    if dry_run:
        return "", None
    return await _github_call(
        mygithub10._idempotent_start,
        tool_name,
        idempotency_key,
        request,
        _write_audit_context(workspace_id, expected_workspace_revision),
    )


async def _run_high_level_put(
    repository: str,
    branch: str,
    expected_head_sha: str,
    prepared: list[dict],
    commit_message: str,
    dry_run: bool,
    operation_id: str,
    workspace_id: str,
    expected_workspace_revision: int,
    infer_expected_blob_shas: bool = False,
    canonical_payload_hash: str = "",
) -> dict:
    try:
        await _github_call(
            mygithub12.workspace_write_preflight,
            _service,
            repository,
            branch,
            expected_head_sha,
            workspace_id,
            expected_workspace_revision,
        )
    except Exception as exc:
        if operation_id:
            await _github_call(mygithub10.fail_put_operation, operation_id, exc, "workspace_preflight")
        raise
    result = await _github_call(
        mygithub10.execute_put_files,
        _service,
        repository,
        branch,
        expected_head_sha,
        prepared,
        commit_message,
        dry_run,
        operation_id,
        infer_expected_blob_shas,
        canonical_payload_hash,
    )
    if not dry_run:
        result = await _finalize_durable_write(result, workspace_id, expected_workspace_revision)
    return result


PUT_GENERATED_FILES_DESCRIPTION = "The only recommended entry point for routine AI-generated UTF-8 text files. Provide repository, branch, exact expected_head_sha, structured files[{path,content}], commit_message, dry_run, and optional idempotency_key. MyGithut12 discovers existing blobs and internally performs path validation, hashing, chunked staging, atomic CAS commit, and durable read-back. V1 rejects oversized payloads, binary content, and deletion; callers must not manage upload IDs, chunks, offsets, hashes, staging paths, or expected_blob_sha."


async def put_generated_files(
    repository: str,
    branch: str,
    expected_head_sha: str,
    files: list[GeneratedTextFile],
    commit_message: str,
    dry_run: bool = True,
    idempotency_key: str = "",
) -> str:
    operation_id = ""
    try:
        prepared = mygithub10.prepare_generated_files(files)
        request, canonical_payload_hash = mygithub10.build_generated_files_request(
            repository,
            branch,
            expected_head_sha,
            prepared,
            commit_message,
            idempotency_key,
        )
        effective_idempotency_key = idempotency_key or f"put-generated-files:{canonical_payload_hash}"
        operation_id, replay = await _claim_high_level_put(
            "put_generated_files",
            request,
            dry_run,
            effective_idempotency_key,
            "",
            0,
        )
        if replay:
            return json.dumps(replay, ensure_ascii=False)
        result = await _run_high_level_put(
            repository,
            branch,
            expected_head_sha,
            prepared,
            commit_message,
            dry_run,
            operation_id,
            "",
            0,
            True,
            canonical_payload_hash,
        )
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        return _mygithub10_error(exc)


PUT_GITHUB_FILE_DESCRIPTION = "Compatibility-only high-level UTF-8 file write requiring caller-supplied blob CAS. Routine AI-generated text files must use put_generated_files instead."


async def put_github_file(
    repository: str,
    branch: str,
    path: str,
    content: str,
    expected_head_sha: str,
    expected_blob_sha: str,
    commit_message: str,
    dry_run: bool = True,
    idempotency_key: str = "",
    workspace_id: str = "",
    expected_workspace_revision: int = 0,
) -> str:
    try:
        prepared = mygithub10.prepare_inline_put_files([
            {"path": path, "content": content, "expected_blob_sha": expected_blob_sha}
        ])
        request = mygithub10.build_put_request(
            "put_github_file",
            repository,
            branch,
            expected_head_sha,
            prepared,
            commit_message,
            workspace_id,
            expected_workspace_revision,
        )
        operation_id, replay = await _claim_high_level_put(
            "put_github_file",
            request,
            dry_run,
            idempotency_key,
            workspace_id,
            expected_workspace_revision,
        )
        if replay:
            return json.dumps(replay, ensure_ascii=False)
        result = await _run_high_level_put(
            repository,
            branch,
            expected_head_sha,
            prepared,
            commit_message,
            dry_run,
            operation_id,
            workspace_id,
            expected_workspace_revision,
        )
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        return _mygithub10_error(exc)


PUT_GITHUB_FILES_DESCRIPTION = "Compatibility-only atomic multi-file write requiring caller-supplied blob CAS. Routine AI-generated text files must use put_generated_files instead."


async def put_github_files(
    repository: str,
    branch: str,
    expected_head_sha: str,
    files: list[dict[str, str]],
    commit_message: str,
    dry_run: bool = True,
    idempotency_key: str = "",
    workspace_id: str = "",
    expected_workspace_revision: int = 0,
) -> str:
    try:
        prepared = mygithub10.prepare_inline_put_files(files)
        request = mygithub10.build_put_request(
            "put_github_files",
            repository,
            branch,
            expected_head_sha,
            prepared,
            commit_message,
            workspace_id,
            expected_workspace_revision,
        )
        operation_id, replay = await _claim_high_level_put(
            "put_github_files",
            request,
            dry_run,
            idempotency_key,
            workspace_id,
            expected_workspace_revision,
        )
        if replay:
            return json.dumps(replay, ensure_ascii=False)
        result = await _run_high_level_put(
            repository,
            branch,
            expected_head_sha,
            prepared,
            commit_message,
            dry_run,
            operation_id,
            workspace_id,
            expected_workspace_revision,
        )
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        return _mygithub10_error(exc)


PUT_GITHUB_FILE_FROM_LOCAL_CANDIDATE_DESCRIPTION = "Compatibility-only local-candidate file write. Routine AI-generated text files must use put_generated_files; V1 returns PAYLOAD_TOO_LARGE instead of asking callers to manage alternate upload routes."


async def put_github_file_from_local_candidate(
    repository: str,
    branch: str,
    path: str,
    candidate_name: str,
    expected_size_bytes: int,
    expected_sha256: str,
    expected_head_sha: str,
    expected_blob_sha: str,
    commit_message: str,
    dry_run: bool = True,
    idempotency_key: str = "",
    workspace_id: str = "",
    expected_workspace_revision: int = 0,
) -> str:
    operation_id = ""
    try:
        # Build the idempotency identity from caller-attested metadata before
        # touching the candidate. A verified replay therefore does not depend
        # on the temporary candidate still being present.
        descriptor = [{
            "path": path,
            "expected_blob_sha": expected_blob_sha,
            "content_sha256": expected_sha256,
            "size_bytes": expected_size_bytes,
        }]
        request = mygithub10.build_put_request(
            "put_github_file_from_local_candidate",
            repository,
            branch,
            expected_head_sha,
            descriptor,
            commit_message,
            workspace_id,
            expected_workspace_revision,
            source_identity={
                "candidate_name": candidate_name,
                "expected_size_bytes": expected_size_bytes,
                "expected_sha256": expected_sha256,
            },
        )
        operation_id, replay = await _claim_high_level_put(
            "put_github_file_from_local_candidate",
            request,
            dry_run,
            idempotency_key,
            workspace_id,
            expected_workspace_revision,
        )
        if replay:
            return json.dumps(replay, ensure_ascii=False)
        try:
            prepared = await _github_call(
                mygithub10.prepare_local_candidate_file,
                path,
                expected_blob_sha,
                candidate_name,
                expected_size_bytes,
                expected_sha256,
            )
        except Exception as exc:
            if operation_id:
                await _github_call(mygithub10.fail_put_operation, operation_id, exc, "payload_load")
            raise
        result = await _run_high_level_put(
            repository,
            branch,
            expected_head_sha,
            prepared,
            commit_message,
            dry_run,
            operation_id,
            workspace_id,
            expected_workspace_revision,
        )
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        return _mygithub10_error(exc)


@mcp.tool(name="begin_github_file_upload", description="Compatibility-only upload primitive. AI-generated text files must use put_generated_files and must not begin uploads directly.")
async def begin_github_file_upload() -> str:
    try: return json.dumps(await _github_call(mygithub10.begin_upload), ensure_ascii=False)
    except Exception as exc: return _mygithub10_error(exc)


@mcp.tool(name="append_github_file_upload_chunk", description="Compatibility-only upload primitive. AI-generated text files must use put_generated_files and must not append chunks directly.")
async def append_github_file_upload_chunk(upload_id: str, offset: int, content_base64: str = "", text: str = "", chunk_sha256: str = "", idempotency_key: str = "") -> str:
    try:
        if bool(content_base64) == bool(text):
            if content_base64:
                raise mygithub10.MyGithub10Error("UPLOAD_CHUNK_ENCODING_AMBIGUOUS", "provide exactly one of content_base64 or text")
            raise mygithub10.MyGithub10Error("UPLOAD_CHUNK_EMPTY", "provide exactly one non-empty upload chunk payload")
        if content_base64:
            try:
                content = base64.b64decode(content_base64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise mygithub10.MyGithub10Error(
                    "UPLOAD_CHUNK_BASE64_INVALID",
                    "content_base64 is invalid; for UTF-8 text use the text field",
                    {"encoded_length": len(content_base64)},
                ) from exc
        else:
            content = text.encode("utf-8")
        return json.dumps(await _github_call(mygithub10.append_upload, upload_id, offset, content, chunk_sha256, idempotency_key), ensure_ascii=False)
    except Exception as exc: return _mygithub10_error(exc)


@mcp.tool(name="finalize_github_file_upload", description="Compatibility-only upload primitive. AI-generated text files must use put_generated_files and must not finalize uploads directly.")
async def finalize_github_file_upload(upload_id: str, expected_size_bytes: int, expected_sha256: str) -> str:
    try: return json.dumps(await _github_call(mygithub10.finalize_upload, upload_id, expected_size_bytes, expected_sha256), ensure_ascii=False)
    except Exception as exc: return _mygithub10_error(exc)


@mcp.tool(name="commit_github_uploaded_files", description="Compatibility-only finalized-upload commit. AI-generated text files must use put_generated_files instead.")
async def commit_github_uploaded_files(repository: str, branch: str, expected_head_sha: str, path: str, expected_blob_sha: str, upload_id: str, commit_message: str, idempotency_key: str = "", workspace_id: str = "", expected_workspace_revision: int = 0) -> str:
    try:
        await _github_call(mygithub12.workspace_write_preflight, _service, repository, branch, expected_head_sha, workspace_id, expected_workspace_revision)
        result = await _github_call(
            mygithub10.commit_upload, _service, repository, branch, expected_head_sha, path,
            expected_blob_sha, upload_id, commit_message, idempotency_key,
            _write_audit_context(workspace_id, expected_workspace_revision),
        )
        result = await _finalize_durable_write(result, workspace_id, expected_workspace_revision)
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        return _mygithub10_error(exc)


@mcp.tool(name="abort_github_file_upload", description="Abort and remove only the selected temporary upload.")
async def abort_github_file_upload(upload_id: str) -> str:
    try: return json.dumps(await _github_call(mygithub10.abort_upload, upload_id), ensure_ascii=False)
    except Exception as exc: return _mygithub10_error(exc)


@mcp.tool(name="create_attestation_for_passed_job", description="Create a Tree SHA attestation from server-side CI evidence; callers may only choose job_id and bounded expiry.")
async def create_attestation_for_passed_job(job_id: str, expires_in_seconds: int = 604800) -> str:
    try:
        item = await _github_call(
            attestation_registry.create_attestation_for_passed_job,
            job_id=job_id,
            expires_in_seconds=expires_in_seconds,
        )
        return json.dumps({"ok": True, "attestation": item}, ensure_ascii=False)
    except Exception as exc: return _mygithub10_error(exc)


@mcp.tool(name="get_attestation", description="Read a persisted Tree SHA attestation by id.")
async def get_attestation(attestation_id: str) -> str:
    item = await _github_call(attestation_registry.get_attestation, attestation_id)
    return json.dumps({"ok": bool(item), "attestation": item}, ensure_ascii=False)


@mcp.tool(name="validate_attestation", description="Validate every identity, job, toolchain, dependency, config, expiry and revocation gate before CI reuse.")
async def validate_attestation(attestation_id: str) -> str:
    try:
        return json.dumps(await _github_call(attestation_registry.validate_attestation, attestation_id), ensure_ascii=False)
    except Exception as exc: return _mygithub10_error(exc)


@mcp.tool(name="revoke_attestation", description="Revoke one persisted attestation so it can never be reused.")
async def revoke_attestation(attestation_id: str) -> str:
    item = await _github_call(attestation_registry.revoke_attestation, attestation_id)
    return json.dumps({"ok": True, "attestation": item}, ensure_ascii=False)


def _register_release_artifact(metadata: dict) -> dict:
    return attestation_registry.register_release_artifact(metadata)


@mcp.tool(name="build_release_artifact", description="Build and register an artifact only from server-controlled source storage and a valid attestation; callers cannot provide hashes, paths, status or identity evidence.")
async def build_release_artifact(repository: str, commit_sha: str, private_ci_job_id: str, source_attestation_id: str) -> str:
    try:
        from app.feature_flags import ARTIFACT_BUILD, enabled
        if not enabled(ARTIFACT_BUILD): return json.dumps({"ok": False, "error_code": "FEATURE_DISABLED"})
        if repository != "frankichen/sxt": return json.dumps({"ok": False, "error_code": "REPOSITORY_NOT_ALLOWED"})
        branch_state = await _github_call(github_utils.get_github_branch, repository, "main")
        if not branch_state.get("ok") or branch_state.get("commit_sha") != commit_sha: return json.dumps({"ok": False, "error_code": "COMMIT_NOT_CURRENT_MAIN"})
        job = attestation_registry.get_job(private_ci_job_id) if hasattr(attestation_registry, "get_job") else None
        attestation = attestation_registry.get_attestation(source_attestation_id)
        if not attestation or attestation["private_ci_job_id"] != private_ci_job_id or attestation["repository"] != repository or attestation["tested_commit_sha"] != commit_sha: return json.dumps({"ok": False, "error_code": "ATTESTATION_INVALID"})
        job = attestation_registry.get_job(private_ci_job_id)
        evidence = ((job or {}).get("summary") or {}).get("evidence") or {}
        attestation_check = attestation_registry.validate_attestation(source_attestation_id)
        if not attestation_check.get("ok"): return json.dumps({"ok": False, "error_code": "ATTESTATION_INVALID"})
        if not job or job.get("status") != "passed" or job.get("exit_code") != 0 or job.get("branch") != "main" or job.get("superseded_by_job_id"): return json.dumps({"ok": False, "error_code": "ARTIFACT_CI_GATE_FAILED"})
        source_root = Path(os.environ.get("ARTIFACT_SOURCE_ROOT", "")).resolve()
        storage_root = Path(os.environ.get("ARTIFACT_STORAGE_ROOT", "/var/lib/private-ci/artifacts")).resolve()
        if not source_root.is_dir(): return json.dumps({"ok": False, "error_code": "ARTIFACT_SOURCE_NOT_CONFIGURED"})
        head = subprocess.run(["git", "-C", str(source_root), "rev-parse", "HEAD"], capture_output=True, text=True, check=False).stdout.strip()
        tree = subprocess.run(["git", "-C", str(source_root), "rev-parse", "HEAD^{tree}"], capture_output=True, text=True, check=False).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(source_root), "status", "--porcelain"], capture_output=True, text=True, check=False).stdout.strip()
        if head != commit_sha or tree != attestation["tested_tree_sha"] or dirty: return json.dumps({"ok": False, "error_code": "ARTIFACT_SOURCE_NOT_EXACT"})
        import importlib.util
        script = Path(__file__).resolve().parents[2] / "private-ci-deploy-executor" / "scripts" / "artifact_release.py"
        spec = importlib.util.spec_from_file_location("controlled_artifact_builder", script)
        if not spec or not spec.loader: return json.dumps({"ok": False, "error_code": "ARTIFACT_BUILDER_NOT_CONFIGURED"})
        builder = importlib.util.module_from_spec(spec); spec.loader.exec_module(builder)
        artifact_id = str(__import__("uuid").uuid4()); output = storage_root / artifact_id
        summary = job.get("summary") or {}; evidence = summary.get("evidence") or {}
        metadata = {"repository": repository, "branch": "main", "commit_sha": commit_sha, "tree_sha": attestation["tested_tree_sha"], "private_ci_job_id": private_ci_job_id, "source_attestation_id": source_attestation_id, "profile": attestation["profile"], "ci_image_digest": attestation["ci_image_digest"], "go_version": attestation["go_version"], "node_version": attestation["node_version"], "npm_version": attestation["npm_version"]}
        built = builder.build_release_artifact(source_root, output, metadata)
        metadata.update({"artifact_id": artifact_id, "status": "ready", "storage_path": built["archive_path"], "storage_dir": built["storage_dir"], "archive_path": built["archive_path"], "archive_sha256": built["archive_sha256"], "archive_size_bytes": built["archive_size_bytes"], "manifest_sha256": built["manifest_sha256"], "checksums_sha256": built["checksums_sha256"], "provenance_sha256": built["provenance_sha256"], "artifact_format_version": 1, "migration_required": any(path.startswith("db/migrations/") for path in evidence.get("changed_files", []))})
        return json.dumps({"ok": True, "artifact": _register_release_artifact(metadata)}, ensure_ascii=False)
    except Exception as exc: return _mygithub10_error(exc)


@mcp.tool(name="get_release_artifact", description="Read one registered artifact without exposing arbitrary filesystem contents.")
async def get_release_artifact(artifact_id: str) -> str:
    item = await _github_call(attestation_registry.get_artifact, artifact_id)
    return json.dumps({"ok": bool(item), "artifact": item}, ensure_ascii=False)


@mcp.tool(name="list_release_artifacts", description="List registered artifact metadata by repository and status.")
async def list_release_artifacts(repository: str = "", status: str = "", limit: int = 50) -> str:
    items = await _github_call(attestation_registry.list_artifacts, repository, status, limit)
    return json.dumps({"ok": True, "artifacts": items}, ensure_ascii=False)


@mcp.tool(name="validate_release_artifact", description="Validate artifact readiness, expiry, provenance, exact main identity and current private CI status.")
async def validate_release_artifact(artifact_id: str, repository: str, branch: str, commit_sha: str, tree_sha: str, private_ci_job_id: str) -> str:
    result = await _github_call(attestation_registry.validate_artifact, artifact_id, repository=repository, branch=branch, commit_sha=commit_sha, tree_sha=tree_sha, private_ci_job_id=private_ci_job_id)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(name="revoke_release_artifact", description="Revoke a registered artifact; revoked artifacts cannot be deployed.")
async def revoke_release_artifact(artifact_id: str) -> str:
    item = await _github_call(attestation_registry.revoke_artifact, artifact_id)
    return json.dumps({"ok": True, "artifact": item}, ensure_ascii=False)


@mcp.tool(name="get_repository_operation_policy", description="Return the operation policy for a repository: what MyGithut12 operations are allowed (GitHub read/write, private CI, test deploy, self deploy).")
async def get_repository_operation_policy(repository: str) -> str:
    """Return allowed operations for a repository based on authoritative policy sources."""
    from app.ci_repository_config import (
        is_private_ci_enabled,
        is_self_deploy_enabled,
        is_test_deploy_enabled,
    )

    github_allowed = repository_is_allowed(repository)
    private_ci_allowed = False
    test_deploy_allowed = False
    self_deploy_allowed = False

    # Private CI: same policy as start_private_ci_job
    private_ci_allowed = is_private_ci_enabled(repository)

    # Test deploy: same config-backed allowlist as plan_test_deployment / start_test_deployment.
    test_deploy_allowed = is_test_deploy_enabled(repository)

    # Self deploy: only repositories with an explicit fixed self-deploy contract.
    self_deploy_allowed = is_self_deploy_enabled(repository)

    return json.dumps({
        "ok": True,
        "repository": repository,
        "policy": {
            "github": github_allowed,
            "private_ci": private_ci_allowed,
            "test_deploy": test_deploy_allowed,
            "self_deploy": self_deploy_allowed,
        },
    }, ensure_ascii=False)


register_github_extended_tools(mcp, _github_call)
register_private_ci_mcp_tools(mcp)
register_mygithub12_tools(mcp, _github_call, _service)
mcp.tool(
    name="read_mcp_response_resource",
    description="Read one bounded UTF-8 chunk from an oversized MCP response resource with SHA and continuation metadata.",
)(read_mcp_response_resource)

# MyGithut12 high-level Web AI write surface. Register these after the frozen
# MyGithub10 compatibility manifest so old tool ordering/schema remains stable.
mcp.tool(
    name="put_generated_files",
    description=PUT_GENERATED_FILES_DESCRIPTION,
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=False,
    ),
)(put_generated_files)
mcp.tool(name="put_github_file", description=PUT_GITHUB_FILE_DESCRIPTION)(put_github_file)
mcp.tool(name="put_github_files", description=PUT_GITHUB_FILES_DESCRIPTION)(put_github_files)
mcp.tool(
    name="put_github_file_from_local_candidate",
    description=PUT_GITHUB_FILE_FROM_LOCAL_CANDIDATE_DESCRIPTION,
)(put_github_file_from_local_candidate)

# Stable DX-1 high-level orchestration surface. Keep registration after the
# pre-12.1 tool set so manifest ordering remains backward compatible.
register_dx_tools(mcp, _github_call, _service, _finalize_durable_write)
register_infrastructure_deployment_tools(mcp, _github_call)


# ========================================================================
# Helper: Log registered tools on startup
# ========================================================================

def _log_registered_tools():
    import asyncio
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            tools = loop.run_until_complete(mcp.list_tools())
        else:
            tools = asyncio.run(mcp.list_tools())
        tool_names = [t.name for t in tools]
        for name in tool_names:
            logger.info("Registered tool: %s", name)
        logger.info("Total registered tools: %d", len(tools))
    except Exception as e:
        logger.error("Failed to list registered tools: %s", e)
