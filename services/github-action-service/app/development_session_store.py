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


def record_validation(session_id: str, session_revision: int, mode: str, commit_sha: str, tree_sha: str, *, job_id: str = "", status: str = "queued", merge_eligible: bool = False, attestation_id: str = "", evidence: dict[str, Any] | None = None, finished: bool = False) -> int:
    init_session_db()
    get_session(session_id)
    with _LOCK, _db() as db:
        existing = db.execute(
            """SELECT id FROM development_session_validations
               WHERE session_id=? AND session_revision=? AND mode=? AND commit_sha=?
                 AND tree_sha=? AND job_id=? ORDER BY id DESC LIMIT 1""",
            (session_id, session_revision, mode, commit_sha, tree_sha, job_id or None),
        ).fetchone()
        if existing:
            db.execute(
                """UPDATE development_session_validations
                   SET status=?,merge_eligible=?,attestation_id=?,evidence_json=?,finished_at=?
                   WHERE id=?""",
                (
                    status,
                    1 if merge_eligible else 0,
                    attestation_id or None,
                    json.dumps(evidence or {}, ensure_ascii=False, separators=(",", ":")),
                    _now() if finished else None,
                    int(existing["id"]),
                ),
            )
            return int(existing["id"])
        cur = db.execute(
            """INSERT INTO development_session_validations(session_id,session_revision,mode,commit_sha,tree_sha,job_id,status,merge_eligible,attestation_id,evidence_json,created_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (session_id, session_revision, mode, commit_sha, tree_sha, job_id or None, status, 1 if merge_eligible else 0, attestation_id or None, json.dumps(evidence or {}, ensure_ascii=False, separators=(",", ":")), _now(), _now() if finished else None),
        )
        return int(cur.lastrowid)


def validation_correlations(
    session_id: str,
    session_revision: int,
    mode: str,
    commit_sha: str,
    tree_sha: str,
) -> list[dict[str, Any]]:
    """Return exact persisted validation/job correlations for reconciliation."""
    init_session_db()
    get_session(session_id)
    with _db() as db:
        rows = db.execute(
            """SELECT * FROM development_session_validations
               WHERE session_id=? AND session_revision<=? AND mode=?
                 AND commit_sha=? AND (tree_sha=? OR tree_sha='') AND job_id IS NOT NULL
               ORDER BY id DESC""",
            (session_id, session_revision, mode, commit_sha, tree_sha),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["merge_eligible"] = bool(item["merge_eligible"])
        item["evidence"] = json.loads(item.pop("evidence_json") or "{}")
        result.append(item)
    return result


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
