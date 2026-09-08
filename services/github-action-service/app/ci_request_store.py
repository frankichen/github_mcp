"""Durable CI Request persistence and transition primitives.

DEV-003 keeps Request identity ahead of Worker execution and provides the
atomic Request-to-Worker dispatch boundary required by startup recovery while
leaving ``ci_jobs`` as the Worker execution truth source.
"""

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Mapping, Optional

from app import ci_database as db_core
from app.ci_models import CIRequestPhase, CIRequestStatus

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")

_TERMINAL_STATUSES = {
    CIRequestStatus.PASSED.value,
    CIRequestStatus.FAILED.value,
    CIRequestStatus.TIMED_OUT.value,
    CIRequestStatus.CANCELLED.value,
    CIRequestStatus.SUPERSEDED.value,
    CIRequestStatus.WORKER_LOST.value,
    CIRequestStatus.INTERNAL_ERROR.value,
    CIRequestStatus.PREFLIGHT_FAILED.value,
}

_VALID_PHASE_STATUS = {
    (CIRequestPhase.ACCEPTED.value, CIRequestStatus.ACCEPTED.value),
    (CIRequestPhase.PREPARING.value, CIRequestStatus.PREPARING.value),
    (CIRequestPhase.QUEUED.value, CIRequestStatus.QUEUED.value),
    (CIRequestPhase.RUNNING.value, CIRequestStatus.RUNNING.value),
    *{(CIRequestPhase.TERMINAL.value, status) for status in _TERMINAL_STATUSES},
}

_ALLOWED_TRANSITIONS = {
    ("accepted", "accepted"): {
        ("preparing", "preparing"),
        ("terminal", "cancelled"),
        ("terminal", "superseded"),
        ("terminal", "internal_error"),
    },
    ("preparing", "preparing"): {
        ("queued", "queued"),
        ("terminal", "preflight_failed"),
        ("terminal", "cancelled"),
        ("terminal", "superseded"),
        ("terminal", "internal_error"),
    },
    ("queued", "queued"): {
        ("running", "running"),
        ("terminal", "cancelled"),
        ("terminal", "superseded"),
        ("terminal", "worker_lost"),
        ("terminal", "internal_error"),
    },
    ("running", "running"): {
        ("terminal", "passed"),
        ("terminal", "failed"),
        ("terminal", "timed_out"),
        ("terminal", "cancelled"),
        ("terminal", "superseded"),
        ("terminal", "worker_lost"),
        ("terminal", "internal_error"),
    },
}

_IDENTITY_FIELDS = (
    "repository",
    "branch",
    "commit_sha",
    "tree_sha",
    "profile",
    "effective_config_digest",
)


class CIRequestStoreError(RuntimeError):
    code = "CI_REQUEST_STORE_ERROR"


class CIRequestNotFoundError(CIRequestStoreError):
    code = "CI_REQUEST_NOT_FOUND"


class CIRequestRevisionConflictError(CIRequestStoreError):
    code = "CI_REQUEST_REVISION_CONFLICT"


class CIRequestTransitionError(CIRequestStoreError):
    code = "CI_REQUEST_ILLEGAL_TRANSITION"


class CIRequestIdentityMismatchError(CIRequestStoreError):
    code = "CI_REQUEST_IDENTITY_MISMATCH"


class CIRequestIdempotencyConflictError(CIRequestStoreError):
    code = "IDEMPOTENCY_CONFLICT"


def compute_normalized_request_hash(payload: Mapping) -> str:
    """Hash canonical JSON bytes supplied by the future create-or-get caller."""
    canonical = json.dumps(
        dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def init_ci_request_schema(db) -> None:
    """Create the DEV-002 schema and idempotently project legacy Jobs.

    The migration is additive. It never updates ``ci_jobs`` and therefore
    cannot reopen, requeue, or rewrite historical Worker execution evidence.
    """
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS ci_requests (
            request_id TEXT PRIMARY KEY,
            repository TEXT NOT NULL,
            branch TEXT NOT NULL DEFAULT '',
            commit_sha TEXT NOT NULL,
            tree_sha TEXT,
            profile TEXT NOT NULL,
            effective_config_digest TEXT,
            status TEXT NOT NULL,
            phase TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0),
            idempotency_key TEXT,
            normalized_request_hash TEXT,
            worker_job_id TEXT UNIQUE,
            preflight_error_id TEXT,
            preflight_error_code TEXT,
            terminal_reason TEXT,
            failure_pack_id TEXT,
            attestation_id TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            last_event_id INTEGER,
            CHECK (
                (phase = 'accepted' AND status = 'accepted') OR
                (phase = 'preparing' AND status = 'preparing') OR
                (phase = 'queued' AND status = 'queued') OR
                (phase = 'running' AND status = 'running') OR
                (phase = 'terminal' AND status IN (
                    'passed', 'failed', 'timed_out', 'cancelled', 'superseded',
                    'worker_lost', 'internal_error', 'preflight_failed'
                ))
            ),
            FOREIGN KEY (worker_job_id) REFERENCES ci_jobs(job_id) ON DELETE SET NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_ci_requests_idempotency_key
            ON ci_requests(idempotency_key) WHERE idempotency_key IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_ci_requests_repository_commit
            ON ci_requests(repository, commit_sha, profile);
        CREATE INDEX IF NOT EXISTS idx_ci_requests_phase_status
            ON ci_requests(phase, status);
        CREATE INDEX IF NOT EXISTS idx_ci_requests_worker_job
            ON ci_requests(worker_job_id);

        CREATE TABLE IF NOT EXISTS ci_request_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            from_phase TEXT,
            from_status TEXT,
            to_phase TEXT NOT NULL,
            to_status TEXT NOT NULL,
            event_data TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            FOREIGN KEY (request_id) REFERENCES ci_requests(request_id) ON DELETE CASCADE,
            UNIQUE (request_id, revision)
        );

        CREATE INDEX IF NOT EXISTS idx_ci_request_events_request
            ON ci_request_events(request_id, revision);
        """
    )

    with db_core._db_write_lock:
        db.execute("BEGIN IMMEDIATE")
        try:
            _backfill_legacy_jobs(db)
            db.commit()
        except Exception:
            db.rollback()
            raise


def _legacy_value(row, key: str):
    return row[key] if key in row.keys() else None


def _legacy_request_state(job_status: str) -> tuple[str, str]:
    if job_status == "queued":
        return "queued", "queued"
    if job_status in {"leased", "downloading", "preparing"}:
        return "queued", "queued"
    if job_status in {"running", "cancel_requested"}:
        return "running", "running"
    if job_status in _TERMINAL_STATUSES - {"preflight_failed"}:
        return "terminal", job_status
    return "terminal", "internal_error"


def _backfill_legacy_jobs(db) -> None:
    rows = db.execute("SELECT * FROM ci_jobs ORDER BY created_at, job_id").fetchall()
    for row in rows:
        phase, status = _legacy_request_state(row["status"])
        created_at = float(row["created_at"])
        updated_at = (
            _legacy_value(row, "finished_at")
            or _legacy_value(row, "started_at")
            or _legacy_value(row, "queued_at")
            or created_at
        )
        terminal_reason = (
            _legacy_value(row, "error_code") if phase == "terminal" else None
        )
        cursor = db.execute(
            """
            INSERT OR IGNORE INTO ci_requests (
                request_id, repository, branch, commit_sha, tree_sha, profile,
                effective_config_digest, status, phase, revision, idempotency_key,
                normalized_request_hash, worker_job_id, preflight_error_id,
                preflight_error_code, terminal_reason, failure_pack_id,
                attestation_id, created_at, updated_at, last_event_id
            ) VALUES (?, ?, ?, ?, NULL, ?, NULL, ?, ?, 0, NULL, NULL, ?,
                      NULL, NULL, ?, NULL, NULL, ?, ?, NULL)
            """,
            (
                row["job_id"],
                row["repository"],
                _legacy_value(row, "branch") or "",
                row["commit_sha"],
                row["profile"],
                status,
                phase,
                row["job_id"],
                terminal_reason,
                created_at,
                float(updated_at),
            ),
        )
        if cursor.rowcount != 1:
            continue
        event_id = _insert_event(
            db,
            request_id=row["job_id"],
            revision=0,
            event_type="legacy_backfill",
            from_phase=None,
            from_status=None,
            to_phase=phase,
            to_status=status,
            event_data={"legacy_job_status": row["status"]},
        )
        db.execute(
            "UPDATE ci_requests SET last_event_id = ? WHERE request_id = ? AND revision = 0",
            (event_id, row["job_id"]),
        )


def create_or_get_ci_request(
    *,
    repository: str,
    branch: str,
    commit_sha: str,
    tree_sha: Optional[str],
    profile: str,
    effective_config_digest: str,
    idempotency_key: str,
    normalized_request_hash: str,
    request_payload: Optional[Mapping] = None,
) -> dict:
    """Durably create one accepted request or resolve an idempotent replay."""
    _validate_new_request_identity(
        repository,
        commit_sha,
        tree_sha,
        profile,
        effective_config_digest,
        idempotency_key,
        normalized_request_hash,
    )
    db = db_core._get_db()
    now = db_core.now_ts()

    with db_core._db_write_lock:
        db.execute("BEGIN IMMEDIATE")
        try:
            existing = db.execute(
                "SELECT * FROM ci_requests WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing:
                if existing["normalized_request_hash"] != normalized_request_hash:
                    raise CIRequestIdempotencyConflictError(idempotency_key)
                expected = {
                    "repository": repository,
                    "branch": branch,
                    "commit_sha": commit_sha,
                    "tree_sha": tree_sha,
                    "profile": profile,
                    "effective_config_digest": effective_config_digest,
                }
                if tree_sha is None:
                    expected.pop("tree_sha")
                _assert_identity(existing, expected)
                result = _request_row_to_dict(existing)
                db.commit()
                result["deduplicated"] = True
                return result

            request_id = f"ci_req_{uuid.uuid4().hex[:24]}"
            db.execute(
                """
                INSERT INTO ci_requests (
                    request_id, repository, branch, commit_sha, tree_sha, profile,
                    effective_config_digest, status, phase, revision,
                    idempotency_key, normalized_request_hash, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'accepted', 'accepted', 0, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    repository,
                    branch,
                    commit_sha,
                    tree_sha,
                    profile,
                    effective_config_digest,
                    idempotency_key,
                    normalized_request_hash,
                    now,
                    now,
                ),
            )
            event_id = _insert_event(
                db,
                request_id=request_id,
                revision=0,
                event_type="accepted",
                from_phase=None,
                from_status=None,
                to_phase="accepted",
                to_status="accepted",
                event_data={"request_payload": dict(request_payload or {})},
            )
            db.execute(
                "UPDATE ci_requests SET last_event_id = ? WHERE request_id = ? AND revision = 0",
                (event_id, request_id),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise

    persisted = db.execute(
        "SELECT * FROM ci_requests WHERE request_id = ?", (request_id,)
    ).fetchone()
    if not persisted:
        raise sqlite3.OperationalError("CI request commit verification failed")
    result = _request_row_to_dict(persisted)
    result["deduplicated"] = False
    return result


def get_ci_request(request_id: str) -> Optional[dict]:
    db = db_core._get_db()
    row = db.execute(
        "SELECT * FROM ci_requests WHERE request_id = ?", (request_id,)
    ).fetchone()
    return _request_row_to_dict(row) if row else None


def get_ci_request_by_idempotency_key(idempotency_key: str) -> Optional[dict]:
    db = db_core._get_db()
    row = db.execute(
        "SELECT * FROM ci_requests WHERE idempotency_key = ?", (idempotency_key,)
    ).fetchone()
    return _request_row_to_dict(row) if row else None


def get_ci_request_by_worker_job_id(worker_job_id: str) -> Optional[dict]:
    db = db_core._get_db()
    row = db.execute(
        "SELECT * FROM ci_requests WHERE worker_job_id = ?", (worker_job_id,)
    ).fetchone()
    return _request_row_to_dict(row) if row else None


def get_ci_request_events(request_id: str) -> list[dict]:
    db = db_core._get_db()
    rows = db.execute(
        "SELECT * FROM ci_request_events WHERE request_id = ? ORDER BY revision",
        (request_id,),
    ).fetchall()
    return [
        {
            "event_id": row["event_id"],
            "request_id": row["request_id"],
            "revision": row["revision"],
            "event_type": row["event_type"],
            "from_phase": row["from_phase"],
            "from_status": row["from_status"],
            "to_phase": row["to_phase"],
            "to_status": row["to_status"],
            "event_data": json.loads(row["event_data"] or "{}"),
            "created_at": _iso(row["created_at"]),
        }
        for row in rows
    ]


def _get_ci_request_payload_in_db(db, request_id: str) -> dict:
    row = db.execute(
        "SELECT event_data FROM ci_request_events WHERE request_id = ? AND revision = 0",
        (request_id,),
    ).fetchone()
    if not row:
        return {}
    try:
        event_data = json.loads(row["event_data"] or "{}")
    except (TypeError, ValueError):
        return {}
    payload = event_data.get("request_payload") if isinstance(event_data, dict) else None
    return dict(payload) if isinstance(payload, dict) else {}


def get_ci_request_payload(request_id: str) -> dict:
    return _get_ci_request_payload_in_db(db_core._get_db(), request_id)


def list_pending_ci_requests(limit: int = 100) -> list[dict]:
    db = db_core._get_db()
    rows = db.execute(
        """SELECT * FROM ci_requests
           WHERE phase IN ('accepted', 'preparing')
           ORDER BY created_at, request_id LIMIT ?""",
        (min(max(int(limit), 1), 1000),),
    ).fetchall()
    return [_request_row_to_dict(row) for row in rows]


def dispatch_ci_request(
    request_id: str,
    *,
    expected_revision: int,
    tree_sha: str,
    changed_files: list[str],
    changed_files_total: int,
    changed_files_truncated: bool,
    event_data: Optional[Mapping] = None,
) -> dict:
    """Atomically create/bind the Worker Job and advance preparing -> queued."""
    if not _SHA_RE.fullmatch(tree_sha or ""):
        raise ValueError("tree_sha must be a lowercase 40-character SHA")
    db = db_core._get_db()
    with db_core._db_write_lock:
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute(
                "SELECT * FROM ci_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            if not row:
                raise CIRequestNotFoundError(request_id)
            if row["phase"] in {"queued", "running", "terminal"}:
                result = _request_row_to_dict(row)
                db.commit()
                result["deduplicated"] = True
                return result
            if (row["phase"], row["status"]) != ("preparing", "preparing"):
                raise CIRequestTransitionError(
                    f"dispatch requires preparing request, got {(row['phase'], row['status'])}"
                )
            if int(row["revision"]) != int(expected_revision):
                raise CIRequestRevisionConflictError(
                    f"expected={expected_revision} actual={row['revision']}"
                )
            if row["tree_sha"] and row["tree_sha"] != tree_sha:
                raise CIRequestIdentityMismatchError("tree_sha")
            payload = _get_ci_request_payload_in_db(db, request_id)
            required = {"priority", "timeout_seconds", "base_sha", "supersede_previous"}
            if not required.issubset(payload):
                raise CIRequestTransitionError("durable request payload is incomplete")
            worker_job = db_core.create_ci_request_job_in_transaction(
                db,
                request_id=request_id,
                repository=row["repository"],
                branch=row["branch"],
                commit_sha=row["commit_sha"],
                profile=row["profile"],
                priority=int(payload["priority"]),
                timeout_seconds=int(payload["timeout_seconds"]),
                base_sha=str(payload.get("base_sha") or ""),
                changed_files=list(changed_files),
                changed_files_total=int(changed_files_total),
                changed_files_truncated=bool(changed_files_truncated),
                supersede_previous=bool(payload.get("supersede_previous")),
            )
            superseded_job_ids = worker_job.pop("_superseded_job_ids", [])
            worker_job_id = worker_job["job_id"]
            _assert_worker_job_identity(db, row, worker_job_id)
            new_revision = int(expected_revision) + 1
            event_id = _insert_event(
                db,
                request_id=request_id,
                revision=new_revision,
                event_type="worker_queued",
                from_phase="preparing",
                from_status="preparing",
                to_phase="queued",
                to_status="queued",
                event_data={
                    "worker_job_id": worker_job_id,
                    "git_tree_sha": tree_sha,
                    **dict(event_data or {}),
                },
            )
            cursor = db.execute(
                """UPDATE ci_requests
                   SET tree_sha = ?, worker_job_id = ?, phase = 'queued', status = 'queued',
                       revision = revision + 1, updated_at = ?, last_event_id = ?
                   WHERE request_id = ? AND revision = ?
                     AND phase = 'preparing' AND status = 'preparing'""",
                (tree_sha, worker_job_id, db_core.now_ts(), event_id,
                 request_id, int(expected_revision)),
            )
            if cursor.rowcount != 1:
                raise CIRequestRevisionConflictError(request_id)
            persisted = db.execute(
                "SELECT * FROM ci_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            db.commit()
        except Exception:
            db.rollback()
            raise
    result = _request_row_to_dict(persisted)
    result["deduplicated"] = False
    result["worker_job"] = worker_job
    for superseded_job_id in superseded_job_ids:
        db_core._notify_job_change(superseded_job_id)
    return result


def transition_ci_request(
    request_id: str,
    expected_revision: int,
    target_phase: str,
    target_status: str,
    *,
    expected_identity: Optional[Mapping[str, Optional[str]]] = None,
    worker_job_id: Optional[str] = None,
    preflight_error_id: Optional[str] = None,
    preflight_error_code: Optional[str] = None,
    terminal_reason: Optional[str] = None,
    failure_pack_id: Optional[str] = None,
    attestation_id: Optional[str] = None,
    event_data: Optional[Mapping] = None,
) -> dict:
    """Advance one request with an explicit DB CAS inside one transaction."""
    target = _validate_phase_status(target_phase, target_status)
    db = db_core._get_db()

    with db_core._db_write_lock:
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute(
                "SELECT * FROM ci_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            if not row:
                raise CIRequestNotFoundError(request_id)
            if int(row["revision"]) != int(expected_revision):
                raise CIRequestRevisionConflictError(
                    f"expected={expected_revision} actual={row['revision']}"
                )
            if expected_identity:
                _assert_identity(row, expected_identity)

            current = (row["phase"], row["status"])
            if target not in _ALLOWED_TRANSITIONS.get(current, set()):
                raise CIRequestTransitionError(f"{current} -> {target}")

            effective_worker_job_id = worker_job_id or row["worker_job_id"]
            if target[0] in {"queued", "running"} and not effective_worker_job_id:
                raise CIRequestTransitionError(
                    f"{target[0]} requires a persisted worker_job_id"
                )
            if effective_worker_job_id:
                _assert_worker_job_identity(db, row, effective_worker_job_id)
                if row["worker_job_id"] and row["worker_job_id"] != effective_worker_job_id:
                    raise CIRequestIdentityMismatchError("worker_job_id")

            _validate_transition_metadata(
                target,
                preflight_error_id=preflight_error_id,
                preflight_error_code=preflight_error_code,
                terminal_reason=terminal_reason,
                failure_pack_id=failure_pack_id,
                attestation_id=attestation_id,
            )

            new_revision = int(expected_revision) + 1
            now = db_core.now_ts()
            event_id = _insert_event(
                db,
                request_id=request_id,
                revision=new_revision,
                event_type="transition",
                from_phase=current[0],
                from_status=current[1],
                to_phase=target[0],
                to_status=target[1],
                event_data=dict(event_data or {}),
            )

            assignments = [
                "phase = ?",
                "status = ?",
                "revision = revision + 1",
                "updated_at = ?",
                "last_event_id = ?",
            ]
            params = [target[0], target[1], now, event_id]
            optional_values = {
                "worker_job_id": worker_job_id,
                "preflight_error_id": preflight_error_id,
                "preflight_error_code": preflight_error_code,
                "terminal_reason": terminal_reason,
                "failure_pack_id": failure_pack_id,
                "attestation_id": attestation_id,
            }
            for field, value in optional_values.items():
                if value is not None:
                    assignments.append(f"{field} = ?")
                    params.append(value)
            params.extend(
                [request_id, int(expected_revision), current[0], current[1]]
            )
            cursor = db.execute(
                f"""
                UPDATE ci_requests SET {', '.join(assignments)}
                WHERE request_id = ? AND revision = ? AND phase = ? AND status = ?
                """,
                params,
            )
            if cursor.rowcount != 1:
                raise CIRequestRevisionConflictError(request_id)
            db.commit()
        except Exception:
            db.rollback()
            raise

    updated = get_ci_request(request_id)
    if not updated:
        raise sqlite3.OperationalError("CI request transition verification failed")
    return updated


def _validate_new_request_identity(
    repository: str,
    commit_sha: str,
    tree_sha: Optional[str],
    profile: str,
    effective_config_digest: str,
    idempotency_key: str,
    normalized_request_hash: str,
) -> None:
    if not repository or "/" not in repository:
        raise ValueError("repository must be owner/repo")
    if not _SHA_RE.fullmatch(commit_sha or ""):
        raise ValueError("commit_sha must be a lowercase 40-character SHA")
    if tree_sha is not None and not _SHA_RE.fullmatch(tree_sha):
        raise ValueError("tree_sha must be a lowercase 40-character SHA")
    if not profile:
        raise ValueError("profile is required")
    if not effective_config_digest:
        raise ValueError("effective_config_digest is required")
    if not idempotency_key:
        raise ValueError("idempotency_key is required")
    if not _HASH_RE.fullmatch(normalized_request_hash or ""):
        raise ValueError("normalized_request_hash must be a lowercase SHA-256")


def _validate_phase_status(phase: str, status: str) -> tuple[str, str]:
    phase_value = phase.value if isinstance(phase, CIRequestPhase) else str(phase)
    status_value = status.value if isinstance(status, CIRequestStatus) else str(status)
    pair = (phase_value, status_value)
    if pair not in _VALID_PHASE_STATUS:
        raise CIRequestTransitionError(f"invalid phase/status combination: {pair}")
    return pair


def _validate_transition_metadata(
    target: tuple[str, str],
    *,
    preflight_error_id: Optional[str],
    preflight_error_code: Optional[str],
    terminal_reason: Optional[str],
    failure_pack_id: Optional[str],
    attestation_id: Optional[str],
) -> None:
    phase, status = target
    if (preflight_error_id or preflight_error_code) and status != "preflight_failed":
        raise CIRequestTransitionError(
            "preflight error fields are only valid for preflight_failed"
        )
    if status == "preflight_failed" and not preflight_error_code:
        raise CIRequestTransitionError("preflight_failed requires preflight_error_code")
    if terminal_reason and phase != "terminal":
        raise CIRequestTransitionError("terminal_reason requires terminal phase")
    if failure_pack_id and phase != "terminal":
        raise CIRequestTransitionError("failure_pack_id requires terminal phase")
    if attestation_id and status != "passed":
        raise CIRequestTransitionError("attestation_id requires passed status")


def _assert_identity(row, expected_identity: Mapping[str, Optional[str]]) -> None:
    for field, expected in expected_identity.items():
        if field not in _IDENTITY_FIELDS:
            raise ValueError(f"unsupported identity field: {field}")
        if row[field] != expected:
            raise CIRequestIdentityMismatchError(
                f"{field}: expected={expected!r} actual={row[field]!r}"
            )


def _assert_worker_job_identity(db, request_row, worker_job_id: str) -> None:
    job = db.execute(
        "SELECT repository, branch, commit_sha, profile FROM ci_jobs WHERE job_id = ?",
        (worker_job_id,),
    ).fetchone()
    if not job:
        raise CIRequestIdentityMismatchError(f"worker job not found: {worker_job_id}")
    for field in ("repository", "branch", "commit_sha", "profile"):
        if job[field] != request_row[field]:
            raise CIRequestIdentityMismatchError(
                f"worker job {field} does not match request"
            )


def _insert_event(
    db,
    *,
    request_id: str,
    revision: int,
    event_type: str,
    from_phase: Optional[str],
    from_status: Optional[str],
    to_phase: str,
    to_status: str,
    event_data: Mapping,
) -> int:
    cursor = db.execute(
        """
        INSERT INTO ci_request_events (
            request_id, revision, event_type, from_phase, from_status,
            to_phase, to_status, event_data, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            request_id,
            revision,
            event_type,
            from_phase,
            from_status,
            to_phase,
            to_status,
            json.dumps(dict(event_data), sort_keys=True, separators=(",", ":")),
            db_core.now_ts(),
        ),
    )
    return int(cursor.lastrowid)


def _request_row_to_dict(row) -> dict:
    return {
        "request_id": row["request_id"],
        "repository": row["repository"],
        "branch": row["branch"],
        "commit_sha": row["commit_sha"],
        "tree_sha": row["tree_sha"],
        "profile": row["profile"],
        "effective_config_digest": row["effective_config_digest"],
        "status": row["status"],
        "phase": row["phase"],
        "revision": int(row["revision"]),
        "idempotency_key": row["idempotency_key"],
        "normalized_request_hash": row["normalized_request_hash"],
        "worker_job_id": row["worker_job_id"],
        "preflight_error_id": row["preflight_error_id"],
        "preflight_error_code": row["preflight_error_code"],
        "terminal_reason": row["terminal_reason"],
        "failure_pack_id": row["failure_pack_id"],
        "attestation_id": row["attestation_id"],
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
        "last_event_id": row["last_event_id"],
        "last_event_revision": int(row["revision"]),
        "terminal": row["phase"] == "terminal",
    }


def _iso(value: Optional[float]) -> Optional[str]:
    if value is None:
        return None
    return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
