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
    is_private_ci_enabled,
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


_PRIVATE_CI_SUMMARY_FIELDS = (
    "job_id", "repository", "branch", "commit_sha", "base_sha", "profile",
    "status", "exit_code", "priority", "worker_id", "queue_position",
    "eligible_workers", "unschedulable_reason",
    "created_at", "started_at", "finished_at", "duration_seconds",
    "cancel_requested", "superseded_by_job_id",
)
_PRIVATE_CI_LIST_FIELDS = _PRIVATE_CI_SUMMARY_FIELDS + ("attempts",)
_PRIVATE_CI_STEP_SUMMARY_FIELDS = ("step_name", "status", "exit_code", "duration_seconds")
_PRIVATE_CI_WORKSPACE_FIELDS = ("path", "stack", "framework", "package_manager")
_MAX_SUMMARY_STEPS = 100
_MAX_SUMMARY_WORKSPACES = 100


def _bounded_status_value(value, max_items: int = 20, max_chars: int = 2048):
    if isinstance(value, str):
        return value if len(value) <= max_chars else value[:max_chars] + "…"
    if isinstance(value, list):
        return [_bounded_status_value(item, max_items, max_chars) for item in value[:max_items]]
    if isinstance(value, dict):
        return {
            str(key): _bounded_status_value(item, max_items, max_chars)
            for key, item in list(value.items())[:max_items]
        }
    return value


def _private_ci_logical_steps(job_summary: dict, persisted_steps: list[dict]) -> list[dict]:
    worker_steps = job_summary.get("steps")
    if isinstance(worker_steps, list) and worker_steps:
        return [step for step in worker_steps if isinstance(step, dict)]
    return [step for step in persisted_steps if isinstance(step, dict)]


def _merge_private_ci_full_steps(job_summary: dict, persisted_steps: list[dict]) -> list[dict]:
    logical_steps = _private_ci_logical_steps(job_summary, persisted_steps)
    persisted_by_name: dict[str, list[dict]] = {}
    for persisted in persisted_steps:
        if not isinstance(persisted, dict):
            continue
        persisted_by_name.setdefault(str(persisted.get("step_name", "")), []).append(persisted)

    merged: list[dict] = []
    consumed: set[int] = set()
    for logical in logical_steps:
        item = dict(logical)
        candidates = persisted_by_name.get(str(item.get("step_name", "")), [])
        persisted = candidates.pop(0) if candidates else None
        if persisted is not None:
            consumed.add(id(persisted))
            for key in ("started_at", "finished_at", "log_start_offset", "log_end_offset"):
                if key in persisted:
                    item[key] = persisted.get(key)
            for key in ("status", "exit_code", "duration_seconds"):
                if item.get(key) is None and key in persisted:
                    item[key] = persisted.get(key)
        merged.append(item)

    for persisted in persisted_steps:
        if isinstance(persisted, dict) and id(persisted) not in consumed:
            merged.append(dict(persisted))
    return merged


def build_private_ci_job_list_item(job: dict) -> dict:
    """Return bounded discovery metadata for one private CI job."""
    job_summary = job.get("summary") if isinstance(job.get("summary"), dict) else {}
    summary_steps = job_summary.get("steps") if isinstance(job_summary.get("steps"), list) else None
    logical_steps = [step for step in (summary_steps or []) if isinstance(step, dict)]

    result = {key: job.get(key) for key in _PRIVATE_CI_LIST_FIELDS}
    current_step = job.get("current_step") or next(
        (step.get("step_name") for step in logical_steps if step.get("status") == "running"),
        None,
    )
    for optional_key in (
        "worker_id", "queue_position", "eligible_workers",
        "unschedulable_reason", "superseded_by_job_id",
    ):
        if result.get(optional_key) is None:
            result.pop(optional_key, None)
    if current_step is not None:
        result["current_step"] = current_step
    result["changed_files_total"] = int(job.get("changed_files_total") or 0)
    result["changed_files_truncated"] = bool(job.get("changed_files_truncated"))
    result["detected_stacks"] = list(job.get("detected_stacks") or job_summary.get("detected_stacks") or [])
    result["selected_profiles"] = list(job.get("selected_profiles") or job_summary.get("selected_profiles") or [])
    git_tree_sha = job_summary.get("git_tree_sha")
    if git_tree_sha:
        result["git_tree_sha"] = git_tree_sha

    if summary_steps is None:
        result["steps_total"] = None
        result["failed_steps_count"] = None
        result["skipped_steps_count"] = None
    else:
        result["steps_total"] = len(logical_steps)
        result["failed_steps_count"] = sum(step.get("status") == "failed" for step in logical_steps)
        result["skipped_steps_count"] = sum(step.get("status") == "skipped" for step in logical_steps)
    return result


def build_private_ci_job_response(job: dict, persisted_steps: list[dict], detail_level: str = "summary") -> dict:
    if detail_level not in {"summary", "full"}:
        raise ValueError("detail_level must be 'summary' or 'full'")

    job_summary = job.get("summary") if isinstance(job.get("summary"), dict) else {}
    logical_steps = _private_ci_logical_steps(job_summary, persisted_steps)
    current_step = next(
        (step.get("step_name") for step in logical_steps if step.get("status") == "running"),
        None,
    )

    if detail_level == "full":
        result = dict(job)
        summary_without_steps = dict(job_summary)
        for duplicate_key in (
            "steps", "status", "exit_code", "detected_stacks",
            "selected_profiles", "workspaces", "git_tree_sha",
        ):
            summary_without_steps.pop(duplicate_key, None)
        result["summary"] = summary_without_steps
        result["current_step"] = current_step
        result["git_tree_sha"] = job_summary.get("git_tree_sha")
        result["steps"] = _merge_private_ci_full_steps(job_summary, persisted_steps)
        result["steps_total"] = len(result["steps"])
        result["steps_truncated"] = False
        result["ok"] = True
        result["_mcp_response_mode"] = "full"
        return result

    result = {key: job.get(key) for key in _PRIVATE_CI_SUMMARY_FIELDS}
    for optional_key in (
        "worker_id", "queue_position", "eligible_workers",
        "unschedulable_reason", "superseded_by_job_id",
    ):
        if result.get(optional_key) is None:
            result.pop(optional_key, None)
    result["current_step"] = current_step
    result["git_tree_sha"] = job_summary.get("git_tree_sha")
    result["detected_stacks"] = list(job.get("detected_stacks") or job_summary.get("detected_stacks") or [])
    result["selected_profiles"] = list(job.get("selected_profiles") or job_summary.get("selected_profiles") or [])

    raw_workspaces = job.get("workspaces") or job_summary.get("workspaces") or []
    normalized_workspaces = [
        {key: workspace.get(key) for key in _PRIVATE_CI_WORKSPACE_FIELDS if key in workspace}
        for workspace in raw_workspaces
        if isinstance(workspace, dict)
    ]
    result["workspaces"] = normalized_workspaces[:_MAX_SUMMARY_WORKSPACES]
    result["workspaces_total"] = len(normalized_workspaces)
    result["workspaces_truncated"] = len(normalized_workspaces) > _MAX_SUMMARY_WORKSPACES
    result["workspaces_next_cursor"] = str(_MAX_SUMMARY_WORKSPACES) if result["workspaces_truncated"] else None

    compact_steps = [
        {key: step.get(key) for key in _PRIVATE_CI_STEP_SUMMARY_FIELDS}
        for step in logical_steps
    ]
    result["steps"] = compact_steps[:_MAX_SUMMARY_STEPS]
    result["steps_total"] = len(compact_steps)
    result["steps_truncated"] = len(compact_steps) > _MAX_SUMMARY_STEPS
    result["steps_next_cursor"] = str(_MAX_SUMMARY_STEPS) if result["steps_truncated"] else None

    status_summary = {}
    for key in ("error", "errors", "warnings", "failure_reason", "error_code", "error_message", "message"):
        value = job_summary.get(key, job.get(key))
        if value not in (None, "", [], {}):
            status_summary[key] = _bounded_status_value(value)
    if job.get("log_truncated") or job_summary.get("log_truncated"):
        status_summary["log_truncated"] = True
    if status_summary:
        result["status_summary"] = status_summary

    result["ok"] = True
    result["_mcp_response_mode"] = "summary"
    return result


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
            await asyncio.to_thread(reconcile_stale_workers)
            workers = await asyncio.to_thread(db_get_workers)
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
            jobs = await asyncio.to_thread(
                db_list_jobs,
                repository=repository if repository else None,
                branch=branch if branch else None,
                commit_sha=commit_sha if commit_sha else None,
                status=status if status else None,
                limit=limit + offset + 10,
            )
            total = len(jobs)
            page_jobs = [build_private_ci_job_list_item(job) for job in jobs[offset:offset + limit]]
            return json.dumps({
                "ok": True,
                "jobs": page_jobs,
                "total_count": total,
                "limit": limit,
                "offset": offset,
                "has_more": (offset + limit) < total,
                "_mcp_response_mode": "summary",
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
            if not is_private_ci_enabled(repository):
                return _error_response("REPOSITORY_OPERATION_DENIED", f"Private CI is disabled for '{repository}'")
            if not is_profile_allowed(repository, profile):
                return _error_response("PRIVATE_CI_PROFILE_NOT_ALLOWED", f"Profile '{profile}' not allowed for '{repository}'")
            if priority not in ALLOWED_PRIORITIES:
                return _error_response("INVALID_ARGUMENT", "priority must be 'normal' or 'high'")

            max_timeout = get_max_timeout(repository)
            timeout_seconds = min(max(timeout_seconds, 60), max_timeout)

            changed = {"changed_files": [], "total_count": 0, "truncated": False}
            if profile in ("repo-auto-check", "repo-fast-check"):
                from app.github_utils import get_github_changed_files_result
                changed = await asyncio.to_thread(
                    get_github_changed_files_result, repository, base_sha, commit_sha
                )
                if not changed.get("ok"):
                    return _error_response(changed["error_code"], changed.get("message", "changed files compare failed"), details=changed.get("details", {}))

            effective_queue_priority = effective_priority(branch, profile, ALLOWED_PRIORITIES[priority])
            result = await asyncio.to_thread(
                create_or_get_job,
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
        description="""Get one private CI job. Defaults to a compact gate-safe summary.

summary keeps exact repository/branch/commit/tree identity, gate status, worker/queue state,
normalized workspaces, and bounded step status without commands, offsets, evidence, or changed files.
Use detail_level='full' only for debugging; oversized full results are returned through a response resource.

This is for the private CI system. NOT for GitHub Actions runs (use get_ci_job for that).""",
    )
    async def get_private_ci_job(job_id: str, detail_level: str = "summary") -> str:
        try:
            job = await asyncio.to_thread(get_job, job_id)
            if not job:
                return _error_response("PRIVATE_CI_JOB_NOT_FOUND", f"Job '{job_id}' not found")
            if detail_level not in {"summary", "full"}:
                return _error_response("INVALID_ARGUMENT", "detail_level must be 'summary' or 'full'")
            persisted_steps = await asyncio.to_thread(get_steps, job_id)
            result = build_private_ci_job_response(job, persisted_steps, detail_level)
            return json.dumps(result, ensure_ascii=False)
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
            job = await asyncio.to_thread(get_job, job_id)
            if not job:
                return _error_response("PRIVATE_CI_JOB_NOT_FOUND", f"Job '{job_id}' not found")
            result = await asyncio.to_thread(get_log_chunks, job_id, offset, limit)
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
            job = await asyncio.to_thread(get_job, job_id)
            if not job:
                return _error_response("PRIVATE_CI_JOB_NOT_FOUND", f"Job '{job_id}' not found")
            lines = min(max(lines, 1), 1000)
            result = await asyncio.to_thread(get_log_tail, job_id, lines)
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
            job = await asyncio.to_thread(get_job, job_id)
            if not job:
                return _error_response("PRIVATE_CI_JOB_NOT_FOUND", f"Job '{job_id}' not found")
            status = job.get("status", "")
            if status == "queued":
                ok = await asyncio.to_thread(cancel_queued_job, job_id)
                return json.dumps({"ok": True, "status": "cancelled", "job_id": job_id} if ok else _error_response("INTERNAL_ERROR", "cancel failed"))
            if status in ("leased", "downloading", "preparing", "running"):
                await asyncio.to_thread(request_cancel_job, job_id)
                return json.dumps({"ok": True, "status": "cancel_requested", "job_id": job_id, "message": "Cancel signal sent to worker"})
            if status in ("passed", "failed", "cancelled", "timed_out", "superseded", "worker_lost"):
                return _error_response("PRIVATE_CI_JOB_ALREADY_FINISHED", f"Cannot cancel job in status '{status}'")
            return _error_response("INVALID_ARGUMENT", f"Cannot cancel job in status '{status}'")
        except Exception as e:
            return _error_response("INTERNAL_ERROR", str(e))
