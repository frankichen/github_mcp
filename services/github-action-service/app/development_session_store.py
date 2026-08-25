"""Durable MyGithut12 DX development-session state.

The store lives in the existing MyGithut12 SQLite database so 12.0.x can ignore
these expand-only tables while 12.1.x coordinates orchestration across process
restarts and blue/green generations.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from app import mygithub12 as core

MyGithub12Error = core.MyGithub12Error
_db = core._db
_LOCK = core._LOCK
_now = core._now

ACTIVE_STATES = {"preparing", "active", "validating_fast", "validating_full", "pr_ready", "drifted", "blocked", "closing"}
TERMINAL_STATES = {"merged", "closed", "abandoned", "prepare_failed"}


def init_session_db() -> None:
    """Create only new tables/indexes; never mutate/drop 12.0.x objects."""
    core.init_db()
    with _LOCK, _db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS development_sessions(
              session_id TEXT PRIMARY KEY,
              workspace_id TEXT NOT NULL,
              repository TEXT NOT NULL,
              branch TEXT NOT NULL,
              base_branch TEXT NOT NULL,
              base_commit_sha TEXT NOT NULL,
              head_commit_sha TEXT NOT NULL,
              tree_sha TEXT NOT NULL,
              session_revision INTEGER NOT NULL,
              workspace_revision INTEGER NOT NULL,
              status TEXT NOT NULL,
              owner TEXT NOT NULL,
              lease_expires_at REAL NOT NULL,
              index_commit_sha TEXT,
              pull_number INTEGER,
              last_fast_ci_job_id TEXT,
              last_full_ci_job_id TEXT,
              last_attestation_id TEXT,
              last_failure_resource_uri TEXT,
              idempotency_key TEXT,
              metadata_json TEXT NOT NULL DEFAULT '{}',
              created_at REAL NOT NULL,
              updated_at REAL NOT NULL,
              closed_at REAL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS uq_active_development_session_workspace
              ON development_sessions(workspace_id)
              WHERE status IN ('preparing','active','validating_fast','validating_full','pr_ready','drifted','blocked','closing');
            CREATE UNIQUE INDEX IF NOT EXISTS uq_development_session_idempotency
              ON development_sessions(repository,idempotency_key)
              WHERE idempotency_key IS NOT NULL AND idempotency_key <> '';
            CREATE TABLE IF NOT EXISTS development_session_events(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              session_id TEXT NOT NULL,
              event_type TEXT NOT NULL,
              from_status TEXT,
              to_status TEXT,
              session_revision INTEGER NOT NULL,
              data_json TEXT NOT NULL DEFAULT '{}',
              created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_development_session_events_session
              ON development_session_events(session_id,id);
            CREATE TABLE IF NOT EXISTS development_session_validations(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              session_id TEXT NOT NULL,
              session_revision INTEGER NOT NULL,
              mode TEXT NOT NULL,
              commit_sha TEXT NOT NULL,
              tree_sha TEXT NOT NULL,
              job_id TEXT,
              status TEXT NOT NULL,
              merge_eligible INTEGER NOT NULL DEFAULT 0,
              attestation_id TEXT,
              evidence_json TEXT NOT NULL DEFAULT '{}',
              created_at REAL NOT NULL,
              finished_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_development_session_validations_session
              ON development_session_validations(session_id,id);
            """
        )


def _public(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    value = dict(row)
    value["metadata"] = json.loads(value.pop("metadata_json") or "{}")
    value["lease_valid"] = float(value.get("lease_expires_at") or 0) > _now() and value.get("status") in ACTIVE_STATES
    return value


def get_session(session_id: str) -> dict[str, Any]:
    init_session_db()
    with _db() as db:
        row = db.execute("SELECT * FROM development_sessions WHERE session_id=?", (session_id,)).fetchone()
    if not row:
        raise MyGithub12Error("DEVELOPMENT_SESSION_NOT_FOUND", "development session was not found", {"development_session_id": session_id})
    return _public(row)


def _require_revision(session_id: str, expected_revision: int, *, writable: bool = True) -> sqlite3.Row:
    init_session_db()
    with _db() as db:
        row = db.execute("SELECT * FROM development_sessions WHERE session_id=?", (session_id,)).fetchone()
    if not row:
        raise MyGithub12Error("DEVELOPMENT_SESSION_NOT_FOUND", "development session was not found", {"development_session_id": session_id})
    if int(row["session_revision"]) != int(expected_revision):
        raise MyGithub12Error(
            "DEVELOPMENT_SESSION_REVISION_MISMATCH",
            "development session revision changed",
            {"expected": int(expected_revision), "actual": int(row["session_revision"]), "development_session_id": session_id},
        )
    if writable and row["status"] in TERMINAL_STATES:
        raise MyGithub12Error("DEVELOPMENT_SESSION_CLOSED", "development session is not active", {"status": row["status"]})
    return row


def _append_event(db: sqlite3.Connection, row: sqlite3.Row | dict[str, Any], event_type: str, from_status: str | None, to_status: str | None, revision: int, data: dict[str, Any] | None = None) -> None:
    db.execute(
        "INSERT INTO development_session_events(session_id,event_type,from_status,to_status,session_revision,data_json,created_at) VALUES(?,?,?,?,?,?,?)",
        (row["session_id"], event_type, from_status, to_status, revision, json.dumps(data or {}, ensure_ascii=False, separators=(",", ":")), _now()),
    )


def create_session(
    workspace: dict[str, Any], *, owner: str = "chatgpt", idempotency_key: str = "",
    metadata: dict[str, Any] | None = None, status: str = "active",
) -> dict[str, Any]:
    init_session_db()
    if status not in ACTIVE_STATES:
        raise MyGithub12Error(
            "DEVELOPMENT_SESSION_STATE_INVALID",
            "development session must start in an active state",
            {"status": status},
        )
    if idempotency_key:
        with _db() as db:
            existing = db.execute(
                "SELECT * FROM development_sessions WHERE repository=? AND idempotency_key=?",
                (workspace["repository"], idempotency_key),
            ).fetchone()
        if existing:
            public = _public(existing)
            if public["workspace_id"] != workspace["workspace_id"] or public["branch"] != workspace["branch"]:
                raise MyGithub12Error("IDEMPOTENCY_CONFLICT", "idempotency key belongs to another development session")
            public["replayed"] = True
            return public
    session_id = "dev_" + uuid.uuid4().hex[:20]
    now = _now()
    row_values = (
        session_id, workspace["workspace_id"], workspace["repository"], workspace["branch"], workspace["base_branch"],
        workspace["base_commit_sha"], workspace["head_sha"], workspace["tree_sha"], 1, int(workspace["revision"]), status,
        owner, float(workspace.get("lease_expires_at") or 0), workspace.get("index_commit_sha"), workspace.get("pr_number"),
        None, None, None, None, idempotency_key or None, json.dumps(metadata or {}, ensure_ascii=False, separators=(",", ":")), now, now, None,
    )
    try:
        with _LOCK, _db() as db:
            db.execute(
                "INSERT INTO development_sessions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                row_values,
            )
            row = db.execute("SELECT * FROM development_sessions WHERE session_id=?", (session_id,)).fetchone()
            _append_event(db, row, "session_created", None, status, 1, {"workspace_revision": workspace["revision"]})
    except sqlite3.IntegrityError as exc:
        if idempotency_key:
            with _db() as db:
                existing = db.execute(
                    "SELECT * FROM development_sessions WHERE repository=? AND idempotency_key=?",
                    (workspace["repository"], idempotency_key),
                ).fetchone()
            if existing:
                public = _public(existing)
                if public["workspace_id"] == workspace["workspace_id"] and public["branch"] == workspace["branch"]:
                    public["replayed"] = True
                    return public
                raise MyGithub12Error(
                    "IDEMPOTENCY_CONFLICT",
                    "idempotency key belongs to another development session",
                    {"development_session_id": public["session_id"]},
                ) from exc
        raise MyGithub12Error(
            "DEVELOPMENT_SESSION_WORKSPACE_MISMATCH",
            "workspace already has an active development session",
            {"workspace_id": workspace["workspace_id"]},
        ) from exc
    return get_session(session_id)


def sync_from_workspace(
    session_id: str, expected_revision: int, workspace: dict[str, Any], *, event_type: str = "workspace_synced", status: str | None = None, metadata_patch: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row = _require_revision(session_id, expected_revision)
    if row["workspace_id"] != workspace["workspace_id"] or row["repository"] != workspace["repository"] or row["branch"] != workspace["branch"]:
        raise MyGithub12Error("DEVELOPMENT_SESSION_WORKSPACE_MISMATCH", "session and workspace identities differ")
    new_status = status or ("drifted" if workspace.get("status") == "drifted" else row["status"])
    metadata = json.loads(row["metadata_json"] or "{}")
    metadata.update(metadata_patch or {})
    now = _now()
    with _LOCK, _db() as db:
        cur = db.execute(
            """UPDATE development_sessions SET head_commit_sha=?,tree_sha=?,workspace_revision=?,status=?,lease_expires_at=?,index_commit_sha=?,pull_number=?,metadata_json=?,session_revision=session_revision+1,updated_at=? WHERE session_id=? AND session_revision=?""",
            (workspace["head_sha"], workspace["tree_sha"], workspace["revision"], new_status, workspace.get("lease_expires_at", 0), workspace.get("index_commit_sha"), workspace.get("pr_number"), json.dumps(metadata, ensure_ascii=False, separators=(",", ":")), now, session_id, expected_revision),
        )
        if cur.rowcount != 1:
            raise MyGithub12Error("DEVELOPMENT_SESSION_REVISION_MISMATCH", "development session changed while synchronizing")
        updated = db.execute("SELECT * FROM development_sessions WHERE session_id=?", (session_id,)).fetchone()
        _append_event(db, updated, event_type, row["status"], new_status, int(updated["session_revision"]), metadata_patch)
    return _public(updated)


def transition(
    session_id: str, expected_revision: int, to_status: str, *, event_type: str = "state_changed", allowed_from: set[str] | None = None, fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row = _require_revision(session_id, expected_revision, writable=to_status not in TERMINAL_STATES)
    if allowed_from is not None and row["status"] not in allowed_from:
        raise MyGithub12Error("DEVELOPMENT_SESSION_STATE_INVALID", "development session state does not allow this action", {"status": row["status"], "allowed": sorted(allowed_from)})
    fields = dict(fields or {})
    allowed_fields = {"pull_number", "last_fast_ci_job_id", "last_full_ci_job_id", "last_attestation_id", "last_failure_resource_uri", "index_commit_sha", "workspace_revision", "head_commit_sha", "tree_sha", "lease_expires_at"}
    unknown = set(fields) - allowed_fields
    if unknown:
        raise MyGithub12Error("DEVELOPMENT_SESSION_STATE_INVALID", "unsupported session field update", {"fields": sorted(unknown)})
    assignments = ["status=?", "session_revision=session_revision+1", "updated_at=?"]
    values: list[Any] = [to_status, _now()]
    if to_status in TERMINAL_STATES:
        assignments.append("closed_at=?")
        values.append(_now())
    for key, value in fields.items():
        assignments.append(f"{key}=?")
        values.append(value)
    values.extend([session_id, expected_revision])
    with _LOCK, _db() as db:
        cur = db.execute(
            f"UPDATE development_sessions SET {','.join(assignments)} WHERE session_id=? AND session_revision=?",
            values,
        )
        if cur.rowcount != 1:
            raise MyGithub12Error("DEVELOPMENT_SESSION_REVISION_MISMATCH", "development session changed while updating")
        updated = db.execute("SELECT * FROM development_sessions WHERE session_id=?", (session_id,)).fetchone()
        _append_event(db, updated, event_type, row["status"], to_status, int(updated["session_revision"]), fields)
    return _public(updated)


def record_validation(session_id: str, session_revision: int, mode: str, commit_sha: str, tree_sha: str, *, job_id: str = "", status: str = "queued", merge_eligible: bool = False, attestation_id: str = "", evidence: dict[str, Any] | None = None, finished: bool = False) -> int:
    init_session_db()
    get_session(session_id)
    with _LOCK, _db() as db:
        cur = db.execute(
            """INSERT INTO development_session_validations(session_id,session_revision,mode,commit_sha,tree_sha,job_id,status,merge_eligible,attestation_id,evidence_json,created_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (session_id, session_revision, mode, commit_sha, tree_sha, job_id or None, status, 1 if merge_eligible else 0, attestation_id or None, json.dumps(evidence or {}, ensure_ascii=False, separators=(",", ":")), _now(), _now() if finished else None),
        )
        return int(cur.lastrowid)


def list_events(session_id: str, limit: int = 100) -> list[dict[str, Any]]:
    get_session(session_id)
    with _db() as db:
        rows = db.execute("SELECT * FROM development_session_events WHERE session_id=? ORDER BY id DESC LIMIT ?", (session_id, max(1, min(limit, 500)))).fetchall()
    out=[]
    for row in reversed(rows):
        value=dict(row); value["data"]=json.loads(value.pop("data_json") or "{}"); out.append(value)
    return out


def recover_sessions(workspace_getter) -> dict[str, int]:
    """Mark only provably stale sessions; never acquire leases or mutate Git refs."""
    init_session_db()
    checked = recovered = 0
    with _db() as db:
        rows = db.execute("SELECT * FROM development_sessions WHERE status IN ('preparing','validating_fast','validating_full','closing')").fetchall()
    for row in rows:
        checked += 1
        try:
            ws = workspace_getter(row["workspace_id"])
        except Exception:
            continue
        # A process restart cannot know whether an interrupted orchestration
        # completed external effects; force explicit recovery instead of guessing.
        if row["status"] in {"preparing", "validating_fast", "validating_full", "closing"}:
            try:
                transition(row["session_id"], row["session_revision"], "blocked", event_type="restart_recovery_required", fields={"workspace_revision": ws["revision"], "head_commit_sha": ws["head_sha"], "tree_sha": ws["tree_sha"]})
                recovered += 1
            except MyGithub12Error:
                pass
    return {"checked_sessions": checked, "recovery_required": recovered}

def find_session_by_idempotency(repository: str, idempotency_key: str) -> dict[str, Any] | None:
    if not idempotency_key:
        return None
    init_session_db()
    with _db() as db:
        row=db.execute("SELECT * FROM development_sessions WHERE repository=? AND idempotency_key=?",(repository,idempotency_key)).fetchone()
    if not row:
        return None
    value=_public(row); value["replayed"]=True; return value
