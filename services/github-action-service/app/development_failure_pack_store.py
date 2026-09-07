"""Durable storage for Web CI failure-pack evidence.

The response-resource directory is intentionally a cache-like transport layer:
it has a TTL and may be cleaned up by the runtime.  This module keeps the
bounded, redacted failure-pack payload in the controller's durable SQLite
database so a new response resource can be materialized without running CI a
second time.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any, Mapping

from app import mygithub12


FAILURE_PACK_SCHEMA_VERSION = 1
MAX_DURABLE_PAYLOAD_BYTES = 512 * 1024
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        dict(value),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


def payload_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _validate_hash(name: str, value: str) -> None:
    if not _HASH_RE.fullmatch(str(value or "")):
        raise ValueError(f"{name} must be a lowercase 64-character SHA-256")


def init_failure_pack_db() -> None:
    """Create the failure-pack table in the shared durable controller DB."""
    mygithub12.init_db()
    with mygithub12._LOCK, mygithub12._db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS development_failure_packs(
              failure_pack_id TEXT PRIMARY KEY,
              evidence_sha256 TEXT NOT NULL UNIQUE,
              schema_version INTEGER NOT NULL,
              payload_json TEXT NOT NULL,
              payload_bytes INTEGER NOT NULL CHECK(payload_bytes >= 0),
              payload_sha256 TEXT NOT NULL,
              created_at REAL NOT NULL,
              updated_at REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_development_failure_packs_job
              ON development_failure_packs(failure_pack_id);
            CREATE INDEX IF NOT EXISTS idx_development_failure_packs_evidence
              ON development_failure_packs(evidence_sha256);
            """
        )


def _record_from_row(row: Any) -> dict[str, Any]:
    try:
        payload = json.loads(row["payload_json"])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("durable failure-pack payload is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("durable failure-pack payload is not an object")
    return {
        "failure_pack_id": row["failure_pack_id"],
        "evidence_sha256": row["evidence_sha256"],
        "schema_version": int(row["schema_version"]),
        "payload": payload,
        "payload_bytes": int(row["payload_bytes"]),
        "payload_sha256": row["payload_sha256"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def create_or_get_failure_pack(
    payload: Mapping[str, Any],
    *,
    evidence_sha256: str,
    failure_pack_id: str | None = None,
) -> dict[str, Any]:
    """Insert one durable pack, or return the existing pack for this evidence.

    ``evidence_sha256`` is computed by the caller from redacted, normalized
    evidence and deliberately excludes temporary resource metadata.  The
    unique constraint plus the write transaction make retries idempotent even
    when two request handlers build the same pack concurrently.
    """
    _validate_hash("evidence_sha256", evidence_sha256)
    pack_id = str(failure_pack_id or evidence_sha256)
    if not pack_id:
        raise ValueError("failure_pack_id must be non-empty")
    encoded = _canonical_json(payload).encode("utf-8")
    if len(encoded) > MAX_DURABLE_PAYLOAD_BYTES:
        raise ValueError("failure-pack payload exceeds durable size limit")
    normalized_payload = json.loads(encoded.decode("utf-8"))
    if not isinstance(normalized_payload, dict):
        raise ValueError("failure-pack payload must be an object")
    digest = hashlib.sha256(encoded).hexdigest()
    now = time.time()

    init_failure_pack_db()
    with mygithub12._LOCK, mygithub12._db() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            existing = db.execute(
                "SELECT * FROM development_failure_packs WHERE evidence_sha256=?",
                (evidence_sha256,),
            ).fetchone()
            if existing:
                result = _record_from_row(existing)
                db.commit()
                result["deduplicated"] = True
                return result

            db.execute(
                """INSERT INTO development_failure_packs(
                     failure_pack_id,evidence_sha256,schema_version,payload_json,
                     payload_bytes,payload_sha256,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    pack_id,
                    evidence_sha256,
                    int(normalized_payload.get("schema_version", FAILURE_PACK_SCHEMA_VERSION)),
                    encoded.decode("utf-8"),
                    len(encoded),
                    digest,
                    now,
                    now,
                ),
            )
            row = db.execute(
                "SELECT * FROM development_failure_packs WHERE failure_pack_id=?",
                (pack_id,),
            ).fetchone()
            result = _record_from_row(row)
            db.commit()
        except Exception:
            db.rollback()
            raise
    result["deduplicated"] = False
    return result


def store_failure_pack(
    payload: Mapping[str, Any],
    *,
    evidence_sha256: str,
    failure_pack_id: str | None = None,
) -> dict[str, Any]:
    """Compatibility spelling for callers that describe this as a store."""
    return create_or_get_failure_pack(
        payload,
        evidence_sha256=evidence_sha256,
        failure_pack_id=failure_pack_id,
    )


def get_failure_pack(failure_pack_id: str) -> dict[str, Any] | None:
    """Return the durable record for a pack without touching CI or resources."""
    if not str(failure_pack_id or ""):
        return None
    init_failure_pack_db()
    with mygithub12._db() as db:
        row = db.execute(
            "SELECT * FROM development_failure_packs WHERE failure_pack_id=?",
            (str(failure_pack_id),),
        ).fetchone()
    return _record_from_row(row) if row else None


def get_failure_pack_by_evidence(evidence_sha256: str) -> dict[str, Any] | None:
    """Return a durable record by its canonical evidence digest."""
    _validate_hash("evidence_sha256", evidence_sha256)
    init_failure_pack_db()
    with mygithub12._db() as db:
        row = db.execute(
            "SELECT * FROM development_failure_packs WHERE evidence_sha256=?",
            (evidence_sha256,),
        ).fetchone()
    return _record_from_row(row) if row else None


def read_failure_pack_payload(failure_pack_id: str) -> dict[str, Any] | None:
    """Read only the durable payload; temporary resource state is ignored."""
    record = get_failure_pack(failure_pack_id)
    return record["payload"] if record else None
