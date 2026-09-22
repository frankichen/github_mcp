"""Minimal shared-SQLite adapter used by the standalone deployment worker."""

import os
import sqlite3
import threading


_local = threading.local()
_schema_lock = threading.Lock()
_initialized_schema_paths: set[str] = set()


def get_deploy_db():
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


def _initialize_deployment_db():
    db = get_deploy_db()
    db.executescript(
        """
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
          artifact_id TEXT, performance_json TEXT NOT NULL DEFAULT '{}',
          log_text TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_deployments_filter
        ON deployments(repository, environment, commit_sha, status, created_at);
        """
    )


    columns = {
        row[1] for row in db.execute("PRAGMA table_info(deployments)").fetchall()
    }
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
    db.execute(
        """UPDATE deployments
           SET updated_at=COALESCE(updated_at, created_at),
               log_revision=COALESCE(log_revision, 0),
               claim_generation=COALESCE(claim_generation, 0),
               state_revision=COALESCE(state_revision, 0)"""
    )
    db.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_deployments_active_lease
          ON deployments(environment, status, lease_expires_at);
        CREATE TABLE IF NOT EXISTS deployment_audit_events (
          event_id INTEGER PRIMARY KEY AUTOINCREMENT,
          deployment_id TEXT NOT NULL, event_type TEXT NOT NULL,
          from_status TEXT, to_status TEXT, reason_code TEXT,
          details_json TEXT NOT NULL DEFAULT '{}',
          claim_generation INTEGER NOT NULL DEFAULT 0,
          state_revision INTEGER NOT NULL DEFAULT 0,
          created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_deployment_audit_events
          ON deployment_audit_events(deployment_id, event_id);
        """
    )


def init_deployment_db():
    path = os.path.abspath(os.environ.get("DEPLOYMENT_DB_PATH", "/data/deployments.db"))
    if path in _initialized_schema_paths:
        return
    with _schema_lock:
        if path in _initialized_schema_paths:
            return
        _initialize_deployment_db()
        _initialized_schema_paths.add(path)
