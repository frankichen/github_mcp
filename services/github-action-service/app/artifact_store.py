"""Short-lived immutable byte artifacts shared by ingress and domain workflows.

The public ``ArtifactRef`` deliberately excludes the server-controlled storage
locator. Transport capabilities, signed URLs, and credentials are never
persisted here.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from app import mygithub12 as core


DEFAULT_ARTIFACT_TTL_SECONDS = 1800
_ARTIFACT_ID_RE = re.compile(r"^art_[A-Za-z0-9_-]{24,80}$")


class ArtifactStoreError(Exception):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: str
    kind: str
    size_bytes: int
    sha256: str
    git_blob_sha: str
    file_name: str
    mime_type: str
    source_transport: str
    created_at: float
    expires_at: float
    status: str
    repository_scope: str
    principal_scope: str
    session_scope: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _root() -> Path:
    configured = os.getenv("MYGITHUB12_ARTIFACT_DIR", "").strip()
    if configured:
        return Path(configured)
    return Path(core._db_path()).parent / "artifacts"


def _ensure_root() -> Path:
    root = _root()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    return root


def _validate_artifact_id(artifact_id: str) -> str:
    if not _ARTIFACT_ID_RE.fullmatch(artifact_id or ""):
        raise ArtifactStoreError("ARTIFACT_NOT_FOUND", "artifact was not found")
    return artifact_id


def _storage_path(artifact_id: str) -> Path:
    return _root() / f"{_validate_artifact_id(artifact_id)}.bin"


def init_artifact_db() -> None:
    core.init_db()
    with core._LOCK, core._db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS runtime_artifacts(
              artifact_id TEXT PRIMARY KEY,
              kind TEXT NOT NULL,
              size_bytes INTEGER NOT NULL,
              sha256 TEXT NOT NULL,
              git_blob_sha TEXT NOT NULL,
              file_name TEXT NOT NULL,
              mime_type TEXT NOT NULL,
              source_transport TEXT NOT NULL,
              created_at REAL NOT NULL,
              expires_at REAL NOT NULL,
              status TEXT NOT NULL,
              repository_scope TEXT NOT NULL,
              principal_scope TEXT NOT NULL,
              session_scope TEXT NOT NULL,
              storage_locator TEXT NOT NULL,
              consumed_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_runtime_artifacts_expiry
              ON runtime_artifacts(status,expires_at);
            """
        )


def _git_blob_sha(path: Path, size_bytes: int) -> str:
    digest = hashlib.sha1(f"blob {size_bytes}\0".encode("ascii"))
    with path.open("rb") as handle:
        while chunk := handle.read(64 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _row_to_ref(row: Any) -> ArtifactRef:
    value = dict(row)
    value.pop("storage_locator", None)
    value.pop("consumed_at", None)
    return ArtifactRef(**value)


def _row(artifact_id: str) -> Any:
    init_artifact_db()
    with core._db() as db:
        row = db.execute(
            "SELECT * FROM runtime_artifacts WHERE artifact_id=?",
            (_validate_artifact_id(artifact_id),),
        ).fetchone()
    if not row:
        raise ArtifactStoreError("ARTIFACT_NOT_FOUND", "artifact was not found")
    return row


def get_artifact(artifact_id: str, *, allow_terminal: bool = False) -> ArtifactRef:
    row = _row(artifact_id)
    if row["status"] in {"READY", "FROZEN"} and float(row["expires_at"]) <= core._now():
        expire_artifact(artifact_id)
        raise ArtifactStoreError(
            "ARTIFACT_EXPIRED", "artifact expired", {"artifact_id": artifact_id}
        )
    if not allow_terminal and row["status"] not in {"READY", "FROZEN"}:
        code = "ARTIFACT_EXPIRED" if row["status"] == "EXPIRED" else "ARTIFACT_UNAVAILABLE"
        raise ArtifactStoreError(
            code,
            "artifact is no longer available",
            {"artifact_id": artifact_id, "status": row["status"]},
        )
    return _row_to_ref(row)


def _unlink_storage(path: Path) -> None:
    try:
        if path.is_file() and not path.is_symlink():
            path.unlink()
    except OSError:
        pass


class ArtifactWriter:
    """Incremental writer that publishes exactly one immutable artifact."""

    def __init__(
        self,
        *,
        kind: str,
        max_bytes: int,
        file_name: str = "",
        mime_type: str = "",
        source_transport: str,
        repository_scope: str = "",
        principal_scope: str = "",
        session_scope: str = "",
        ttl_seconds: int = DEFAULT_ARTIFACT_TTL_SECONDS,
    ) -> None:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        self.artifact_id = "art_" + secrets.token_urlsafe(24)
        self.kind = str(kind or "opaque")
        self.max_bytes = max_bytes
        self.file_name = str(file_name or "")[:255]
        self.mime_type = str(mime_type or "")[:255]
        self.source_transport = str(source_transport or "unknown")[:80]
        self.repository_scope = str(repository_scope or "")[:255]
        self.principal_scope = str(principal_scope or "")[:255]
        self.session_scope = str(session_scope or "")[:255]
        self.ttl_seconds = max(60, min(int(ttl_seconds), 24 * 60 * 60))
        root = _ensure_root()
        self._final_path = root / f"{self.artifact_id}.bin"
        self._temporary_path = root / f".{self.artifact_id}.{secrets.token_hex(8)}.part"
        descriptor = os.open(
            self._temporary_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        self._handle = os.fdopen(descriptor, "wb")
        self._sha256 = hashlib.sha256()
        self._size_bytes = 0
        self._closed = False

    @property
    def size_bytes(self) -> int:
        return self._size_bytes

    def write(self, chunk: bytes) -> None:
        if self._closed:
            raise RuntimeError("artifact writer is closed")
        if not isinstance(chunk, bytes):
            chunk = bytes(chunk)
        next_size = self._size_bytes + len(chunk)
        if next_size > self.max_bytes:
            raise ArtifactStoreError(
                "ARTIFACT_TOO_LARGE",
                "artifact exceeds the ingress limit",
                {"max_bytes": self.max_bytes},
            )
        self._handle.write(chunk)
        self._sha256.update(chunk)
        self._size_bytes = next_size

    def commit(self) -> ArtifactRef:
        if self._closed:
            raise RuntimeError("artifact writer is closed")
        self._handle.flush()
        os.fsync(self._handle.fileno())
        self._handle.close()
        self._closed = True
        os.chmod(self._temporary_path, 0o600)
        try:
            os.link(self._temporary_path, self._final_path)
            self._temporary_path.unlink()
            directory_descriptor = os.open(_ensure_root(), os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
            created_at = core._now()
            expires_at = created_at + self.ttl_seconds
            git_blob_sha = _git_blob_sha(self._final_path, self._size_bytes)
            init_artifact_db()
            with core._LOCK, core._db() as db:
                db.execute(
                    """INSERT INTO runtime_artifacts(
                    artifact_id,kind,size_bytes,sha256,git_blob_sha,file_name,mime_type,
                    source_transport,created_at,expires_at,status,repository_scope,
                    principal_scope,session_scope,storage_locator,consumed_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)""",
                    (
                        self.artifact_id,
                        self.kind,
                        self._size_bytes,
                        self._sha256.hexdigest(),
                        git_blob_sha,
                        self.file_name,
                        self.mime_type,
                        self.source_transport,
                        created_at,
                        expires_at,
                        "READY",
                        self.repository_scope,
                        self.principal_scope,
                        self.session_scope,
                        str(self._final_path),
                    ),
                )
        except Exception:
            _unlink_storage(self._temporary_path)
            _unlink_storage(self._final_path)
            raise
        return get_artifact(self.artifact_id)

    def abort(self) -> None:
        if not self._closed:
            self._handle.close()
            self._closed = True
        _unlink_storage(self._temporary_path)


def store_bytes(
    data: bytes,
    *,
    kind: str,
    max_bytes: int,
    source_transport: str,
    file_name: str = "",
    mime_type: str = "",
    repository_scope: str = "",
    principal_scope: str = "",
    session_scope: str = "",
    ttl_seconds: int = DEFAULT_ARTIFACT_TTL_SECONDS,
) -> ArtifactRef:
    writer = ArtifactWriter(
        kind=kind,
        max_bytes=max_bytes,
        source_transport=source_transport,
        file_name=file_name,
        mime_type=mime_type,
        repository_scope=repository_scope,
        principal_scope=principal_scope,
        session_scope=session_scope,
        ttl_seconds=ttl_seconds,
    )
    try:
        writer.write(data)
        return writer.commit()
    except Exception:
        writer.abort()
        raise


def freeze_artifact(
    artifact_id: str,
    *,
    repository_scope: str,
    session_scope: str,
    expires_at: float,
) -> ArtifactRef:
    artifact = get_artifact(artifact_id)
    bounded_expiry = min(float(expires_at), artifact.expires_at)
    with core._LOCK, core._db() as db:
        cur = db.execute(
            """UPDATE runtime_artifacts
               SET status='FROZEN',repository_scope=?,session_scope=?,expires_at=?
               WHERE artifact_id=? AND status IN ('READY','FROZEN')""",
            (repository_scope, session_scope, bounded_expiry, artifact_id),
        )
        if cur.rowcount != 1:
            raise ArtifactStoreError(
                "ARTIFACT_UNAVAILABLE", "artifact could not be frozen"
            )
    return get_artifact(artifact_id)


def read_artifact_bytes(
    artifact_id: str,
    *,
    repository_scope: str = "",
    session_scope: str = "",
) -> bytes:
    artifact = get_artifact(artifact_id)
    if repository_scope and artifact.repository_scope not in {"", repository_scope}:
        raise ArtifactStoreError("ARTIFACT_SCOPE_MISMATCH", "artifact repository scope differs")
    if session_scope and artifact.session_scope not in {"", session_scope}:
        raise ArtifactStoreError("ARTIFACT_SCOPE_MISMATCH", "artifact session scope differs")
    path = _storage_path(artifact_id)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != artifact.size_bytes:
                raise OSError("artifact storage identity changed")
            digest = hashlib.sha256()
            data = bytearray()
            while chunk := handle.read(64 * 1024):
                digest.update(chunk)
                data.extend(chunk)
    except OSError as exc:
        mark_corrupt(artifact_id)
        raise ArtifactStoreError(
            "ARTIFACT_UNAVAILABLE", "artifact storage is unavailable"
        ) from exc
    if len(data) != artifact.size_bytes or digest.hexdigest() != artifact.sha256:
        mark_corrupt(artifact_id)
        raise ArtifactStoreError("ARTIFACT_UNAVAILABLE", "artifact identity verification failed")
    return bytes(data)


def _terminalize(artifact_id: str, status: str) -> None:
    row = _row(artifact_id)
    with core._LOCK, core._db() as db:
        db.execute(
            "UPDATE runtime_artifacts SET status=?,consumed_at=? WHERE artifact_id=?",
            (status, core._now(), artifact_id),
        )
    _unlink_storage(Path(row["storage_locator"]))


def consume_artifact(artifact_id: str) -> None:
    _terminalize(artifact_id, "CONSUMED")


def expire_artifact(artifact_id: str) -> None:
    _terminalize(artifact_id, "EXPIRED")


def mark_corrupt(artifact_id: str) -> None:
    _terminalize(artifact_id, "CORRUPT")


def cleanup_expired(now: float | None = None) -> int:
    init_artifact_db()
    current = core._now() if now is None else float(now)
    with core._LOCK, core._db() as db:
        rows = db.execute(
            "SELECT artifact_id,storage_locator FROM runtime_artifacts "
            "WHERE status IN ('READY','FROZEN') AND expires_at<=?",
            (current,),
        ).fetchall()
        db.execute(
            "UPDATE runtime_artifacts SET status='EXPIRED',consumed_at=? "
            "WHERE status IN ('READY','FROZEN') AND expires_at<=?",
            (current, current),
        )
    for row in rows:
        _unlink_storage(Path(row["storage_locator"]))
    return len(rows)
