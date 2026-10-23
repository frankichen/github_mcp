"""Authenticated, fail-closed bookkeeping for SXT releases deployed outside MyGithut12.

Only the dedicated WSL executor callback credential may invoke this contract.
The caller must independently verify the live server through SSH. Never infer
success from a build exit code, a requested release ID, or a CI job alone.
"""

import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app import deployment_service as ds
from app.ci_database import list_jobs
from app.github_utils import SHA_RE, _error_response

RELEASE_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{12}$")
ROOT = Path("/home/dly/releases")
ACTIVE_STATUSES = (
    "queued", "claimed", "running", "preparing", "validating_main",
    "testing", "building", "packaging", "checksum_verification",
    "uploading", "migrating", "switching", "restarting_services",
    "health_checking", "cancel_requested", "rolling_back",
)


def _error(code: str) -> dict:
    return _error_response(code, "manual release reconciliation refused")


def _eligible_sha(sha: str, job_id: str = "") -> tuple[str | None, str | None]:
    if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
        return "INVALID_COMMIT_SHA", None
    try:
        current_main, _ = ds._repo_state(ds.REPOSITORY, sha)
    except Exception:
        return "GITHUB_STATE_UNAVAILABLE", None
    if sha != current_main:
        return "COMMIT_NOT_CURRENT_MAIN", None
    try:
        jobs = list_jobs(repository=ds.REPOSITORY, branch="main",
                         commit_sha=sha, status="passed", limit=30)
    except Exception:
        return "PRIVATE_CI_UNAVAILABLE", None
    candidates = [item for item in jobs if not job_id or item.get("job_id") == job_id]
    for job in candidates:
        candidate = job.get("job_id", "")
        reason, _ = ds._ci_gate(ds.REPOSITORY, candidate, sha)
        if not reason:
            return None, candidate
    return "PRIVATE_CI_REQUIRED", None


def _registered_current() -> str:
    snapshot = ds._status_snapshot(ds.REPOSITORY) or {}
    current, _ = ds._release_snapshot(snapshot)
    return (current or {}).get("release_id") or ""


def _active_deployment(db) -> bool:
    params = ",".join("?" for _ in ACTIVE_STATUSES)
    row = db.execute(
        f"SELECT 1 FROM deployments WHERE repository=? AND environment=? "
        f"AND status IN ({params}) LIMIT 1",
        (ds.REPOSITORY, ds.ENVIRONMENT, *ACTIVE_STATUSES),
    ).fetchone()
    return row is not None


def preflight_manual_release(body: dict) -> dict:
    """Call before a manual deployment; reject unverified SHA or active writer."""
    if body.get("repository") != ds.REPOSITORY or body.get("environment") != ds.ENVIRONMENT:
        return _error("MANUAL_RELEASE_SCOPE_INVALID")
    sha = body.get("git_sha")
    reason, job_id = _eligible_sha(sha)
    if reason:
        return _error(reason)
    ds.init_deployment_db()
    with ds._lock:
        db = ds._get_deploy_db()
        if _active_deployment(db):
            return _error("DEPLOYMENT_ALREADY_ACTIVE")
        return {
            "ok": True,
            "repository": ds.REPOSITORY,
            "environment": ds.ENVIRONMENT,
            "git_sha": sha,
            "private_ci_job_id": job_id,
            "expected_current_release_id": _registered_current(),
        }


def _valid_proof(body: dict) -> bool:
    sha = body.get("git_sha")
    release_id = body.get("release_id")
    path = body.get("current_release_path")
    if not all(isinstance(v, str) for v in (sha, release_id, path)):
        return False
    if (not SHA_RE.fullmatch(sha) or not RELEASE_RE.fullmatch(release_id)
            or release_id[-12:] != sha[:12]):
        return False
    if path != str(ROOT / release_id):
        return False
    if body.get("repository") != ds.REPOSITORY or body.get("environment") != ds.ENVIRONMENT:
        return False
    for field in ("manifest_verified", "checksum_verified", "health_verified", "services_healthy"):
        if body.get(field) is not True:
            return False
    if type(body.get("frontend_included")) is not bool:
        return False
    try:
        seen = datetime.fromisoformat(str(body.get("observed_at_utc") or "").replace("Z", "+00:00"))
        if seen.tzinfo is None or abs(time.time() - seen.timestamp()) > 120:
            return False
    except (ValueError, TypeError, OverflowError):
        return False
    return True


def reconcile_manual_release(body: dict) -> dict:
    """Append a distinct audited record; do not rewrite historical failed jobs."""
    if not _valid_proof(body):
        return _error("MANUAL_RELEASE_EVIDENCE_INVALID")
    sha = body["git_sha"]
    job_id = body.get("private_ci_job_id")
    if not isinstance(job_id, str) or not job_id:
        return _error("PRIVATE_CI_REQUIRED")
    reason, selected_job = _eligible_sha(sha, job_id)
    if reason:
        return _error(reason)
    expected = body.get("expected_current_release_id")
    if not isinstance(expected, str):
        return _error("MANUAL_RELEASE_EXPECTED_CURRENT_REQUIRED")

    ds.init_deployment_db()
    with ds._lock:
        db = ds._get_deploy_db()
        if _active_deployment(db):
            return _error("DEPLOYMENT_ALREADY_ACTIVE")
        actual = _registered_current()
        if expected != actual:
            return _error("DEPLOYMENT_RELEASE_DRIFT")
        existing = db.execute(
            "SELECT * FROM deployments WHERE repository=? AND environment=? "
            "AND target_release=? AND status='passed' ORDER BY created_at DESC LIMIT 1",
            (ds.REPOSITORY, ds.ENVIRONMENT, body["release_id"]),
        ).fetchone()
        if existing and existing["commit_sha"] != sha:
            return _error("MANUAL_RELEASE_ID_CONFLICT")
        already = bool(existing and existing["commit_sha"] == sha)
        deployment_id = existing["deployment_id"] if already else "dep_" + uuid.uuid4().hex
        now = ds._now()
        if not already:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    "INSERT INTO deployments (deployment_id,repository,environment,commit_sha,"
                    "private_ci_job_id,requested_scope,current_release_before,target_release,"
                    "requested_by,status,current_step,exit_code,created_at,started_at,finished_at,"
                    "updated_at,log_revision,log_text,current_release_path,current_git_sha,"
                    "frontend_included,reconciled_at,reconciliation_reason)"
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        deployment_id, ds.REPOSITORY, ds.ENVIRONMENT, sha, selected_job,
                        ds.SCOPE, actual or None, body["release_id"], "manual-script",
                        "passed", "completed", 0, now, now, now, now, 1,
                        "Authenticated manual script; current link, manifest, checksums, "
                        "active container mounts and health checked on gongshi-test",
                        body["current_release_path"], sha, int(body["frontend_included"]),
                        now, "verified_manual_script",
                    ),
                )
                db.commit()
            except Exception:
                db.rollback()
                raise

        release = {
            "release_id": body["release_id"],
            "repository": ds.REPOSITORY, "environment": ds.ENVIRONMENT,
            "git_sha": sha, "current_release_path": body["current_release_path"],
            "manifest_verified": True, "checksum_verified": True,
            "health_verified": True, "services_healthy": True,
            "frontend_included": body["frontend_included"],
            "deployment_id": deployment_id, "status": "passed",
        }
        try:
            ds._persist_release_snapshot(release, ds.REPOSITORY)
        except OSError:
            return _error("MANUAL_RELEASE_REGISTRY_WRITE_FAILED")
        if _registered_current() != body["release_id"]:
            return _error("MANUAL_RELEASE_REGISTRY_VERIFY_FAILED")
        return {
            "ok": True, "idempotent": already, "deployment_id": deployment_id,
            "release_id": body["release_id"], "git_sha": sha, "status": "passed",
            "source": "verified_manual_script",
        }
