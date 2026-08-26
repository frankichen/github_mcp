"""SQLite persistence helpers for MyGithut12 infrastructure deployments."""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone

SECRET_RE = re.compile(
    r"(?i)(authorization|cookie|token|password|secret|database_url|private_key)\s*[=:]\s*\S+"
)

_local = threading.local()


def now() -> float:
    return time.time()


def iso(value: float | None) -> str | None:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat() if value else None


def db_path() -> str:
    return os.environ.get(
        "INFRASTRUCTURE_DEPLOYMENT_DB_PATH",
        "/data/infrastructure-deployments.db",
    )


def get_db() -> sqlite3.Connection:
    if not hasattr(_local, "db") or _local.db is None:
        path = db_path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        db = sqlite3.connect(path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=15000")
        _local.db = db
    return _local.db


def init_db() -> None:
    db = get_db()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS infrastructure_deployments (
          deployment_id TEXT PRIMARY KEY,
          repository TEXT NOT NULL,
          environment TEXT NOT NULL,
          requested_scope TEXT NOT NULL,
          commit_sha TEXT NOT NULL,
          tree_sha TEXT NOT NULL,
          private_ci_job_id TEXT NOT NULL,
          expected_current_build_sha TEXT NOT NULL,
          requested_by TEXT NOT NULL DEFAULT 'mcp',
          status TEXT NOT NULL,
          current_step TEXT,
          exit_code INTEGER,
          error_code TEXT,
          error_message TEXT,
          created_at REAL NOT NULL,
          started_at REAL,
          finished_at REAL,
          updated_at REAL NOT NULL,
          log_revision INTEGER NOT NULL DEFAULT 0,
          log_text TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_infrastructure_deployments_active
          ON infrastructure_deployments(repository, environment, status, created_at);

        CREATE TABLE IF NOT EXISTS infrastructure_executor_heartbeats (
          executor_id TEXT PRIMARY KEY,
          state TEXT NOT NULL,
          current_deployment_id TEXT,
          updated_at REAL NOT NULL
        );
        """
    )
    db.commit()


def sanitize_log(value: str) -> str:
    text = SECRET_RE.sub(lambda match: match.group(1) + "=***", str(value or ""))
    return text[:4000]


def public_deployment(row: sqlite3.Row | None) -> dict | None:
    if not row:
        return None
    data = dict(row)
    for key in ("created_at", "started_at", "finished_at", "updated_at"):
        data[key] = iso(data.get(key))
    data.pop("log_text", None)
    return data


def get_deployment(deployment_id: str) -> sqlite3.Row | None:
    init_db()
    return get_db().execute(
        "SELECT * FROM infrastructure_deployments WHERE deployment_id=?",
        (deployment_id,),
    ).fetchone()


def active_deployment(repository: str, environment: str) -> sqlite3.Row | None:
    init_db()
    return get_db().execute(
        "SELECT * FROM infrastructure_deployments WHERE repository=? AND environment=? "
        "AND status IN ('queued','claimed','running') ORDER BY created_at LIMIT 1",
        (repository, environment),
    ).fetchone()


def executor_snapshot(executor_id: str, heartbeat_ttl_seconds: int) -> dict:
    init_db()
    row = get_db().execute(
        "SELECT * FROM infrastructure_executor_heartbeats WHERE executor_id=?",
        (executor_id,),
    ).fetchone()
    if not row:
        return {
            "executor_id": executor_id,
            "online": False,
            "state": "unknown",
            "current_deployment_id": None,
            "last_seen_at": None,
            "heartbeat_ttl_seconds": heartbeat_ttl_seconds,
        }
    age = max(0.0, now() - float(row["updated_at"]))
    return {
        "executor_id": executor_id,
        "online": age <= heartbeat_ttl_seconds,
        "state": row["state"],
        "current_deployment_id": row["current_deployment_id"],
        "last_seen_at": iso(row["updated_at"]),
        "age_seconds": round(age, 3),
        "heartbeat_ttl_seconds": heartbeat_ttl_seconds,
    }


def write_executor_heartbeat(
    executor_id: str,
    state: str,
    current_deployment_id: str | None,
) -> dict:
    init_db()
    timestamp = now()
    db = get_db()
    db.execute(
        """
        INSERT INTO infrastructure_executor_heartbeats(executor_id,state,current_deployment_id,updated_at)
        VALUES(?,?,?,?)
        ON CONFLICT(executor_id) DO UPDATE SET
          state=excluded.state,
          current_deployment_id=excluded.current_deployment_id,
          updated_at=excluded.updated_at
        """,
        (executor_id, state, current_deployment_id, timestamp),
    )
    db.commit()
    return {
        "executor_id": executor_id,
        "state": state,
        "current_deployment_id": current_deployment_id,
        "last_seen_at": iso(timestamp),
    }


def append_log(row: sqlite3.Row, message: str) -> tuple[str, int]:
    safe = sanitize_log(message)
    current = str(row["log_text"] or "")
    joined = (current + ("\n" if current else "") + safe)[-100000:]
    return joined, int(row["log_revision"] or 0) + 1
