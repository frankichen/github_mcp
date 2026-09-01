"""Durable DX1 PreparedChangeSet gates referencing immutable artifacts."""
from __future__ import annotations

import json
import re
import secrets
import sqlite3
from typing import Any

from app import artifact_store
from app import mygithub10
from app import mygithub12 as core


MyGithub12Error = core.MyGithub12Error
PREPARED_CHANGE_SET_TTL_SECONDS = mygithub10.PREPARED_CHANGE_SET_TTL_SECONDS
_ID_RE = re.compile(r"^pcs_[A-Za-z0-9_-]{24,80}$")


def init_prepared_change_set_db() -> None:
    core.init_db()
    artifact_store.init_artifact_db()
    with core._LOCK, core._db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS prepared_change_sets(
              prepared_change_set_id TEXT PRIMARY KEY,
              artifact_id TEXT NOT NULL,
              repository TEXT NOT NULL,
              branch TEXT NOT NULL,
              raw_size_bytes INTEGER NOT NULL,
              raw_sha256 TEXT NOT NULL,
              raw_git_blob_sha TEXT NOT NULL,
              canonical_change_set_hash TEXT NOT NULL,
              development_session_id TEXT NOT NULL,
              session_revision INTEGER NOT NULL,
              workspace_id TEXT NOT NULL,
              workspace_revision INTEGER NOT NULL,
              expected_head_sha TEXT NOT NULL,
              affected_paths_json TEXT NOT NULL,
              expected_blob_identities_json TEXT NOT NULL,
              created_at REAL NOT NULL,
              expires_at REAL NOT NULL,
              status TEXT NOT NULL,
              execution_idempotency_key TEXT,
              execution_fingerprint TEXT,
              committed_result_json TEXT,
              post_write_identity_json TEXT,
              failure_code TEXT,
              executing_at REAL,
              committed_at REAL,
              terminal_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_prepared_change_sets_expiry
              ON prepared_change_sets(status,expires_at);
            """
        )


def _validate_id(prepared_id: str) -> str:
    if not _ID_RE.fullmatch(prepared_id or ""):
        raise MyGithub12Error(
            "PREPARED_CHANGE_SET_NOT_FOUND", "prepared change set was not found"
        )
    return prepared_id


def _row(prepared_id: str) -> sqlite3.Row:
    init_prepared_change_set_db()
    with core._db() as db:
        row = db.execute(
            "SELECT * FROM prepared_change_sets WHERE prepared_change_set_id=?",
            (_validate_id(prepared_id),),
        ).fetchone()
    if not row:
        raise MyGithub12Error(
            "PREPARED_CHANGE_SET_NOT_FOUND", "prepared change set was not found"
        )
    return row


def _public(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    value = dict(row)
    value["affected_paths"] = json.loads(value.pop("affected_paths_json") or "[]")
    value["expected_blob_identities"] = json.loads(
        value.pop("expected_blob_identities_json") or "{}"
    )
    for key in (
        "execution_fingerprint",
        "execution_idempotency_key",
        "committed_result_json",
        "post_write_identity_json",
    ):
        value.pop(key, None)
    return value


def cleanup_expired(now: float | None = None) -> int:
    init_prepared_change_set_db()
    current = core._now() if now is None else float(now)
    with core._LOCK, core._db() as db:
        rows = db.execute(
            "SELECT prepared_change_set_id,artifact_id FROM prepared_change_sets "
            "WHERE status IN ('PREPARED','COMMITTED','FAILED_TERMINAL') AND expires_at<=?",
            (current,),
        ).fetchall()
        db.execute(
            "UPDATE prepared_change_sets SET status='EXPIRED',terminal_at=? "
            "WHERE status IN ('PREPARED','COMMITTED','FAILED_TERMINAL') AND expires_at<=?",
            (current, current),
        )
    for row in rows:
        try:
            artifact_store.expire_artifact(str(row["artifact_id"]))
        except artifact_store.ArtifactStoreError:
            pass
    return len(rows)


def _expected_blobs(
    parsed: dict[str, Any], dry_run_result: dict[str, Any]
) -> tuple[list[str], dict[str, str | None]]:
    changed_files = dry_run_result.get("changed_files") or []
    affected_paths = sorted(
        {
            str(item.get("path"))
            for item in changed_files
            if isinstance(item, dict) and item.get("path")
        }
    )
    expected: dict[str, str | None] = {}
    for item in changed_files:
        if isinstance(item, dict) and item.get("path") and "old_blob_sha" in item:
            expected[str(item["path"])] = item.get("old_blob_sha")
    change = parsed.get("change") or {}
    if parsed.get("mode") == "patch":
        for path, blob_sha in (change.get("expected_blob_shas") or {}).items():
            expected.setdefault(str(path), str(blob_sha) or None)
    elif parsed.get("mode") == "range":
        for item in change.get("range_operations") or []:
            if isinstance(item, dict) and item.get("path") and item.get("expected_blob_sha"):
                expected.setdefault(str(item["path"]), str(item["expected_blob_sha"]))
    elif parsed.get("mode") == "upload":
        for item in change.get("uploaded_files") or []:
            if isinstance(item, dict) and item.get("path"):
                expected.setdefault(
                    str(item["path"]), str(item.get("expected_blob_sha") or "") or None
                )
    return affected_paths, expected


def create_prepared_change_set(
    artifact: artifact_store.ArtifactRef,
    parsed: dict[str, Any],
    dry_run_result: dict[str, Any],
    session: dict[str, Any],
    workspace: dict[str, Any],
    *,
    expected_head_sha: str,
    ttl_seconds: int = PREPARED_CHANGE_SET_TTL_SECONDS,
) -> dict[str, Any]:
    init_prepared_change_set_db()
    cleanup_expired()
    current_artifact = artifact_store.get_artifact(artifact.artifact_id)
    if current_artifact != artifact:
        raise MyGithub12Error(
            "PREPARED_CHANGE_SET_NOT_FOUND", "artifact identity changed before prepare"
        )
    prepared_id = "pcs_" + secrets.token_urlsafe(24)
    now = core._now()
    bounded_ttl = max(60, min(int(ttl_seconds), PREPARED_CHANGE_SET_TTL_SECONDS))
    expires_at = min(now + bounded_ttl, artifact.expires_at)
    affected_paths, expected_blobs = _expected_blobs(parsed, dry_run_result)
    with core._LOCK, core._db() as db:
        db.execute(
            """INSERT INTO prepared_change_sets(
            prepared_change_set_id,artifact_id,repository,branch,raw_size_bytes,
            raw_sha256,raw_git_blob_sha,canonical_change_set_hash,
            development_session_id,session_revision,workspace_id,workspace_revision,
            expected_head_sha,affected_paths_json,expected_blob_identities_json,
            created_at,expires_at,status,execution_idempotency_key,
            execution_fingerprint,committed_result_json,post_write_identity_json,
            failure_code,executing_at,committed_at,terminal_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'PREPARED',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL)""",
            (
                prepared_id,
                artifact.artifact_id,
                session["repository"],
                session["branch"],
                artifact.size_bytes,
                artifact.sha256,
                artifact.git_blob_sha,
                parsed["canonical_hash"],
                session["session_id"],
                int(session["session_revision"]),
                workspace["workspace_id"],
                int(workspace["revision"]),
                expected_head_sha,
                json.dumps(affected_paths, ensure_ascii=False, separators=(",", ":")),
                json.dumps(
                    expected_blobs,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                now,
                expires_at,
            ),
        )
    try:
        artifact_store.freeze_artifact(
            artifact.artifact_id,
            repository_scope=session["repository"],
            session_scope=session["session_id"],
            expires_at=expires_at,
        )
    except Exception:
        with core._LOCK, core._db() as db:
            db.execute(
                "DELETE FROM prepared_change_sets WHERE prepared_change_set_id=?",
                (prepared_id,),
            )
        raise
    return get_prepared_change_set(prepared_id)


def get_prepared_change_set(prepared_id: str) -> dict[str, Any]:
    row = _row(prepared_id)
    if row["status"] != "EXECUTING" and float(row["expires_at"]) <= core._now():
        cleanup_expired()
        raise MyGithub12Error(
            "PREPARED_CHANGE_SET_EXPIRED",
            "prepared change set expired",
            {"prepared_change_set_id": prepared_id},
        )
    if row["status"] == "EXPIRED":
        raise MyGithub12Error(
            "PREPARED_CHANGE_SET_EXPIRED",
            "prepared change set expired",
            {"prepared_change_set_id": prepared_id},
        )
    return _public(row)


def load_prepared_bytes(prepared_id: str) -> tuple[dict[str, Any], bytes]:
    prepared = get_prepared_change_set(prepared_id)
    if prepared["status"] != "PREPARED":
        raise MyGithub12Error(
            "PREPARED_CHANGE_SET_ALREADY_CONSUMED",
            "prepared change set is not available for a new write",
            {"prepared_change_set_id": prepared_id, "status": prepared["status"]},
        )
    try:
        raw = artifact_store.read_artifact_bytes(
            prepared["artifact_id"],
            repository_scope=prepared["repository"],
            session_scope=prepared["development_session_id"],
        )
    except artifact_store.ArtifactStoreError as exc:
        code = (
            "PREPARED_CHANGE_SET_EXPIRED"
            if exc.code == "ARTIFACT_EXPIRED"
            else "PREPARED_CHANGE_SET_NOT_FOUND"
        )
        raise MyGithub12Error(code, "prepared artifact is unavailable") from exc
    if (
        len(raw) != int(prepared["raw_size_bytes"])
        or artifact_store.get_artifact(prepared["artifact_id"]).sha256
        != prepared["raw_sha256"]
    ):
        raise MyGithub12Error(
            "PREPARED_CHANGE_SET_NOT_FOUND", "prepared artifact identity is invalid"
        )
    return prepared, raw


def replay_write(
    prepared_id: str,
    *,
    idempotency_key: str,
    request_fingerprint: str,
) -> dict[str, Any] | None:
    row = _row(prepared_id)
    if row["status"] == "PREPARED":
        if float(row["expires_at"]) <= core._now():
            cleanup_expired()
            raise MyGithub12Error(
                "PREPARED_CHANGE_SET_EXPIRED", "prepared change set expired"
            )
        return None
    if row["status"] == "EXPIRED":
        raise MyGithub12Error(
            "PREPARED_CHANGE_SET_EXPIRED", "prepared change set expired"
        )
    same_key = row["execution_idempotency_key"] == idempotency_key
    if same_key and row["execution_fingerprint"] != request_fingerprint:
        raise MyGithub12Error(
            "IDEMPOTENCY_CONFLICT",
            "idempotency key was used with a different prepared write request",
            {"prepared_change_set_id": prepared_id},
        )
    if same_key and row["status"] == "COMMITTED" and row["committed_result_json"]:
        result = json.loads(row["committed_result_json"])
        result["replayed"] = True
        return result
    if same_key and row["status"] == "EXECUTING":
        raise MyGithub12Error(
            "IDEMPOTENCY_IN_PROGRESS",
            "prepared change set write is already executing",
            {"prepared_change_set_id": prepared_id, "retryable": True},
        )
    raise MyGithub12Error(
        "PREPARED_CHANGE_SET_ALREADY_CONSUMED",
        "prepared change set is already committed, executing, failed, or unavailable",
        {"prepared_change_set_id": prepared_id, "status": row["status"]},
    )


def claim_for_write(
    prepared_id: str,
    *,
    idempotency_key: str,
    request_fingerprint: str,
) -> dict[str, Any] | None:
    replay = replay_write(
        prepared_id,
        idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint,
    )
    if replay is not None:
        return replay
    with core._LOCK, core._db() as db:
        cur = db.execute(
            """UPDATE prepared_change_sets
               SET status='EXECUTING',execution_idempotency_key=?,
                   execution_fingerprint=?,executing_at=?
               WHERE prepared_change_set_id=? AND status='PREPARED' AND expires_at>?""",
            (
                idempotency_key,
                request_fingerprint,
                core._now(),
                prepared_id,
                core._now(),
            ),
        )
        if cur.rowcount == 1:
            return None
    return replay_write(
        prepared_id,
        idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint,
    )


def _safe_result(result: dict[str, Any]) -> dict[str, Any]:
    safe = dict(result)
    for key in (
        "diff_preview",
        "patch",
        "content",
        "change_set_json",
        "download_url",
        "storage_locator",
    ):
        safe.pop(key, None)
    return safe


def mark_committed(prepared_id: str, result: dict[str, Any]) -> None:
    safe = _safe_result(result)
    post_write_identity = {
        key: safe.get(key)
        for key in (
            "commit_sha",
            "tree_sha",
            "verified_branch_head_sha",
            "verified_commit_sha",
            "verified_tree_sha",
            "write_verified",
        )
        if key in safe
    }
    row = _row(prepared_id)
    with core._LOCK, core._db() as db:
        cur = db.execute(
            """UPDATE prepared_change_sets
               SET status='COMMITTED',committed_result_json=?,post_write_identity_json=?,
                   committed_at=?,terminal_at=?
               WHERE prepared_change_set_id=? AND status='EXECUTING'""",
            (
                json.dumps(safe, ensure_ascii=False, separators=(",", ":")),
                json.dumps(post_write_identity, ensure_ascii=False, separators=(",", ":")),
                core._now(),
                core._now(),
                prepared_id,
            ),
        )
        if cur.rowcount != 1:
            raise MyGithub12Error(
                "PREPARED_CHANGE_SET_ALREADY_CONSUMED",
                "prepared change set state changed while recording the commit",
            )
    try:
        artifact_store.consume_artifact(str(row["artifact_id"]))
    except artifact_store.ArtifactStoreError:
        pass


def mark_failed_terminal(prepared_id: str, error_code: str) -> None:
    row = _row(prepared_id)
    with core._LOCK, core._db() as db:
        db.execute(
            """UPDATE prepared_change_sets
               SET status='FAILED_TERMINAL',failure_code=?,terminal_at=?
               WHERE prepared_change_set_id=? AND status='EXECUTING'""",
            (error_code, core._now(), prepared_id),
        )
    try:
        artifact_store.consume_artifact(str(row["artifact_id"]))
    except artifact_store.ArtifactStoreError:
        pass
