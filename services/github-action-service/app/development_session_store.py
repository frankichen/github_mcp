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
from app.ci_models import REQUEST_ONLY_TERMINAL_STATUSES

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
              request_id TEXT,
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
        validation_columns = {
            row[1] for row in db.execute("PRAGMA table_info(development_session_validations)").fetchall()
        }
        if "request_id" not in validation_columns:
            db.execute("ALTER TABLE development_session_validations ADD COLUMN request_id TEXT")
        db.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS uq_development_session_validation_request
               ON development_session_validations(session_id,request_id)
               WHERE request_id IS NOT NULL AND request_id <> ''"""
        )
        db.execute(
            """CREATE INDEX IF NOT EXISTS idx_development_session_validation_job
               ON development_session_validations(session_id,job_id)
               WHERE job_id IS NOT NULL AND job_id <> ''"""
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


def find_active_session_for_workspace(workspace_id: str) -> dict[str, Any] | None:
    """Return the unique non-terminal Session that owns a Workspace."""
    init_session_db()
    placeholders = ",".join("?" for _ in ACTIVE_STATES)
    with _db() as db:
        rows = db.execute(
            f"SELECT * FROM development_sessions WHERE workspace_id=? AND status IN ({placeholders}) ORDER BY updated_at DESC LIMIT 2",
            (workspace_id, *sorted(ACTIVE_STATES)),
        ).fetchall()
    if len(rows) > 1:
        raise MyGithub12Error(
            "DEVELOPMENT_SESSION_WORKSPACE_MISMATCH",
            "workspace has more than one active development session",
            {"workspace_id": workspace_id},
        )
    return _public(rows[0]) if rows else None


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


def append_recovery_event(
    session_id: str, expected_revision: int, event_type: str, data: dict[str, Any],
) -> dict[str, Any]:
    """Append a recovery diagnostic without changing Session/Workspace identity."""
    if event_type not in {"external_drift_detected", "recovery_refused"}:
        raise MyGithub12Error("DEVELOPMENT_SESSION_STATE_INVALID", "unsupported recovery event type")
    init_session_db()
    with _LOCK, _db() as db:
        row = db.execute("SELECT * FROM development_sessions WHERE session_id=?", (session_id,)).fetchone()
        if not row:
            raise MyGithub12Error(
                "DEVELOPMENT_SESSION_NOT_FOUND",
                "development session was not found",
                {"development_session_id": session_id},
            )
        if int(row["session_revision"]) != int(expected_revision):
            raise MyGithub12Error(
                "DEVELOPMENT_SESSION_REVISION_MISMATCH",
                "development session revision changed",
                {"expected": int(expected_revision), "actual": int(row["session_revision"])},
            )
        _append_event(
            db, row, event_type, row["status"], row["status"], int(row["session_revision"]), data,
        )
    return get_session(session_id)


def recover_stale_session_from_workspace(
    session_id: str, expected_revision: int, workspace: dict[str, Any], *,
    idempotency_key: str = "", index_commit_sha: str | None = None,
    recovery_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """CAS-sync a provably stale Session from already-verified Workspace evidence."""
    init_session_db()
    with _LOCK, _db() as db:
        row = db.execute("SELECT * FROM development_sessions WHERE session_id=?", (session_id,)).fetchone()
        if not row:
            raise MyGithub12Error(
                "DEVELOPMENT_SESSION_NOT_FOUND",
                "development session was not found",
                {"development_session_id": session_id},
            )
        metadata = json.loads(row["metadata_json"] or "{}")
        last = metadata.get("last_session_recovery") if isinstance(metadata, dict) else None
        if idempotency_key and isinstance(last, dict) and last.get("idempotency_key") == idempotency_key:
            after = last.get("after") if isinstance(last.get("after"), dict) else {}
            if int(row["session_revision"]) == int(after.get("session_revision", -1)):
                return {
                    "session": _public(row), "recovered": bool(last.get("recovered")),
                    "replayed": True, "before": last.get("before"), "after": after,
                    "audit": last.get("audit"),
                }
            raise MyGithub12Error(
                "IDEMPOTENCY_CONFLICT",
                "session recovery idempotency key belongs to an earlier Session revision",
                {"development_session_id": session_id},
            )
        if int(row["session_revision"]) != int(expected_revision):
            raise MyGithub12Error(
                "DEVELOPMENT_SESSION_REVISION_MISMATCH",
                "development session revision changed",
                {"expected": int(expected_revision), "actual": int(row["session_revision"])},
            )
        if row["status"] in TERMINAL_STATES:
            raise MyGithub12Error("DEVELOPMENT_SESSION_CLOSED", "development session is not active")
        identity_fields = ("workspace_id", "repository", "branch", "base_branch", "base_commit_sha")
        if any(row[field] != workspace.get(field) for field in identity_fields):
            raise MyGithub12Error(
                "DEVELOPMENT_SESSION_WORKSPACE_MISMATCH",
                "session and workspace identities differ during recovery",
            )
        target_head = str(workspace["head_sha"])
        target_tree = str(workspace["tree_sha"])
        target_workspace_revision = int(workspace["revision"])
        target_lease = float(workspace.get("lease_expires_at") or 0)
        if index_commit_sha and index_commit_sha != target_head:
            raise MyGithub12Error(
                "DEVELOPMENT_SESSION_RECOVERY_REQUIRED",
                "recovery index identity is not the recovered HEAD",
                {"index_commit_sha": index_commit_sha, "head_commit_sha": target_head},
            )
        before = {
            "head_commit_sha": row["head_commit_sha"], "tree_sha": row["tree_sha"],
            "workspace_revision": int(row["workspace_revision"]),
            "session_revision": int(row["session_revision"]),
            "lease_expires_at": float(row["lease_expires_at"] or 0),
            "index_commit_sha": row["index_commit_sha"],
        }
        head_changed = row["head_commit_sha"] != target_head or row["tree_sha"] != target_tree
        needs_update = (
            head_changed
            or int(row["workspace_revision"]) != target_workspace_revision
            or abs(float(row["lease_expires_at"] or 0) - target_lease) > 0.001
            or row["index_commit_sha"] != index_commit_sha
        )
        if not needs_update:
            after = {**before}
            return {
                "session": _public(row), "recovered": False, "replayed": False,
                "before": before, "after": after, "audit": None,
            }
        next_revision = int(row["session_revision"]) + 1
        after = {
            "head_commit_sha": target_head, "tree_sha": target_tree,
            "workspace_revision": target_workspace_revision, "session_revision": next_revision,
            "lease_expires_at": target_lease, "index_commit_sha": index_commit_sha,
        }
        cleared = head_changed and any(
            row[field]
            for field in (
                "last_fast_ci_job_id", "last_full_ci_job_id", "last_attestation_id",
                "last_failure_resource_uri",
            )
        )
        audit = {
            "idempotency_key": idempotency_key or None, "before": before, "after": after,
            "head_changed": head_changed, "stale_ci_evidence_cleared": bool(cleared),
            "evidence": recovery_evidence or {},
        }
        if idempotency_key:
            metadata["last_session_recovery"] = {
                "idempotency_key": idempotency_key, "recovered": True,
                "before": before, "after": after, "audit": audit,
            }
        cur = db.execute(
            """UPDATE development_sessions SET head_commit_sha=?,tree_sha=?,workspace_revision=?,
            lease_expires_at=?,index_commit_sha=?,last_fast_ci_job_id=?,last_full_ci_job_id=?,
            last_attestation_id=?,last_failure_resource_uri=?,metadata_json=?,
            session_revision=session_revision+1,updated_at=? WHERE session_id=? AND session_revision=?""",
            (
                target_head, target_tree, target_workspace_revision, target_lease, index_commit_sha,
                None if head_changed else row["last_fast_ci_job_id"],
                None if head_changed else row["last_full_ci_job_id"],
                None if head_changed else row["last_attestation_id"],
                None if head_changed else row["last_failure_resource_uri"],
                json.dumps(metadata, ensure_ascii=False, separators=(",", ":")), _now(),
                session_id, expected_revision,
            ),
        )
        if cur.rowcount != 1:
            raise MyGithub12Error(
                "DEVELOPMENT_SESSION_REVISION_MISMATCH", "development session changed while recovering"
            )
        updated = db.execute("SELECT * FROM development_sessions WHERE session_id=?", (session_id,)).fetchone()
        _append_event(
            db, updated, "session_recovered", row["status"], updated["status"],
            int(updated["session_revision"]), audit,
        )
    return {
        "session": _public(updated), "recovered": True, "replayed": False,
        "before": before, "after": after, "audit": audit,
    }


def auto_renew_session_workspace_lease(
    session_id: str, expected_session_revision: int, workspace_id: str, expected_workspace_revision: int,
    *, lease_seconds: int, event_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Atomically advance an active Workspace lease and its Session revision."""
    init_session_db(); now=_now(); bounded_lease=max(60,min(int(lease_seconds),core.MAX_LEASE_SECONDS))
    with _LOCK,_db() as db:
        session_row=db.execute("SELECT * FROM development_sessions WHERE session_id=?",(session_id,)).fetchone()
        if not session_row: raise MyGithub12Error("DEVELOPMENT_SESSION_NOT_FOUND","development session was not found",{"development_session_id":session_id})
        workspace_row=db.execute("SELECT * FROM workspaces WHERE workspace_id=?",(workspace_id,)).fetchone()
        if not workspace_row: raise MyGithub12Error("WORKSPACE_NOT_FOUND","workspace was not found",{"workspace_id":workspace_id})
        if int(session_row["session_revision"])!=int(expected_session_revision): raise MyGithub12Error("DEVELOPMENT_SESSION_REVISION_MISMATCH","development session revision changed",{"expected":expected_session_revision,"actual":session_row["session_revision"],"development_session_id":session_id})
        if session_row["status"] in TERMINAL_STATES: raise MyGithub12Error("DEVELOPMENT_SESSION_CLOSED","development session is not active",{"status":session_row["status"]})
        if session_row["workspace_id"]!=workspace_id: raise MyGithub12Error("DEVELOPMENT_SESSION_WORKSPACE_MISMATCH","session and workspace identities differ")
        if int(workspace_row["revision"])!=int(expected_workspace_revision): raise MyGithub12Error("WORKSPACE_REVISION_MISMATCH","workspace revision changed",{"expected":expected_workspace_revision,"actual":workspace_row["revision"]})
        if int(session_row["workspace_revision"])!=int(expected_workspace_revision): raise MyGithub12Error("DEVELOPMENT_SESSION_WORKSPACE_MISMATCH","session does not reference the current workspace revision",{"session_workspace_revision":session_row["workspace_revision"],"workspace_revision":expected_workspace_revision})
        if workspace_row["status"]=="drifted": raise MyGithub12Error("WORKSPACE_BRANCH_DRIFTED","drifted workspace cannot be auto-renewed")
        if workspace_row["status"]!="active" or float(workspace_row["lease_expires_at"] or 0)<=now or float(session_row["lease_expires_at"] or 0)<=now: raise MyGithub12Error("WORKSPACE_LEASE_REQUIRED","expired workspace cannot be auto-renewed",{"workspace_id":workspace_id,"requires_resume":True})
        if session_row["head_commit_sha"]!=workspace_row["head_sha"] or session_row["tree_sha"]!=workspace_row["tree_sha"]: raise MyGithub12Error("DEVELOPMENT_SESSION_WORKSPACE_MISMATCH","session and workspace Git identities differ")
        if abs(float(session_row["lease_expires_at"])-float(workspace_row["lease_expires_at"]))>0.001: raise MyGithub12Error("DEVELOPMENT_SESSION_WORKSPACE_MISMATCH","session and workspace lease identities differ")
        before_expiry=float(workspace_row["lease_expires_at"]); new_expiry=now+bounded_lease; new_workspace_revision=int(expected_workspace_revision)+1
        cur=db.execute("UPDATE workspaces SET lease_expires_at=?,revision=revision+1,updated_at=? WHERE workspace_id=? AND revision=? AND status='active' AND lease_expires_at>?",(new_expiry,now,workspace_id,expected_workspace_revision,now))
        if cur.rowcount!=1: raise MyGithub12Error("WORKSPACE_REVISION_MISMATCH","workspace changed while auto-renewing lease")
        cur=db.execute("UPDATE development_sessions SET workspace_revision=?,lease_expires_at=?,session_revision=session_revision+1,updated_at=? WHERE session_id=? AND session_revision=?",(new_workspace_revision,new_expiry,now,session_id,expected_session_revision))
        if cur.rowcount!=1: raise MyGithub12Error("DEVELOPMENT_SESSION_REVISION_MISMATCH","development session changed while auto-renewing lease")
        updated=db.execute("SELECT * FROM development_sessions WHERE session_id=?",(session_id,)).fetchone()
        audit={**(event_data or {}),"before_expiry":before_expiry,"after_expiry":new_expiry,"before_workspace_revision":int(expected_workspace_revision),"after_workspace_revision":new_workspace_revision}
        _append_event(db,updated,"workspace_lease_auto_renewed",session_row["status"],updated["status"],int(updated["session_revision"]),audit)
    return {"session":_public(updated),"workspace_revision":new_workspace_revision,"lease_expires_at":new_expiry,"audit":audit}


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




def finalize_merged_session_workspace(
    session_id: str,
    expected_session_revision: int,
    workspace_id: str,
    expected_workspace_revision: int,
    *,
    merge_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Atomically mark a managed Session merged and release its Workspace Writer lease/index pin."""
    init_session_db()
    now = _now()
    with _LOCK, _db() as db:
        session_row = db.execute("SELECT * FROM development_sessions WHERE session_id=?", (session_id,)).fetchone()
        workspace_row = db.execute("SELECT * FROM workspaces WHERE workspace_id=?", (workspace_id,)).fetchone()
        if not session_row:
            raise MyGithub12Error("DEVELOPMENT_SESSION_NOT_FOUND", "development session was not found", {"development_session_id": session_id})
        if not workspace_row:
            raise MyGithub12Error("WORKSPACE_NOT_FOUND", "workspace was not found", {"workspace_id": workspace_id})
        if int(session_row["session_revision"]) != int(expected_session_revision):
            raise MyGithub12Error(
                "DEVELOPMENT_SESSION_REVISION_MISMATCH", "development session changed before merge finalization",
                {"expected": expected_session_revision, "actual": session_row["session_revision"]},
            )
        if int(workspace_row["revision"]) != int(expected_workspace_revision):
            raise MyGithub12Error(
                "WORKSPACE_REVISION_MISMATCH", "workspace changed before merge finalization",
                {"expected": expected_workspace_revision, "actual": workspace_row["revision"]},
            )
        if session_row["workspace_id"] != workspace_id or int(session_row["workspace_revision"]) != int(expected_workspace_revision):
            raise MyGithub12Error("DEVELOPMENT_SESSION_WORKSPACE_MISMATCH", "session and workspace identities differ before merge finalization")
        if session_row["status"] not in {"active", "pr_ready"}:
            raise MyGithub12Error(
                "DEVELOPMENT_SESSION_STATE_INVALID", "development session state does not allow merge finalization",
                {"status": session_row["status"]},
            )
        if workspace_row["status"] != "active":
            raise MyGithub12Error(
                "WORKSPACE_CLOSED", "managed merge finalization requires the active owning Workspace",
                {"workspace_id": workspace_id, "status": workspace_row["status"]},
            )
        if (
            session_row["repository"] != workspace_row["repository"]
            or session_row["branch"] != workspace_row["branch"]
            or session_row["base_branch"] != workspace_row["base_branch"]
            or session_row["base_commit_sha"] != workspace_row["base_commit_sha"]
            or session_row["head_commit_sha"] != workspace_row["head_sha"]
            or session_row["tree_sha"] != workspace_row["tree_sha"]
        ):
            raise MyGithub12Error("DEVELOPMENT_SESSION_WORKSPACE_MISMATCH", "session and workspace Git identities differ before merge finalization")
        merge_pull_number = int((merge_evidence or {}).get("pull_number") or 0)
        if merge_pull_number <= 0:
            raise MyGithub12Error(
                "MERGE_EVIDENCE_INCOMPLETE",
                "managed merge finalization requires a verified pull_number",
            )
        current_pull_number = session_row["pull_number"]
        if current_pull_number is not None and int(current_pull_number) != merge_pull_number:
            raise MyGithub12Error(
                "MANAGED_PR_IDENTITY_MISMATCH",
                "development session is already bound to another pull request",
                {
                    "development_session_id": session_id,
                    "expected_pull_number": merge_pull_number,
                    "actual_pull_number": int(current_pull_number),
                },
            )
        pull_number_backfilled = current_pull_number is None
        new_workspace_revision = int(expected_workspace_revision) + 1
        workspace_update = db.execute(
            """UPDATE workspaces SET status='closed',lease_expires_at=0,index_commit_sha=NULL,drift_reason=NULL,
            revision=revision+1,updated_at=? WHERE workspace_id=? AND revision=? AND status='active'""",
            (now, workspace_id, expected_workspace_revision),
        )
        if workspace_update.rowcount != 1:
            raise MyGithub12Error("WORKSPACE_REVISION_MISMATCH", "workspace changed while finalizing managed merge")
        session_update = db.execute(
            """UPDATE development_sessions SET status='merged',pull_number=?,workspace_revision=?,lease_expires_at=0,
            session_revision=session_revision+1,updated_at=?,closed_at=?
            WHERE session_id=? AND session_revision=?""",
            (merge_pull_number, new_workspace_revision, now, now, session_id, expected_session_revision),
        )
        if session_update.rowcount != 1:
            raise MyGithub12Error("DEVELOPMENT_SESSION_REVISION_MISMATCH", "development session changed while finalizing managed merge")
        updated = db.execute("SELECT * FROM development_sessions WHERE session_id=?", (session_id,)).fetchone()
        closed_workspace = db.execute("SELECT * FROM workspaces WHERE workspace_id=?", (workspace_id,)).fetchone()
        audit = {
            "workspace_id": workspace_id,
            "workspace_closed": True,
            "old_workspace_revision": int(expected_workspace_revision),
            "new_workspace_revision": new_workspace_revision,
            "pull_number": merge_pull_number,
            "pull_number_backfilled": pull_number_backfilled,
            "merge": dict(merge_evidence or {}),
        }
        _append_event(
            db,
            updated,
            "pull_request_merged",
            session_row["status"],
            "merged",
            int(updated["session_revision"]),
            audit,
        )
    workspace_value = dict(closed_workspace)
    workspace_value["scope"] = json.loads(workspace_value.pop("scope_json") or "{}")
    workspace_value["persisted_status"] = workspace_value["status"]
    workspace_value["lease_valid"] = False
    workspace_value["index_pin_active"] = False
    workspace_value["index_pin_grace_expires_at"] = 0.0
    return {"session": _public(updated), "workspace": workspace_value, "audit": audit}

def _validation_request_id(row: sqlite3.Row | dict[str, Any], evidence: dict[str, Any] | None = None) -> str:
    value = dict(row)
    first_class = str(value.get("request_id") or "")
    if first_class:
        return first_class
    if evidence is None:
        try:
            evidence = json.loads(value.get("evidence_json") or "{}")
        except (TypeError, ValueError):
            evidence = {}
    return str((evidence or {}).get("request_id") or "")


def record_validation(
    session_id: str, session_revision: int, mode: str, commit_sha: str, tree_sha: str, *,
    request_id: str = "", job_id: str = "", status: str = "queued", merge_eligible: bool = False,
    attestation_id: str = "", evidence: dict[str, Any] | None = None, finished: bool = False,
) -> int:
    """Persist one logical managed validation without creating NULL-job duplicates."""
    init_session_db()
    payload = dict(evidence or {})
    logical_request_id = str(request_id or payload.get("request_id") or "")
    logical_job_id = str(job_id or "")
    if logical_request_id:
        payload["request_id"] = logical_request_id
    with _LOCK, _db() as db:
        session_row = db.execute(
            "SELECT * FROM development_sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if not session_row:
            raise MyGithub12Error(
                "DEVELOPMENT_SESSION_NOT_FOUND", "development session was not found",
                {"development_session_id": session_id},
            )
        if int(session_row["session_revision"]) != int(session_revision):
            raise MyGithub12Error(
                "DEVELOPMENT_SESSION_REVISION_MISMATCH", "development session revision changed",
                {"expected": int(session_revision), "actual": int(session_row["session_revision"])},
            )
        if session_row["status"] in TERMINAL_STATES:
            raise MyGithub12Error("DEVELOPMENT_SESSION_CLOSED", "development session is not active")

        existing = None
        if logical_request_id:
            existing = db.execute(
                """SELECT * FROM development_session_validations
                   WHERE session_id=? AND request_id=? ORDER BY id DESC LIMIT 1""",
                (session_id, logical_request_id),
            ).fetchone()
            if not existing:
                # Before request_id became first-class, the same logical
                # Request was persisted only in evidence_json while the
                # Worker was still absent. Reuse that placeholder instead of
                # inserting a second row when late binding supplies the
                # first-class request_id.
                legacy_rows = db.execute(
                    """SELECT * FROM development_session_validations
                       WHERE session_id=? AND mode=? AND commit_sha=?
                         AND (tree_sha=? OR tree_sha='')
                         AND (request_id IS NULL OR request_id='')
                       ORDER BY id DESC""",
                    (session_id, mode, commit_sha, tree_sha),
                ).fetchall()
                for legacy_row in legacy_rows:
                    try:
                        legacy_evidence = json.loads(legacy_row["evidence_json"] or "{}")
                    except (TypeError, ValueError):
                        legacy_evidence = {}
                    if _validation_request_id(legacy_row, legacy_evidence) == logical_request_id:
                        existing = legacy_row
                        break
            if existing and (
                existing["mode"] != mode
                or existing["commit_sha"] != commit_sha
                or (tree_sha and existing["tree_sha"] not in {"", tree_sha})
                or int(existing["session_revision"]) > int(session_revision)
            ):
                raise MyGithub12Error(
                    "DEVELOPMENT_SESSION_RECOVERY_REQUIRED",
                    "validation request_id is already bound to another validation identity",
                    {"request_id": logical_request_id},
                )
        elif logical_job_id:
            existing = db.execute(
                """SELECT * FROM development_session_validations
                   WHERE session_id=? AND session_revision=? AND mode=? AND commit_sha=?
                     AND tree_sha=? AND job_id=? ORDER BY id DESC LIMIT 1""",
                (session_id, session_revision, mode, commit_sha, tree_sha, logical_job_id),
            ).fetchone()
        else:
            existing = db.execute(
                """SELECT * FROM development_session_validations
                   WHERE session_id=? AND session_revision=? AND mode=? AND commit_sha=?
                     AND tree_sha=? AND job_id IS NULL AND request_id IS NULL
                   ORDER BY id DESC LIMIT 1""",
                (session_id, session_revision, mode, commit_sha, tree_sha),
            ).fetchone()

        if existing:
            existing_job_id = str(existing["job_id"] or "")
            if existing_job_id and logical_job_id and existing_job_id != logical_job_id:
                raise MyGithub12Error(
                    "DEVELOPMENT_SESSION_RECOVERY_REQUIRED",
                    "validation request is already bound to another Worker job",
                    {"request_id": logical_request_id or None, "existing_job_id": existing_job_id, "job_id": logical_job_id},
                )
            existing_status = str(existing["status"] or "")
            existing_terminal = existing_status in {
                "passed", "failed", "timed_out", "cancelled", "superseded",
                "worker_lost", "internal_error", "preflight_failed",
            }
            incoming_terminal = status in {
                "passed", "failed", "timed_out", "cancelled", "superseded",
                "worker_lost", "internal_error", "preflight_failed",
            }
            if existing_terminal and incoming_terminal and existing_status != status:
                raise MyGithub12Error(
                    "DEVELOPMENT_SESSION_RECOVERY_REQUIRED",
                    "logical validation has conflicting terminal Worker statuses",
                    {
                        "request_id": logical_request_id or existing["request_id"],
                        "existing_status": existing_status,
                        "status": status,
                    },
                )
            if existing_terminal and not incoming_terminal:
                # A stale Request snapshot must not overwrite terminal Worker
                # truth during a replay or a late correlation backfill.
                effective_status = existing_status
                effective_merge_eligible = bool(existing["merge_eligible"])
                effective_attestation_id = str(existing["attestation_id"] or "")
                effective_finished_at = existing["finished_at"] or _now()
            elif existing_terminal and incoming_terminal:
                # Terminal evidence is monotonic for one logical validation:
                # retain an earlier reusable attestation/eligibility while
                # allowing a later backfill to add it when it was absent.
                effective_status = existing_status
                effective_merge_eligible = bool(existing["merge_eligible"]) or bool(merge_eligible)
                effective_attestation_id = attestation_id or str(existing["attestation_id"] or "")
                effective_finished_at = existing["finished_at"] or _now()
            else:
                effective_status = status
                effective_merge_eligible = bool(merge_eligible)
                effective_attestation_id = attestation_id or str(existing["attestation_id"] or "")
                effective_finished_at = (
                    _now() if finished or incoming_terminal else existing["finished_at"]
                )
            try:
                existing_evidence = json.loads(existing["evidence_json"] or "{}")
            except (TypeError, ValueError):
                existing_evidence = {}
            if not isinstance(existing_evidence, dict):
                existing_evidence = {}
            merged_payload = {**existing_evidence, **payload}
            db.execute(
                """UPDATE development_session_validations
                   SET request_id=?,job_id=?,tree_sha=CASE WHEN tree_sha='' THEN ? ELSE tree_sha END,
                       status=?,merge_eligible=?,attestation_id=?,evidence_json=?,finished_at=?
                   WHERE id=?""",
                (
                    logical_request_id or existing["request_id"],
                    logical_job_id or existing["job_id"],
                    tree_sha,
                    effective_status,
                    1 if effective_merge_eligible else 0,
                    effective_attestation_id or None,
                    json.dumps(merged_payload, ensure_ascii=False, separators=(",", ":")),
                    effective_finished_at,
                    int(existing["id"]),
                ),
            )
            return int(existing["id"])
        cur = db.execute(
            """INSERT INTO development_session_validations(
               session_id,session_revision,mode,commit_sha,tree_sha,request_id,job_id,status,
               merge_eligible,attestation_id,evidence_json,created_at,finished_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                session_id, session_revision, mode, commit_sha, tree_sha,
                logical_request_id or None, logical_job_id or None, status,
                1 if merge_eligible else 0, attestation_id or None,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                _now(), _now() if finished else None,
            ),
        )
        return int(cur.lastrowid)


def validation_correlations(
    session_id: str,
    session_revision: int,
    mode: str,
    commit_sha: str,
    tree_sha: str,
    *,
    exact_revision: bool = False,
) -> list[dict[str, Any]]:
    """Return persisted request/job anchors for one validation identity.

    Recovery normally resolves the active validation generation first and then
    requests only that exact Session revision.  The legacy <= behavior remains
    available for existing callers that only need historical inspection.
    """
    init_session_db()
    get_session(session_id)
    revision_operator = "=" if exact_revision else "<="
    with _db() as db:
        rows = db.execute(
            f"""SELECT * FROM development_session_validations
               WHERE session_id=? AND session_revision{revision_operator}? AND mode=?
                 AND commit_sha=? AND (tree_sha=? OR tree_sha='')
               ORDER BY id DESC""",
            (session_id, session_revision, mode, commit_sha, tree_sha),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["merge_eligible"] = bool(item["merge_eligible"])
        try:
            evidence_value = json.loads(item.pop("evidence_json") or "{}")
        except (TypeError, ValueError):
            evidence_value = {}
        item["evidence"] = evidence_value
        first_class_request_id = str(item.get("request_id") or "")
        logical_request_id = first_class_request_id or str(evidence_value.get("request_id") or "")
        item["request_id"] = logical_request_id or None
        item["request_id_source"] = (
            "column" if first_class_request_id else "legacy_evidence" if logical_request_id
            else "legacy_job" if item.get("job_id") else "missing"
        )
        result.append(item)
    return result


_VALIDATION_MAINTENANCE_EVENTS = frozenset({
    "session_recovered", "workspace_lease_auto_renewed", "validation_observed",
})
_VALIDATION_NONMUTATING_AUDIT_EVENTS = frozenset({
    "validation_correlation_backfilled", "external_drift_detected", "recovery_refused",
})


def _event_data(row: sqlite3.Row) -> dict[str, Any]:
    try:
        value = json.loads(row["data_json"] or "{}")
    except (TypeError, ValueError):
        value = {}
    return value if isinstance(value, dict) else {}


def _validation_generation_context_db(
    db: sqlite3.Connection,
    session_row: sqlite3.Row,
    mode: str,
    commit_sha: str,
    tree_sha: str,
) -> dict[str, Any]:
    """Prove the validation revision that still owns a transient Session.

    A validation may legitimately remain active while revision-only Workspace
    maintenance advances the Session CAS.  Only a contiguous, identity-
    preserving maintenance event chain may bridge that historical validation
    generation to the current transient Session.
    """
    if mode not in {"fast", "full"}:
        raise MyGithub12Error("DEVELOPMENT_SESSION_RECOVERY_REQUIRED", "validation mode is invalid")
    expected_status = "validating_fast" if mode == "fast" else "validating_full"
    current_revision = int(session_row["session_revision"])
    if session_row["status"] != expected_status:
        raise MyGithub12Error(
            "DEVELOPMENT_SESSION_STATE_INVALID",
            "development session validation phase changed",
            {"expected_status": expected_status, "actual_status": session_row["status"]},
        )
    if session_row["head_commit_sha"] != commit_sha or session_row["tree_sha"] != tree_sha:
        raise MyGithub12Error(
            "DEVELOPMENT_SESSION_RECOVERY_REQUIRED",
            "development session Git identity changed during validation generation recovery",
        )

    events = db.execute(
        """SELECT * FROM development_session_events
           WHERE session_id=? AND session_revision<=? ORDER BY id""",
        (session_row["session_id"], current_revision),
    ).fetchall()
    anchors = [
        row for row in events
        if row["event_type"] == "validation_started" and row["to_status"] == expected_status
    ]
    if anchors:
        anchor = anchors[-1]
        generation_revision = int(anchor["session_revision"])
        if generation_revision > current_revision:
            raise MyGithub12Error(
                "DEVELOPMENT_SESSION_RECOVERY_REQUIRED",
                "validation generation revision is ahead of the current Session",
            )
        generation_source = "validation_started_event"
    else:
        current_rows = db.execute(
            """SELECT id FROM development_session_validations
               WHERE session_id=? AND session_revision=? AND mode=? AND commit_sha=?
                 AND (tree_sha=? OR tree_sha='') LIMIT 1""",
            (session_row["session_id"], current_revision, mode, commit_sha, tree_sha),
        ).fetchone()
        if not current_rows:
            raise MyGithub12Error(
                "DEVELOPMENT_SESSION_RECOVERY_REQUIRED",
                "validation generation anchor is missing",
            )
        generation_revision = current_revision
        generation_source = "legacy_current_revision"

    later_validation = db.execute(
        """SELECT id,session_revision,mode,commit_sha,tree_sha,request_id,job_id
           FROM development_session_validations
           WHERE session_id=? AND session_revision>? AND session_revision<=?
           ORDER BY id""",
        (session_row["session_id"], generation_revision, current_revision),
    ).fetchall()
    if later_validation:
        raise MyGithub12Error(
            "DEVELOPMENT_SESSION_RECOVERY_REQUIRED",
            "a later validation correlation exists after the active generation anchor",
            {
                "generation_revision": generation_revision,
                "later_validation_ids": [int(row["id"]) for row in later_validation],
            },
        )

    events_after_generation = [
        row for row in events if generation_revision < int(row["session_revision"]) <= current_revision
    ]
    by_revision: dict[int, list[sqlite3.Row]] = {}
    for row in events_after_generation:
        by_revision.setdefault(int(row["session_revision"]), []).append(row)
    expected_revisions = list(range(generation_revision + 1, current_revision + 1))
    if sorted(by_revision) != expected_revisions:
        raise MyGithub12Error(
            "DEVELOPMENT_SESSION_RECOVERY_REQUIRED",
            "validation generation maintenance audit chain is incomplete",
            {
                "generation_revision": generation_revision,
                "current_session_revision": current_revision,
                "event_revisions": sorted(by_revision),
            },
        )

    generation_workspace_revision = int(session_row["workspace_revision"])
    maintenance_audit: list[dict[str, Any]] = []
    for revision in reversed(expected_revisions):
        revision_events = by_revision[revision]
        maintenance = [
            row for row in revision_events if str(row["event_type"] or "") in _VALIDATION_MAINTENANCE_EVENTS
        ]
        benign_audits = [
            row for row in revision_events
            if str(row["event_type"] or "") in _VALIDATION_NONMUTATING_AUDIT_EVENTS
            and row["from_status"] == expected_status
            and row["to_status"] == expected_status
        ]
        unknown_events = [row for row in revision_events if row not in maintenance and row not in benign_audits]
        if len(maintenance) != 1 or unknown_events:
            raise MyGithub12Error(
                "DEVELOPMENT_SESSION_RECOVERY_REQUIRED",
                "validation generation maintenance audit chain is ambiguous",
                {
                    "session_revision": revision,
                    "maintenance_events": [str(row["event_type"] or "") for row in maintenance],
                    "unknown_events": [str(row["event_type"] or "") for row in unknown_events],
                },
            )
        event = maintenance[0]
        event_type = str(event["event_type"] or "")
        if event["from_status"] != expected_status or event["to_status"] != expected_status:
            raise MyGithub12Error(
                "DEVELOPMENT_SESSION_RECOVERY_REQUIRED",
                "validation generation maintenance changed Session status",
                {"session_revision": revision, "event_type": event_type},
            )
        data = _event_data(event)
        before_workspace_revision: int
        after_workspace_revision: int
        if event_type == "session_recovered":
            before = data.get("before") if isinstance(data.get("before"), dict) else {}
            after = data.get("after") if isinstance(data.get("after"), dict) else {}
            before_workspace_revision = int(before.get("workspace_revision") or -1)
            after_workspace_revision = int(after.get("workspace_revision") or -1)
            identity_preserved = bool(
                data.get("head_changed") is False
                and int(before.get("session_revision") or -1) == revision - 1
                and int(after.get("session_revision") or -1) == revision
                and before.get("head_commit_sha") == commit_sha
                and after.get("head_commit_sha") == commit_sha
                and before.get("tree_sha") == tree_sha
                and after.get("tree_sha") == tree_sha
            )
            if not identity_preserved:
                raise MyGithub12Error(
                    "DEVELOPMENT_SESSION_RECOVERY_REQUIRED",
                    "Session recovery changed validation identity",
                    {"session_revision": revision},
                )
        else:
            before_workspace_revision = int(data.get("before_workspace_revision") or -1)
            after_workspace_revision = int(data.get("after_workspace_revision") or -1)
            if before_workspace_revision < 0 or after_workspace_revision != before_workspace_revision + 1:
                raise MyGithub12Error(
                    "DEVELOPMENT_SESSION_RECOVERY_REQUIRED",
                    "Workspace lease maintenance revision chain is invalid",
                    {"session_revision": revision},
                )
        if after_workspace_revision != generation_workspace_revision:
            raise MyGithub12Error(
                "DEVELOPMENT_SESSION_RECOVERY_REQUIRED",
                "validation generation Workspace revision chain is discontinuous",
                {
                    "session_revision": revision,
                    "expected_after_workspace_revision": generation_workspace_revision,
                    "actual_after_workspace_revision": after_workspace_revision,
                },
            )
        maintenance_audit.append({
            "session_revision": revision,
            "event_type": event_type,
            "before_workspace_revision": before_workspace_revision,
            "after_workspace_revision": after_workspace_revision,
        })
        generation_workspace_revision = before_workspace_revision

    return {
        "generation_revision": generation_revision,
        "generation_workspace_revision": generation_workspace_revision,
        "current_session_revision": current_revision,
        "current_workspace_revision": int(session_row["workspace_revision"]),
        "source": generation_source,
        "maintenance_events": list(reversed(maintenance_audit)),
    }


def validation_generation_context(
    session_id: str,
    current_session_revision: int,
    mode: str,
    commit_sha: str,
    tree_sha: str,
) -> dict[str, Any]:
    init_session_db()
    with _db() as db:
        session_row = db.execute(
            "SELECT * FROM development_sessions WHERE session_id=?", (session_id,),
        ).fetchone()
        if not session_row:
            raise MyGithub12Error(
                "DEVELOPMENT_SESSION_NOT_FOUND", "development session was not found",
                {"development_session_id": session_id},
            )
        if int(session_row["session_revision"]) != int(current_session_revision):
            raise MyGithub12Error(
                "DEVELOPMENT_SESSION_REVISION_MISMATCH", "development session revision changed",
                {"expected": int(current_session_revision), "actual": int(session_row["session_revision"])},
            )
        return _validation_generation_context_db(db, session_row, mode, commit_sha, tree_sha)


def bind_validation_request_worker(
    session_id: str,
    expected_session_revision: int,
    expected_workspace_revision: int,
    mode: str,
    commit_sha: str,
    tree_sha: str,
    request_id: str,
    job_id: str,
    *,
    validation_generation_revision: int = 0,
    validation_generation_workspace_revision: int = 0,
    allow_branch_drift: bool = False,
    request_terminal_status: str = "",
) -> dict[str, Any]:
    """CAS-bind one durable Request/Worker pair or proven request-only terminal."""
    request_only_terminal = bool(
        not job_id and request_terminal_status in REQUEST_ONLY_TERMINAL_STATUSES
    )
    if mode not in {"fast", "full"} or not request_id or (not job_id and not request_only_terminal):
        raise MyGithub12Error(
            "DEVELOPMENT_SESSION_RECOVERY_REQUIRED", "validation correlation identity is incomplete"
        )
    expected_status = "validating_fast" if mode == "fast" else "validating_full"
    init_session_db()
    with _LOCK, _db() as db:
        session_row = db.execute(
            "SELECT * FROM development_sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if not session_row:
            raise MyGithub12Error(
                "DEVELOPMENT_SESSION_NOT_FOUND", "development session was not found",
                {"development_session_id": session_id},
            )
        if int(session_row["session_revision"]) != int(expected_session_revision):
            raise MyGithub12Error(
                "DEVELOPMENT_SESSION_REVISION_MISMATCH", "development session revision changed",
                {"expected": int(expected_session_revision), "actual": int(session_row["session_revision"])},
            )
        if session_row["status"] != expected_status:
            raise MyGithub12Error(
                "DEVELOPMENT_SESSION_STATE_INVALID", "development session validation phase changed",
                {"expected_status": expected_status, "actual_status": session_row["status"]},
            )
        if session_row["head_commit_sha"] != commit_sha or session_row["tree_sha"] != tree_sha:
            raise MyGithub12Error(
                "DEVELOPMENT_SESSION_RECOVERY_REQUIRED", "development session Git identity changed during validation recovery"
            )
        session_workspace_revision = int(session_row["workspace_revision"])
        workspace_row = db.execute(
            "SELECT * FROM workspaces WHERE workspace_id=?", (session_row["workspace_id"],)
        ).fetchone()
        if not workspace_row:
            raise MyGithub12Error("WORKSPACE_NOT_FOUND", "validation recovery Workspace was not found")
        if int(workspace_row["revision"]) != int(expected_workspace_revision):
            raise MyGithub12Error(
                "WORKSPACE_REVISION_MISMATCH", "Workspace revision changed during validation recovery",
                {"expected": int(expected_workspace_revision), "actual": int(workspace_row["revision"])},
            )
        drift_reconciliation = bool(
            allow_branch_drift
            and workspace_row["status"] == "drifted"
            and workspace_row["drift_reason"] == "branch_moved_externally"
            and session_workspace_revision < int(expected_workspace_revision)
            and (
                workspace_row["head_sha"] != session_row["head_commit_sha"]
                or workspace_row["tree_sha"] != session_row["tree_sha"]
            )
        )
        if session_workspace_revision != int(expected_workspace_revision) and not drift_reconciliation:
            raise MyGithub12Error(
                "DEVELOPMENT_SESSION_WORKSPACE_MISMATCH", "session does not reference the expected Workspace revision"
            )
        if not drift_reconciliation:
            if workspace_row["status"] != "active" or workspace_row["drift_reason"]:
                raise MyGithub12Error("WORKSPACE_BRANCH_DRIFTED", "validation recovery requires an active non-drifted Workspace")
            if float(workspace_row["lease_expires_at"] or 0) <= _now():
                raise MyGithub12Error("WORKSPACE_LEASE_REQUIRED", "validation recovery requires a live Workspace lease")
        static_identity = (
            workspace_row["repository"] == session_row["repository"]
            and workspace_row["branch"] == session_row["branch"]
            and workspace_row["base_branch"] == session_row["base_branch"]
            and workspace_row["base_commit_sha"] == session_row["base_commit_sha"]
        )
        code_identity = (
            workspace_row["head_sha"] == session_row["head_commit_sha"]
            and workspace_row["tree_sha"] == session_row["tree_sha"]
        )
        if not static_identity or (not drift_reconciliation and not code_identity):
            raise MyGithub12Error(
                "DEVELOPMENT_SESSION_WORKSPACE_MISMATCH", "Session and Workspace identities differ during validation recovery"
            )

        exact_generation = int(validation_generation_revision or 0) > 0
        if exact_generation:
            generation = _validation_generation_context_db(db, session_row, mode, commit_sha, tree_sha)
            if (
                int(generation["generation_revision"]) != int(validation_generation_revision)
                or int(generation["generation_workspace_revision"]) != int(validation_generation_workspace_revision)
            ):
                raise MyGithub12Error(
                    "DEVELOPMENT_SESSION_RECOVERY_REQUIRED",
                    "validation generation identity changed during correlation binding",
                    {
                        "expected_generation_revision": int(validation_generation_revision),
                        "actual_generation_revision": int(generation["generation_revision"]),
                        "expected_generation_workspace_revision": int(validation_generation_workspace_revision),
                        "actual_generation_workspace_revision": int(generation["generation_workspace_revision"]),
                    },
                )
            rows = db.execute(
                """SELECT * FROM development_session_validations
                   WHERE session_id=? AND session_revision=? AND mode=? AND commit_sha=?
                     AND (tree_sha=? OR tree_sha='') ORDER BY id DESC""",
                (session_id, int(validation_generation_revision), mode, commit_sha, tree_sha),
            ).fetchall()
        else:
            rows = db.execute(
                """SELECT * FROM development_session_validations
                   WHERE session_id=? AND session_revision<=? AND mode=? AND commit_sha=?
                     AND (tree_sha=? OR tree_sha='') ORDER BY id DESC""",
                (session_id, expected_session_revision, mode, commit_sha, tree_sha),
            ).fetchall()
        if not rows:
            raise MyGithub12Error(
                "DEVELOPMENT_SESSION_RECOVERY_REQUIRED", "validation recovery has no persisted correlation row"
            )
        decorated = []
        distinct_request_ids: set[str] = set()
        for row in rows:
            try:
                evidence_value = json.loads(row["evidence_json"] or "{}")
            except (TypeError, ValueError):
                evidence_value = {}
            logical_request_id = _validation_request_id(row, evidence_value)
            if logical_request_id:
                distinct_request_ids.add(logical_request_id)
            decorated.append((row, evidence_value, logical_request_id, str(row["job_id"] or "")))
        if distinct_request_ids and distinct_request_ids != {request_id}:
            raise MyGithub12Error(
                "DEVELOPMENT_SESSION_RECOVERY_REQUIRED", "multiple logical validation request_ids are persisted",
                {"request_ids": sorted(distinct_request_ids)},
            )
        strict_job_ids = {item[3] for item in decorated if item[3]}
        if request_only_terminal and strict_job_ids:
            raise MyGithub12Error(
                "DEVELOPMENT_SESSION_RECOVERY_REQUIRED", "request-only terminal conflicts with a persisted Worker correlation",
                {"job_ids": sorted(strict_job_ids)},
            )
        if not request_only_terminal and any(value != job_id for value in strict_job_ids):
            raise MyGithub12Error(
                "DEVELOPMENT_SESSION_RECOVERY_REQUIRED", "persisted validation Worker correlation conflicts with the durable Request",
                {"job_ids": sorted(strict_job_ids), "expected_job_id": job_id},
            )
        unowned = [int(item[0]["id"]) for item in decorated if not item[2] and not item[3]]
        if unowned:
            raise MyGithub12Error(
                "DEVELOPMENT_SESSION_RECOVERY_REQUIRED", "legacy validation placeholder has neither request_id nor strict job correlation",
                {"validation_ids": unowned},
            )
        if request_only_terminal:
            candidates = [item for item in decorated if item[2] == request_id]
        else:
            candidates = [
                item for item in decorated
                if item[2] == request_id or (not item[2] and item[3] == job_id)
            ]
        if not candidates:
            raise MyGithub12Error(
                "DEVELOPMENT_SESSION_RECOVERY_REQUIRED", "durable Request/Worker pair does not own a persisted validation row"
            )
        first_class = [item for item in candidates if str(item[0]["request_id"] or "") == request_id]
        canonical = (first_class or candidates)[0]
        canonical_row = canonical[0]
        existing_job_id = str(canonical_row["job_id"] or "")
        if request_only_terminal and existing_job_id:
            raise MyGithub12Error(
                "DEVELOPMENT_SESSION_RECOVERY_REQUIRED", "request-only terminal is already bound to a Worker job"
            )
        if not request_only_terminal and existing_job_id and existing_job_id != job_id:
            raise MyGithub12Error(
                "DEVELOPMENT_SESSION_RECOVERY_REQUIRED", "canonical validation row is bound to another Worker job"
            )
        try:
            if request_only_terminal:
                db.execute(
                    """UPDATE development_session_validations
                       SET request_id=?,status=?,finished_at=COALESCE(finished_at,?) WHERE id=?""",
                    (request_id, request_terminal_status, _now(), int(canonical_row["id"])),
                )
            else:
                db.execute(
                    "UPDATE development_session_validations SET request_id=?,job_id=? WHERE id=?",
                    (request_id, job_id, int(canonical_row["id"])),
                )
        except sqlite3.IntegrityError as exc:
            raise MyGithub12Error(
                "DEVELOPMENT_SESSION_RECOVERY_REQUIRED", "request_id uniqueness changed during validation recovery"
            ) from exc
        duplicate_ids = [int(item[0]["id"]) for item in candidates if int(item[0]["id"]) != int(canonical_row["id"])]
        audit = {
            "mode": mode,
            "request_id": request_id,
            "job_id": job_id or None,
            "canonical_validation_id": int(canonical_row["id"]),
            "duplicate_validation_ids": duplicate_ids,
            "logical_duplicate_count": len(duplicate_ids),
            "workspace_drift_reconciliation": drift_reconciliation,
            "request_only_terminal": request_only_terminal,
            "request_terminal_status": request_terminal_status or None,
        }
        _append_event(
            db, session_row, "validation_correlation_backfilled",
            session_row["status"], session_row["status"], int(session_row["session_revision"]), audit,
        )
    return audit


def reconcile_terminal_validation_set(
    session_id: str,
    expected_session_revision: int,
    expected_workspace_revision: int,
    mode: str,
    commit_sha: str,
    tree_sha: str,
    correlations: list[dict[str, Any]],
    *,
    validation_generation_revision: int,
    validation_generation_workspace_revision: int,
    allow_branch_drift: bool = False,
) -> dict[str, Any]:
    """Atomically settle one exact terminal validation generation.

    The current Session CAS may be newer than the validation rows only when a
    server-proven identity-preserving maintenance event chain bridges the exact
    generation revision to the current transient Session.  No member becomes
    authoritative merge evidence.
    """
    terminal_statuses = {
        "passed", "failed", "timed_out", "cancelled", "worker_lost", "internal_error",
    }
    if mode not in {"fast", "full"} or len(correlations) < 2:
        raise MyGithub12Error(
            "DEVELOPMENT_SESSION_RECOVERY_REQUIRED",
            "terminal validation correlation set is incomplete",
        )
    normalized: list[dict[str, str]] = []
    for item in correlations:
        request_id = str(item.get("request_id") or "")
        job_id = str(item.get("job_id") or "")
        status = str(item.get("status") or "")
        if not request_id or not job_id or status not in terminal_statuses:
            raise MyGithub12Error(
                "DEVELOPMENT_SESSION_RECOVERY_REQUIRED",
                "terminal validation correlation set contains incomplete or non-terminal evidence",
                {"request_id": request_id or None, "job_id": job_id or None, "status": status or None},
            )
        normalized.append({"request_id": request_id, "job_id": job_id, "status": status})
    request_ids = {item["request_id"] for item in normalized}
    job_ids = {item["job_id"] for item in normalized}
    if len(request_ids) != len(normalized) or len(job_ids) != len(normalized):
        raise MyGithub12Error(
            "DEVELOPMENT_SESSION_RECOVERY_REQUIRED",
            "terminal validation correlation set contains duplicate Request or Worker identity",
            {"request_ids": sorted(request_ids), "job_ids": sorted(job_ids)},
        )

    expected_status = "validating_fast" if mode == "fast" else "validating_full"
    init_session_db()
    with _LOCK, _db() as db:
        session_row = db.execute(
            "SELECT * FROM development_sessions WHERE session_id=?", (session_id,),
        ).fetchone()
        if not session_row:
            raise MyGithub12Error(
                "DEVELOPMENT_SESSION_NOT_FOUND", "development session was not found",
                {"development_session_id": session_id},
            )
        if int(session_row["session_revision"]) != int(expected_session_revision):
            raise MyGithub12Error(
                "DEVELOPMENT_SESSION_REVISION_MISMATCH", "development session revision changed",
                {"expected": int(expected_session_revision), "actual": int(session_row["session_revision"])},
            )
        if session_row["status"] != expected_status:
            raise MyGithub12Error(
                "DEVELOPMENT_SESSION_STATE_INVALID", "development session validation phase changed",
                {"expected_status": expected_status, "actual_status": session_row["status"]},
            )
        if session_row["head_commit_sha"] != commit_sha or session_row["tree_sha"] != tree_sha:
            raise MyGithub12Error(
                "DEVELOPMENT_SESSION_RECOVERY_REQUIRED",
                "development session Git identity changed during validation set recovery",
            )
        workspace_row = db.execute(
            "SELECT * FROM workspaces WHERE workspace_id=?", (session_row["workspace_id"],),
        ).fetchone()
        if not workspace_row:
            raise MyGithub12Error("WORKSPACE_NOT_FOUND", "validation recovery Workspace was not found")
        if int(workspace_row["revision"]) != int(expected_workspace_revision):
            raise MyGithub12Error(
                "WORKSPACE_REVISION_MISMATCH", "Workspace revision changed during validation recovery",
                {"expected": int(expected_workspace_revision), "actual": int(workspace_row["revision"])},
            )
        session_workspace_revision = int(session_row["workspace_revision"])
        drift_reconciliation = bool(
            allow_branch_drift
            and workspace_row["status"] == "drifted"
            and workspace_row["drift_reason"] == "branch_moved_externally"
            and session_workspace_revision < int(expected_workspace_revision)
            and (
                workspace_row["head_sha"] != session_row["head_commit_sha"]
                or workspace_row["tree_sha"] != session_row["tree_sha"]
            )
        )
        if session_workspace_revision != int(expected_workspace_revision) and not drift_reconciliation:
            raise MyGithub12Error(
                "DEVELOPMENT_SESSION_WORKSPACE_MISMATCH",
                "session does not reference the expected Workspace revision",
            )
        if not drift_reconciliation:
            if workspace_row["status"] != "active" or workspace_row["drift_reason"]:
                raise MyGithub12Error(
                    "WORKSPACE_BRANCH_DRIFTED", "validation recovery requires an active non-drifted Workspace",
                )
            if float(workspace_row["lease_expires_at"] or 0) <= _now():
                raise MyGithub12Error(
                    "WORKSPACE_LEASE_REQUIRED", "validation recovery requires a live Workspace lease",
                )
        static_identity = (
            workspace_row["repository"] == session_row["repository"]
            and workspace_row["branch"] == session_row["branch"]
            and workspace_row["base_branch"] == session_row["base_branch"]
            and workspace_row["base_commit_sha"] == session_row["base_commit_sha"]
        )
        code_identity = (
            workspace_row["head_sha"] == session_row["head_commit_sha"]
            and workspace_row["tree_sha"] == session_row["tree_sha"]
        )
        if not static_identity or (not drift_reconciliation and not code_identity):
            raise MyGithub12Error(
                "DEVELOPMENT_SESSION_WORKSPACE_MISMATCH",
                "Session and Workspace identities differ during validation recovery",
            )

        generation = _validation_generation_context_db(db, session_row, mode, commit_sha, tree_sha)
        if (
            int(generation["generation_revision"]) != int(validation_generation_revision)
            or int(generation["generation_workspace_revision"]) != int(validation_generation_workspace_revision)
        ):
            raise MyGithub12Error(
                "DEVELOPMENT_SESSION_RECOVERY_REQUIRED",
                "validation generation identity changed during terminal-set recovery",
                {
                    "expected_generation_revision": int(validation_generation_revision),
                    "actual_generation_revision": int(generation["generation_revision"]),
                    "expected_generation_workspace_revision": int(validation_generation_workspace_revision),
                    "actual_generation_workspace_revision": int(generation["generation_workspace_revision"]),
                },
            )

        rows = db.execute(
            """SELECT * FROM development_session_validations
               WHERE session_id=? AND session_revision=? ORDER BY id DESC""",
            (session_id, int(validation_generation_revision)),
        ).fetchall()
        if not rows:
            raise MyGithub12Error(
                "DEVELOPMENT_SESSION_RECOVERY_REQUIRED", "validation recovery has no generation correlations",
            )
        decorated: list[tuple[sqlite3.Row, dict[str, Any], str, str]] = []
        persisted_request_ids: set[str] = set()
        for row in rows:
            try:
                evidence_value = json.loads(row["evidence_json"] or "{}")
            except (TypeError, ValueError):
                evidence_value = {}
            if not isinstance(evidence_value, dict):
                evidence_value = {}
            logical_request_id = _validation_request_id(row, evidence_value)
            row_job_id = str(row["job_id"] or "")
            row_tree = str(row["tree_sha"] or "")
            if (
                row["mode"] != mode
                or row["commit_sha"] != commit_sha
                or row_tree not in {"", tree_sha}
            ):
                raise MyGithub12Error(
                    "DEVELOPMENT_SESSION_RECOVERY_REQUIRED",
                    "current Session revision owns another validation identity",
                    {"validation_id": int(row["id"])},
                )
            if not logical_request_id:
                raise MyGithub12Error(
                    "DEVELOPMENT_SESSION_RECOVERY_REQUIRED",
                    "current validation correlation is not owned by a durable Request",
                    {"validation_id": int(row["id"])},
                )
            persisted_request_ids.add(logical_request_id)
            decorated.append((row, evidence_value, logical_request_id, row_job_id))
        if persisted_request_ids != request_ids:
            raise MyGithub12Error(
                "DEVELOPMENT_SESSION_RECOVERY_REQUIRED",
                "terminal validation set does not cover the complete current Session revision",
                {"persisted_request_ids": sorted(persisted_request_ids), "request_ids": sorted(request_ids)},
            )

        validation_ids: list[int] = []
        duplicate_validation_ids: list[int] = []
        pair_audit: list[dict[str, Any]] = []
        for pair in sorted(normalized, key=lambda item: item["request_id"]):
            candidates = [item for item in decorated if item[2] == pair["request_id"]]
            if not candidates:
                raise MyGithub12Error(
                    "DEVELOPMENT_SESSION_RECOVERY_REQUIRED",
                    "terminal validation Request does not own a persisted correlation row",
                    {"request_id": pair["request_id"]},
                )
            strict_job_ids = {item[3] for item in candidates if item[3]}
            if strict_job_ids and strict_job_ids != {pair["job_id"]}:
                raise MyGithub12Error(
                    "DEVELOPMENT_SESSION_RECOVERY_REQUIRED",
                    "persisted validation Worker correlation conflicts with terminal validation set",
                    {"request_id": pair["request_id"], "job_ids": sorted(strict_job_ids), "expected_job_id": pair["job_id"]},
                )
            for row, _evidence, _request_id, _job_id in candidates:
                existing_status = str(row["status"] or "")
                if existing_status in terminal_statuses and existing_status != pair["status"]:
                    raise MyGithub12Error(
                        "DEVELOPMENT_SESSION_RECOVERY_REQUIRED",
                        "persisted validation terminal status conflicts with durable Worker truth",
                        {"request_id": pair["request_id"], "existing_status": existing_status, "status": pair["status"]},
                    )
            first_class = [item for item in candidates if str(item[0]["request_id"] or "") == pair["request_id"]]
            canonical = (first_class or candidates)[0]
            canonical_id = int(canonical[0]["id"])
            validation_ids.append(canonical_id)
            duplicate_ids = [int(item[0]["id"]) for item in candidates if int(item[0]["id"]) != canonical_id]
            duplicate_validation_ids.extend(duplicate_ids)
            try:
                db.execute(
                    """UPDATE development_session_validations
                       SET request_id=?,job_id=?,tree_sha=CASE WHEN tree_sha='' THEN ? ELSE tree_sha END,
                           status=?,merge_eligible=0,attestation_id=NULL,finished_at=COALESCE(finished_at,?)
                       WHERE id=?""",
                    (pair["request_id"], pair["job_id"], tree_sha, pair["status"], _now(), canonical_id),
                )
            except sqlite3.IntegrityError as exc:
                raise MyGithub12Error(
                    "DEVELOPMENT_SESSION_RECOVERY_REQUIRED",
                    "request_id uniqueness changed during validation set recovery",
                ) from exc
            for duplicate_id in duplicate_ids:
                db.execute(
                    """UPDATE development_session_validations
                       SET status=?,merge_eligible=0,attestation_id=NULL,finished_at=COALESCE(finished_at,?)
                       WHERE id=?""",
                    (pair["status"], _now(), duplicate_id),
                )
            pair_audit.append(dict(pair))

        audit = {
            "mode": mode,
            "request_ids": sorted(request_ids),
            "job_ids": sorted(job_ids),
            "terminal_correlations": pair_audit,
            "validation_ids": validation_ids,
            "duplicate_validation_ids": sorted(duplicate_validation_ids),
            "validation_generation_revision": int(generation["generation_revision"]),
            "validation_generation_workspace_revision": int(generation["generation_workspace_revision"]),
            "validation_generation_source": generation.get("source"),
            "validation_generation_maintenance_events": generation.get("maintenance_events", []),
            "workspace_drift_reconciliation": drift_reconciliation,
            "commit_sha": commit_sha,
            "tree_sha": tree_sha,
            "base_sha": str(session_row["base_commit_sha"]),
        }
        now = _now()
        job_field = "last_fast_ci_job_id" if mode == "fast" else "last_full_ci_job_id"
        cursor = db.execute(
            f"""UPDATE development_sessions
                SET status='active',session_revision=session_revision+1,updated_at=?,
                    {job_field}=NULL,last_attestation_id=NULL,last_failure_resource_uri=NULL
                WHERE session_id=? AND session_revision=? AND status=?""",
            (now, session_id, int(expected_session_revision), expected_status),
        )
        if cursor.rowcount != 1:
            raise MyGithub12Error(
                "DEVELOPMENT_SESSION_REVISION_MISMATCH",
                "development session changed while reconciling terminal validation set",
            )
        updated = db.execute(
            "SELECT * FROM development_sessions WHERE session_id=?", (session_id,),
        ).fetchone()
        _append_event(
            db, updated, "validation_terminal_correlation_set_reconciled",
            expected_status, "active", int(updated["session_revision"]), audit,
        )
    return {"session": _public(updated), "audit": audit}


def list_events(session_id: str, limit: int = 100) -> list[dict[str, Any]]:
    get_session(session_id)
    with _db() as db:
        rows = db.execute("SELECT * FROM development_session_events WHERE session_id=? ORDER BY id DESC LIMIT ?", (session_id, max(1, min(limit, 500)))).fetchall()
    out=[]
    for row in reversed(rows):
        value=dict(row); value["data"]=json.loads(value.pop("data_json") or "{}"); out.append(value)
    return out


def recover_sessions(workspace_getter) -> dict[str, int]:
    """Fail-stop ambiguous startup phases while preserving reconcilable validation evidence."""
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
        # validating_* is deliberately preserved. resume_task reconciles it
        # from the exact persisted validation/job correlation after restart.
        if row["status"] in {"preparing", "closing"}:
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
