"""Durable Convergence Run persistence primitives for Web-safe CI.

DEV-006 intentionally stops at the persistence boundary.  This module stores
stable convergence identity, exact Git/session/workspace evidence, analysis
readiness, durable CI Request/Worker identities, terminal evidence and
revision-CAS transitions.  It does not start Index or CI work and it does not
change the public convergence orchestration state machine.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from typing import Any, Mapping

from app import mygithub12 as core

MyGithub12Error = core.MyGithub12Error
_db = core._db
_LOCK = core._LOCK
_now = core._now

VALID_MODES = frozenset({"fast", "full"})
CONVERGENCE_PHASES = (
    "accepted",
    "index_requested",
    "analysis_pending",
    "ci_requested",
    "ci_running",
    "post_ci_finalize",
    "passed",
    "failed",
    "blocked",
)
TERMINAL_PHASES = frozenset({"passed", "failed", "blocked"})
ANALYSIS_STAGES = (
    "index",
    "change_context",
    "change_impact",
    "contract_detection",
    "affected_tests",
)
ANALYSIS_STATES = frozenset({"pending", "ready", "failed", "degraded"})

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_EVENT_METADATA_LIMIT_BYTES = 8192


def _error(code: str, message: str, details: dict[str, Any] | None = None) -> MyGithub12Error:
    return MyGithub12Error(code, message, details or {})


def _canonical_hash(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_sha(name: str, value: str) -> None:
    if not _SHA_RE.fullmatch(value or ""):
        raise ValueError(f"{name} must be a lowercase 40-character SHA")


def _validate_create_identity(
    *,
    repository: str,
    branch: str,
    development_session_id: str,
    workspace_id: str,
    session_revision: int,
    workspace_revision: int,
    head_sha: str,
    tree_sha: str,
    base_branch: str,
    base_sha: str,
    mode: str,
    caller_identity_hash: str,
) -> None:
    required = {
        "repository": repository,
        "branch": branch,
        "development_session_id": development_session_id,
        "workspace_id": workspace_id,
        "base_branch": base_branch,
    }
    empty = sorted(key for key, value in required.items() if not str(value or "").strip())
    if empty:
        raise ValueError(f"required convergence identity is empty: {','.join(empty)}")
    if mode not in VALID_MODES:
        raise ValueError("mode must be fast or full")
    if int(session_revision) < 0 or int(workspace_revision) < 0:
        raise ValueError("session/workspace revision must be non-negative")
    _validate_sha("head_sha", head_sha)
    _validate_sha("tree_sha", tree_sha)
    _validate_sha("base_sha", base_sha)
    if caller_identity_hash and not _HASH_RE.fullmatch(caller_identity_hash):
        raise ValueError("caller_identity_hash must be a lowercase 64-character SHA-256")


def _request_identity(
    *,
    repository: str,
    branch: str,
    development_session_id: str,
    workspace_id: str,
    head_sha: str,
    tree_sha: str,
    base_branch: str,
    base_sha: str,
    mode: str,
    caller_identity_hash: str,
) -> dict[str, Any]:
    # Session/Workspace revisions are creation-time evidence, not Run identity.
    # A lease/scope revision change must not create a second active convergence
    # for the same exact Session/Git/base/mode semantics.
    return {
        "repository": repository,
        "branch": branch,
        "development_session_id": development_session_id,
        "workspace_id": workspace_id,
        "head_sha": head_sha,
        "tree_sha": tree_sha,
        "base_branch": base_branch,
        "base_sha": base_sha,
        "mode": mode,
        "caller_identity_hash": caller_identity_hash or None,
    }


def _bounded_metadata(data: Mapping[str, Any] | None) -> str:
    encoded = json.dumps(dict(data or {}), ensure_ascii=False, separators=(",", ":"))
    raw = encoded.encode("utf-8")
    if len(raw) <= _EVENT_METADATA_LIMIT_BYTES:
        return encoded
    return json.dumps(
        {
            "truncated": True,
            "size_bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        },
        separators=(",", ":"),
    )


def init_convergence_db() -> None:
    """Add DEV-006 tables/indexes without rewriting existing durable evidence."""
    core.init_db()
    with _LOCK, _db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS development_convergences(
              convergence_id TEXT PRIMARY KEY,
              repository TEXT NOT NULL,
              branch TEXT NOT NULL,
              development_session_id TEXT NOT NULL,
              workspace_id TEXT NOT NULL,
              session_revision INTEGER NOT NULL CHECK(session_revision >= 0),
              workspace_revision INTEGER NOT NULL CHECK(workspace_revision >= 0),
              head_sha TEXT NOT NULL,
              tree_sha TEXT NOT NULL,
              base_branch TEXT NOT NULL,
              base_sha TEXT NOT NULL,
              mode TEXT NOT NULL CHECK(mode IN ('fast','full')),
              phase TEXT NOT NULL,
              status TEXT NOT NULL,
              revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0),
              terminal INTEGER NOT NULL DEFAULT 0 CHECK(terminal IN (0,1)),
              index_job_id TEXT,
              ci_request_id TEXT,
              ci_job_id TEXT,
              attestation_id TEXT,
              failure_pack_id TEXT,
              error_code TEXT,
              error_message TEXT,
              idempotency_key TEXT,
              request_identity_hash TEXT NOT NULL,
              caller_identity_hash TEXT,
              created_at REAL NOT NULL,
              updated_at REAL NOT NULL,
              finished_at REAL
            );

            CREATE UNIQUE INDEX IF NOT EXISTS uq_development_convergence_idempotency
              ON development_convergences(repository,idempotency_key)
              WHERE idempotency_key IS NOT NULL AND idempotency_key <> '';

            CREATE UNIQUE INDEX IF NOT EXISTS uq_active_development_convergence_identity
              ON development_convergences(
                repository,development_session_id,head_sha,tree_sha,mode,base_branch,base_sha
              )
              WHERE terminal = 0;

            CREATE INDEX IF NOT EXISTS idx_development_convergence_workspace
              ON development_convergences(workspace_id,updated_at);
            CREATE INDEX IF NOT EXISTS idx_development_convergence_ci_request
              ON development_convergences(ci_request_id);
            CREATE INDEX IF NOT EXISTS idx_development_convergence_ci_job
              ON development_convergences(ci_job_id);

            CREATE TABLE IF NOT EXISTS development_convergence_analysis(
              convergence_id TEXT NOT NULL,
              stage TEXT NOT NULL,
              state TEXT NOT NULL CHECK(state IN ('pending','ready','failed','degraded')),
              resource_uri TEXT,
              resource_identity TEXT,
              error_code TEXT,
              error_message TEXT,
              updated_at REAL NOT NULL,
              PRIMARY KEY(convergence_id,stage),
              FOREIGN KEY(convergence_id) REFERENCES development_convergences(convergence_id)
                ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS development_convergence_events(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              convergence_id TEXT NOT NULL,
              revision INTEGER NOT NULL,
              event_type TEXT NOT NULL,
              from_phase TEXT,
              to_phase TEXT,
              from_status TEXT,
              to_status TEXT,
              metadata_json TEXT NOT NULL DEFAULT '{}',
              created_at REAL NOT NULL,
              FOREIGN KEY(convergence_id) REFERENCES development_convergences(convergence_id)
                ON DELETE CASCADE,
              UNIQUE(convergence_id,revision)
            );
            CREATE INDEX IF NOT EXISTS idx_development_convergence_events_run
              ON development_convergence_events(convergence_id,revision);
            """
        )


def _analysis_rows(db: sqlite3.Connection, convergence_id: str) -> dict[str, dict[str, Any]]:
    rows = db.execute(
        """SELECT stage,state,resource_uri,resource_identity,error_code,error_message,updated_at
           FROM development_convergence_analysis WHERE convergence_id=? ORDER BY stage""",
        (convergence_id,),
    ).fetchall()
    return {
        row["stage"]: {
            "state": row["state"],
            "resource_uri": row["resource_uri"],
            "resource_identity": row["resource_identity"],
            "error_code": row["error_code"],
            "error_message": row["error_message"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    }


def _public(db: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    value["terminal"] = bool(value["terminal"])
    value["analysis"] = _analysis_rows(db, value["convergence_id"])
    return value


def _get_row(db: sqlite3.Connection, convergence_id: str) -> sqlite3.Row:
    row = db.execute(
        "SELECT * FROM development_convergences WHERE convergence_id=?",
        (convergence_id,),
    ).fetchone()
    if not row:
        raise _error(
            "DEVELOPMENT_CONVERGENCE_NOT_FOUND",
            "development convergence was not found",
            {"convergence_id": convergence_id},
        )
    return row


def _require_revision(
    db: sqlite3.Connection, convergence_id: str, expected_revision: int
) -> sqlite3.Row:
    row = _get_row(db, convergence_id)
    if int(row["revision"]) != int(expected_revision):
        raise _error(
            "DEVELOPMENT_CONVERGENCE_REVISION_MISMATCH",
            "development convergence revision changed",
            {
                "convergence_id": convergence_id,
                "expected": int(expected_revision),
                "actual": int(row["revision"]),
            },
        )
    return row


def _insert_event(
    db: sqlite3.Connection,
    *,
    row: sqlite3.Row,
    revision: int,
    event_type: str,
    from_phase: str | None,
    to_phase: str | None,
    from_status: str | None,
    to_status: str | None,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    db.execute(
        """INSERT INTO development_convergence_events(
             convergence_id,revision,event_type,from_phase,to_phase,from_status,to_status,
             metadata_json,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            row["convergence_id"],
            revision,
            event_type,
            from_phase,
            to_phase,
            from_status,
            to_status,
            _bounded_metadata(metadata),
            _now(),
        ),
    )


def _active_row(
    db: sqlite3.Connection,
    *,
    repository: str,
    development_session_id: str,
    head_sha: str,
    tree_sha: str,
    mode: str,
    base_branch: str,
    base_sha: str,
) -> sqlite3.Row | None:
    return db.execute(
        """SELECT * FROM development_convergences
           WHERE repository=? AND development_session_id=? AND head_sha=? AND tree_sha=?
             AND mode=? AND base_branch=? AND base_sha=? AND terminal=0
           ORDER BY created_at LIMIT 1""",
        (
            repository,
            development_session_id,
            head_sha,
            tree_sha,
            mode,
            base_branch,
            base_sha,
        ),
    ).fetchone()


def _assert_active_identity(
    row: sqlite3.Row, *, branch: str, workspace_id: str
) -> None:
    if row["branch"] != branch or row["workspace_id"] != workspace_id:
        raise _error(
            "DEVELOPMENT_CONVERGENCE_IDENTITY_MISMATCH",
            "active convergence identity does not match branch/workspace evidence",
            {
                "convergence_id": row["convergence_id"],
                "branch": row["branch"],
                "workspace_id": row["workspace_id"],
            },
        )


def create_or_get_convergence(
    *,
    repository: str,
    branch: str,
    development_session_id: str,
    workspace_id: str,
    session_revision: int,
    workspace_revision: int,
    head_sha: str,
    tree_sha: str,
    base_branch: str,
    base_sha: str,
    mode: str,
    idempotency_key: str = "",
    caller_identity_hash: str = "",
) -> dict[str, Any]:
    """Create one canonical active Run or return its durable existing identity."""
    _validate_create_identity(
        repository=repository,
        branch=branch,
        development_session_id=development_session_id,
        workspace_id=workspace_id,
        session_revision=session_revision,
        workspace_revision=workspace_revision,
        head_sha=head_sha,
        tree_sha=tree_sha,
        base_branch=base_branch,
        base_sha=base_sha,
        mode=mode,
        caller_identity_hash=caller_identity_hash,
    )
    init_convergence_db()
    identity = _request_identity(
        repository=repository,
        branch=branch,
        development_session_id=development_session_id,
        workspace_id=workspace_id,
        head_sha=head_sha,
        tree_sha=tree_sha,
        base_branch=base_branch,
        base_sha=base_sha,
        mode=mode,
        caller_identity_hash=caller_identity_hash,
    )
    request_identity_hash = _canonical_hash(identity)

    with _LOCK, _db() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            if idempotency_key:
                existing = db.execute(
                    """SELECT * FROM development_convergences
                       WHERE repository=? AND idempotency_key=?""",
                    (repository, idempotency_key),
                ).fetchone()
                if existing:
                    if existing["request_identity_hash"] != request_identity_hash:
                        raise _error(
                            "IDEMPOTENCY_CONFLICT",
                            "idempotency key belongs to a different convergence request",
                            {
                                "idempotency_key": idempotency_key,
                                "convergence_id": existing["convergence_id"],
                            },
                        )
                    result = _public(db, existing)
                    db.commit()
                    result["deduplicated"] = True
                    result["dedupe_reason"] = "idempotency_key"
                    return result

            active = _active_row(
                db,
                repository=repository,
                development_session_id=development_session_id,
                head_sha=head_sha,
                tree_sha=tree_sha,
                mode=mode,
                base_branch=base_branch,
                base_sha=base_sha,
            )
            if active:
                _assert_active_identity(active, branch=branch, workspace_id=workspace_id)
                result = _public(db, active)
                db.commit()
                result["deduplicated"] = True
                result["dedupe_reason"] = "active_identity"
                return result

            convergence_id = "conv_" + uuid.uuid4().hex[:24]
            now = _now()
            db.execute(
                """INSERT INTO development_convergences(
                     convergence_id,repository,branch,development_session_id,workspace_id,
                     session_revision,workspace_revision,head_sha,tree_sha,base_branch,base_sha,
                     mode,phase,status,revision,terminal,idempotency_key,request_identity_hash,
                     caller_identity_hash,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'accepted','accepted',0,0,?,?,?,?,?)""",
                (
                    convergence_id,
                    repository,
                    branch,
                    development_session_id,
                    workspace_id,
                    int(session_revision),
                    int(workspace_revision),
                    head_sha,
                    tree_sha,
                    base_branch,
                    base_sha,
                    mode,
                    idempotency_key or None,
                    request_identity_hash,
                    caller_identity_hash or None,
                    now,
                    now,
                ),
            )
            for stage in ANALYSIS_STAGES:
                db.execute(
                    """INSERT INTO development_convergence_analysis(
                         convergence_id,stage,state,updated_at
                       ) VALUES(?,?,'pending',?)""",
                    (convergence_id, stage, now),
                )
            created = _get_row(db, convergence_id)
            _insert_event(
                db,
                row=created,
                revision=0,
                event_type="convergence_created",
                from_phase=None,
                to_phase="accepted",
                from_status=None,
                to_status="accepted",
                metadata={
                    "session_revision": int(session_revision),
                    "workspace_revision": int(workspace_revision),
                },
            )
            result = _public(db, created)
            db.commit()
        except Exception:
            db.rollback()
            raise

    result["deduplicated"] = False
    result["dedupe_reason"] = None
    return result


def get_convergence(convergence_id: str) -> dict[str, Any]:
    init_convergence_db()
    with _db() as db:
        row = _get_row(db, convergence_id)
        return _public(db, row)


def find_active_convergence(
    *,
    repository: str,
    development_session_id: str,
    head_sha: str,
    tree_sha: str,
    mode: str,
    base_branch: str,
    base_sha: str,
) -> dict[str, Any] | None:
    init_convergence_db()
    with _db() as db:
        row = _active_row(
            db,
            repository=repository,
            development_session_id=development_session_id,
            head_sha=head_sha,
            tree_sha=tree_sha,
            mode=mode,
            base_branch=base_branch,
            base_sha=base_sha,
        )
        return _public(db, row) if row else None


def list_convergences_for_session(
    *,
    repository: str,
    branch: str,
    development_session_id: str,
    base_branch: str,
    base_sha: str,
    modes: tuple[str, ...] = ("full", "fast"),
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return bounded convergence history for one Session/base identity."""
    normalized_modes = tuple(dict.fromkeys(str(mode) for mode in modes))
    if not normalized_modes or any(mode not in VALID_MODES for mode in normalized_modes):
        raise ValueError("modes must contain only fast/full")
    limit = max(1, min(int(limit or 50), 100))
    placeholders = ",".join("?" for _ in normalized_modes)
    init_convergence_db()
    with _db() as db:
        rows = db.execute(
            f"""SELECT * FROM development_convergences
                WHERE repository=? AND branch=? AND development_session_id=?
                  AND base_branch=? AND base_sha=? AND mode IN ({placeholders})
                ORDER BY updated_at DESC, created_at DESC LIMIT ?""",
            (
                repository,
                branch,
                development_session_id,
                base_branch,
                base_sha,
                *normalized_modes,
                limit,
            ),
        ).fetchall()
        return [_public(db, row) for row in rows]


def transition_convergence(
    convergence_id: str,
    expected_revision: int,
    phase: str,
    *,
    status: str = "",
    error_code: str | None = None,
    error_message: str | None = None,
    event_type: str = "phase_changed",
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """CAS-update the durable Run phase without implementing orchestration."""
    if phase not in CONVERGENCE_PHASES:
        raise ValueError("unsupported convergence phase")
    target_status = status or phase
    if not target_status:
        raise ValueError("status must be non-empty")
    terminal = phase in TERMINAL_PHASES
    init_convergence_db()
    with _LOCK, _db() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            row = _require_revision(db, convergence_id, expected_revision)
            if bool(row["terminal"]):
                raise _error(
                    "DEVELOPMENT_CONVERGENCE_STATE_INVALID",
                    "terminal convergence cannot transition again",
                    {"convergence_id": convergence_id, "phase": row["phase"]},
                )
            now = _now()
            finished_at = now if terminal else None
            cursor = db.execute(
                """UPDATE development_convergences
                   SET phase=?,status=?,terminal=?,error_code=?,error_message=?,
                       revision=revision+1,updated_at=?,finished_at=?
                   WHERE convergence_id=? AND revision=?""",
                (
                    phase,
                    target_status,
                    1 if terminal else 0,
                    error_code,
                    error_message,
                    now,
                    finished_at,
                    convergence_id,
                    int(expected_revision),
                ),
            )
            if cursor.rowcount != 1:
                raise _error(
                    "DEVELOPMENT_CONVERGENCE_REVISION_MISMATCH",
                    "development convergence changed while transitioning",
                    {"convergence_id": convergence_id, "expected": int(expected_revision)},
                )
            updated = _get_row(db, convergence_id)
            _insert_event(
                db,
                row=updated,
                revision=int(updated["revision"]),
                event_type=event_type,
                from_phase=row["phase"],
                to_phase=phase,
                from_status=row["status"],
                to_status=target_status,
                metadata=metadata,
            )
            result = _public(db, updated)
            db.commit()
            return result
        except Exception:
            db.rollback()
            raise


def _bind_once(
    convergence_id: str,
    expected_revision: int,
    *,
    field: str,
    value: str,
    event_type: str,
    metadata: Mapping[str, Any] | None = None,
    allow_terminal: bool = False,
) -> dict[str, Any]:
    if field not in {"index_job_id", "ci_request_id", "ci_job_id"}:
        raise ValueError("unsupported convergence identity field")
    if not str(value or "").strip():
        raise ValueError(f"{field} must be non-empty")
    init_convergence_db()
    with _LOCK, _db() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            row = _require_revision(db, convergence_id, expected_revision)
            if bool(row["terminal"]) and not allow_terminal:
                raise _error(
                    "DEVELOPMENT_CONVERGENCE_STATE_INVALID",
                    "terminal convergence cannot bind new execution identity",
                    {"convergence_id": convergence_id, "field": field},
                )
            current = row[field]
            if current:
                if current != value:
                    raise _error(
                        "DEVELOPMENT_CONVERGENCE_IDENTITY_MISMATCH",
                        f"{field} is already bound to a different identity",
                        {"convergence_id": convergence_id, "existing": current, "requested": value},
                    )
                result = _public(db, row)
                db.commit()
                result["deduplicated"] = True
                return result
            now = _now()
            cursor = db.execute(
                f"""UPDATE development_convergences
                    SET {field}=?,revision=revision+1,updated_at=?
                    WHERE convergence_id=? AND revision=?""",
                (value, now, convergence_id, int(expected_revision)),
            )
            if cursor.rowcount != 1:
                raise _error(
                    "DEVELOPMENT_CONVERGENCE_REVISION_MISMATCH",
                    "development convergence changed while binding identity",
                    {"convergence_id": convergence_id, "expected": int(expected_revision)},
                )
            updated = _get_row(db, convergence_id)
            _insert_event(
                db,
                row=updated,
                revision=int(updated["revision"]),
                event_type=event_type,
                from_phase=row["phase"],
                to_phase=updated["phase"],
                from_status=row["status"],
                to_status=updated["status"],
                metadata={field: value, **dict(metadata or {})},
            )
            result = _public(db, updated)
            db.commit()
            result["deduplicated"] = False
            return result
        except Exception:
            db.rollback()
            raise


def bind_index_job(
    convergence_id: str, expected_revision: int, index_job_id: str
) -> dict[str, Any]:
    return _bind_once(
        convergence_id,
        expected_revision,
        field="index_job_id",
        value=index_job_id,
        event_type="index_job_bound",
    )


def bind_ci_request(
    convergence_id: str, expected_revision: int, ci_request_id: str
) -> dict[str, Any]:
    """Persist CI Request identity before a Worker Job necessarily exists."""
    return _bind_once(
        convergence_id,
        expected_revision,
        field="ci_request_id",
        value=ci_request_id,
        event_type="ci_request_bound",
    )


def bind_ci_job(
    convergence_id: str,
    expected_revision: int,
    *,
    ci_request_id: str,
    ci_job_id: str,
) -> dict[str, Any]:
    """Bind a Worker Job only under the already-persisted CI Request identity."""
    init_convergence_db()
    with _LOCK, _db() as db:
        row = _require_revision(db, convergence_id, expected_revision)
        if row["ci_request_id"] != ci_request_id:
            raise _error(
                "DEVELOPMENT_CONVERGENCE_IDENTITY_MISMATCH",
                "worker CI identity does not belong to the persisted CI Request",
                {
                    "convergence_id": convergence_id,
                    "persisted_ci_request_id": row["ci_request_id"],
                    "requested_ci_request_id": ci_request_id,
                },
            )
    return _bind_once(
        convergence_id,
        expected_revision,
        field="ci_job_id",
        value=ci_job_id,
        event_type="ci_job_bound",
        metadata={"ci_request_id": ci_request_id},
    )


def record_analysis_state(
    convergence_id: str,
    expected_revision: int,
    *,
    stage: str,
    state: str,
    resource_uri: str | None = None,
    resource_identity: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> dict[str, Any]:
    """Persist bounded readiness/reference data for one analysis stage using CAS."""
    if stage not in ANALYSIS_STAGES:
        raise ValueError("unsupported analysis stage")
    if state not in ANALYSIS_STATES:
        raise ValueError("unsupported analysis state")
    init_convergence_db()
    with _LOCK, _db() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            row = _require_revision(db, convergence_id, expected_revision)
            if bool(row["terminal"]):
                raise _error(
                    "DEVELOPMENT_CONVERGENCE_STATE_INVALID",
                    "terminal convergence analysis cannot be changed",
                    {"convergence_id": convergence_id, "stage": stage},
                )
            now = _now()
            db.execute(
                """UPDATE development_convergence_analysis
                   SET state=?,resource_uri=?,resource_identity=?,error_code=?,error_message=?,updated_at=?
                   WHERE convergence_id=? AND stage=?""",
                (
                    state,
                    resource_uri,
                    resource_identity,
                    error_code,
                    error_message,
                    now,
                    convergence_id,
                    stage,
                ),
            )
            cursor = db.execute(
                """UPDATE development_convergences SET revision=revision+1,updated_at=?
                   WHERE convergence_id=? AND revision=?""",
                (now, convergence_id, int(expected_revision)),
            )
            if cursor.rowcount != 1:
                raise _error(
                    "DEVELOPMENT_CONVERGENCE_REVISION_MISMATCH",
                    "development convergence changed while recording analysis readiness",
                    {"convergence_id": convergence_id, "expected": int(expected_revision)},
                )
            updated = _get_row(db, convergence_id)
            _insert_event(
                db,
                row=updated,
                revision=int(updated["revision"]),
                event_type="analysis_state_recorded",
                from_phase=row["phase"],
                to_phase=updated["phase"],
                from_status=row["status"],
                to_status=updated["status"],
                metadata={"stage": stage, "state": state, "resource_identity": resource_identity},
            )
            result = _public(db, updated)
            db.commit()
            return result
        except Exception:
            db.rollback()
            raise


def record_terminal_evidence(
    convergence_id: str,
    expected_revision: int,
    *,
    attestation_id: str | None = None,
    failure_pack_id: str | None = None,
) -> dict[str, Any]:
    """CAS-persist terminal evidence identities without storing evidence bodies."""
    if not attestation_id and not failure_pack_id:
        raise ValueError("attestation_id or failure_pack_id is required")
    init_convergence_db()
    with _LOCK, _db() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            row = _require_revision(db, convergence_id, expected_revision)
            requested = {
                "attestation_id": attestation_id,
                "failure_pack_id": failure_pack_id,
            }
            updates: dict[str, str] = {}
            for field, value in requested.items():
                if not value:
                    continue
                current = row[field]
                if current and current != value:
                    raise _error(
                        "DEVELOPMENT_CONVERGENCE_IDENTITY_MISMATCH",
                        f"{field} is already bound to a different identity",
                        {"convergence_id": convergence_id, "existing": current, "requested": value},
                    )
                if not current:
                    updates[field] = value
            if not updates:
                result = _public(db, row)
                db.commit()
                result["deduplicated"] = True
                return result
            now = _now()
            assignments = [f"{field}=?" for field in updates]
            values: list[Any] = list(updates.values())
            assignments.extend(["revision=revision+1", "updated_at=?"])
            values.extend([now, convergence_id, int(expected_revision)])
            cursor = db.execute(
                f"""UPDATE development_convergences SET {','.join(assignments)}
                    WHERE convergence_id=? AND revision=?""",
                values,
            )
            if cursor.rowcount != 1:
                raise _error(
                    "DEVELOPMENT_CONVERGENCE_REVISION_MISMATCH",
                    "development convergence changed while recording terminal evidence",
                    {"convergence_id": convergence_id, "expected": int(expected_revision)},
                )
            updated = _get_row(db, convergence_id)
            _insert_event(
                db,
                row=updated,
                revision=int(updated["revision"]),
                event_type="terminal_evidence_recorded",
                from_phase=row["phase"],
                to_phase=updated["phase"],
                from_status=row["status"],
                to_status=updated["status"],
                metadata=updates,
            )
            result = _public(db, updated)
            db.commit()
            result["deduplicated"] = False
            return result
        except Exception:
            db.rollback()
            raise


def list_convergence_events(convergence_id: str, limit: int = 100) -> list[dict[str, Any]]:
    get_convergence(convergence_id)
    with _db() as db:
        rows = db.execute(
            """SELECT * FROM development_convergence_events
               WHERE convergence_id=? ORDER BY revision LIMIT ?""",
            (convergence_id, max(1, min(int(limit), 500))),
        ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
        result.append(item)
    return result
