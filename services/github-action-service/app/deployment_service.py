"""受控 gongshi-test 部署编排。

这里只保存部署意图和状态；远程执行由独立 deploy worker 负责，Controller 不接受任意 SSH、Shell、主机或脚本路径。
"""

import json
import os
import random
import re
import secrets
import sqlite3
import threading
import time
import uuid
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from app.ci_database import get_job
from app.github_utils import SHA_RE, _get_gh, _error_response
from app.ci_repository_config import get_deployment_config, is_private_ci_enabled, get_allowed_repositories

REPOSITORY = "frankichen/sxt"
ENVIRONMENT = "gongshi-test"
SCOPE = "fullstack"
PROFILE = "repo-auto-check"
STATUSES = ("queued", "claimed", "running", "preparing", "validating_main", "testing", "building", "packaging", "checksum_verification", "uploading", "migrating", "switching", "restarting_services", "health_checking", "completed", "passed", "failed", "cancel_requested", "cancelled", "rolling_back", "rollback_failed", "rolled_back")
TERMINAL_STATUSES = {"passed", "failed", "cancelled", "rollback_failed", "rolled_back"}
ACTIVE_STATUSES = set(STATUSES) - TERMINAL_STATUSES
SAFE_PRE_SWITCH_STEPS = {None, "queued", "claimed", "preparing", "preparing_workspace", "validating_main", "testing", "building", "packaging", "checksum_verification", "uploading"}
DELEGATED_HANDOFF_OWNER = "private-deploy-agent"
INFRA_FILES = ("scripts/deploy_gongshi_test.sh", "deploy/", "scripts/sync_test_env.sh")
_lock = threading.Lock()
_local = threading.local()
_schema_lock = threading.Lock()
_initialized_schema_paths: set[str] = set()


def _deployment_spec(repository: str) -> dict:
    configured = get_deployment_config(repository)
    if not configured.get("enabled") and repository != REPOSITORY:
        return {}
    return {
        "environment": configured.get("environment", ENVIRONMENT),
        "scope": configured.get("scope", SCOPE),
        "private_ci": configured.get("private_ci", is_private_ci_enabled(repository)) is True,
        "profile": configured.get("profile", PROFILE),
        "script": configured.get("script", "scripts/deploy_gongshi_test.sh"),
        "status_file_env": configured.get("status_file_env", "DEPLOY_STATUS_FILE"),
    }


def _deployable_repositories() -> list[str]:
    """Return repositories with fixed deployment contracts."""
    return [repo for repo in get_allowed_repositories() if _deployment_spec(repo)]


def _deployment_status_path(repository: str) -> str:
    spec = _deployment_spec(repository)
    status_env = spec.get("status_file_env") or ""
    if status_env and os.environ.get(status_env):
        return os.environ[status_env]
    if repository == REPOSITORY:
        return os.environ.get("DEPLOY_STATUS_FILE", "/data/gongshi-test-status.json")
    environment = spec.get("environment") or repository.replace("/", "-")
    return f"/data/{environment}-status.json"


def _get_deploy_db():
    """部署状态使用独立 SQLite 文件，避免与 private CI 队列互相锁表或混淆 ID。"""
    if not hasattr(_local, "db") or _local.db is None:
        path = os.environ.get("DEPLOYMENT_DB_PATH", "/data/deployments.db")
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        db = sqlite3.connect(path, timeout=3, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("PRAGMA busy_timeout=2500")
        _local.db = db
    return _local.db


def _now() -> float:
    return time.time()


def _iso(value):
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat() if value else None


def _initialize_deployment_db():
    db = _get_deploy_db()
    db.executescript("""
    CREATE TABLE IF NOT EXISTS deployments (
      deployment_id TEXT PRIMARY KEY, repository TEXT NOT NULL, environment TEXT NOT NULL,
      commit_sha TEXT NOT NULL, private_ci_job_id TEXT NOT NULL, requested_scope TEXT NOT NULL,
      current_release_before TEXT, target_release TEXT, requested_by TEXT NOT NULL DEFAULT 'mcp',
      status TEXT NOT NULL, current_step TEXT, exit_code INTEGER, error_code TEXT,
      error_message TEXT, rollback_attempted INTEGER NOT NULL DEFAULT 0,
      rollback_succeeded INTEGER NOT NULL DEFAULT 0, cancel_requested INTEGER NOT NULL DEFAULT 0,
      frontend_included INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL,
      started_at REAL, finished_at REAL, updated_at REAL, log_revision INTEGER NOT NULL DEFAULT 0,
      current_release_path TEXT, current_git_sha TEXT, lease_token TEXT,
      artifact_id TEXT,
      performance_json TEXT NOT NULL DEFAULT '{}',
      log_text TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_deployments_filter ON deployments(repository, environment, commit_sha, status, created_at);
    """)


    columns = {row[1] for row in db.execute("PRAGMA table_info(deployments)").fetchall()}
    additions = {
        "updated_at": "REAL",
        "log_revision": "INTEGER NOT NULL DEFAULT 0",
        "current_release_path": "TEXT",
        "current_git_sha": "TEXT",
        "lease_token": "TEXT",
        "performance_json": "TEXT NOT NULL DEFAULT '{}'",
        "artifact_id": "TEXT",
        "claim_owner": "TEXT",
        "claim_started_at": "REAL",
        "heartbeat_at": "REAL",
        "lease_expires_at": "REAL",
        "claim_generation": "INTEGER NOT NULL DEFAULT 0",
        "claim_token_hash": "TEXT",
        "state_revision": "INTEGER NOT NULL DEFAULT 0",
        "reconciled_at": "REAL",
        "reconciliation_reason": "TEXT",
    }
    for name, definition in additions.items():
        if name not in columns:
            db.execute(f"ALTER TABLE deployments ADD COLUMN {name} {definition}")
    db.execute("UPDATE deployments SET updated_at=COALESCE(updated_at, created_at), log_revision=COALESCE(log_revision, 0), claim_generation=COALESCE(claim_generation, 0), state_revision=COALESCE(state_revision, 0)")
    db.execute("""CREATE TABLE IF NOT EXISTS deployment_log_batches (
        deployment_id TEXT NOT NULL, batch_id TEXT NOT NULL, created_at REAL NOT NULL,
        PRIMARY KEY (deployment_id, batch_id)
    )""")
    db.executescript("""
    CREATE INDEX IF NOT EXISTS idx_deployments_active_lease
      ON deployments(environment, status, lease_expires_at);
    CREATE TABLE IF NOT EXISTS deployment_audit_events (
      event_id INTEGER PRIMARY KEY AUTOINCREMENT,
      deployment_id TEXT NOT NULL,
      event_type TEXT NOT NULL,
      from_status TEXT,
      to_status TEXT,
      reason_code TEXT,
      details_json TEXT NOT NULL DEFAULT '{}',
      claim_generation INTEGER NOT NULL DEFAULT 0,
      state_revision INTEGER NOT NULL DEFAULT 0,
      created_at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_deployment_audit_events
      ON deployment_audit_events(deployment_id, event_id);
    """)


def init_deployment_db():
    """Initialize/migrate each database path once per process, never per poll."""
    path = os.path.abspath(os.environ.get("DEPLOYMENT_DB_PATH", "/data/deployments.db"))
    if path in _initialized_schema_paths:
        return
    with _schema_lock:
        if path in _initialized_schema_paths:
            return
        _initialize_deployment_db()
        _initialized_schema_paths.add(path)


def _row(row):
    if not row: return None
    data = dict(row)
    for key in ("created_at", "started_at", "finished_at", "updated_at", "claim_started_at", "heartbeat_at", "lease_expires_at", "reconciled_at"):
        data[key] = _iso(data[key])
    data["frontend_included"] = bool(data["frontend_included"])
    data["rollback_attempted"] = bool(data["rollback_attempted"])
    data["rollback_succeeded"] = bool(data["rollback_succeeded"])
    data["cancel_requested"] = bool(data["cancel_requested"])
    data["log_revision"] = int(data.get("log_revision") or 0)
    try:
        data["performance"] = json.loads(data.get("performance_json") or "{}")
    except (TypeError, ValueError):
        data["performance"] = {}
    data.pop("performance_json", None)
    data.pop("lease_token", None)
    data.pop("claim_token_hash", None)
    data.pop("log_text", None)
    return data


def _lease_seconds() -> int:
    return max(10, int(os.environ.get("DEPLOYMENT_CLAIM_LEASE_SECONDS", "45")))


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _begin_immediate(db, attempts: int = 3) -> None:
    """Acquire a short write transaction; never hold it across I/O or sleeps."""
    for attempt in range(attempts):
        try:
            db.execute("BEGIN IMMEDIATE")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt + 1 >= attempts:
                raise
            time.sleep((0.025 * (2 ** attempt)) + random.uniform(0, 0.015))


def _audit(db, row, event_type: str, to_status: str | None, reason_code: str, details: dict | None = None) -> None:
    db.execute(
        """INSERT INTO deployment_audit_events(
             deployment_id,event_type,from_status,to_status,reason_code,details_json,
             claim_generation,state_revision,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            row["deployment_id"], event_type, row["status"], to_status,
            reason_code, json.dumps(details or {}, ensure_ascii=False, sort_keys=True),
            int(row["claim_generation"] or 0), int(row["state_revision"] or 0), _now(),
        ),
    )


def _claim_is_valid(row, now: float | None = None) -> bool:
    current = _now() if now is None else now
    return bool(
        row["claim_owner"]
        and (row["claim_token_hash"] or row["claim_owner"] == DELEGATED_HANDOFF_OWNER)
        and row["heartbeat_at"]
        and row["lease_expires_at"]
        and float(row["heartbeat_at"]) <= current
        and float(row["lease_expires_at"]) > current
    )


def _claim_public(row, token: str = "") -> dict:
    result = _row(row)
    if token:
        result["claim_token"] = token
    return result


def claim_delegated_test_deployment(executor_id: str) -> dict:
    """Atomically transfer one queued-agent handoff to a delegated executor."""
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{3,128}", executor_id or ""):
        return _error_response("EXECUTOR_ID_INVALID", "executor_id must be a stable bounded identity")
    init_deployment_db(); db = _get_deploy_db(); now = _now()
    _begin_immediate(db)
    try:
        row = db.execute(
            """SELECT * FROM deployments
               WHERE repository=? AND current_step='claimed' AND artifact_id IS NULL
                 AND status IN ('claimed','running')
                 AND (
                   claim_owner=? OR lease_expires_at<=?
                 )
               ORDER BY created_at LIMIT 1""",
            (REPOSITORY, DELEGATED_HANDOFF_OWNER, now),
        ).fetchone()
        if not row:
            db.rollback()
            reconcile_stale_deployments()
            return {"ok": True, "deployment": None}
        token = secrets.token_urlsafe(32)
        generation = int(row["claim_generation"] or 0) + 1
        updated = db.execute(
            """UPDATE deployments
               SET status='claimed',claim_owner=?,claim_started_at=?,heartbeat_at=?,
                   lease_expires_at=?,claim_generation=?,claim_token_hash=?,lease_token=NULL,
                   started_at=COALESCE(started_at,?),updated_at=?,state_revision=state_revision+1
               WHERE deployment_id=? AND state_revision=?""",
            (
                executor_id, now, now, now + _lease_seconds(), generation,
                _token_hash(token), now, now, row["deployment_id"], int(row["state_revision"] or 0),
            ),
        )
        if updated.rowcount != 1:
            db.rollback()
            return _error_response("DEPLOYMENT_CAS_CONFLICT", "deployment was claimed concurrently")
        _audit(db, row, "claim_acquired", "claimed", "DELEGATED_EXECUTOR_CLAIMED", {"executor_id": executor_id})
        db.commit()
    except Exception:
        db.rollback()
        raise
    claimed = db.execute("SELECT * FROM deployments WHERE deployment_id=?", (row["deployment_id"],)).fetchone()
    return {"ok": True, "deployment": _claim_public(claimed, token)}


def heartbeat_test_deployment(deployment_id: str, claim_owner: str, claim_token: str, claim_generation: int) -> dict:
    init_deployment_db(); db = _get_deploy_db(); now = _now()
    _begin_immediate(db)
    try:
        row = db.execute("SELECT * FROM deployments WHERE deployment_id=?", (deployment_id,)).fetchone()
        if not row:
            db.rollback(); return _error_response("DEPLOYMENT_NOT_FOUND", "deployment not found")
        if row["status"] in TERMINAL_STATUSES:
            db.rollback(); return {"ok": True, "terminal": True, "deployment": _row(row)}
        if not _claim_matches(row, claim_owner, claim_token, claim_generation):
            db.rollback(); return _error_response("DEPLOYMENT_CLAIM_MISMATCH", "heartbeat does not own the current deployment claim")
        updated = db.execute(
            """UPDATE deployments SET heartbeat_at=?,lease_expires_at=?,updated_at=?,state_revision=state_revision+1
               WHERE deployment_id=? AND state_revision=?""",
            (now, now + _lease_seconds(), now, deployment_id, int(row["state_revision"] or 0)),
        )
        if updated.rowcount != 1:
            db.rollback(); return _error_response("DEPLOYMENT_CAS_CONFLICT", "deployment changed during heartbeat")
        db.commit()
    except Exception:
        db.rollback(); raise
    current = db.execute("SELECT status,cancel_requested FROM deployments WHERE deployment_id=?", (deployment_id,)).fetchone()
    return {"ok": True, "terminal": False, "status": current["status"],
            "cancel_requested": bool(current["cancel_requested"]),
            "lease_expires_at": _iso(now + _lease_seconds())}


def _reconciliation_evidence(row) -> dict:
    snapshot = _status_snapshot(row["repository"]) or {}
    current, releases = _release_snapshot(snapshot)
    target = next((item for item in releases if item.get("release_id") == row["target_release"]), None)
    return {
        "current_release_id": (current or {}).get("release_id"),
        "current_git_sha": (current or {}).get("git_sha"),
        "current_release_verified": bool(current),
        "target_release_verified": bool(target),
        "target_is_current": bool(current and current.get("release_id") == row["target_release"]),
        "target_git_sha_matches": bool(target and target.get("git_sha") == row["commit_sha"]),
    }


def reconcile_stale_test_deployment(deployment_id: str, automatic: bool = False) -> dict:
    """Fail-closed stale-claim reconciliation; callers cannot choose a status."""
    init_deployment_db(); db = _get_deploy_db()
    initial = db.execute("SELECT * FROM deployments WHERE deployment_id=?", (deployment_id,)).fetchone()
    if not initial:
        return _error_response("DEPLOYMENT_NOT_FOUND", "deployment not found")
    if initial["status"] in TERMINAL_STATUSES:
        return {"ok": True, "idempotent": True, "reconciled": False, "reason": "ALREADY_TERMINAL", "deployment": _row(initial)}
    if initial["status"] == "queued" or _claim_is_valid(initial):
        return {"ok": True, "idempotent": True, "reconciled": False, "reason": "CLAIM_NOT_STALE", "deployment": _row(initial)}

    evidence = _reconciliation_evidence(initial)
    now = _now()
    _begin_immediate(db)
    try:
        row = db.execute("SELECT * FROM deployments WHERE deployment_id=?", (deployment_id,)).fetchone()
        if row["status"] in TERMINAL_STATUSES:
            db.rollback()
            return {"ok": True, "idempotent": True, "reconciled": False, "reason": "ALREADY_TERMINAL", "deployment": _row(row)}
        if row["status"] == "queued" or _claim_is_valid(row, now):
            db.rollback()
            return {"ok": True, "idempotent": True, "reconciled": False, "reason": "CLAIM_RECOVERED", "deployment": _row(row)}

        safe_pre_switch = row["current_step"] in SAFE_PRE_SWITCH_STEPS
        if evidence["target_is_current"]:
            next_status = "failed"
            next_step = "operator_attention"
            error_code = "DEPLOYMENT_CALLBACK_LOST_AFTER_RELEASE_SWITCH"
            reason = "verified target release is current; preserving release and failing closed"
        elif not evidence["current_release_verified"] or not safe_pre_switch:
            next_status = "failed"
            next_step = "operator_attention"
            error_code = "DEPLOYMENT_STALE_OUTCOME_UNKNOWN"
            reason = "stale claim outcome cannot be proven safe from release evidence"
        elif row["status"] == "cancel_requested" or row["cancel_requested"]:
            next_status = "cancelled"
            next_step = "cancelled"
            error_code = "DEPLOYMENT_CANCELED_STALE_CLAIM"
            reason = "cancel requested; claim expired before release switch and verified current release differs"
        else:
            next_status = "failed"
            next_step = "stale_claim"
            error_code = "DEPLOYMENT_EXECUTOR_LOST"
            reason = "executor claim expired before release switch"
        log_text, log_revision = _append_deployment_log(db, row, f"reconciled stale deployment: {error_code}; {reason}")
        updated = db.execute(
            """UPDATE deployments
               SET status=?,current_step=?,exit_code=?,error_code=?,error_message=?,finished_at=?,updated_at=?,
                   reconciled_at=?,reconciliation_reason=?,claim_token_hash=NULL,lease_expires_at=NULL,
                   log_text=?,log_revision=?,state_revision=state_revision+1
               WHERE deployment_id=? AND state_revision=? AND status=?""",
            (
                next_status, next_step, 130 if next_status == "cancelled" else 1,
                error_code, reason, now, now, now, error_code, log_text, log_revision,
                deployment_id, int(row["state_revision"] or 0), row["status"],
            ),
        )
        if updated.rowcount != 1:
            db.rollback()
            return _error_response("DEPLOYMENT_CAS_CONFLICT", "deployment changed while reconciling stale claim")
        _audit(db, row, "stale_claim_reconciled_auto" if automatic else "stale_claim_reconciled", next_status, error_code, evidence)
        db.commit()
    except Exception:
        db.rollback(); raise
    settled = db.execute("SELECT * FROM deployments WHERE deployment_id=?", (deployment_id,)).fetchone()
    return {"ok": True, "idempotent": False, "reconciled": True, "reason": error_code, "evidence": evidence, "deployment": _row(settled)}


def reconcile_stale_deployments(environment: str = "", limit: int = 20) -> dict:
    init_deployment_db(); db = _get_deploy_db(); now = _now()
    clauses = ["status IN ('claimed','running','cancel_requested')", "(lease_expires_at IS NULL OR lease_expires_at<=?)"]
    params: list[object] = [now]
    if environment:
        clauses.append("environment=?"); params.append(environment)
    rows = db.execute(
        "SELECT deployment_id FROM deployments WHERE " + " AND ".join(clauses) + " ORDER BY updated_at LIMIT ?",
        (*params, min(max(int(limit), 1), 100)),
    ).fetchall()
    results = [reconcile_stale_test_deployment(row["deployment_id"], automatic=True) for row in rows]
    return {"ok": True, "examined": len(rows), "reconciled": sum(bool(item.get("reconciled")) for item in results), "results": results}


def _claim_matches(row, owner: str, token: str, generation: int) -> bool:
    return bool(
        owner and token
        and row["claim_owner"] == owner
        and int(row["claim_generation"] or 0) == int(generation)
        and secrets.compare_digest(str(row["claim_token_hash"] or ""), _token_hash(token))
    )


def _validate_common(repository, environment, scope, commit_sha):
    spec = _deployment_spec(repository)
    if not spec: return "REPOSITORY_NOT_ALLOWED"
    if environment != spec["environment"]: return "ENVIRONMENT_NOT_ALLOWED"
    if scope != spec["scope"]: return "SCOPE_NOT_ALLOWED"
    if not SHA_RE.fullmatch(commit_sha): return "INVALID_COMMIT_SHA"
    return None


def _append_deployment_log(db, row, message: str) -> tuple[str, int]:
    safe = str(message or "")[:4000]
    current = row["log_text"] or ""
    joined = (current + ("\n" if current else "") + safe)[-200000:]
    return joined, int(row["log_revision"] or 0) + 1


def _deployment_callback_update(
    deployment_id: str,
    current_step: str,
    message: str = "",
    status: str | None = None,
    exit_code: int | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    release: dict | None = None,
    claim_owner: str = "",
    claim_token: str = "",
    claim_generation: int = 0,
) -> dict:
    init_deployment_db(); db = _get_deploy_db()
    _begin_immediate(db)
    try:
        row = db.execute("SELECT * FROM deployments WHERE deployment_id=?", (deployment_id,)).fetchone()
        if not row:
            db.rollback()
            return _error_response("DEPLOYMENT_NOT_FOUND", "deployment not found")
        if row["status"] in TERMINAL_STATUSES:
            db.rollback()
            return {"ok": True, "idempotent": True, "deployment": _row(row)}
        if row["claim_owner"] and not _claim_matches(row, claim_owner, claim_token, claim_generation):
            db.rollback()
            return _error_response("DEPLOYMENT_CLAIM_MISMATCH", "callback does not own the current deployment claim")
        now = _now()
        if row["status"] == "cancel_requested" and status not in TERMINAL_STATUSES:
            next_status = "cancel_requested"
        else:
            next_status = status or ("running" if row["status"] in ("queued", "claimed", "preparing") else row["status"])
        if next_status not in STATUSES:
            db.rollback()
            return _error_response("DEPLOYMENT_STATUS_INVALID", "invalid deployment status")
        log_text, revision = _append_deployment_log(db, row, message)
        values = [next_status, current_step, now, revision, log_text]
        assignments = "status=?,current_step=?,updated_at=?,log_revision=?,log_text=?,state_revision=state_revision+1"
        if next_status in ("claimed", "running") and not row["started_at"]:
            assignments += ",started_at=?"; values.append(now)
        if row["claim_owner"]:
            assignments += ",heartbeat_at=?,lease_expires_at=?"
            values.extend([now, now + _lease_seconds()])
        if exit_code is not None:
            assignments += ",exit_code=?"; values.append(exit_code)
        if error_code is not None:
            assignments += ",error_code=?"; values.append(error_code)
        if error_message is not None:
            assignments += ",error_message=?"; values.append(str(error_message)[:1000])
        if next_status in TERMINAL_STATUSES:
            assignments += ",claim_token_hash=NULL,lease_expires_at=NULL"
        if release:
            assignments += ",current_release_path=?,current_git_sha=?"
            values.extend([release.get("current_release_path"), release.get("git_sha") or release.get("current_git_sha")])
            if release.get("release_id"):
                assignments += ",target_release=?"
                values.append(release.get("release_id"))
        values.extend([deployment_id, int(row["state_revision"] or 0)])
        updated = db.execute(
            f"UPDATE deployments SET {assignments} WHERE deployment_id=? AND state_revision=?",
            values,
        )
        if updated.rowcount != 1:
            db.rollback()
            return _error_response("DEPLOYMENT_CAS_CONFLICT", "deployment changed while applying callback")
        if next_status in TERMINAL_STATUSES:
            _audit(
                db, row, "deployment_terminal_callback", next_status,
                error_code or ("DEPLOYMENT_COMPLETED_VERIFIED" if next_status == "passed" else "DEPLOYMENT_TERMINAL"),
                {"release_id": (release or {}).get("release_id")} if release else {},
            )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"ok": True, "idempotent": False, "deployment": _row(db.execute("SELECT * FROM deployments WHERE deployment_id=?", (deployment_id,)).fetchone())}


def update_test_deployment_progress(deployment_id: str, current_step: str, message: str = "", status: str | None = None, release: dict | None = None,
                                    claim_owner: str = "", claim_token: str = "", claim_generation: int = 0) -> dict:
    return _deployment_callback_update(deployment_id, current_step, message, status=status, release=release,
                                       claim_owner=claim_owner, claim_token=claim_token, claim_generation=claim_generation)


def complete_test_deployment(deployment_id: str, exit_code: int, message: str = "", release: dict | None = None,
                             claim_owner: str = "", claim_token: str = "", claim_generation: int = 0) -> dict:
    if exit_code != 0:
        return fail_test_deployment(deployment_id, exit_code, "DEPLOYMENT_EXIT_NONZERO", message, claim_owner, claim_token, claim_generation)
    required_proof = ("release_id", "git_sha", "manifest_verified", "checksum_verified", "health_verified", "services_healthy")
    if not release or any(not release.get(key) for key in required_proof):
        return _error_response("RECONCILIATION_EVIDENCE_REQUIRED", "terminal success requires verified release, checksum, services and health evidence")
    init_deployment_db(); db = _get_deploy_db()
    row = db.execute("SELECT * FROM deployments WHERE deployment_id=?", (deployment_id,)).fetchone()
    if not row:
        return _error_response("DEPLOYMENT_NOT_FOUND", "deployment not found")
    release_path = str(release.get("current_release_path") or "")
    if (Path(release_path).name != str(release.get("release_id"))
            or release.get("repository") != row["repository"]
            or release.get("environment") != row["environment"]
            or release.get("git_sha") != row["commit_sha"]):
        return _error_response("RELEASE_EVIDENCE_INVALID", "release id, path, repository, environment and deployment SHA must agree")
    result = _deployment_callback_update(deployment_id, "completed", message, status="passed", exit_code=0, release=release,
                                         claim_owner=claim_owner, claim_token=claim_token, claim_generation=claim_generation)
    if result.get("ok") and not result.get("idempotent"):
        init_deployment_db(); db = _get_deploy_db(); now = _now()
        db.execute("UPDATE deployments SET finished_at=?,updated_at=?,current_step=? WHERE deployment_id=?", (now, now, "completed", deployment_id)); db.commit()
        result["deployment"] = _row(db.execute("SELECT * FROM deployments WHERE deployment_id=?", (deployment_id,)).fetchone())
        _persist_release_snapshot(release, row["repository"])
    return result


def fail_test_deployment(deployment_id: str, exit_code: int, error_code: str, error_message: str = "",
                         claim_owner: str = "", claim_token: str = "", claim_generation: int = 0) -> dict:
    result = _deployment_callback_update(deployment_id, "failed", error_message, status="failed", exit_code=exit_code, error_code=error_code, error_message=error_message,
                                         claim_owner=claim_owner, claim_token=claim_token, claim_generation=claim_generation)
    if result.get("ok") and not result.get("idempotent"):
        init_deployment_db(); db = _get_deploy_db(); now = _now()
        db.execute("UPDATE deployments SET finished_at=?,updated_at=? WHERE deployment_id=?", (now, now, deployment_id)); db.commit()
        result["deployment"] = _row(db.execute("SELECT * FROM deployments WHERE deployment_id=?", (deployment_id,)).fetchone())
    return result


def _persist_release_snapshot(release: dict, repository: str = REPOSITORY) -> None:
    """Persist only verified release metadata; never persist environment secrets."""
    release_id = str(release.get("release_id") or "")
    release_path = str(release.get("current_release_path") or "")
    if (not release_id or Path(release_path).name != release_id
            or not release.get("manifest_verified")
            or not release.get("checksum_verified")
            or not release.get("health_verified")
            or not release.get("services_healthy")):
        return
    spec = _deployment_spec(repository)
    path = _deployment_status_path(repository)
    current = _status_snapshot(repository) or {}
    releases = current.get("releases") or []
    by_id = {
        item.get("release_id"): item for item in releases
        if isinstance(item, dict)
        and item.get("release_id")
        and Path(str(item.get("current_release_path") or "")).name == item.get("release_id")
        and item.get("manifest_verified") and item.get("checksum_verified")
        and item.get("health_verified") and item.get("services_healthy")
    }
    previous = current.get("current_release_id")
    item = {key: release.get(key) for key in (
        "release_id", "repository", "environment", "git_sha", "current_release_path", "created_at",
        "frontend_included", "manifest_verified", "checksum_verified", "health_verified", "services_healthy",
        "deployment_id", "status",
    ) if key in release}
    item.update({"is_current": True, "is_previous": False})
    if item.get("release_id"):
        by_id[item["release_id"]] = item
    for value in by_id.values():
        value["is_current"] = value.get("release_id") == item.get("release_id")
        value["is_previous"] = value.get("release_id") == previous and value.get("release_id") != item.get("release_id")
    current.update({
        "environment": spec.get("environment", ENVIRONMENT),
        "repository": repository,
        "current_release_id": item.get("release_id"),
        "previous_release_id": previous,
        "current_git_sha": item.get("git_sha"),
        "current_release_path": item.get("current_release_path"),
        "source": "verified_release_registry",
        "manifest_verified": True,
        "checksum_verified": True,
        "health_verified": True,
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "releases": list(by_id.values()),
    })
    safe = {key: value for key, value in current.items() if key not in {"env", "secrets", "credentials"}}
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(safe, handle, ensure_ascii=False)
        handle.write("\n")


def _repo_state(repository, commit_sha):
    gh = _get_gh()
    repo = gh.get_repo(repository)
    main_sha = repo.get_branch("main").commit.sha
    files = [f.filename for f in repo.get_commit(commit_sha).files]
    return main_sha, files


def _ci_gate(repository, job_id, commit_sha):
    spec = _deployment_spec(repository)
    if not spec.get("private_ci"):
        return None, {"required": False, "status": "not_required"}
    job = get_job(job_id)
    if not job: return "PRIVATE_CI_JOB_NOT_FOUND", None
    if job.get("repository") != repository: return "PRIVATE_CI_REPOSITORY_MISMATCH", job
    if job.get("branch") != "main": return "PRIVATE_CI_BRANCH_MISMATCH", job
    if job.get("commit_sha") != commit_sha: return "PRIVATE_CI_SHA_MISMATCH", job
    if job.get("profile") != spec.get("profile", PROFILE): return "PRIVATE_CI_PROFILE_MISMATCH", job
    if job.get("status") != "passed" or job.get("exit_code") != 0: return "PRIVATE_CI_NOT_PASSED", job
    if job.get("superseded_by_job_id"): return "PRIVATE_CI_SUPERSEDED", job
    return None, job


def plan_test_deployment(repository, environment, commit_sha, private_ci_job_id, scope=SCOPE, expected_current_release_id="", allow_deploy_infrastructure_changes=False, artifact_id=""):
    if artifact_id and os.environ.get("MYGITHUB10_ARTIFACT_DEPLOY_ENABLED", "false").lower() not in {"1", "true", "yes", "on"}: return {"ok": False, "error_code": "FEATURE_DISABLED", "ready": False}
    reason = _validate_common(repository, environment, scope, commit_sha)
    if reason: return {"ok": True, "ready": False, "reasons": [reason]}
    reasons = []
    try:
        main_sha, files = _repo_state(repository, commit_sha)
    except Exception as exc:
        return {"ok": True, "ready": False, "reasons": ["GITHUB_STATE_UNAVAILABLE"], "message": str(exc)}
    if commit_sha != main_sha: reasons.append("COMMIT_NOT_CURRENT_MAIN")
    ci_reason, ci_job = _ci_gate(repository, private_ci_job_id, commit_sha)
    if ci_reason: reasons.append(ci_reason)
    artifact = None
    if artifact_id:
        from app.attestation_registry import validate_artifact
        artifact_result = validate_artifact(artifact_id, repository=repository, branch="main", commit_sha=commit_sha, tree_sha="", private_ci_job_id=private_ci_job_id)
        artifact = artifact_result.get("artifact") if artifact_result.get("ok") else None
        if not artifact_result.get("ok"):
            reasons.append(artifact_result.get("error_code", "ARTIFACT_INVALID"))
    spec = _deployment_spec(repository)
    infra_changed = any(path == spec.get("script") or path == "scripts/deploy_gongshi_test.sh" or path == "scripts/sync_test_env.sh" or path.startswith("deploy/") for path in files)
    if infra_changed and not allow_deploy_infrastructure_changes: reasons.append("DEPLOY_INFRASTRUCTURE_CHANGE_REQUIRES_EXPLICIT_ALLOW")
    migrations_changed = any(path.startswith("db/migrations/") for path in files)
    snapshot = _status_snapshot(repository) or {}
    current_release, _ = _release_snapshot(snapshot)
    current_release_id = (current_release or {}).get("release_id")
    return {"ok": True, "ready": not reasons, "reasons": reasons, "repository": repository, "environment": environment,
            "commit_sha": commit_sha, "exact_main_sha": main_sha, "private_ci": ci_job,
            "current_release_id": current_release_id, "previous_release_id": snapshot.get("previous_release_id"),
            "current_git_sha": (current_release or {}).get("git_sha"),
            "current_release_path": (current_release or {}).get("current_release_path"),
            "target_release_id": f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{commit_sha[:12]}",
            "changed_files": files, "migrations_changed": migrations_changed, "deploy_infrastructure_changed": infra_changed,
            "artifact_id": artifact_id or None, "artifact": artifact,
            "backend_components": [] if repository == "frankichen/auto_gupiao" else ["lenshub-api", "lenshub-worker", "lenshub-scheduler", "lenshub-admin-bootstrap", "goose"],
            "frontend_components": [] if repository == "frankichen/auto_gupiao" else ["admin", "newadmin", "root", "app", "h5", "tester", "docs", "openapi"],
            "restart_services": ["auto-gupiao"] if repository == "frankichen/auto_gupiao" else ["api", "worker", "scheduler", "web"],
            "health_endpoints": ["/"] if repository == "frankichen/auto_gupiao" else ["/public/v1/health", "/nginx-health", "/", "/app/", "/h5/", "/admin/", "/newadmin/", "/tester/", "/docs/"],
            "rollback_plan": "flock 后恢复 previous current；不执行 goose down，不删除 release", "expected_current_release_id": expected_current_release_id}


def start_test_deployment(repository, environment, commit_sha, private_ci_job_id, scope=SCOPE, expected_current_release_id="", allow_deploy_infrastructure_changes=False, force_redeploy=False, confirm=False, requested_by="mcp", artifact_id=""):
    if artifact_id and os.environ.get("MYGITHUB10_ARTIFACT_DEPLOY_ENABLED", "false").lower() not in {"1", "true", "yes", "on"}: return _error_response("FEATURE_DISABLED", "artifact deployment is disabled")
    if not confirm: return _error_response("CONFIRM_REQUIRED", "confirm must be true")
    plan = plan_test_deployment(repository, environment, commit_sha, private_ci_job_id, scope, expected_current_release_id, allow_deploy_infrastructure_changes, artifact_id)
    if not plan.get("ready"): return {**plan, "error": {"code": plan.get("reasons", ["NOT_READY"])[0]}}
    if not private_ci_job_id and not _deployment_spec(repository).get("private_ci"):
        private_ci_job_id = "not_required"
    reconcile_stale_deployments(environment)
    init_deployment_db(); db = _get_deploy_db()
    with _lock:
        active_statuses = sorted(ACTIVE_STATUSES)
        active = db.execute(
            "SELECT deployment_id FROM deployments WHERE environment=? AND status IN (" + ",".join("?" for _ in active_statuses) + ") ORDER BY created_at LIMIT 1",
            (environment, *active_statuses),
        ).fetchone()
        if active: return _error_response("DEPLOYMENT_ALREADY_ACTIVE", "another deployment is active", details={"deployment_id": active[0]})
        if not force_redeploy and db.execute("SELECT 1 FROM deployments WHERE environment=? AND commit_sha=? AND status='passed'", (environment, commit_sha)).fetchone():
            return _error_response("ALREADY_DEPLOYED", "same SHA already passed")
        deployment_id = "dep_" + uuid.uuid4().hex
        now = _now()
        db.execute("INSERT INTO deployments(deployment_id,repository,environment,commit_sha,private_ci_job_id,requested_scope,current_release_before,target_release,status,created_at,updated_at,requested_by,frontend_included,artifact_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1,?)", (deployment_id, repository, environment, commit_sha, private_ci_job_id, scope, plan.get("current_release_id"), plan["target_release_id"], "queued", now, now, requested_by, artifact_id or None))
        db.commit()
    return {"ok": True, "deployment_id": deployment_id, "status": "queued", "target_release_id": plan["target_release_id"], "message": "queued for private-deploy-agent"}


def get_test_deployment(deployment_id):
    init_deployment_db(); row = _get_deploy_db().execute("SELECT * FROM deployments WHERE deployment_id=?", (deployment_id,)).fetchone()
    if not row:
        return _error_response("DEPLOYMENT_NOT_FOUND", "deployment not found")
    deployment = _row(row)
    # Current release identity is authoritative in the verified registry/status
    # snapshot, not in a possibly stale deployment row.  Keep this response
    # consistent with environment status, planning, and release listing.
    snapshot = _status_snapshot() or {}
    current, _ = _release_snapshot(snapshot)
    if current:
        deployment.update({
            "current_release_id": current.get("release_id"),
            "current_git_sha": current.get("git_sha"),
            "current_release_path": current.get("current_release_path"),
            "previous_release_id": snapshot.get("previous_release_id"),
        })
    return {"ok": True, "deployment": deployment}


def get_test_deployment_logs(deployment_id, offset=0, limit=200):
    init_deployment_db(); row = _get_deploy_db().execute("SELECT log_text FROM deployments WHERE deployment_id=?", (deployment_id,)).fetchone()
    if not row: return _error_response("DEPLOYMENT_NOT_FOUND", "deployment not found")
    text = row[0] or ""; raw = text.encode("utf-8"); page_bytes = raw[offset:offset + min(max(limit, 1), 100000)]
    page = page_bytes.decode("utf-8", errors="replace")
    next_offset = offset + len(page_bytes) if offset + len(page_bytes) < len(raw) else None
    return {"ok": True, "deployment_id": deployment_id, "offset": offset, "limit": limit, "content": page, "next_offset": next_offset, "total_bytes": len(raw)}


def append_deployment_log_batch(deployment_id: str, batch_id: str, content: str) -> dict:
    """Append one idempotent log batch in one SQLite transaction."""
    if not batch_id or not content:
        return _error_response("INVALID_LOG_BATCH", "batch_id and content are required")
    init_deployment_db(); db = _get_deploy_db(); _begin_immediate(db)
    try:
        row = db.execute("SELECT log_text, log_revision FROM deployments WHERE deployment_id=?", (deployment_id,)).fetchone()
        if not row:
            db.rollback(); return _error_response("DEPLOYMENT_NOT_FOUND", "deployment not found")
        duplicate = db.execute("SELECT 1 FROM deployment_log_batches WHERE deployment_id=? AND batch_id=?", (deployment_id, batch_id)).fetchone()
        if duplicate:
            db.rollback()
            return {"ok": True, "idempotent": True, "deployment_id": deployment_id, "revision": int(row["log_revision"] or 0)}
        log_text, revision = _append_deployment_log(db, row, content)
        now = _now()
        db.execute("INSERT INTO deployment_log_batches(deployment_id,batch_id,created_at) VALUES(?,?,?)", (deployment_id, batch_id, now))
        db.execute("UPDATE deployments SET log_text=?,log_revision=?,updated_at=?,state_revision=state_revision+1 WHERE deployment_id=?", (log_text, revision, now, deployment_id))
        db.commit()
    except Exception:
        db.rollback(); raise
    return {"ok": True, "idempotent": False, "deployment_id": deployment_id, "revision": revision}


def wait_test_deployment(deployment_id: str, timeout_seconds: int = 55, last_known_status: str = "",
                         last_known_step: str = "", last_known_revision: int = 0) -> dict:
    """Long-poll deployment metadata without returning logs or lease material."""
    timeout = min(max(int(timeout_seconds), 1), 55)
    started = time.monotonic()
    while True:
        init_deployment_db(); db = _get_deploy_db()
        row = db.execute("SELECT status,current_step,log_revision FROM deployments WHERE deployment_id=?", (deployment_id,)).fetchone()
        if not row:
            return _error_response("DEPLOYMENT_NOT_FOUND", "deployment not found")
        status = row["status"]
        step = row["current_step"]
        revision = int(row["log_revision"] or 0)
        terminal = status in {"passed", "failed", "cancelled", "rollback_failed", "rolled_back"}
        changed = status != last_known_status or step != last_known_step or revision != int(last_known_revision or 0)
        if changed or terminal or time.monotonic() - started >= timeout:
            return {"ok": True, "changed": changed, "timed_out": not changed and not terminal,
                    "deployment_id": deployment_id, "status": status, "current_step": step,
                    "revision": revision, "terminal": terminal,
                    "elapsed_seconds": round(time.monotonic() - started, 3)}
        time.sleep(min(0.5, max(0.05, timeout - (time.monotonic() - started))))


def get_test_deployment_log_tail(deployment_id: str, lines: int = 100) -> dict:
    init_deployment_db(); row = _get_deploy_db().execute("SELECT log_text FROM deployments WHERE deployment_id=?", (deployment_id,)).fetchone()
    if not row:
        return _error_response("DEPLOYMENT_NOT_FOUND", "deployment not found")
    safe_lines = min(max(int(lines), 1), 500)
    content = "\n".join((row[0] or "").splitlines()[-safe_lines:])
    return {"ok": True, "deployment_id": deployment_id, "lines": safe_lines, "content": content}


def list_delegated_deployments(repository="", environment="", executor_id=""):
    init_deployment_db()
    clauses = ["status IN ('running','claimed')", "current_step='claimed'", "lease_expires_at>?", "claim_token_hash IS NOT NULL"]
    params = [_now()]
    if executor_id:
        clauses.append("claim_owner=?")
        params.append(executor_id)
    if repository:
        clauses.append("repository=?")
        params.append(repository)
    else:
        allowed = _deployable_repositories()
        if not allowed:
            return []
        clauses.append("repository IN (" + ",".join("?" for _ in allowed) + ")")
        params.extend(allowed)
    if environment:
        clauses.append("environment=?")
        params.append(environment)
    rows = _get_deploy_db().execute(
        "SELECT * FROM deployments WHERE " + " AND ".join(clauses) + " ORDER BY created_at",
        params,
    ).fetchall()
    return [_row(row) for row in rows]


def list_test_deployments(repository="", environment="", commit_sha="", status="", limit=20, offset=0):
    init_deployment_db(); clauses=[]; params=[]
    for col, value in (("repository",repository),("environment",environment),("commit_sha",commit_sha),("status",status)):
        if value: clauses.append(col+"=?"); params.append(value)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = _get_deploy_db().execute("SELECT * FROM deployments" + where + " ORDER BY created_at DESC LIMIT ? OFFSET ?", (*params, min(max(limit, 1), 100), max(offset,0))).fetchall()
    return {"ok": True, "items": [_row(row) for row in rows], "limit": limit, "offset": offset}


def cancel_test_deployment(deployment_id):
    init_deployment_db(); db = _get_deploy_db(); now = _now()
    _begin_immediate(db)
    try:
        row = db.execute("SELECT * FROM deployments WHERE deployment_id=?", (deployment_id,)).fetchone()
        if not row:
            db.rollback(); return _error_response("DEPLOYMENT_NOT_FOUND", "deployment not found")
        if row["status"] == "cancelled":
            db.rollback(); result = get_test_deployment(deployment_id); result["idempotent"] = True; return result
        if row["status"] in TERMINAL_STATUSES:
            db.rollback(); return _error_response("DEPLOYMENT_NOT_CANCELLABLE", "deployment already finished")
        if row["status"] == "queued":
            next_status = "cancelled"
            db.execute(
                """UPDATE deployments SET status='cancelled',current_step='cancelled',cancel_requested=1,
                   finished_at=?,updated_at=?,state_revision=state_revision+1 WHERE deployment_id=? AND state_revision=?""",
                (now, now, deployment_id, int(row["state_revision"] or 0)),
            )
            _audit(db, row, "cancelled", next_status, "DEPLOYMENT_CANCELED_BEFORE_CLAIM")
        else:
            next_status = "cancel_requested"
            db.execute(
                """UPDATE deployments SET status='cancel_requested',cancel_requested=1,updated_at=?,
                   state_revision=state_revision+1 WHERE deployment_id=? AND state_revision=?""",
                (now, deployment_id, int(row["state_revision"] or 0)),
            )
            _audit(db, row, "cancel_requested", next_status, "DEPLOYMENT_CANCEL_REQUESTED")
        db.commit()
    except Exception:
        db.rollback(); raise
    if next_status == "cancel_requested":
        reconcile_stale_test_deployment(deployment_id, automatic=True)
    return get_test_deployment(deployment_id)


def get_test_deployment_audit(deployment_id: str, limit: int = 100) -> dict:
    init_deployment_db(); db = _get_deploy_db()
    if not db.execute("SELECT 1 FROM deployments WHERE deployment_id=?", (deployment_id,)).fetchone():
        return _error_response("DEPLOYMENT_NOT_FOUND", "deployment not found")
    rows = db.execute(
        """SELECT event_id,event_type,from_status,to_status,reason_code,details_json,
                  claim_generation,state_revision,created_at
           FROM deployment_audit_events WHERE deployment_id=? ORDER BY event_id DESC LIMIT ?""",
        (deployment_id, min(max(int(limit), 1), 500)),
    ).fetchall()
    items = []
    for row in rows:
        item = dict(row); item["created_at"] = _iso(item["created_at"])
        try: item["details"] = json.loads(item.pop("details_json"))
        except (TypeError, ValueError): item["details"] = {}
        items.append(item)
    return {"ok": True, "deployment_id": deployment_id, "items": items}


def _status_snapshot(repository=""):
    path = _deployment_status_path(repository) if repository else os.environ.get("DEPLOY_STATUS_FILE", "/data/gongshi-test-status.json")
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
        # 仅接受状态摘要，拒绝把 env/credential 内容带回 MCP。
        value.pop("env", None); value.pop("secrets", None); value.pop("credentials", None)
        return value
    except (OSError, ValueError):
        return None


def _release_snapshot(snapshot: dict) -> tuple[dict | None, list[dict]]:
    """Normalize current/release data so all deployment tools share one source."""
    current_id = snapshot.get("current_release_id")
    previous_id = snapshot.get("previous_release_id")
    raw = snapshot.get("releases") or snapshot.get("release_registry") or []
    releases = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict) or not item.get("release_id") or item.get("is_incoming"):
            continue
        if not item.get("manifest_verified") or not item.get("checksum_verified") or not item.get("health_verified"):
            continue
        if Path(str(item.get("current_release_path") or "")).name != item.get("release_id"):
            continue
        normalized = dict(item)
        normalized["is_current"] = normalized.get("release_id") == current_id
        normalized["is_previous"] = normalized.get("release_id") == previous_id
        releases.append(normalized)
    current = next((item for item in releases if item.get("release_id") == current_id), None)
    # A status file may be stale or may only contain a claimed target.  Never
    # promote that claim to an authoritative current release without proof
    # from manifest/checksum/health verification.
    if (current_id and current is None
            and Path(str(snapshot.get("current_release_path") or "")).name == current_id
            and all(snapshot.get(key) is True for key in (
                "manifest_verified", "checksum_verified", "health_verified"
            ))):
        current = {
            "release_id": current_id,
            "repository": snapshot.get("repository", REPOSITORY),
            "environment": snapshot.get("environment", ENVIRONMENT),
            "git_sha": snapshot.get("current_git_sha"),
            "current_release_path": snapshot.get("current_release_path"),
            "is_current": True,
            "is_previous": False,
            "manifest_verified": bool(snapshot.get("manifest_verified")),
            "checksum_verified": bool(snapshot.get("checksum_verified")),
            "health_verified": bool(snapshot.get("health_verified")),
            "status": "passed" if snapshot.get("health_verified") else "unknown",
        }
        releases.insert(0, current)
    return current, releases


def get_test_environment_status(repository, environment):
    spec = _deployment_spec(repository)
    reason = _validate_common(repository, environment, spec.get("scope", SCOPE), "0" * 40)
    if reason == "INVALID_COMMIT_SHA": reason = None
    if reason: return _error_response(reason, "repository, environment or scope is not allowed")
    snapshot = _status_snapshot(repository) or {}
    current, releases = _release_snapshot(snapshot)
    status = dict(snapshot)
    status.update({
        "current_release_id": (current or {}).get("release_id"),
        "current_git_sha": (current or {}).get("git_sha") or status.get("current_git_sha"),
        "current_release_path": (current or {}).get("current_release_path") or status.get("current_release_path"),
        "source": "verified_release_registry" if current else status.get("source", "status_file"),
        "verified_at": status.get("verified_at") or status.get("updated_at"),
        "releases": releases,
    })
    return {"ok": True, "environment": environment, "status_available": bool(snapshot), "status": status or {"current_release_id": None, "message": "deploy worker status is not configured"}}


def list_test_releases(repository, environment, limit=20):
    spec = _deployment_spec(repository)
    if not spec: return _error_response("REPOSITORY_NOT_ALLOWED", "repository is not allowed")
    if environment != spec.get("environment"): return _error_response("ENVIRONMENT_NOT_ALLOWED", "environment is not allowed")
    snapshot = _status_snapshot(repository) or {}
    _, releases = _release_snapshot(snapshot)
    return {"ok": True, "items": releases[:min(max(limit, 1), 100)]}


def rollback_test_deployment(repository, environment, target_release_id, expected_current_release_id, confirm=False, requested_by="mcp"):
    if not confirm: return _error_response("CONFIRM_REQUIRED", "confirm must be true")
    spec = _deployment_spec(repository)
    if not spec: return _error_response("REPOSITORY_NOT_ALLOWED", "repository is not allowed")
    if environment != spec.get("environment"): return _error_response("ENVIRONMENT_NOT_ALLOWED", "environment is not allowed")
    snapshot = _status_snapshot(repository) or {}
    current = snapshot.get("current_release_id")
    if not current: return _error_response("ENVIRONMENT_STATUS_UNAVAILABLE", "deploy worker status is unavailable")
    if current != expected_current_release_id: return _error_response("CURRENT_RELEASE_CHANGED", "current release changed", details={"current_release_id": current})
    if target_release_id == current: return _error_response("TARGET_IS_CURRENT", "target release is already current")
    _, verified_releases = _release_snapshot(snapshot)
    releases = {r.get("release_id"): r for r in verified_releases}
    target = releases.get(target_release_id)
    if not target: return _error_response("RELEASE_NOT_FOUND", "target release not found")
    if not target.get("checksum_verified"): return _error_response("RELEASE_CHECKSUM_FAILED", "target release checksum is not verified")
    init_deployment_db(); db=_get_deploy_db(); deployment_id="dep_"+uuid.uuid4().hex; now=_now()
    db.execute("INSERT INTO deployments(deployment_id,repository,environment,commit_sha,private_ci_job_id,requested_scope,current_release_before,target_release,status,created_at,requested_by,frontend_included) VALUES(?,?,?,?,?,?,?,?,?,?,?,1)", (deployment_id,repository,environment,target.get("git_sha", ""),"not_applicable", "rollback", current,target_release_id,"queued",now,requested_by))
    db.commit()
    return {"ok": True, "deployment_id": deployment_id, "type": "rollback", "status": "queued", "target_release_id": target_release_id}
