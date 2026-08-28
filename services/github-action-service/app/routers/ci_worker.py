"""CI Worker internal API router.

Only accessible via Tailscale internal network.
"""

import asyncio
import hashlib
import json
import logging
import time
import uuid
from typing import Optional

from fastapi import APIRouter, Request, HTTPException, Query, Depends

from app.ci_worker_auth import verify_ci_worker
from app.github_auth import credential_provider
from app.version import SERVICE_VERSION
from app.ci_database import (
    register_worker,
    update_worker_heartbeat,
    get_workers,
    get_worker,
    lease_job,
    renew_lease,
    need_heartbeat,
    complete_job,
    release_job,
    set_job_status,
    set_job_source_downloaded,
    StaleJobLeaseError,
    get_job,
    append_log_chunk,
    append_log_batch,
    get_log_chunks,
    add_step,
    finish_step,
    get_steps,
    recover_expired_leases,
    recover_worker_jobs,
    reconcile_worker_startup,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/ci", tags=["CI Worker"])


@router.get("/health")
async def ci_health():
    return {
        "status": "ok",
        "service": "ci-controller",
        "version": SERVICE_VERSION,
    }


@router.post("/workers/register")
async def worker_register(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": "bad_request", "message": "Request body must be valid JSON"})
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail={"error": "bad_request", "message": "Request body must be an object"})
    worker_id = body.get("worker_id", "")
    token = body.get("token", "")
    profiles = body.get("profiles", [])
    max_concurrent = body.get("max_concurrent", 1)

    if not isinstance(worker_id, str) or not worker_id.strip() or not isinstance(token, str) or not token:
        raise HTTPException(status_code=400, detail={"error": "bad_request", "message": "worker_id and token required"})

    if not isinstance(profiles, list) or not all(isinstance(profile, str) and profile for profile in profiles):
        raise HTTPException(status_code=400, detail={"error": "bad_request", "message": "profiles must be a list of strings"})

    if isinstance(max_concurrent, bool) or not isinstance(max_concurrent, int) or max_concurrent < 1 or max_concurrent > 10:
        raise HTTPException(status_code=400, detail={"error": "bad_request", "message": "max_concurrent must be 1-10"})

    try:
        recover_worker_jobs(worker_id)
        ok = register_worker(worker_id.strip(), token, profiles, max_concurrent)
    except Exception:
        logger.exception("Worker registration failed before database result: worker_id=%s", worker_id)
        raise HTTPException(status_code=503, detail={"error": "worker_registration_unavailable", "message": "Worker registration storage is temporarily unavailable"})

    if ok:
        logger.info("Worker registered: %s profiles=%s", worker_id, profiles)
        return {"status": "registered", "worker_id": worker_id.strip()}
    else:
        raise HTTPException(status_code=503, detail={"error": "worker_registration_unavailable", "message": "Worker registration storage is temporarily unavailable"})


@router.post("/workers/reconcile")
async def worker_reconcile(request: Request):
    """Reconcile one authenticated worker after process restart.

    Recovery semantics are fixed on the server.  Request-provided terminal
    state lists are intentionally ignored so a worker cannot redefine the CI
    state machine.
    """
    worker_id = await verify_ci_worker(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail={"error": "bad_request", "message": "Request body must be an object"})

    requested_worker_id = body.get("worker_id")
    if requested_worker_id not in (None, "", worker_id):
        raise HTTPException(status_code=403, detail={"error": "forbidden", "message": "worker_id does not match authenticated worker"})
    if body.get("action", "startup_reconcile") != "startup_reconcile":
        raise HTTPException(status_code=400, detail={"error": "bad_request", "message": "unsupported reconcile action"})

    try:
        # Idempotent with registration: registration releases active jobs;
        # reconciliation clears the stale worker pointer left behind.
        recover_worker_jobs(worker_id)
        result = reconcile_worker_startup(worker_id)
    except Exception:
        logger.exception("Worker startup reconciliation failed: worker_id=%s", worker_id)
        raise HTTPException(status_code=503, detail={"error": "worker_reconcile_unavailable"})
    if result is None:
        raise HTTPException(status_code=404, detail={"error": "worker_not_found"})
    return {"status": "ok", "worker_id": worker_id, **result}


def _require_job_lease(request: Request) -> str:
    lease_token = request.headers.get("X-CI-Lease-Token", "").strip()
    if not lease_token:
        raise HTTPException(
            status_code=409,
            detail={"error": "stale_job_lease", "message": "Current job lease token is required"},
        )
    return lease_token


def _raise_stale_job_lease(job_id: str):
    raise HTTPException(
        status_code=409,
        detail={"error": "stale_job_lease", "job_id": job_id, "message": "Job attempt lease is no longer current"},
    )


@router.post("/workers/heartbeat")
async def worker_heartbeat(request: Request):
    worker_id = await verify_ci_worker(request)
    update_worker_heartbeat(worker_id)

    try:
        body = await request.json()
    except Exception:
        body = {}
    current_job_id = body.get("current_job_id")
    lease_token = body.get("lease_token")

    if current_job_id:
        renewed = bool(lease_token) and renew_lease(current_job_id, lease_token, worker_id)
        lease_lost = not renewed
        cancel_requested = True if lease_lost else need_heartbeat(current_job_id)
        return {
            "status": "ok",
            "worker_id": worker_id,
            "lease_renewed": renewed,
            "lease_lost": lease_lost,
            "cancel_requested": cancel_requested,
        }

    return {"status": "ok", "worker_id": worker_id}


@router.post("/jobs/lease")
async def job_lease(request: Request):
    worker_id = await verify_ci_worker(request)
    worker = get_worker(worker_id)
    if not worker:
        raise HTTPException(status_code=404, detail={"error": "worker_not_found"})

    recover_expired_leases()
    result = lease_job(worker_id, worker["supported_profiles"], worker["max_concurrent"])
    if result:
        logger.info(
            "Job leased: worker=%s job=%s repo=%s sha=%.12s",
            worker_id, result["job_id"], result["repository"], result["commit_sha"],
        )
        return result
    return {"job_id": None, "message": "No jobs available"}


@router.get("/jobs/{job_id}/source/download")
async def job_get_source(job_id: str, request: Request):
    """Download source only for the Worker that owns the current attempt."""
    from fastapi.responses import StreamingResponse
    from app.ci_source_proxy import download_github_archive
    import os
    import tempfile

    worker_id = await verify_ci_worker(request)
    lease_token = _require_job_lease(request)
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "Job not found"})
    if not renew_lease(job_id, lease_token, worker_id):
        _raise_stale_job_lease(job_id)

    try:
        gh_token = credential_provider.token()
    except Exception:
        raise HTTPException(status_code=500, detail={"error": "config_error", "message": "GITHUB_TOKEN not configured"})

    try:
        temporary = tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False)
        tmp_file = temporary.name
        temporary.close()
        result = await asyncio.to_thread(
            download_github_archive,
            job["repository"],
            job["commit_sha"],
            gh_token,
            tmp_file,
        )
    except Exception as e:
        if "tmp_file" in locals():
            try:
                os.unlink(tmp_file)
            except OSError:
                pass
        raise HTTPException(status_code=502, detail={"error": "download_failed", "message": str(e)[:500]})

    try:
        set_job_source_downloaded(
            job_id, result["sha256"], result["size_bytes"], worker_id, lease_token
        )
    except StaleJobLeaseError:
        try:
            os.unlink(tmp_file)
        except OSError:
            pass
        _raise_stale_job_lease(job_id)

    def file_iterator():
        with open(tmp_file, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                yield chunk
        try:
            os.unlink(tmp_file)
        except Exception:
            pass

    return StreamingResponse(
        file_iterator(),
        media_type="application/octet-stream",
        headers={
            "X-SHA256": result["sha256"],
            "X-Size": str(result["size_bytes"]),
        },
    )


@router.post("/jobs/{job_id}/source")
async def job_download_source(job_id: str, request: Request):
    """Worker reports source evidence for its current leased attempt."""
    worker_id = await verify_ci_worker(request)
    lease_token = _require_job_lease(request)
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail={"error": "not_found"})

    body = await request.json()
    sha256 = body.get("sha256", "")
    size_bytes = body.get("size_bytes", 0)
    try:
        set_job_source_downloaded(job_id, sha256, size_bytes, worker_id, lease_token)
    except StaleJobLeaseError:
        _raise_stale_job_lease(job_id)

    return {"status": "ok", "job_id": job_id}


@router.post("/jobs/{job_id}/logs")
async def job_upload_logs(job_id: str, request: Request):
    worker_id = await verify_ci_worker(request)
    lease_token = _require_job_lease(request)
    body = await request.json()
    content = body.get("content", "")
    if not content:
        if not renew_lease(job_id, lease_token, worker_id):
            _raise_stale_job_lease(job_id)
        return {"status": "ok", "bytes_written": 0}

    try:
        new_size = append_log_chunk(job_id, content, worker_id, lease_token)
    except StaleJobLeaseError:
        _raise_stale_job_lease(job_id)
    logger.info(
        "Log uploaded: worker=%s job=%s repo=%.12s bytes=%d total=%d",
        worker_id, job_id,
        body.get("repository", job_id)[:12] if body.get("repository") else job_id[:12],
        len(content), new_size,
    )

    return {"status": "ok", "bytes_written": len(content), "total_bytes": new_size}


@router.post("/jobs/{job_id}/logs/batch")
async def job_upload_log_batch(job_id: str, request: Request):
    worker_id = await verify_ci_worker(request)
    lease_token = _require_job_lease(request)
    body = await request.json()
    content = str(body.get("content") or "")
    batch_id = str(body.get("batch_id") or "")
    try:
        new_size, idempotent = append_log_batch(
            job_id, batch_id, content, worker_id, lease_token
        )
    except StaleJobLeaseError:
        _raise_stale_job_lease(job_id)
    logger.info("Log batch uploaded: worker=%s job=%s batch=%s bytes=%d total=%d idempotent=%s", worker_id, job_id, batch_id[:12], len(content), new_size, idempotent)
    return {"status": "ok", "bytes_written": 0 if idempotent else len(content), "total_bytes": new_size, "idempotent": idempotent}


@router.post("/jobs/{job_id}/steps")
async def job_upload_step(job_id: str, request: Request):
    worker_id = await verify_ci_worker(request)
    lease_token = _require_job_lease(request)
    body = await request.json()
    step_name = body.get("step_name", "")
    action = body.get("action", "start")  # start, finish, update_status

    try:
        if action == "start":
            step_id = add_step(job_id, step_name, "running", worker_id, lease_token)
            return {"status": "ok", "step_id": step_id}
        if action == "finish":
            step_id = body.get("step_id")
            step_status = body.get("status", "completed")
            exit_code = body.get("exit_code")
            log_end_offset = body.get("log_end_offset")
            finish_step(
                step_id, step_status, exit_code, log_end_offset,
                job_id=job_id, worker_id=worker_id, lease_token=lease_token,
            )
            return {"status": "ok", "step_id": step_id}
        if action == "update_status":
            set_job_status(
                job_id, body.get("job_status", "running"), worker_id, lease_token
            )
            return {"status": "ok"}
    except StaleJobLeaseError:
        _raise_stale_job_lease(job_id)

    raise HTTPException(status_code=400, detail={"error": "bad_request", "message": f"Unknown action: {action}"})


@router.post("/jobs/{job_id}/finish")
async def job_finish(job_id: str, request: Request):
    worker_id = await verify_ci_worker(request)
    lease_token = _require_job_lease(request)
    body = await request.json()
    exit_code = body.get("exit_code", -1)
    status = body.get("status", "failed")
    summary = body.get("summary")
    error_code = body.get("error_code")
    error_message = body.get("error_message")

    try:
        complete_job(
            job_id, exit_code, status, summary, error_code, error_message,
            worker_id=worker_id, lease_token=lease_token,
        )
    except StaleJobLeaseError:
        _raise_stale_job_lease(job_id)
    logger.info(
        "Job finished: worker=%s job=%s status=%s exit=%d",
        worker_id, job_id, status, exit_code,
    )

    return {"status": "ok", "job_id": job_id, "final_status": status}


@router.post("/jobs/{job_id}/release")
async def job_release(job_id: str, request: Request):
    worker_id = await verify_ci_worker(request)
    lease_token = _require_job_lease(request)
    try:
        release_job(job_id, worker_id, lease_token)
    except StaleJobLeaseError:
        _raise_stale_job_lease(job_id)
    logger.info("Job released: worker=%s job=%s", worker_id, job_id)
    return {"status": "ok", "job_id": job_id}


@router.get("/workers")
async def list_workers(request: Request):
    worker_id = await verify_ci_worker(request)
    return {"workers": get_workers()}


@router.get("/jobs/{job_id}")
async def get_ci_job_internal(job_id: str, request: Request):
    worker_id = await verify_ci_worker(request)
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    return job
