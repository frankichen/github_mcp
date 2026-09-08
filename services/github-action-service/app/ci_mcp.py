"""Private CI MCP tools for MyGithub06.

These MCP tools expose the private German-controller + WSL-Podman CI system.
Distinct from GitHub Actions CI tools (start_ci_job, list_ci_workers, etc.).
"""

import json
import asyncio
import logging
import re
import uuid
from typing import Optional

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from app.config import settings as app_settings
from app.ci_database import (
    decode_step_log_cursor,
    encode_step_log_cursor,
    get_job,
    get_worker,
    list_jobs as db_list_jobs,
    get_workers as db_get_workers,
    get_log_chunks,
    get_log_tail,
    get_steps,
    wait_for_job_change,
    cancel_queued_job,
    request_cancel_job,
)
from app.ci_models import ALLOWED_PRIORITIES, effective_priority
from app.ci_request_dispatch import effective_ci_config_digest, schedule_ci_request_preparation
from app.ci_request_store import (
    CIRequestIdempotencyConflictError,
    compute_normalized_request_hash,
    create_or_get_ci_request,
    get_ci_request,
    get_ci_request_by_idempotency_key,
    get_ci_request_by_worker_job_id,
    get_ci_request_payload,
)
from app.ci_repository_config import (
    is_repository_allowed,
    is_profile_allowed,
    is_private_ci_enabled,
    get_max_timeout,
    get_allowed_profiles,
)
from app.development_failure_pack import redact_text

logger = logging.getLogger(__name__)

# Keep the private-CI annotations grouped by the side effects of the actual
# handlers. In particular, a long-poll is still read-only, while worker
# reconciliation is a durable write even though its public operation is named
# "list".
_PRIVATE_CI_DIAGNOSTIC_READ = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
_PRIVATE_CI_WORKER_RECONCILIATION = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)
_PRIVATE_CI_EXECUTION_WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)
_PRIVATE_CI_CANCELLATION = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=True,
    openWorldHint=True,
)

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_PRIVATE_CI_SECRET_FIELD_RE = re.compile(
    r"(?i)(?:^|[_-])(?:token|secret|password|passwd|api[_-]?key|access[_-]?key|"
    r"credential|authorization|private[_-]?key|headers?|dsn|database[_-]?url|"
    r"connection[_-]?(?:string|url))(?:$|[_-])"
)
_START_REPLAY_CALLER_FIELDS = (
    "repository",
    "branch",
    "commit_sha",
    "profile",
    "requested_timeout_seconds",
    "requested_priority",
    "base_sha",
    "force_rerun",
    "supersede_previous",
)


def _accepted_start_replay_matches(accepted_payload: dict, caller_identity: dict) -> bool:
    return all(
        field in accepted_payload and accepted_payload[field] == caller_identity[field]
        for field in _START_REPLAY_CALLER_FIELDS
    )


def _redact_log_line(line: str) -> str:
    return redact_text(line)


def _redact_private_ci_value(value):
    """Recursively redact JSON-like Private CI payloads without dropping diagnostics."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, (bytes, bytearray)):
        return "[BINARY_REDACTED]"
    if isinstance(value, dict):
        output = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            output[key] = (
                "[REDACTED]" if _PRIVATE_CI_SECRET_FIELD_RE.search(key)
                else _redact_private_ci_value(raw_value)
            )
        return output
    if isinstance(value, (list, tuple, set)):
        return [_redact_private_ci_value(item) for item in value]
    return value


def _error_response(code: str, message: str, retryable: bool = False, details: dict = None) -> str:
    return json.dumps(_redact_private_ci_value({
        "ok": False,
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
            "details": details or {},
        },
    }), ensure_ascii=False)


def _step_selector_details(step: dict) -> dict:
    return {
        "step_id": step.get("step_id"),
        "step_name": redact_text(step.get("step_name") or ""),
        "status": step.get("status"),
        "exit_code": step.get("exit_code"),
        "log_start_offset": step.get("log_start_offset"),
        "log_end_offset": step.get("log_end_offset"),
    }


def _redact_log_chunks(result: dict) -> dict:
    """Redact log content while preserving durable raw-offset metadata."""
    output = dict(result)
    output["chunks"] = [
        {
            **chunk,
            "content": redact_text(chunk.get("content") or ""),
        }
        for chunk in result.get("chunks", [])
        if isinstance(chunk, dict)
    ]
    output["redacted"] = True
    return output


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
_MAX_PRIVATE_CI_LOG_PAGE_CHUNKS = 200
_MAX_PRIVATE_CI_STEP_PAGE_BYTES = 24 * 1024
_PRIVATE_CI_COMPLETED_STEP_STATUSES = {
    "passed", "failed", "timed_out", "cancelled", "completed", "skipped", "autofixed",
}
_PRIVATE_CI_FAILED_STEP_STATUSES = {"failed", "timed_out", "cancelled"}
_PRIVATE_CI_WORKER_TERMINAL_STATUSES = {
    "passed", "failed", "timed_out", "cancelled", "superseded", "worker_lost", "internal_error",
}
_PRIVATE_CI_WORKER_RUNNING_STATUSES = {"running", "cancel_requested"}
_PRIVATE_CI_WORKER_QUEUED_STATUSES = {"queued", "leased", "downloading", "preparing"}


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
        return _redact_private_ci_value(result)

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
    result["completed_steps_count"] = sum(
        step.get("status") in _PRIVATE_CI_COMPLETED_STEP_STATUSES for step in logical_steps
    )
    result["failed_steps_count"] = sum(
        step.get("status") in _PRIVATE_CI_FAILED_STEP_STATUSES for step in logical_steps
    )
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
    return _redact_private_ci_value(result)


def _private_ci_preflight_error(request: Optional[dict]) -> Optional[dict]:
    if not request or request.get("status") != "preflight_failed":
        return None
    return {
        "error_id": request.get("preflight_error_id"),
        "code": request.get("preflight_error_code"),
        "reason": request.get("terminal_reason"),
    }


def _private_ci_request_worker_mismatch(request: dict, job: dict) -> Optional[str]:
    if request.get("worker_job_id") != job.get("job_id"):
        return "worker_job_id"
    for field in ("repository", "branch", "commit_sha", "profile"):
        if request.get(field) != job.get(field):
            return field
    worker_summary = job.get("summary") if isinstance(job.get("summary"), dict) else {}
    worker_tree_sha = worker_summary.get("git_tree_sha")
    if request.get("tree_sha") and worker_tree_sha and request["tree_sha"] != worker_tree_sha:
        return "tree_sha"
    return None


def _private_ci_effective_state(
    request: Optional[dict], job: Optional[dict],
) -> tuple[Optional[str], Optional[str], bool]:
    """Compose truthful top-level state without mutating either durable row."""
    if not job:
        if not request:
            return None, None, False
        return request.get("phase"), request.get("status"), bool(request.get("terminal"))

    worker_status = str(job.get("status") or "")
    if worker_status in _PRIVATE_CI_WORKER_TERMINAL_STATUSES:
        return "terminal", worker_status, True
    if worker_status in _PRIVATE_CI_WORKER_RUNNING_STATUSES:
        return "running", worker_status, False
    if worker_status in _PRIVATE_CI_WORKER_QUEUED_STATUSES:
        return "queued", worker_status, False
    return (request.get("phase") if request else None), worker_status or None, False


def _private_ci_snapshot_next_actions(
    request_id: Optional[str], worker_job_id: Optional[str], terminal: bool,
) -> list[dict]:
    if terminal:
        return []
    action = {"tool": "get_private_ci_job"}
    if request_id:
        action["request_id"] = request_id
    if worker_job_id:
        action["job_id"] = worker_job_id
    return [action]


def build_private_ci_snapshot_response(
    request: Optional[dict],
    job: Optional[dict],
    persisted_steps: list[dict],
    detail_level: str = "summary",
    worker: Optional[dict] = None,
) -> dict:
    """Build one pure read snapshot from Request control-plane + Worker execution facts."""
    if detail_level not in {"summary", "full"}:
        raise ValueError("detail_level must be 'summary' or 'full'")

    if job:
        result = build_private_ci_job_response(job, persisted_steps, detail_level)
    else:
        result = {
            "ok": True,
            "job_id": None,
            "repository": request.get("repository") if request else None,
            "branch": request.get("branch") if request else None,
            "commit_sha": request.get("commit_sha") if request else None,
            "base_sha": None,
            "profile": request.get("profile") if request else None,
            "exit_code": None,
            "priority": None,
            "worker_id": None,
            "queue_position": None,
            "eligible_workers": None,
            "unschedulable_reason": None,
            "current_step": None,
            "git_tree_sha": None,
            "detected_stacks": [],
            "selected_profiles": [],
            "workspaces": [],
            "workspaces_total": 0,
            "workspaces_truncated": False,
            "workspaces_next_cursor": None,
            "steps": [],
            "steps_total": 0,
            "completed_steps_count": 0,
            "failed_steps_count": 0,
            "steps_truncated": False,
            "steps_next_cursor": None,
            "_mcp_response_mode": detail_level,
        }

    request_id = request.get("request_id") if request else None
    worker_job_id = job.get("job_id") if job else (request.get("worker_job_id") if request else None)
    phase, status, terminal = _private_ci_effective_state(request, job)
    request_phase = request.get("phase") if request else None
    request_status = request.get("status") if request else None
    request_revision = request.get("revision") if request else None
    tree_sha = request.get("tree_sha") if request else None
    worker_status = job.get("status") if job else None
    worker_id = job.get("worker_id") if job else None
    eligible_workers = job.get("eligible_workers") if job else None
    worker_online = worker.get("online") if worker else None
    if job and worker_status == "queued" and eligible_workers is not None:
        worker_available = bool(eligible_workers)
    elif worker is not None:
        worker_available = bool(worker_online)
    else:
        worker_available = None

    result.update({
        "request_id": request_id,
        "job_id": worker_job_id,
        "worker_job_id": worker_job_id,
        "tree_sha": tree_sha,
        "tree_pending": bool(
            request and tree_sha is None and request_phase in {"accepted", "preparing"}
        ),
        "request_phase": request_phase,
        "request_status": request_status,
        "request_revision": request_revision,
        "request_updated_at": request.get("updated_at") if request else None,
        "request_stage": request_phase,
        "worker_status": worker_status,
        "phase": phase,
        "status": status,
        "revision": request_revision,
        "terminal": terminal,
        "continuation_required": not terminal,
        "current_step": result.get("current_step") if job else None,
        "priority": job.get("priority") if job else None,
        "queue_position": job.get("queue_position") if job else None,
        "eligible_workers": eligible_workers,
        "unschedulable_reason": job.get("unschedulable_reason") if job else None,
        "queue_state": "not_created" if not job else (
            "queued" if worker_status == "queued" else "not_queued"
        ),
        "worker_id": worker_id,
        "worker_assigned": bool(worker_id),
        "worker_online": worker_online,
        "worker_available": worker_available,
        "worker_agent_status": worker.get("status") if worker else None,
        "worker_current_job": worker.get("current_job") if worker else None,
        "failure_pack_id": request.get("failure_pack_id") if request else None,
        "failure_pack_available": bool(request and request.get("failure_pack_id")),
        "attestation_id": request.get("attestation_id") if request else None,
        "attestation_available": bool(request and request.get("attestation_id")),
        "preflight_error": _private_ci_preflight_error(request),
        "next_actions": _private_ci_snapshot_next_actions(request_id, worker_job_id, terminal),
    })
    if detail_level == "full" and request:
        result["request"] = dict(request)
    return _redact_private_ci_value(result)


def build_private_ci_start_response(request: dict) -> dict:
    """Build a truthful Request continuation snapshot without inventing a Job."""
    worker_job_id = request.get("worker_job_id")
    terminal = bool(request.get("terminal"))
    if terminal:
        next_actions = []
        continuation_hint = "request is terminal"
    elif worker_job_id:
        next_actions = [{
            "tool": "get_private_ci_job",
            "request_id": request.get("request_id"),
            "job_id": worker_job_id,
        }]
        continuation_hint = "Worker Job exists; continue with request-aware get_private_ci_job"
    else:
        next_actions = [{
            "tool": "get_private_ci_job",
            "request_id": request.get("request_id"),
        }]
        continuation_hint = "Request is durable; continue with get_private_ci_job(request_id=...)"
    preflight_error = None
    if request.get("status") == "preflight_failed":
        preflight_error = {
            "error_id": request.get("preflight_error_id"),
            "code": request.get("preflight_error_code"),
            "reason": request.get("terminal_reason"),
        }
    return {
        "ok": True,
        "request_id": request["request_id"],
        "job_id": worker_job_id,
        "worker_job_id": worker_job_id,
        "idempotency_key": request.get("idempotency_key"),
        "normalized_request_hash": request.get("normalized_request_hash"),
        "repository": request["repository"],
        "branch": request["branch"],
        "commit_sha": request["commit_sha"],
        "tree_sha": request.get("tree_sha"),
        "tree_pending": request.get("tree_sha") is None,
        "profile": request["profile"],
        "effective_config_digest": request.get("effective_config_digest"),
        "phase": request["phase"],
        "status": request["status"],
        "revision": request["revision"],
        "terminal": terminal,
        "continuation_required": not terminal,
        "continuation_hint": continuation_hint,
        "next_actions": next_actions,
        "deduplicated": bool(request.get("deduplicated")),
        "reused": bool(request.get("deduplicated")),
        "preflight_error": preflight_error,
        "created_at": request.get("created_at"),
    }


def register_private_ci_mcp_tools(mcp: FastMCP):
    """Register private CI MCP tools on the FastMCP server."""

    @mcp.tool(
        name="list_private_ci_workers",
        description="""List workers registered with the private CI controller, including wsl-ci-01.

Returns each worker's ID, online status, supported profiles, max concurrency, and current job.

This is for the private German-controller + WSL-Podman CI system.
NOT for GitHub Actions self-hosted runners (use list_ci_workers for that).""",
        annotations=_PRIVATE_CI_WORKER_RECONCILIATION,
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
        annotations=_PRIVATE_CI_DIAGNOSTIC_READ,
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
        annotations=_PRIVATE_CI_DIAGNOSTIC_READ,
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
        description="""Durably accept a private CI Request for an exact Git commit without waiting for Worker execution.

CRITICAL WORKFLOW:
1. Supply the FULL 40-character commit_sha and a stable idempotency_key
2. Save request_id; worker_job_id/job_id may truthfully be null while preparing
3. Replaying the same request/key is safe; a different request with the same key conflicts
4. Continue non-terminal tracking with get_private_ci_job snapshots; use diagnostics only when needed

This is for the private WSL CI system. NOT for GitHub Actions dispatch (use start_ci_job for that).""",
        annotations=_PRIVATE_CI_EXECUTION_WRITE,
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
        idempotency_key: str = "",
    ) -> str:
        try:
            if not repository or "/" not in repository:
                return _error_response("INVALID_ARGUMENT", "repository must be in owner/repo format")
            if not branch:
                return _error_response("INVALID_ARGUMENT", "branch is required")
            if not SHA_RE.match(commit_sha):
                return _error_response("INVALID_ARGUMENT", "commit_sha must be exactly 40 hex characters")
            if base_sha and not SHA_RE.match(base_sha):
                return _error_response("INVALID_ARGUMENT", "base_sha must be empty or exactly 40 hex characters")
            if idempotency_key and (idempotency_key != idempotency_key.strip() or len(idempotency_key) > 200):
                return _error_response("INVALID_ARGUMENT", "idempotency_key must be at most 200 characters with no surrounding whitespace")
            if priority not in ALLOWED_PRIORITIES:
                return _error_response("INVALID_ARGUMENT", "priority must be 'normal' or 'high'")

            requested_timeout_seconds = timeout_seconds
            caller_identity = {
                "repository": repository,
                "branch": branch,
                "commit_sha": commit_sha,
                "profile": profile,
                "requested_timeout_seconds": requested_timeout_seconds,
                "requested_priority": priority,
                "base_sha": base_sha,
                "force_rerun": bool(force_rerun),
                "supersede_previous": bool(supersede_previous),
            }
            if idempotency_key:
                existing = await asyncio.to_thread(
                    get_ci_request_by_idempotency_key, idempotency_key
                )
                if existing:
                    accepted_payload = await asyncio.to_thread(
                        get_ci_request_payload, existing["request_id"]
                    )
                    if not _accepted_start_replay_matches(
                        accepted_payload, caller_identity
                    ):
                        raise CIRequestIdempotencyConflictError(idempotency_key)
                    request = await asyncio.to_thread(
                        get_ci_request, existing["request_id"]
                    ) or existing
                    request["deduplicated"] = True
                    snapshot = build_private_ci_start_response(request)
                    if request["phase"] in {"accepted", "preparing"}:
                        schedule_ci_request_preparation(request["request_id"])
                    return json.dumps(snapshot, ensure_ascii=False)

            if not is_repository_allowed(repository):
                return _error_response("REPOSITORY_NOT_ALLOWED", f"Repository '{repository}' is not in the CI allowed list")
            if not is_private_ci_enabled(repository):
                return _error_response("REPOSITORY_OPERATION_DENIED", f"Private CI is disabled for '{repository}'")
            if not is_profile_allowed(repository, profile):
                return _error_response("PRIVATE_CI_PROFILE_NOT_ALLOWED", f"Profile '{profile}' not allowed for '{repository}'")

            max_timeout = get_max_timeout(repository)
            timeout_seconds = min(max(timeout_seconds, 60), max_timeout)
            effective_queue_priority = effective_priority(branch, profile, ALLOWED_PRIORITIES[priority])
            config_digest = effective_ci_config_digest(repository)
            normalized_payload = {
                "schema": "private-ci-start-v2",
                "repository": repository,
                "branch": branch,
                "commit_sha": commit_sha,
                "tree_sha": "derived_from_exact_commit_during_preflight",
                "profile": profile,
                "requested_timeout_seconds": requested_timeout_seconds,
                "timeout_seconds": timeout_seconds,
                "requested_priority": priority,
                "priority": effective_queue_priority,
                "base_sha": base_sha,
                "force_rerun": bool(force_rerun),
                "supersede_previous": bool(supersede_previous),
                "effective_config_digest": config_digest,
            }
            request_hash = compute_normalized_request_hash(normalized_payload)
            effective_idempotency_key = idempotency_key or (
                f"auto:{request_hash}" if not force_rerun else f"auto-force:{uuid.uuid4().hex}"
            )
            request = await asyncio.to_thread(
                create_or_get_ci_request,
                repository=repository,
                branch=branch,
                commit_sha=commit_sha,
                tree_sha=None,
                profile=profile,
                effective_config_digest=config_digest,
                idempotency_key=effective_idempotency_key,
                normalized_request_hash=request_hash,
                request_payload=normalized_payload,
            )
            snapshot = build_private_ci_start_response(request)
            if request["phase"] in {"accepted", "preparing"}:
                schedule_ci_request_preparation(request["request_id"])
            return json.dumps(snapshot, ensure_ascii=False)
        except CIRequestIdempotencyConflictError:
            return _error_response(
                "IDEMPOTENCY_CONFLICT",
                "idempotency_key is already bound to a different normalized CI request",
                details={"idempotency_key": idempotency_key},
            )
        except Exception as e:
            return _error_response("INTERNAL_ERROR", str(e))

    @mcp.tool(
        name="get_private_ci_job",
        description="""Get one private CI Request/Worker snapshot. Defaults to a compact gate-safe summary.

Provide request_id and/or job_id; at least one is required. request_id works before a Worker Job exists.
When both are supplied they must be the exact persisted Request/Worker pair or the call fails closed.
summary is a pure read of current durable state: it never waits, polls, calls GitHub/network, or loads logs.
It keeps exact identity, Request revision, truthful Worker execution status, queue/worker state,
normalized workspaces, and bounded step status without commands, offsets, evidence, or changed files.
Use detail_level='full' only for debugging; oversized full results are returned through a response resource.

This is for the private CI system. NOT for GitHub Actions runs (use get_ci_job for that).""",
        annotations=_PRIVATE_CI_DIAGNOSTIC_READ,
    )
    async def get_private_ci_job(
        job_id: str = "", detail_level: str = "summary", request_id: str = "",
    ) -> str:
        try:
            if detail_level not in {"summary", "full"}:
                return _error_response("INVALID_ARGUMENT", "detail_level must be 'summary' or 'full'")
            if not job_id and not request_id:
                return _error_response(
                    "INVALID_ARGUMENT", "at least one of request_id or job_id is required"
                )

            request = None
            job = None
            if request_id:
                request = await asyncio.to_thread(get_ci_request, request_id)
                if not request:
                    return _error_response(
                        "CI_REQUEST_NOT_FOUND", f"CI Request '{request_id}' not found"
                    )
            if job_id:
                job = await asyncio.to_thread(get_job, job_id)
                if not job:
                    return _error_response(
                        "PRIVATE_CI_JOB_NOT_FOUND", f"Job '{job_id}' not found"
                    )

            if request and not job and request.get("worker_job_id"):
                linked_job_id = request["worker_job_id"]
                job = await asyncio.to_thread(get_job, linked_job_id)
                if not job:
                    return _error_response(
                        "CI_REQUEST_IDENTITY_MISMATCH",
                        "CI Request references a Worker Job that is not present",
                        details={
                            "request_id": request_id,
                            "job_id": linked_job_id,
                            "field": "worker_job_id",
                        },
                    )
            elif job and not request:
                request = await asyncio.to_thread(
                    get_ci_request_by_worker_job_id, job["job_id"]
                )

            if request and job:
                mismatch = _private_ci_request_worker_mismatch(request, job)
                if mismatch:
                    return _error_response(
                        "CI_REQUEST_IDENTITY_MISMATCH",
                        "CI Request and Worker Job identity do not match",
                        details={
                            "request_id": request.get("request_id"),
                            "job_id": job.get("job_id"),
                            "field": mismatch,
                        },
                    )

            persisted_steps = (
                await asyncio.to_thread(get_steps, job["job_id"]) if job else []
            )
            worker = None
            if job and job.get("worker_id"):
                worker = await asyncio.to_thread(get_worker, job["worker_id"])
            result = build_private_ci_snapshot_response(
                request, job, persisted_steps, detail_level, worker
            )
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            return _error_response("INTERNAL_ERROR", str(e))

    @mcp.tool(
        name="wait_private_ci_job",
        description="Deprecated compatibility-only long-poll for explicit legacy/debug callers. Retains the legacy up-to-55-second status/step/revision wait contract. It is not the canonical ChatGPT Web CI tracking path; normal Web continuation uses get_private_ci_job snapshots and must not loop this tool until terminal.",
        annotations=_PRIVATE_CI_DIAGNOSTIC_READ,
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
        description="""Get private CI job execution logs by job_id. Supports pagination for large logs and precise persisted-step reads.

Use the exact job_id from start_private_ci_job. Do NOT assume the latest job's logs.

Without step_id/step_name this preserves the complete job-log API. With step_id,
or a unique step_name, only the persisted half-open step range is returned.
step_id is the stable selector when step names repeat. Pass next_cursor back
as cursor for the next step page; cursor-only continuation is supported.

This is for the private CI system. NOT for GitHub Actions logs (use get_ci_logs for that).""",
        annotations=_PRIVATE_CI_DIAGNOSTIC_READ,
    )
    async def get_private_ci_logs(
        job_id: str = "", offset: int = 0, limit: int = 200,
        step_id: Optional[int] = None, step_name: str = "", cursor: str = "",
    ) -> str:
        try:
            cursor_data = None
            if cursor:
                try:
                    cursor_data = decode_step_log_cursor(cursor)
                except ValueError as exc:
                    return _error_response("PRIVATE_CI_LOG_CURSOR_INVALID", str(exc))
                if job_id and cursor_data["job_id"] != job_id:
                    return _error_response(
                        "PRIVATE_CI_LOG_CURSOR_MISMATCH",
                        "cursor belongs to a different private CI job",
                        details={"job_id": job_id, "cursor_job_id": cursor_data["job_id"]},
                    )
                if not job_id:
                    job_id = cursor_data["job_id"]
            if not job_id:
                return _error_response("INVALID_ARGUMENT", "job_id or cursor is required")
            job = await asyncio.to_thread(get_job, job_id)
            if not job:
                return _error_response("PRIVATE_CI_JOB_NOT_FOUND", f"Job '{job_id}' not found")

            normalized_step_id = None
            if step_id is not None:
                if isinstance(step_id, bool):
                    return _error_response(
                        "INVALID_ARGUMENT", "step_id must be an integer",
                    )
                try:
                    normalized_step_id = int(step_id)
                except (TypeError, ValueError):
                    return _error_response("INVALID_ARGUMENT", "step_id must be an integer")
                if normalized_step_id <= 0:
                    return _error_response("INVALID_ARGUMENT", "step_id must be positive")

            requested_step_name = str(step_name or "")
            safe_step_name = redact_text(requested_step_name)
            step_mode = bool(cursor_data or normalized_step_id is not None or requested_step_name)
            if not step_mode:
                result = await asyncio.to_thread(get_log_chunks, job_id, offset, limit)
                result = _redact_log_chunks(result)
                result["mode"] = "job"
                result["limit"] = min(max(int(limit), 1), _MAX_PRIVATE_CI_LOG_PAGE_CHUNKS)
            else:
                if cursor_data and normalized_step_id is not None and (
                    cursor_data["step_id"] != normalized_step_id
                ):
                    return _error_response(
                        "PRIVATE_CI_LOG_CURSOR_MISMATCH",
                        "cursor does not belong to the selected step",
                        details={
                            "step_id": normalized_step_id,
                            "cursor_step_id": cursor_data["step_id"],
                        },
                    )

                persisted_steps = await asyncio.to_thread(get_steps, job_id)
                cursor_step_id = cursor_data["step_id"] if cursor_data else None
                selected_step_id = normalized_step_id or cursor_step_id
                matching_by_name = [
                    step for step in persisted_steps
                    if str(step.get("step_name") or "") == requested_step_name
                ] if requested_step_name else []

                if requested_step_name and not matching_by_name:
                    return _error_response(
                        "PRIVATE_CI_STEP_NOT_FOUND",
                        f"Step '{safe_step_name}' was not found in job '{job_id}'",
                        details={"job_id": job_id, "step_name": safe_step_name},
                    )
                if requested_step_name and selected_step_id is None and len(matching_by_name) > 1:
                    return _error_response(
                        "PRIVATE_CI_STEP_SELECTOR_AMBIGUOUS",
                        f"Step name '{requested_step_name}' is not unique",
                        details={
                            "job_id": job_id,
                            "step_name": safe_step_name,
                            "matches": [
                                _step_selector_details(step) for step in matching_by_name
                            ],
                        },
                    )
                if requested_step_name and selected_step_id is not None:
                    matching_ids = {
                        step.get("step_id") for step in matching_by_name
                    }
                    if selected_step_id not in matching_ids:
                        return _error_response(
                            "PRIVATE_CI_LOG_CURSOR_MISMATCH" if cursor_data else "PRIVATE_CI_STEP_SELECTOR_MISMATCH",
                            "step selector does not identify the requested step name",
                            details={
                                "job_id": job_id,
                                "step_id": selected_step_id,
                                "step_name": safe_step_name,
                            },
                        )

                selected_step = next(
                    (
                        step for step in persisted_steps
                        if step.get("step_id") == selected_step_id
                    ),
                    None,
                ) if selected_step_id is not None else (
                    matching_by_name[0] if len(matching_by_name) == 1 else None
                )
                if selected_step is None:
                    error_code = (
                        "PRIVATE_CI_STEP_NOT_FOUND"
                        if selected_step_id is not None
                        else "PRIVATE_CI_STEP_ID_UNAVAILABLE"
                    )
                    message = (
                        f"Step id '{selected_step_id}' was not found in job '{job_id}'"
                        if selected_step_id is not None
                        else f"Step '{requested_step_name}' has no stable persisted identity"
                    )
                    return _error_response(
                        error_code,
                        message,
                        details={
                            "job_id": job_id,
                            "step_id": selected_step_id,
                            "step_name": safe_step_name or None,
                        },
                    )

                selected_step_id = selected_step["step_id"]
                range_start = int(selected_step.get("log_start_offset") or 0)
                range_end = int(selected_step.get("log_end_offset") or 0)
                if range_start < 0 or range_end < range_start:
                    return _error_response(
                        "PRIVATE_CI_STEP_LOG_RANGE_INVALID",
                        "persisted step log range is invalid",
                        details={
                            "job_id": job_id,
                            "step": _step_selector_details(selected_step),
                        },
                    )

                if cursor_data:
                    if (
                        cursor_data["step_id"] != selected_step_id
                        or cursor_data["range_start"] != range_start
                        or cursor_data["range_end"] != range_end
                    ):
                        return _error_response(
                            "PRIVATE_CI_LOG_CURSOR_STALE",
                            "cursor no longer matches the persisted step log range",
                            details={
                                "job_id": job_id,
                                "step_id": selected_step_id,
                                "cursor_range": {
                                    "start_offset": cursor_data["range_start"],
                                    "end_offset": cursor_data["range_end"],
                                },
                                "current_range": {
                                    "start_offset": range_start,
                                    "end_offset": range_end,
                                },
                            },
                        )
                    current_offset = cursor_data["offset"]
                    if current_offset < range_start or current_offset > range_end:
                        return _error_response(
                            "PRIVATE_CI_LOG_CURSOR_INVALID",
                            "cursor offset is outside the persisted step log range",
                            details={
                                "job_id": job_id,
                                "step_id": selected_step_id,
                                "range": {
                                    "start_offset": range_start,
                                    "end_offset": range_end,
                                },
                            },
                        )
                else:
                    try:
                        requested_offset = int(offset)
                    except (TypeError, ValueError):
                        return _error_response("INVALID_ARGUMENT", "offset must be an integer")
                    if requested_offset < 0:
                        return _error_response("INVALID_ARGUMENT", "offset must be non-negative")
                    if requested_offset == 0:
                        current_offset = range_start
                    elif range_start <= requested_offset <= range_end:
                        # An absolute controller-log offset is accepted for
                        # callers that already know the persisted range.
                        current_offset = requested_offset
                    elif requested_offset <= range_end - range_start:
                        # Offset remains useful as a relative step offset when
                        # the selected step starts after the beginning of the job log.
                        current_offset = range_start + requested_offset
                    else:
                        return _error_response(
                            "PRIVATE_CI_STEP_LOG_OFFSET_INVALID",
                            "offset is outside the selected step log range",
                            details={
                                "job_id": job_id,
                                "step_id": selected_step_id,
                                "range": {
                                    "start_offset": range_start,
                                    "end_offset": range_end,
                                },
                                "offset": requested_offset,
                            },
                        )

                if cursor_data and int(offset) != 0:
                    return _error_response(
                        "PRIVATE_CI_LOG_CURSOR_OFFSET_CONFLICT",
                        "cursor and offset cannot select different positions",
                        details={"cursor_offset": current_offset, "offset": offset},
                    )

                result = await asyncio.to_thread(
                    get_log_chunks,
                    job_id,
                    current_offset,
                    min(max(int(limit), 1), _MAX_PRIVATE_CI_LOG_PAGE_CHUNKS),
                    range_start=range_start,
                    range_end=range_end,
                    max_bytes=_MAX_PRIVATE_CI_STEP_PAGE_BYTES,
                )
                result = _redact_log_chunks(result)
                has_more = bool(result.get("has_more"))
                result.update({
                    "mode": "step",
                    "step": _step_selector_details(selected_step),
                    "step_selector": {"step_id": selected_step_id},
                    "step_log_range": {
                        "start_offset": range_start,
                        "end_offset": range_end,
                        "end_exclusive": True,
                    },
                    "cursor": cursor or None,
                    "next_cursor": (
                        encode_step_log_cursor(
                            job_id,
                            selected_step_id,
                            range_start,
                            range_end,
                            int(result["next_offset"]),
                        )
                        if has_more and result.get("next_offset") is not None
                        else None
                    ),
                    "has_more": has_more,
                    "page_limit_bytes": _MAX_PRIVATE_CI_STEP_PAGE_BYTES,
                })
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
        annotations=_PRIVATE_CI_DIAGNOSTIC_READ,
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
        annotations=_PRIVATE_CI_CANCELLATION,
    )
    async def cancel_private_ci_job(job_id: str) -> str:
        try:
            job = await asyncio.to_thread(get_job, job_id)
            if not job:
                return _error_response("PRIVATE_CI_JOB_NOT_FOUND", f"Job '{job_id}' not found")
            status = job.get("status", "")
            if status == "queued":
                ok = await asyncio.to_thread(cancel_queued_job, job_id)
                if ok:
                    return json.dumps({"ok": True, "status": "cancelled", "job_id": job_id})
                latest = await asyncio.to_thread(get_job, job_id)
                if latest and latest.get("status") in ("leased", "downloading", "preparing", "running"):
                    await asyncio.to_thread(request_cancel_job, job_id)
                    latest = await asyncio.to_thread(get_job, job_id)
                    if latest and latest.get("cancel_requested"):
                        return json.dumps({"ok": True, "status": "cancel_requested", "job_id": job_id, "message": "Cancel signal sent to worker"})
                if latest and latest.get("status") in ("passed", "failed", "cancelled", "timed_out", "superseded", "worker_lost", "internal_error"):
                    return _error_response("PRIVATE_CI_JOB_ALREADY_FINISHED", f"Cannot cancel job in status '{latest['status']}'")
                return _error_response("INTERNAL_ERROR", "cancel failed")
            if status in ("leased", "downloading", "preparing", "running"):
                await asyncio.to_thread(request_cancel_job, job_id)
                latest = await asyncio.to_thread(get_job, job_id)
                if latest and latest.get("status") in ("leased", "downloading", "preparing", "running") and latest.get("cancel_requested"):
                    return json.dumps({"ok": True, "status": "cancel_requested", "job_id": job_id, "message": "Cancel signal sent to worker"})
                if latest and latest.get("status") in ("passed", "failed", "cancelled", "timed_out", "superseded", "worker_lost", "internal_error"):
                    return _error_response("PRIVATE_CI_JOB_ALREADY_FINISHED", f"Cannot cancel job in status '{latest['status']}'")
                return _error_response("INTERNAL_ERROR", "cancel failed")
            if status in ("passed", "failed", "cancelled", "timed_out", "superseded", "worker_lost", "internal_error"):
                return _error_response("PRIVATE_CI_JOB_ALREADY_FINISHED", f"Cannot cancel job in status '{status}'")
            return _error_response("INVALID_ARGUMENT", f"Cannot cancel job in status '{status}'")
        except Exception as e:
            return _error_response("INTERNAL_ERROR", str(e))
