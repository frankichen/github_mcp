"""Private CI MCP tools for MyGithub06.

These MCP tools expose the private German-controller + WSL-Podman CI system.
Distinct from GitHub Actions CI tools (start_ci_job, list_ci_workers, etc.).
"""

import json
import asyncio
import logging
import re
from typing import Optional

from mcp.server.fastmcp import FastMCP

from app.config import settings as app_settings
from app.ci_database import (
    create_or_get_job,
    get_job,
    list_jobs as db_list_jobs,
    get_workers as db_get_workers,
    get_log_chunks,
    get_log_tail,
    get_steps,
    wait_for_job_change,
    cancel_queued_job,
    request_cancel_job,
)
from app.ci_models import ALLOWED_PRIORITIES, effective_priority, make_idempotency_key
from app.ci_repository_config import (
    is_repository_allowed,
    is_profile_allowed,
    get_max_timeout,
    get_allowed_profiles,
)

logger = logging.getLogger(__name__)

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_TAIL_SECRET_RE = re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+|(?:token|password|secret|api[_-]?key)\s*[:=]\s*)([^\s,;]+)")


def _redact_log_line(line: str) -> str:
    return _TAIL_SECRET_RE.sub(lambda match: match.group(1) + "[REDACTED]", line)


def _error_response(code: str, message: str, retryable: bool = False, details: dict = None) -> str:
    return json.dumps({
        "ok": False,
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
            "details": details or {},
        },
    }, ensure_ascii=False)


def register_private_ci_mcp_tools(mcp: FastMCP):
    """Register private CI MCP tools on the FastMCP server."""

    @mcp.tool(
        name="list_private_ci_workers",
        description="""List workers registered with the private CI controller, including wsl-ci-01.

Returns each worker's ID, online status, supported profiles, max concurrency, and current job.

This is for the private German-controller + WSL-Podman CI system.
NOT for GitHub Actions self-hosted runners (use list_ci_workers for that).""",
    )
    async def list_private_ci_workers(online_only: bool = False) -> str:
        try:
            from app.ci_database import reconcile_stale_workers
            reconcile_stale_workers()
            workers = db_get_workers()
            if online_only:
                workers = [w for w in workers if w.get("online")]
            return json.dumps({"ok": True, "workers": workers}, ensure_ascii=False)
        except Exception as e:
            return _error_response("INTERNAL_ERROR", str(e))

    @mcp.tool(
        name="list_private_ci_profiles",
        description="""List available CI profiles for the private CI system.

Returns profiles like repo-auto-check, python-check, etc.
Use these profile names with start_private_ci_job.

This is for the private CI system. NOT for GitHub Actions workflows (use list_ci_profiles for that).""",
    )
    async def list_private_ci_profiles(repository: str = "") -> str:
        try:
            profiles = get_allowed_profiles(repository or None)
            result = {
                "ok": True,
                "profiles": [{"name": p, "description": f"CI check profile: {p}"} for p in profiles],
            }
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            return _error_response("INTERNAL_ERROR", str(e))

    @mcp.tool(
        name="list_private_ci_jobs",
        description="""List private CI jobs with optional filters.

Use this to find jobs for a specific repository, branch, commit SHA, or status.
Jobs are executed by wsl-ci-01 on the WSL machine with Rootless Podman.

This is for the private CI system. NOT for GitHub Actions runs (use list_ci_jobs for that).""",
    )
    async def list_private_ci_jobs(
        repository: str = "",
        branch: str = "",
        commit_sha: str = "",
        status: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> str:
        try:
            jobs = db_list_jobs(
                repository=repository if repository else None,
                branch=branch if branch else None,
                commit_sha=commit_sha if commit_sha else None,
                status=status if status else None,
                limit=limit + offset + 10,
            )
            total = len(jobs)
            page_jobs = jobs[offset:offset + limit]
            return json.dumps({
                "ok": True,
                "jobs": page_jobs,
                "total_count": total,
                "limit": limit,
                "offset": offset,
                "has_more": (offset + limit) < total,
            }, ensure_ascii=False)
        except Exception as e:
            return _error_response("INTERNAL_ERROR", str(e))

    @mcp.tool(
        name="start_private_ci_job",
        description="""Start a job in the private German-controller and WSL-Podman CI system for an exact Git commit SHA.

CRITICAL WORKFLOW:
1. After committing code via commit_github_files, save the commit_sha
2. Call start_private_ci_job with the FULL 40-character commit_sha
3. Save the returned job_id
4. Use get_private_ci_job to check the job status
5. Use get_private_ci_logs to read logs if the job failed

This is for the private WSL CI system. NOT for GitHub Actions dispatch (use start_ci_job for that).""",
    )
    async def start_private_ci_job(
        repository: str,
        branch: str,
        commit_sha: str,
        profile: str = "repo-auto-check",
        timeout_seconds: int = 900,
        priority: str = "normal",
        force_rerun: bool = False,
        supersede_previous: bool = False,
        base_sha: str = "",
    ) -> str:
        try:
            if not repository or "/" not in repository:
                return _error_response("INVALID_ARGUMENT", "repository must be in owner/repo format")
            if not SHA_RE.match(commit_sha):
                return _error_response("INVALID_ARGUMENT", "commit_sha must be exactly 40 hex characters")
            if not is_repository_allowed(repository):
                return _error_response("REPOSITORY_NOT_ALLOWED", f"Repository '{repository}' is not in the CI allowed list")
            if not is_profile_allowed(repository, profile):
                return _error_response("PRIVATE_CI_PROFILE_NOT_ALLOWED", f"Profile '{profile}' not allowed for '{repository}'")
            if priority not in ALLOWED_PRIORITIES:
                return _error_response("INVALID_ARGUMENT", "priority must be 'normal' or 'high'")

            max_timeout = get_max_timeout(repository)
            timeout_seconds = min(max(timeout_seconds, 60), max_timeout)

            changed = {"changed_files": [], "total_count": 0, "truncated": False}
            if profile in ("repo-auto-check", "repo-fast-check"):
                from app.github_utils import get_github_changed_files_result
                changed = get_github_changed_files_result(repository, base_sha, commit_sha)
                if not changed.get("ok"):
                    return _error_response(changed["error_code"], changed.get("message", "changed files compare failed"), details=changed.get("details", {}))

            effective_queue_priority = effective_priority(branch, profile, ALLOWED_PRIORITIES[priority])
            result = create_or_get_job(
                repository=repository, branch=branch, commit_sha=commit_sha,
                profile=profile, priority=effective_queue_priority,
                timeout_seconds=timeout_seconds, force_rerun=force_rerun,
                supersede_previous=supersede_previous,
                base_sha=base_sha, changed_files=changed["changed_files"],
                changed_files_total=changed["total_count"],
                changed_files_truncated=changed["truncated"],
            )
            result["ok"] = True
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            return _error_response("INTERNAL_ERROR", str(e))

    @mcp.tool(
        name="get_private_ci_job",
        description="""Get the current status and details of a specific private CI job by job_id.

Returns complete job information including status, queue_position, current_step, exit_code, steps array, and timestamps.

This is for the private CI system. NOT for GitHub Actions runs (use get_ci_job for that).""",
    )
    async def get_private_ci_job(job_id: str) -> str:
        try:
            job = get_job(job_id)
            if not job:
                return _error_response("PRIVATE_CI_JOB_NOT_FOUND", f"Job '{job_id}' not found")
            steps = get_steps(job_id)
            current_step = None
            for s in steps:
                if s.get("status") == "running":
                    current_step = s.get("step_name")
                    break
            job["current_step"] = current_step
            job["steps"] = steps
            job["ok"] = True
            return json.dumps(job, ensure_ascii=False)
        except Exception as e:
            return _error_response("INTERNAL_ERROR", str(e))

    @mcp.tool(
        name="wait_private_ci_job",
        description="Long-poll one private CI job for up to 55 seconds. Returns on status, step, log revision, terminal, or timeout; use instead of repeated polling.",
    )
    async def wait_private_ci_job(
        job_id: str, timeout_seconds: int = 55, last_known_status: str = "",
        last_known_step: str = "", last_known_revision: int = 0,
    ) -> str:
        try:
            result = await asyncio.to_thread(
                wait_for_job_change, job_id, timeout_seconds, last_known_status,
                last_known_step, last_known_revision,
            )
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            return _error_response("INTERNAL_ERROR", str(e))

    @mcp.tool(
        name="get_private_ci_logs",
        description="""Get private CI job execution logs by job_id. Supports pagination for large logs.

Use the exact job_id from start_private_ci_job. Do NOT assume the latest job's logs.

This is for the private CI system. NOT for GitHub Actions logs (use get_ci_logs for that).""",
    )
    async def get_private_ci_logs(
        job_id: str, offset: int = 0, limit: int = 200,
    ) -> str:
        try:
            job = get_job(job_id)
            if not job:
                return _error_response("PRIVATE_CI_JOB_NOT_FOUND", f"Job '{job_id}' not found")
            result = get_log_chunks(job_id, offset, limit)
            result["repository"] = job.get("repository")
            result["branch"] = job.get("branch")
            result["commit_sha"] = job.get("commit_sha")
            result["status"] = job.get("status")
            result["ok"] = True
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            return _error_response("INTERNAL_ERROR", str(e))

    @mcp.tool(
        name="get_private_ci_log_tail",
        description="Return the redacted tail of a private CI job log without requiring the caller to page through the full log.",
    )
    async def get_private_ci_log_tail(job_id: str, lines: int = 100) -> str:
        try:
            job = get_job(job_id)
            if not job:
                return _error_response("PRIVATE_CI_JOB_NOT_FOUND", f"Job '{job_id}' not found")
            lines = min(max(lines, 1), 1000)
            result = get_log_tail(job_id, lines)
            return json.dumps({
                "ok": True, "job_id": job_id, "repository": job.get("repository"),
                "branch": job.get("branch"), "commit_sha": job.get("commit_sha"),
                "status": job.get("status"), "last_sequence": result.get("last_sequence"), "lines": [_redact_log_line(line) for line in result.get("lines", [])],
                "line_count": result.get("returned_lines", 0), "total_bytes": result.get("total_bytes", 0),
                "requested_lines": result.get("requested_lines", lines), "bytes_scanned": result.get("bytes_scanned", 0), "first_line_partial": result.get("first_line_partial", False), "max_bytes_reached": result.get("max_bytes_reached", False), "truncated": result.get("truncated", False),
            }, ensure_ascii=False)
        except Exception as e:
            return _error_response("INTERNAL_ERROR", str(e))

    @mcp.tool(
        name="cancel_private_ci_job",
        description="""Cancel a private CI job by job_id.

- For queued jobs: immediately cancels
- For running jobs: sends cancel signal to the worker
- For completed jobs: returns current status (cannot cancel)

This is for the private CI system. NOT for GitHub Actions (use cancel_ci_job for that).""",
    )
    async def cancel_private_ci_job(job_id: str) -> str:
        try:
            job = get_job(job_id)
            if not job:
                return _error_response("PRIVATE_CI_JOB_NOT_FOUND", f"Job '{job_id}' not found")
            status = job.get("status", "")
            if status == "queued":
                ok = cancel_queued_job(job_id)
                return json.dumps({"ok": True, "status": "cancelled", "job_id": job_id} if ok else _error_response("INTERNAL_ERROR", "cancel failed"))
            if status in ("leased", "downloading", "preparing", "running"):
                request_cancel_job(job_id)
                return json.dumps({"ok": True, "status": "cancel_requested", "job_id": job_id, "message": "Cancel signal sent to worker"})
            if status in ("passed", "failed", "cancelled", "timed_out", "superseded", "worker_lost"):
                return _error_response("PRIVATE_CI_JOB_ALREADY_FINISHED", f"Cannot cancel job in status '{status}'")
            return _error_response("INVALID_ARGUMENT", f"Cannot cancel job in status '{status}'")
        except Exception as e:
            return _error_response("INTERNAL_ERROR", str(e))
