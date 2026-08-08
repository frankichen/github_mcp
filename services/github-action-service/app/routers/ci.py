"""CI MCP endpoint.

Public MCP API for CI job management, available through MCP server.
"""

import json
import logging
import re
from typing import Optional

from fastapi import APIRouter, Request, Query, HTTPException, Depends

from app.auth import verify_api_key
from app.ci_database import (
    get_job,
    get_job_by_idempotency_key,
    list_jobs,
    get_workers,
    get_log_chunks,
    get_steps,
    cancel_queued_job,
    request_cancel_job,
    create_or_get_job,
)
from app.ci_models import ALLOWED_PRIORITIES, effective_priority, make_idempotency_key
from app.ci_repository_config import (
    is_repository_allowed,
    is_profile_allowed,
    get_max_timeout,
    get_allowed_repositories,
    get_allowed_profiles,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/ci", tags=["CI MCP"])

SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _validate_repository(repository: str):
    if not repository or "/" not in repository:
        raise HTTPException(status_code=422, detail={"error": "validation_error", "message": "repository must be in owner/repo format"})
    if not is_repository_allowed(repository):
        raise HTTPException(status_code=403, detail={"error": "repository_not_allowed", "message": f"Repository '{repository}' is not in the CI allowed list"})


def _validate_commit_sha(commit_sha: str):
    if not commit_sha or not SHA_RE.match(commit_sha):
        raise HTTPException(status_code=422, detail={"error": "validation_error", "message": "commit_sha must be a 40-character hex string"})


def _validate_profile(repository: str, profile: str):
    if not is_profile_allowed(repository, profile):
        raise HTTPException(status_code=422, detail={"error": "profile_not_allowed", "message": f"Profile '{profile}' is not allowed for repository '{repository}'"})


### PUBLIC / MCP ACCESSIBLE ENDPOINTS

@router.get("/workers")
async def list_ci_workers_mcp(request: Request):
    verify_api_key(request)
    return {"workers": get_workers()}


@router.get("/profiles")
async def list_ci_profiles(request: Request):
    verify_api_key(request)
    profiles = get_allowed_profiles()
    return {
        "profiles": [
            {"name": p, "description": f"CI profile: {p}"}
            for p in profiles
        ]
    }


@router.get("/jobs")
async def list_ci_jobs(
    request: Request,
    repository: Optional[str] = Query(None),
    branch: Optional[str] = Query(None),
    commit_sha: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(default=20, ge=1, le=100),
):
    verify_api_key(request)
    return {"jobs": list_jobs(repository, branch, commit_sha, status, limit)}


@router.post("/jobs")
async def start_ci_job(request: Request):
    verify_api_key(request)

    body = await request.json()
    repository = body.get("repository", "")
    branch = body.get("branch", "")
    commit_sha = body.get("commit_sha", "")
    profile = body.get("profile", "repo-auto-check")
    timeout_seconds = body.get("timeout_seconds", 900)
    priority_str = body.get("priority", "normal")
    force_rerun = body.get("force_rerun", False)
    supersede_previous = body.get("supersede_previous", False)
    base_sha = body.get("base_sha", "")

    _validate_repository(repository)
    _validate_commit_sha(commit_sha)
    _validate_profile(repository, profile)

    # Validate priority
    if priority_str not in ALLOWED_PRIORITIES:
        raise HTTPException(status_code=422, detail={"error": "validation_error", "message": f"priority must be one of: {', '.join(ALLOWED_PRIORITIES.keys())}"})

    priority = effective_priority(branch, profile, ALLOWED_PRIORITIES[priority_str])

    # Validate timeout
    max_timeout = get_max_timeout(repository)
    if timeout_seconds < 60 or timeout_seconds > max_timeout:
        raise HTTPException(status_code=422, detail={"error": "validation_error", "message": f"timeout_seconds must be between 60 and {max_timeout}"})

    changed = {"changed_files": [], "total_count": 0, "truncated": False}
    if profile in ("repo-auto-check", "repo-fast-check"):
        from app.github_utils import get_github_changed_files_result
        changed = get_github_changed_files_result(repository, base_sha, commit_sha)
        if not changed.get("ok"):
            raise HTTPException(status_code=422, detail={"error": changed["error_code"], "message": changed.get("message", "changed files compare failed"), "details": changed.get("details", {})})

    result = create_or_get_job(
        repository=repository,
        branch=branch,
        commit_sha=commit_sha,
        profile=profile,
        priority=priority,
        timeout_seconds=timeout_seconds,
        force_rerun=force_rerun,
        supersede_previous=supersede_previous,
        base_sha=base_sha, changed_files=changed["changed_files"],
        changed_files_total=changed["total_count"],
        changed_files_truncated=changed["truncated"],
    )

    logger.info(
        "CI job %s: repo=%s sha=%.12s profile=%s dedup=%s",
        "deduplicated" if result["deduplicated"] else "created",
        repository, commit_sha, profile, result["deduplicated"],
    )

    return result


@router.get("/jobs/{job_id}")
async def get_ci_job(job_id: str, request: Request):
    verify_api_key(request)
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": f"Job '{job_id}' not found"})

    steps = get_steps(job_id, job.get("attempts") or None)
    current_step = None
    for s in steps:
        if s["status"] == "running":
            current_step = s["step_name"]
            break

    job["current_step"] = current_step
    job["steps"] = steps
    return job


@router.get("/jobs/{job_id}/logs")
async def get_ci_logs(
    job_id: str,
    request: Request,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
):
    verify_api_key(request)
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail={"error": "not_found"})

    return get_log_chunks(job_id, offset, limit)


@router.post("/jobs/{job_id}/cancel")
async def cancel_ci_job(job_id: str, request: Request):
    verify_api_key(request)

    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": f"Job '{job_id}' not found"})

    if job["status"] == "queued":
        ok = cancel_queued_job(job_id)
        return {"status": "cancelled", "job_id": job_id} if ok else {"status": "error", "message": "Could not cancel job"}

    if job["status"] in ("leased", "downloading", "preparing", "running"):
        request_cancel_job(job_id)
        return {"status": "cancel_requested", "job_id": job_id, "message": "Cancel signal sent to worker"}

    raise HTTPException(status_code=409, detail={"error": "invalid_status", "message": f"Cannot cancel job in status '{job['status']}'"})
