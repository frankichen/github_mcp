"""CI database initialization and operations."""

import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from app.ci_models import CIJobStatus, ALLOWED_PRIORITIES, make_idempotency_key

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("CI_DB_PATH", "/data/ci.db")

_local = threading.local()
_job_change_condition = threading.Condition()
# SQLite serializes writers.  The controller runs one Uvicorn process, so a
# small in-process lock keeps request threads from competing for the same
# write transaction.  WAL still allows concurrent readers.
_db_write_lock = threading.RLock()


def _notify_job_change(job_id: str) -> None:
    with _job_change_condition:
        _job_change_condition.notify_all()


def _get_db():
    import sqlite3
    if not hasattr(_local, "db") or _local.db is None:
        db = sqlite3.connect(DB_PATH, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=15000")
        db.execute("PRAGMA foreign_keys=ON")
        _local.db = db
    return _local.db


def init_db():
    db = _get_db()
    # Set this once during startup.  Running journal_mode=WAL on every new
    # request-thread connection can itself require a database write lock.
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS ci_workers (
            worker_id TEXT PRIMARY KEY,
            token_hash TEXT NOT NULL,
            supported_profiles TEXT NOT NULL DEFAULT '[]',
            max_concurrent INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'idle',
            current_job_id TEXT,
            last_heartbeat REAL NOT NULL DEFAULT 0,
            registered_at REAL NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS ci_jobs (
            job_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL,
            repository TEXT NOT NULL,
            branch TEXT NOT NULL DEFAULT '',
            commit_sha TEXT NOT NULL,
            base_sha TEXT NOT NULL DEFAULT '',
            changed_files_json TEXT NOT NULL DEFAULT '[]',
            changed_files_total INTEGER NOT NULL DEFAULT 0,
            changed_files_truncated INTEGER NOT NULL DEFAULT 0,
            performance_json TEXT NOT NULL DEFAULT '{}',
            profile TEXT NOT NULL,
            profile_version TEXT NOT NULL DEFAULT 'v1',
            priority INTEGER NOT NULL DEFAULT 100,
            status TEXT NOT NULL DEFAULT 'queued',
            worker_id TEXT,
            lease_token_hash TEXT,
            lease_expires_at REAL,
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            superseded_by_job_id TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 2,
            exit_code INTEGER,
            summary_json TEXT,
            source_sha256 TEXT,
            source_size_bytes INTEGER,
            timeout_seconds INTEGER NOT NULL DEFAULT 900,
            error_code TEXT,
            error_message TEXT,
            created_at REAL NOT NULL,
            queued_at REAL,
            started_at REAL,
            finished_at REAL,
            duration_seconds REAL,
            log_total_bytes INTEGER NOT NULL DEFAULT 0,
            log_truncated INTEGER NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_ci_jobs_idempotency ON ci_jobs(idempotency_key);
        CREATE INDEX IF NOT EXISTS idx_ci_jobs_repository ON ci_jobs(repository);
        CREATE INDEX IF NOT EXISTS idx_ci_jobs_status ON ci_jobs(status);
        CREATE INDEX IF NOT EXISTS idx_ci_jobs_worker ON ci_jobs(worker_id);
        CREATE INDEX IF NOT EXISTS idx_ci_jobs_commit ON ci_jobs(commit_sha);
        CREATE INDEX IF NOT EXISTS idx_ci_jobs_priority_created ON ci_jobs(priority, created_at);

        CREATE TABLE IF NOT EXISTS ci_job_steps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            attempt_number INTEGER NOT NULL DEFAULT 0,
            step_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            exit_code INTEGER,
            started_at REAL,
            finished_at REAL,
            duration_seconds REAL,
            log_start_offset INTEGER NOT NULL DEFAULT 0,
            log_end_offset INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (job_id) REFERENCES ci_jobs(job_id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_ci_job_steps_job ON ci_job_steps(job_id);

        CREATE TABLE IF NOT EXISTS ci_job_log_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            offset_from INTEGER NOT NULL,
            offset_to INTEGER NOT NULL,
            content TEXT NOT NULL,
            created_at REAL NOT NULL,
            FOREIGN KEY (job_id) REFERENCES ci_jobs(job_id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_ci_job_log_chunks_job ON ci_job_log_chunks(job_id, chunk_index);

        CREATE TABLE IF NOT EXISTS ci_job_log_batches (
            job_id TEXT NOT NULL, batch_id TEXT NOT NULL, created_at REAL NOT NULL,
            PRIMARY KEY (job_id, batch_id), FOREIGN KEY (job_id) REFERENCES ci_jobs(job_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS ci_repository_queue_state (
            repository TEXT PRIMARY KEY,
            last_dispatched_at REAL NOT NULL DEFAULT 0,
            running_jobs INTEGER NOT NULL DEFAULT 0,
            queued_jobs INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS ci_job_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            event_data TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            FOREIGN KEY (job_id) REFERENCES ci_jobs(job_id) ON DELETE CASCADE
        );
    """)
    columns = {row[1] for row in db.execute("PRAGMA table_info(ci_jobs)").fetchall()}
    if "base_sha" not in columns:
        db.execute("ALTER TABLE ci_jobs ADD COLUMN base_sha TEXT NOT NULL DEFAULT ''")
    if "changed_files_json" not in columns:
        db.execute("ALTER TABLE ci_jobs ADD COLUMN changed_files_json TEXT NOT NULL DEFAULT '[]'")
    if "changed_files_total" not in columns:
        db.execute("ALTER TABLE ci_jobs ADD COLUMN changed_files_total INTEGER NOT NULL DEFAULT 0")
    if "changed_files_truncated" not in columns:
        db.execute("ALTER TABLE ci_jobs ADD COLUMN changed_files_truncated INTEGER NOT NULL DEFAULT 0")
    if "performance_json" not in columns:
        db.execute("ALTER TABLE ci_jobs ADD COLUMN performance_json TEXT NOT NULL DEFAULT '{}'")
    step_columns = {row[1] for row in db.execute("PRAGMA table_info(ci_job_steps)").fetchall()}
    if "attempt_number" not in step_columns:
        db.execute("ALTER TABLE ci_job_steps ADD COLUMN attempt_number INTEGER NOT NULL DEFAULT 0")
    db.commit()


def now_ts() -> float:
    return time.time()


### WORKER OPERATIONS

def register_worker(worker_id: str, token: str, profiles: list[str], max_concurrent: int) -> bool:
    db = _get_db()
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    profiles_json = json.dumps(profiles)
    ts = now_ts()
    try:
        db.execute(
            """INSERT INTO ci_workers (worker_id, token_hash, supported_profiles, max_concurrent, status, last_heartbeat, registered_at, metadata_json)
               VALUES (?, ?, ?, ?, 'idle', ?, ?, '{}')
               ON CONFLICT(worker_id) DO UPDATE SET
               token_hash=excluded.token_hash,
               supported_profiles=excluded.supported_profiles,
               max_concurrent=excluded.max_concurrent,
               last_heartbeat=excluded.last_heartbeat,
               registered_at=registered_at,
               status=CASE WHEN ci_workers.current_job_id IS NULL THEN 'idle' ELSE ci_workers.status END""",
            (worker_id, token_hash, profiles_json, max_concurrent, ts, ts),
        )
        db.commit()
        return True
    except sqlite3.Error:
        logger.exception("register_worker database write failed: worker_id=%s", worker_id)
        return False
    except Exception:
        logger.exception("register_worker failed: worker_id=%s", worker_id)
        return False


def verify_worker_token(worker_id: str, token: str) -> bool:
    db = _get_db()
    row = db.execute("SELECT token_hash FROM ci_workers WHERE worker_id = ?", (worker_id,)).fetchone()
    if not row:
        return False
    expected_hash = hashlib.sha256(token.encode()).hexdigest()
    return hmac.compare_digest(row["token_hash"], expected_hash)


def update_worker_heartbeat(worker_id: str) -> bool:
    db = _get_db()
    ts = now_ts()
    with _db_write_lock:
        db.execute("UPDATE ci_workers SET last_heartbeat = ? WHERE worker_id = ?", (ts, worker_id))
        db.commit()
    return True


def set_worker_busy(worker_id: str, job_id: str):
    db = _get_db()
    db.execute("UPDATE ci_workers SET status = 'busy', current_job_id = ? WHERE worker_id = ?", (job_id, worker_id))


def set_worker_idle(worker_id: str):
    db = _get_db()
    db.execute("UPDATE ci_workers SET status = 'idle', current_job_id = NULL WHERE worker_id = ?", (worker_id,))


def get_workers() -> list[dict]:
    db = _get_db()
    rows = db.execute("SELECT * FROM ci_workers ORDER BY worker_id").fetchall()
    ts = now_ts()
    return [_worker_row_to_dict(r, ts) for r in rows]


def get_worker(worker_id: str) -> Optional[dict]:
    db = _get_db()
    row = db.execute("SELECT * FROM ci_workers WHERE worker_id = ?", (worker_id,)).fetchone()
    if not row:
        return None
    return _worker_row_to_dict(row, now_ts())


def get_current_lease_attempt(job_id: str, worker_id: str, lease_token: str) -> Optional[int]:
    """Return the current attempt only when the worker holds an unexpired live lease."""
    if not lease_token:
        return None
    db = _get_db()
    token_hash = hashlib.sha256(lease_token.encode()).hexdigest()
    row = db.execute(
        """SELECT attempts FROM ci_jobs
           WHERE job_id = ? AND worker_id = ? AND lease_token_hash = ?
           AND status IN ('leased', 'downloading', 'preparing', 'running')
           AND lease_expires_at >= ?""",
        (job_id, worker_id, token_hash, now_ts()),
    ).fetchone()
    return int(row["attempts"]) if row else None


def _worker_row_to_dict(row, ts) -> dict:
    heartbeat_age = ts - row["last_heartbeat"]
    online = heartbeat_age < 60
    result = {
        "worker_id": row["worker_id"],
        "online": online,
        "last_heartbeat": datetime.fromtimestamp(row["last_heartbeat"], tz=timezone.utc).isoformat() if row["last_heartbeat"] else None,
        "supported_profiles": json.loads(row["supported_profiles"]),
        "max_concurrent": row["max_concurrent"],
        "current_job": row["current_job_id"],
        "status": row["status"],
    }
    return result


def recover_worker_jobs(worker_id: str):
    """Release all jobs leased to a lost worker."""
    db = _get_db()
    db.execute(
        """UPDATE ci_jobs SET status = 'queued', worker_id = NULL, lease_token_hash = NULL, lease_expires_at = NULL
           WHERE worker_id = ? AND status IN ('leased', 'downloading', 'preparing', 'running')""",
        (worker_id,),
    )
    db.commit()


### JOB OPERATIONS

def create_or_get_job(
    repository: str,
    branch: str,
    commit_sha: str,
    profile: str,
    priority: int,
    timeout_seconds: int,
    force_rerun: bool,
    supersede_previous: bool,
    base_sha: str = "",
    changed_files: Optional[list[str]] = None,
    changed_files_total: Optional[int] = None,
    changed_files_truncated: bool = False,
) -> dict:
    db = _get_db()
    idem_key = make_idempotency_key(repository, commit_sha, profile)
    ts = now_ts()

    # The idempotency read and insert must be one transaction.  Otherwise two
    # concurrent starts can both pass the read, and a writer such as a worker
    # heartbeat can leave the request with a partially completed operation.
    with _db_write_lock:
        db.execute("BEGIN IMMEDIATE")
        try:
            return _create_or_get_job_in_transaction(
                db, repository, branch, commit_sha, profile, priority,
                timeout_seconds, force_rerun, supersede_previous, base_sha,
                changed_files, changed_files_total, changed_files_truncated,
                idem_key, ts,
            )
        except Exception:
            db.rollback()
            raise


def _create_or_get_job_in_transaction(
    db, repository, branch, commit_sha, profile, priority, timeout_seconds,
    force_rerun, supersede_previous, base_sha, changed_files,
    changed_files_total, changed_files_truncated, idem_key, ts,
) -> dict:

    if not force_rerun:
        row = db.execute(
            """SELECT * FROM ci_jobs WHERE idempotency_key = ? AND status IN ('queued', 'leased', 'downloading', 'preparing', 'running', 'passed')
               ORDER BY created_at DESC LIMIT 1""",
            (idem_key,),
        ).fetchone()
        if row:
            result = {
                "job_id": row["job_id"],
                "idempotency_key": idem_key,
                "repository": row["repository"],
                "branch": row["branch"],
                "commit_sha": row["commit_sha"],
                "base_sha": row["base_sha"],
                "changed_files": json.loads(row["changed_files_json"] or "[]"),
                "changed_files_total": row["changed_files_total"],
                "changed_files_truncated": bool(row["changed_files_truncated"]),
                "profile": row["profile"],
                "status": row["status"],
                "priority": row["priority"],
                "queue_position": _get_queue_position(db, row["job_id"]),
                "worker_online": _any_worker_online(db),
                "deduplicated": True,
                "previous_job_id": None,
                "queued_count": _count_queued(db),
                "created_at": datetime.fromtimestamp(row["created_at"], tz=timezone.utc).isoformat(),
            }
            db.commit()
            return result

    if supersede_previous:
        db.execute(
            """UPDATE ci_jobs SET status = 'superseded'
               WHERE repository = ? AND branch = ? AND profile = ? AND status = 'queued' AND idempotency_key != ?""",
            (repository, branch, profile, idem_key),
        )

    job_id = uuid.uuid4().hex[:16]
    db.execute(
        """INSERT INTO ci_jobs (job_id, idempotency_key, repository, branch, commit_sha, base_sha, changed_files_json,
           changed_files_total, changed_files_truncated, profile, priority, status, timeout_seconds, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)""",
        (job_id, idem_key, repository, branch, commit_sha, base_sha, json.dumps(changed_files or []),
         int(changed_files_total if changed_files_total is not None else len(changed_files or [])),
         int(bool(changed_files_truncated)), profile, priority, timeout_seconds, ts),
    )
    _upsert_repo_queue_state(db, repository, queued_delta=1)
    db.commit()

    # Verify the durable row on the same connection before reporting success.
    # This turns any unexpected persistence failure into an error response,
    # never a false-positive job id.
    persisted = db.execute("SELECT 1 FROM ci_jobs WHERE job_id = ?", (job_id,)).fetchone()
    if not persisted:
        raise sqlite3.OperationalError("CI job commit verification failed")

    return {
        "job_id": job_id,
        "idempotency_key": idem_key,
        "repository": repository,
        "branch": branch,
        "commit_sha": commit_sha,
        "base_sha": base_sha,
        "changed_files": changed_files or [],
        "changed_files_total": int(changed_files_total if changed_files_total is not None else len(changed_files or [])),
        "changed_files_truncated": bool(changed_files_truncated),
        "profile": profile,
        "status": "queued",
        "priority": priority,
        "queue_position": _get_queue_position(db, job_id),
        "worker_online": _any_worker_online(db),
        "deduplicated": False,
        "previous_job_id": None,
        "queued_count": _count_queued(db),
        "created_at": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
    }


def _get_queue_position(db, job_id: str) -> int:
    row = db.execute("SELECT priority, created_at FROM ci_jobs WHERE job_id = ?", (job_id,)).fetchone()
    if not row:
        return 0
    count = db.execute(
        "SELECT COUNT(*) FROM ci_jobs WHERE status = 'queued' AND (priority > ? OR (priority = ? AND created_at <= ?))",
        (row["priority"], row["priority"], row["created_at"]),
    ).fetchone()[0]
    return count


def _count_queued(db) -> int:
    return db.execute("SELECT COUNT(*) FROM ci_jobs WHERE status = 'queued'").fetchone()[0]


def _any_worker_online(db) -> bool:
    ts = now_ts()
    row = db.execute("SELECT COUNT(*) FROM ci_workers WHERE last_heartbeat > ?", (ts - 60,)).fetchone()
    return row[0] > 0


def get_job(job_id: str) -> Optional[dict]:
    db = _get_db()
    row = db.execute("SELECT * FROM ci_jobs WHERE job_id = ?", (job_id,)).fetchone()
    if not row:
        return None
    return _job_row_to_dict(row, db)


def _job_snapshot(job_id: str) -> dict:
    """Small, non-transactional snapshot used by the long-poll endpoint."""
    job = get_job(job_id)
    if not job:
        return {"status": "not_found", "current_step": None, "revision": 0}
    steps = get_steps(job_id)
    current = next((s["step_name"] for s in steps if s["status"] == "running"), None)
    db = _get_db()
    revision = db.execute(
        "SELECT MAX(id) FROM ci_job_events WHERE job_id = ?", (job_id,)
    ).fetchone()[0] or 0
    return {"status": job["status"], "current_step": current, "revision": revision}


def wait_for_job_change(job_id: str, timeout_seconds: int = 55, last_known_status: str = "",
                        last_known_step: str = "", last_known_revision: int = 0) -> dict:
    """Wait on an in-process condition; never holds a SQLite transaction."""
    timeout = min(max(int(timeout_seconds), 1), 55)
    started = time.monotonic()
    with _job_change_condition:
        while True:
            snapshot = _job_snapshot(job_id)
            changed = (
                snapshot["status"] != last_known_status
                or snapshot["current_step"] != last_known_step
                or snapshot["revision"] != last_known_revision
            )
            terminal = snapshot["status"] in {"passed", "failed", "cancelled", "timed_out", "superseded", "worker_lost", "not_found"}
            if changed or terminal:
                break
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                break
            _job_change_condition.wait(timeout=remaining)
    job = get_job(job_id)
    if not job:
        return {"ok": False, "error": {"code": "PRIVATE_CI_JOB_NOT_FOUND", "message": "Job not found", "details": {}}}
    steps = get_steps(job_id)
    current = next((s["step_name"] for s in steps if s["status"] == "running"), None)
    snapshot = _job_snapshot(job_id)
    elapsed = round(time.monotonic() - started, 3)
    return {
        "ok": True, "changed": changed, "timed_out": not changed and not terminal,
        "job_id": job_id, "status": job["status"], "current_step": current,
        "revision": snapshot["revision"], "elapsed_seconds": elapsed,
        "completed_steps": [s for s in steps if s["status"] in {"passed", "failed", "timed_out", "cancelled"}],
        "newly_completed_steps": _newly_completed_steps(job_id, int(last_known_revision)),
        "terminal": job["status"] in {"passed", "failed", "cancelled", "timed_out", "superseded", "worker_lost"},
    }


def get_job_by_idempotency_key(idem_key: str) -> Optional[dict]:
    db = _get_db()
    row = db.execute(
        "SELECT * FROM ci_jobs WHERE idempotency_key = ? ORDER BY created_at DESC LIMIT 1", (idem_key,)
    ).fetchone()
    if not row:
        return None
    return _job_row_to_dict(row, db)


def list_jobs(
    repository: Optional[str] = None,
    branch: Optional[str] = None,
    commit_sha: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 20,
) -> list[dict]:
    db = _get_db()
    query = "SELECT * FROM ci_jobs WHERE 1=1"
    params = []
    if repository:
        query += " AND repository = ?"
        params.append(repository)
    if branch:
        query += " AND branch = ?"
        params.append(branch)
    if commit_sha:
        query += " AND commit_sha = ?"
        params.append(commit_sha)
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = db.execute(query, params).fetchall()
    return [_job_row_to_dict(r, db) for r in rows]


def get_controller_drain_status() -> dict:
    """Return whether the controller can restart without orphaning a live CI lease."""
    db = _get_db()
    active_statuses = ("leased", "downloading", "preparing", "running")
    placeholders = ",".join("?" for _ in active_statuses)
    rows = db.execute(
        f"SELECT job_id, repository, branch, commit_sha, profile, status, worker_id, attempts, started_at, lease_expires_at FROM ci_jobs WHERE status IN ({placeholders}) ORDER BY created_at",
        active_statuses,
    ).fetchall()
    active_jobs = [{
        "job_id": row["job_id"],
        "repository": row["repository"],
        "branch": row["branch"],
        "commit_sha": row["commit_sha"],
        "profile": row["profile"],
        "status": row["status"],
        "worker_id": row["worker_id"],
        "attempt_number": row["attempts"],
        "started_at": datetime.fromtimestamp(row["started_at"], tz=timezone.utc).isoformat() if row["started_at"] else None,
        "lease_expires_at": datetime.fromtimestamp(row["lease_expires_at"], tz=timezone.utc).isoformat() if row["lease_expires_at"] else None,
    } for row in rows]
    return {"safe_to_restart": not active_jobs, "active_job_count": len(active_jobs), "active_jobs": active_jobs}


def get_monitor_snapshot(active_limit: int = 50, recent_limit: int = 20) -> dict:
    """Return a read-only CI monitor snapshot for the web dashboard."""
    db = _get_db()
    ts = now_ts()
    active_limit = min(max(int(active_limit), 1), 100)
    recent_limit = min(max(int(recent_limit), 1), 100)
    active_statuses = ("queued", "leased", "downloading", "preparing", "running", "cancel_requested")
    terminal_statuses = ("passed", "failed", "timed_out", "cancelled", "worker_lost", "internal_error", "superseded")

    counts = {
        row["status"]: row["count"]
        for row in db.execute("SELECT status, COUNT(*) AS count FROM ci_jobs GROUP BY status").fetchall()
    }
    active_rows = db.execute(
        f"""SELECT * FROM ci_jobs
            WHERE status IN ({",".join(["?"] * len(active_statuses))})
            ORDER BY
                CASE WHEN status = 'queued' THEN 1 ELSE 0 END,
                priority DESC,
                created_at ASC
            LIMIT ?""",
        (*active_statuses, active_limit),
    ).fetchall()
    recent_rows = db.execute(
        f"""SELECT * FROM ci_jobs
            WHERE status IN ({",".join(["?"] * len(terminal_statuses))})
            ORDER BY COALESCE(finished_at, created_at) DESC
            LIMIT ?""",
        (*terminal_statuses, recent_limit),
    ).fetchall()

    workers = get_workers()
    active_jobs = [_monitor_job_row_to_dict(row, db, ts) for row in active_rows]
    recent_jobs = [_monitor_job_row_to_dict(row, db, ts) for row in recent_rows]

    return {
        "ok": True,
        "generated_at": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
        "summary": {
            "queued": counts.get("queued", 0),
            "active": sum(counts.get(status, 0) for status in active_statuses if status != "queued"),
            "running": counts.get("running", 0),
            "leased": counts.get("leased", 0),
            "downloading": counts.get("downloading", 0),
            "preparing": counts.get("preparing", 0),
            "terminal": sum(counts.get(status, 0) for status in terminal_statuses),
            "total": sum(counts.values()),
            "by_status": counts,
            "workers_online": sum(1 for worker in workers if worker.get("online")),
            "workers_total": len(workers),
        },
        "workers": workers,
        "active_jobs": active_jobs,
        "recent_jobs": recent_jobs,
    }


def _monitor_job_row_to_dict(row, db, ts: float) -> dict:
    job = _job_row_to_dict(row, db)
    steps = get_steps(row["job_id"])
    running_step_index = None
    current_step = None
    current_step_elapsed_seconds = None
    completed_steps = 0

    for index, step in enumerate(steps, start=1):
        status = step.get("status")
        if status == "running" and running_step_index is None:
            running_step_index = index
            current_step = step.get("step_name")
            started_at = _parse_iso_ts(step.get("started_at"))
            current_step_elapsed_seconds = max(0.0, ts - started_at) if started_at else None
        if status in {"passed", "failed", "timed_out", "cancelled", "completed", "skipped", "autofixed"}:
            completed_steps += 1

    started_ts = row["started_at"]
    finished_ts = row["finished_at"]
    if row["duration_seconds"] is not None:
        elapsed = row["duration_seconds"]
    elif started_ts:
        elapsed = max(0.0, (finished_ts or ts) - started_ts)
    else:
        elapsed = None

    job.update({
        "current_step": current_step,
        "current_step_index": running_step_index,
        "current_step_elapsed_seconds": current_step_elapsed_seconds,
        "completed_steps": completed_steps,
        "total_steps": len(steps),
        "elapsed_seconds": elapsed,
        "steps": steps,
    })
    return job


def _parse_iso_ts(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def _job_row_to_dict(row, db) -> dict:
    qpos = _get_queue_position(db, row["job_id"]) if row["status"] == "queued" else None
    summary = None
    if row["summary_json"]:
        try:
            summary = json.loads(row["summary_json"])
        except Exception:
            pass
    result = {
        "job_id": row["job_id"],
        "repository": row["repository"],
        "branch": row["branch"],
        "commit_sha": row["commit_sha"],
        "base_sha": row["base_sha"],
        "changed_files": json.loads(row["changed_files_json"] or "[]"),
        "changed_files_total": row["changed_files_total"],
        "changed_files_truncated": bool(row["changed_files_truncated"]),
        "profile": row["profile"],
        "status": row["status"],
        "priority": row["priority"],
        "worker_id": row["worker_id"],
        "queue_position": qpos,
        "current_step": None,
        "exit_code": row["exit_code"],
        "created_at": datetime.fromtimestamp(row["created_at"], tz=timezone.utc).isoformat() if row["created_at"] else None,
        "queued_at": datetime.fromtimestamp(row["queued_at"], tz=timezone.utc).isoformat() if row["queued_at"] else None,
        "started_at": datetime.fromtimestamp(row["started_at"], tz=timezone.utc).isoformat() if row["started_at"] else None,
        "finished_at": datetime.fromtimestamp(row["finished_at"], tz=timezone.utc).isoformat() if row["finished_at"] else None,
        "duration_seconds": row["duration_seconds"],
        "summary": summary,
        "log_truncated": bool(row["log_truncated"]),
        "cancel_requested": bool(row["cancel_requested"]),
        "superseded_by_job_id": row["superseded_by_job_id"],
        "attempts": row["attempts"],
    }
    for field in ("detected_stacks", "selected_profiles", "workspaces"):
        result[field] = summary.get(field, []) if isinstance(summary, dict) else []
    return result


### FAIR SCHEDULING: Lease a job

def lease_job(worker_id: str, supported_profiles: list[str], max_concurrent: int) -> Optional[dict]:
    db = _get_db()
    try:
        db.execute("BEGIN IMMEDIATE")

        # Check worker concurrency
        running_count = db.execute(
            "SELECT COUNT(*) FROM ci_jobs WHERE worker_id = ? AND status IN ('leased', 'downloading', 'preparing', 'running')",
            (worker_id,),
        ).fetchone()[0]
        if running_count >= max_concurrent:
            db.execute("ROLLBACK")
            return None

        # Generate lease token
        lease_token = secrets.token_hex(16)
        lease_token_hash = hashlib.sha256(lease_token.encode()).hexdigest()
        ts = now_ts()
        lease_expires = ts + 120

        # Fair scheduling:
        # 1. Get queued jobs ordered by priority (higher=higher)
        # 2. Within same priority, prefer repos with oldest last_dispatched_at (round-robin)
        # 3. Within same repo, oldest first (FIFO)

        profile_filter = " AND (" + " OR ".join(["profile = ?"] * len(supported_profiles)) + ")"
        params = []

        # Find the repository that was least recently dispatched among queued jobs
        rows = db.execute(
            f"""SELECT j.*, COALESCE(rqs.last_dispatched_at, 0) as repo_last_dispatched
               FROM ci_jobs j
               LEFT JOIN ci_repository_queue_state rqs ON j.repository = rqs.repository
               WHERE j.status = 'queued'{profile_filter}
               ORDER BY j.priority DESC, repo_last_dispatched ASC, j.created_at ASC
               LIMIT 1""",
            supported_profiles,
        ).fetchone()

        if not rows:
            db.execute("ROLLBACK")
            return None

        job_id = rows["job_id"]
        repository = rows["repository"]

        attempt_number = int(rows["attempts"]) + 1
        db.execute(
            """UPDATE ci_jobs SET status = 'leased', worker_id = ?, lease_token_hash = ?, lease_expires_at = ?,
               attempts = attempts + 1, started_at = COALESCE(started_at, ?)
               WHERE job_id = ? AND status = 'queued'""",
            (worker_id, lease_token_hash, lease_expires, ts, job_id),
        )

        if db.total_changes == 0:
            db.execute("ROLLBACK")
            return None

        _upsert_repo_queue_state(db, repository, queued_delta=-1, running_delta=1, last_dispatched_at=ts)
        set_worker_busy(worker_id, job_id)
        _add_event(db, job_id, "leased", json.dumps({"worker_id": worker_id, "attempt_number": attempt_number}))

        db.commit()

        return {
            "job_id": job_id,
            "repository": rows["repository"],
            "branch": rows["branch"],
            "commit_sha": rows["commit_sha"],
            "base_sha": rows["base_sha"],
            "changed_files": json.loads(rows["changed_files_json"] or "[]"),
            "changed_files_total": rows["changed_files_total"],
            "changed_files_truncated": bool(rows["changed_files_truncated"]),
            "profile": rows["profile"],
            "timeout_seconds": rows["timeout_seconds"],
            "attempt_number": attempt_number,
            "lease_token": lease_token,
            "lease_expires_at": datetime.fromtimestamp(lease_expires, tz=timezone.utc).isoformat(),
        }
    except Exception as e:
        try:
            db.execute("ROLLBACK")
        except Exception:
            pass
        logger.error(f"lease_job failed: {e}")
        return None


def _upsert_repo_queue_state(db, repository: str, queued_delta: int = 0, running_delta: int = 0, last_dispatched_at: Optional[float] = None):
    existing = db.execute("SELECT * FROM ci_repository_queue_state WHERE repository = ?", (repository,)).fetchone()
    if existing:
        updates = []
        params = []
        if queued_delta != 0:
            updates.append("queued_jobs = MAX(0, queued_jobs + ?)")
            params.append(queued_delta)
        if running_delta != 0:
            updates.append("running_jobs = MAX(0, running_jobs + ?)")
            params.append(running_delta)
        if last_dispatched_at is not None:
            updates.append("last_dispatched_at = ?")
            params.append(last_dispatched_at)
        if updates:
            params.append(repository)
            db.execute(f"UPDATE ci_repository_queue_state SET {', '.join(updates)} WHERE repository = ?", params)
    else:
        db.execute(
            "INSERT INTO ci_repository_queue_state (repository, last_dispatched_at, running_jobs, queued_jobs) VALUES (?, ?, ?, ?)",
            (repository, last_dispatched_at or 0, max(0, running_delta), max(0, queued_delta)),
        )


def complete_job(job_id: str, exit_code: int, status: str, summary: Optional[dict] = None, error_code: Optional[str] = None, error_message: Optional[str] = None):
    db = _get_db()
    ts = now_ts()

    row = db.execute("SELECT * FROM ci_jobs WHERE job_id = ?", (job_id,)).fetchone()
    if not row:
        return

    started = row["started_at"]
    duration = ts - started if started else 0

    summary_json = json.dumps(summary) if summary else None

    db.execute(
        """UPDATE ci_jobs SET status = ?, exit_code = ?, summary_json = ?, finished_at = ?, duration_seconds = ?,
           error_code = ?, error_message = ?, lease_token_hash = NULL, lease_expires_at = NULL, worker_id = NULL
           WHERE job_id = ?""",
        (status, exit_code, summary_json, ts, duration, error_code, error_message, job_id),
    )

    repo = row["repository"]
    _upsert_repo_queue_state(db, repo, running_delta=-1)
    _add_event(db, job_id, "completed", json.dumps({"status": status, "exit_code": exit_code}))
    db.commit()
    _notify_job_change(job_id)


def request_cancel_job(job_id: str) -> bool:
    db = _get_db()
    db.execute("UPDATE ci_jobs SET cancel_requested = 1 WHERE job_id = ? AND status IN ('queued', 'leased', 'downloading', 'preparing', 'running')", (job_id,))
    if db.total_changes > 0:
        _add_event(db, job_id, "cancel_requested", "{}")
        db.commit()
        _notify_job_change(job_id)
        return True
    return False


def cancel_queued_job(job_id: str) -> bool:
    db = _get_db()
    db.execute(
        "UPDATE ci_jobs SET status = 'cancelled', finished_at = ? WHERE job_id = ? AND status = 'queued'",
        (now_ts(), job_id),
    )
    if db.total_changes > 0:
        _add_event(db, job_id, "cancelled", "{}")
        db.commit()
        _notify_job_change(job_id)
        return True
    return False


def release_job(job_id: str):
    db = _get_db()
    row = db.execute("SELECT * FROM ci_jobs WHERE job_id = ?", (job_id,)).fetchone()
    if not row:
        return
    db.execute("UPDATE ci_jobs SET status = 'queued', worker_id = NULL, lease_token_hash = NULL, lease_expires_at = NULL WHERE job_id = ?", (job_id,))
    _upsert_repo_queue_state(db, row["repository"], queued_delta=1, running_delta=-1)
    _add_event(db, job_id, "released", "{}")
    db.commit()
    _notify_job_change(job_id)


def renew_lease(job_id: str, lease_token: str) -> bool:
    db = _get_db()
    expected_hash = hashlib.sha256(lease_token.encode()).hexdigest()
    ts = now_ts()
    with _db_write_lock:
        db.execute(
            "UPDATE ci_jobs SET lease_expires_at = ? WHERE job_id = ? AND lease_token_hash = ?",
            (ts + 120, job_id, expected_hash),
        )
        changed = db.total_changes > 0
        # This endpoint is called by every Worker heartbeat.  Leaving this
        # UPDATE uncommitted keeps a write transaction open on the thread-local
        # SQLite connection and blocks registration/job creation in others.
        db.commit()
    return changed


def need_heartbeat(job_id: str) -> bool:
    db = _get_db()
    row = db.execute(
        "SELECT lease_expires_at, cancel_requested FROM ci_jobs WHERE job_id = ? AND status IN ('leased', 'downloading', 'preparing', 'running')",
        (job_id,),
    ).fetchone()
    if not row:
        return True  # Job doesn't exist or wrong status - signal to release
    if row["cancel_requested"]:
        return True
    return False


def recover_expired_leases():
    """Recover jobs with expired leases."""
    db = _get_db()
    ts = now_ts()
    rows = db.execute(
        """SELECT * FROM ci_jobs WHERE status IN ('leased', 'downloading', 'preparing', 'running')
           AND lease_expires_at < ?""",
        (ts,),
    ).fetchall()
    for row in rows:
        max_attempts = row["max_attempts"]
        attempts = row["attempts"]
        if attempts >= max_attempts:
            db.execute(
                "UPDATE ci_jobs SET status = 'worker_lost', finished_at = ?, worker_id = NULL, lease_token_hash = NULL, lease_expires_at = NULL WHERE job_id = ?",
                (ts, row["job_id"]),
            )
        else:
            db.execute(
                "UPDATE ci_jobs SET status = 'queued', worker_id = NULL, lease_token_hash = NULL, lease_expires_at = NULL WHERE job_id = ?",
                (row["job_id"],),
            )
        _upsert_repo_queue_state(db, row["repository"], running_delta=-1)
        if row["worker_id"]:
            set_worker_idle(row["worker_id"])
        _add_event(db, row["job_id"], "lease_expired", json.dumps({"attempts": attempts, "max_attempts": max_attempts}))
    if rows:
        db.commit()


def set_job_status(job_id: str, status: str):
    db = _get_db()
    ts = now_ts()
    if status == "running":
        db.execute("UPDATE ci_jobs SET status = 'running' WHERE job_id = ? AND status IN ('downloading', 'preparing')", (job_id,))
    else:
        db.execute("UPDATE ci_jobs SET status = ? WHERE job_id = ?", (status, job_id))
    db.commit()
    _notify_job_change(job_id)


def set_job_source_info(job_id: str, sha256: str, size_bytes: int):
    db = _get_db()
    db.execute("UPDATE ci_jobs SET source_sha256 = ?, source_size_bytes = ? WHERE job_id = ?", (sha256, size_bytes, job_id))
    db.commit()
    _notify_job_change(job_id)


### LOGS

def append_log_chunk(job_id: str, content: str) -> int:
    db = _get_db()
    current_offset = db.execute(
        "SELECT MAX(offset_to) FROM ci_job_log_chunks WHERE job_id = ?", (job_id,)
    ).fetchone()[0] or 0

    chunk_index = db.execute(
        "SELECT COALESCE(MAX(chunk_index), -1) + 1 FROM ci_job_log_chunks WHERE job_id = ?", (job_id,)
    ).fetchone()[0]

    content_len = len(content)
    new_offset = current_offset + content_len

    db.execute(
        "INSERT INTO ci_job_log_chunks (job_id, chunk_index, offset_from, offset_to, content, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (job_id, chunk_index, current_offset, new_offset, content, now_ts()),
    )
    db.execute(
        "UPDATE ci_jobs SET log_total_bytes = log_total_bytes + ? WHERE job_id = ?",
        (content_len, job_id),
    )
    _add_event(db, job_id, "log_revision", json.dumps({"bytes": content_len}))
    db.commit()
    _notify_job_change(job_id)
    return new_offset


def append_log_batch(job_id: str, batch_id: str, content: str) -> tuple[int, bool]:
    db = _get_db()
    if not batch_id or not content:
        return 0, True
    row = db.execute("SELECT 1 FROM ci_job_log_batches WHERE job_id=? AND batch_id=?", (job_id, batch_id)).fetchone()
    if row:
        offset = db.execute("SELECT MAX(offset_to) FROM ci_job_log_chunks WHERE job_id=?", (job_id,)).fetchone()[0] or 0
        return offset, True
    current_offset = db.execute("SELECT MAX(offset_to) FROM ci_job_log_chunks WHERE job_id = ?", (job_id,)).fetchone()[0] or 0
    chunk_index = db.execute("SELECT COALESCE(MAX(chunk_index), -1) + 1 FROM ci_job_log_chunks WHERE job_id = ?", (job_id,)).fetchone()[0]
    new_offset = current_offset + len(content)
    now = now_ts()
    db.execute("INSERT INTO ci_job_log_batches(job_id,batch_id,created_at) VALUES(?,?,?)", (job_id, batch_id, now))
    db.execute("INSERT INTO ci_job_log_chunks(job_id,chunk_index,offset_from,offset_to,content,created_at) VALUES(?,?,?,?,?,?)", (job_id, chunk_index, current_offset, new_offset, content, now))
    db.execute("UPDATE ci_jobs SET log_total_bytes=log_total_bytes+? WHERE job_id=?", (len(content), job_id))
    _add_event(db, job_id, "log_revision", json.dumps({"bytes": len(content), "batch_id": batch_id}))
    db.commit(); _notify_job_change(job_id)
    return new_offset, False


def get_log_chunks(job_id: str, offset: int = 0, limit: int = 50) -> dict:
    db = _get_db()
    job = db.execute("SELECT log_total_bytes, log_truncated FROM ci_jobs WHERE job_id = ?", (job_id,)).fetchone()
    if not job:
        return {"job_id": job_id, "chunks": [], "next_offset": None, "total_bytes": 0, "truncated": False}

    rows = db.execute(
        "SELECT * FROM ci_job_log_chunks WHERE job_id = ? AND offset_from >= ? ORDER BY chunk_index LIMIT ?",
        (job_id, offset, limit),
    ).fetchall()

    chunks = []
    next_offset = None
    for r in rows:
        chunks.append({
            "chunk_index": r["chunk_index"],
            "offset_from": r["offset_from"],
            "offset_to": r["offset_to"],
            "content": r["content"],
        })
        next_offset = r["offset_to"]

    has_more = db.execute(
        "SELECT COUNT(*) FROM ci_job_log_chunks WHERE job_id = ? AND chunk_index > ?",
        (job_id, rows[-1]["chunk_index"] if rows else 0),
    ).fetchone()[0] > 0

    return {
        "job_id": job_id,
        "chunks": chunks,
        "next_offset": next_offset if has_more else None,
        "total_bytes": job["log_total_bytes"],
        "truncated": bool(job["log_truncated"]),
    }


def get_log_tail(job_id: str, lines: int = 100, max_scan_bytes: int = 4 * 1024 * 1024) -> dict:
    """Read backwards by chunk and stop only after enough complete newlines."""
    db = _get_db()
    row = db.execute("SELECT status, log_total_bytes, log_truncated FROM ci_jobs WHERE job_id=?", (job_id,)).fetchone()
    if not row:
        return {"job_id": job_id, "status": None, "last_sequence": None, "requested_lines": lines, "returned_lines": 0, "total_bytes": 0, "bytes_scanned": 0, "first_line_partial": False, "max_bytes_reached": False, "truncated": False, "lines": []}
    lines = min(max(int(lines), 1), 1000)
    max_scan_bytes = min(max(int(max_scan_bytes), 1), 64 * 1024 * 1024)
    collected, scanned, cursor, last_sequence = [], 0, None, None
    newline_count = 0
    while scanned < max_scan_bytes:
        params = [job_id] if cursor is None else [job_id, cursor]
        where = "job_id=?" if cursor is None else "job_id=? AND chunk_index<?"
        batch = db.execute(f"SELECT chunk_index, content FROM ci_job_log_chunks WHERE {where} ORDER BY chunk_index DESC LIMIT 100", params).fetchall()
        if not batch: break
        if last_sequence is None: last_sequence = batch[0]["chunk_index"]
        for item in batch:
            content = item["content"] or ""
            remaining = max_scan_bytes - scanned
            if len(content.encode("utf-8")) > remaining:
                encoded = content.encode("utf-8")[:remaining]
                content = encoded.decode("utf-8", errors="ignore")
            collected.append(content); scanned += len(content.encode("utf-8")); newline_count += content.count("\n")
            cursor = item["chunk_index"]
            if newline_count >= lines + 1 or scanned >= max_scan_bytes: break
        if newline_count >= lines + 1 or scanned >= max_scan_bytes: break
    content = "".join(reversed(collected)); all_lines = content.splitlines()
    first_partial = bool(collected and cursor is not None and newline_count < lines + 1 and scanned >= max_scan_bytes)
    tail = all_lines[-lines:]
    return {"job_id": job_id, "status": row["status"], "last_sequence": last_sequence, "requested_lines": lines, "returned_lines": len(tail), "total_bytes": row["log_total_bytes"], "bytes_scanned": scanned, "first_line_partial": first_partial, "max_bytes_reached": scanned >= max_scan_bytes, "truncated": bool(row["log_truncated"]), "lines": tail}


### STEPS

def add_step(job_id: str, step_name: str, status: str = "pending") -> int:
    db = _get_db()
    ts = now_ts()
    cursor = db.execute(
        "INSERT INTO ci_job_steps (job_id, step_name, status, started_at) VALUES (?, ?, ?, ?)",
        (job_id, step_name, status, ts if status == "running" else None),
    )
    db.commit()
    _notify_job_change(job_id)
    return cursor.lastrowid


def finish_step(step_id: int, status: str, exit_code: Optional[int] = None, log_end_offset: Optional[int] = None):
    db = _get_db()
    ts = now_ts()
    row = db.execute("SELECT job_id, step_name, started_at FROM ci_job_steps WHERE id = ?", (step_id,)).fetchone()
    duration = ts - row["started_at"] if row and row["started_at"] else 0
    db.execute(
        "UPDATE ci_job_steps SET status = ?, exit_code = ?, finished_at = ?, duration_seconds = ?, log_end_offset = COALESCE(?, log_end_offset) WHERE id = ?",
        (status, exit_code, ts, duration, log_end_offset, step_id),
    )
    if row:
        _add_event(db, row["job_id"], "step_completed", json.dumps({"step_id": step_id, "step_name": row["step_name"], "status": status}))
    db.commit()
    if row:
        _notify_job_change(row["job_id"])


def _newly_completed_steps(job_id: str, last_known_revision: int) -> list[dict]:
    db = _get_db()
    rows = db.execute(
        "SELECT id, event_data FROM ci_job_events WHERE job_id = ? AND id > ? AND event_type = 'step_completed' ORDER BY id",
        (job_id, last_known_revision),
    ).fetchall()
    result = []
    for row in rows:
        try:
            data = json.loads(row["event_data"])
        except (TypeError, ValueError):
            continue
        result.append({"step_name": data.get("step_name"), "status": data.get("status"), "revision": row["id"]})
    return result


def get_steps(job_id: str) -> list[dict]:
    db = _get_db()
    rows = db.execute("SELECT * FROM ci_job_steps WHERE job_id = ? ORDER BY id", (job_id,)).fetchall()
    return [
        {
            "step_name": r["step_name"],
            "status": r["status"],
            "exit_code": r["exit_code"],
            "started_at": datetime.fromtimestamp(r["started_at"], tz=timezone.utc).isoformat() if r["started_at"] else None,
            "finished_at": datetime.fromtimestamp(r["finished_at"], tz=timezone.utc).isoformat() if r["finished_at"] else None,
            "duration_seconds": r["duration_seconds"],
            "log_start_offset": r["log_start_offset"],
            "log_end_offset": r["log_end_offset"],
        }
        for r in rows
    ]


def _add_event(db, job_id: str, event_type: str, event_data: str):
    db.execute(
        "INSERT INTO ci_job_events (job_id, event_type, event_data, created_at) VALUES (?, ?, ?, ?)",
        (job_id, event_type, event_data, now_ts()),
    )


### CLEANUP



def reconcile_stale_workers():
    db = _get_db()
    ts = now_ts()
    rows = db.execute("""
        SELECT w.worker_id, w.current_job_id, j.status
        FROM ci_workers w
        LEFT JOIN ci_jobs j ON w.current_job_id = j.job_id
        WHERE w.current_job_id IS NOT NULL
    """).fetchall()
    changed = 0
    for row in rows:
        job_status = row["status"]
        if job_status is None or job_status in ("passed", "failed", "timed_out", "cancelled", "internal_error", "superseded", "worker_lost"):
            db.execute(
                "UPDATE ci_workers SET current_job_id = NULL, status = chr(39)+chr(105)+chr(100)+chr(108)+chr(101)+chr(39) WHERE worker_id = ?",
                (row["worker_id"],),
            )
            changed += 1
    offline_cutoff = ts - 120
    db.execute(
        "UPDATE ci_workers SET status = chr(39)+chr(105)+chr(100)+chr(108)+chr(101)+chr(39), current_job_id = NULL WHERE last_heartbeat < ? AND status != chr(39)+chr(105)+chr(100)+chr(108)+chr(101)+chr(39)",
        (offline_cutoff,),
    )
    if changed > 0:
        db.commit()
    return changed




def reconcile_stale_workers():
    """Clean up stale current_job pointers and offline worker status."""
    db = _get_db()
    ts = now_ts()
    rows = db.execute("""
        SELECT w.worker_id, w.current_job_id, j.status
        FROM ci_workers w
        LEFT JOIN ci_jobs j ON w.current_job_id = j.job_id
        WHERE w.current_job_id IS NOT NULL
    """).fetchall()

    changed = 0
    for row in rows:
        job_status = row["status"]
        if job_status is None or job_status in ("passed", "failed", "timed_out", "cancelled", "internal_error", "superseded", "worker_lost"):
            db.execute(
                "UPDATE ci_workers SET current_job_id = NULL, status = 'idle' WHERE worker_id = ?",
                (row["worker_id"],),
            )
            changed += 1
            logger.info(f"Reconciled stale current_job for {row['worker_id']}: job={row['current_job_id']} status={job_status}")

    offline_cutoff = ts - 120
    db.execute(
        "UPDATE ci_workers SET status = 'idle', current_job_id = NULL WHERE last_heartbeat < ? AND status != 'idle'",
        (offline_cutoff,),
    )

    # Commit even when only the offline-status cleanup changed rows.  The old
    # conditional commit left that UPDATE open on the request thread.
    offline_changed = db.total_changes
    db.commit()
    if changed > 0 or offline_changed > 0:
        logger.info(f"Reconciled {changed} stale current_job entries and {offline_changed} offline workers")

    return changed



def cleanup_old_jobs(max_age_hours: int = 168):
    """Clean up jobs older than max_age_hours (default 7 days)."""
    db = _get_db()
    cutoff = now_ts() - (max_age_hours * 3600)
    db.execute("DELETE FROM ci_job_log_chunks WHERE job_id IN (SELECT job_id FROM ci_jobs WHERE finished_at < ?)", (cutoff,))
    db.execute("DELETE FROM ci_job_steps WHERE job_id IN (SELECT job_id FROM ci_jobs WHERE finished_at < ?)", (cutoff,))
    db.execute("DELETE FROM ci_job_events WHERE job_id IN (SELECT job_id FROM ci_jobs WHERE finished_at < ?)", (cutoff,))
    db.execute("DELETE FROM ci_jobs WHERE finished_at < ?", (cutoff,))
    db.commit()
