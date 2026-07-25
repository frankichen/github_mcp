"""MyGithub10 large-file and incremental-edit primitives.

The module deliberately keeps full request/file bodies out of the audit database.
GitHub remains the source of truth; all writes use a non-forced ref update.
"""

from __future__ import annotations

import base64
import difflib
import hashlib
import json
import os
import re
import secrets
import sqlite3
import tempfile
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from app.config import settings
from app.exceptions import ValidationError

MAX_INLINE_RESPONSE_BYTES = 65536
MAX_FILE_CHUNK_BYTES = 65536
MAX_PATCH_BYTES = 262144
MAX_UPLOAD_CHUNK_BYTES = 131072
MAX_UPLOAD_BYTES = 1048576
UPLOAD_TTL_SECONDS = 3600
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class MyGithub10Error(Exception):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_path(path: str) -> str:
    if not path or path.startswith("/") or "\\" in path or any(part in ("", ".", "..") for part in path.split("/")):
        raise MyGithub10Error("PATCH_UNSAFE_PATH", "path must be a relative repository path")
    if any(ord(char) < 32 for char in path):
        raise MyGithub10Error("PATCH_UNSAFE_PATH", "path contains control characters")
    return path


def _db() -> sqlite3.Connection:
    path = settings.IDEMPOTENCY_DB_PATH
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=15)
    db.execute(
        """CREATE TABLE IF NOT EXISTS mygithub10_operations (
            operation_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
            request_sha256 TEXT NOT NULL, tool_name TEXT NOT NULL, repository TEXT NOT NULL,
            branch TEXT NOT NULL, expected_head_sha TEXT NOT NULL, status TEXT NOT NULL,
            result_commit_sha TEXT, created_at REAL NOT NULL, finished_at REAL,
            error_code TEXT
        )"""
    )
    db.commit()
    return db


def _idempotent_start(tool_name: str, key: str, request: dict[str, Any]) -> tuple[str, str | None]:
    if not key:
        return "", None
    request_hash = _sha256(_json(request).encode())
    db = _db()
    row = db.execute("SELECT * FROM mygithub10_operations WHERE idempotency_key = ?", (key,)).fetchone()
    if row:
        columns = [item[1] for item in db.execute("PRAGMA table_info(mygithub10_operations)")]
        old = dict(zip(columns, row))
        if old["request_sha256"] != request_hash:
            raise MyGithub10Error("IDEMPOTENCY_CONFLICT", "idempotency key was used for a different request")
        if old["status"] == "running":
            raise MyGithub10Error("IDEMPOTENCY_IN_PROGRESS", "operation with this key is still running")
        if old["status"] == "succeeded":
            return "replay", old["result_commit_sha"]
        raise MyGithub10Error(old["error_code"] or "IDEMPOTENCY_FAILED", "previous operation failed")
    operation_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO mygithub10_operations(operation_id,idempotency_key,request_sha256,tool_name,repository,branch,expected_head_sha,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (operation_id, key, request_hash, tool_name, request.get("repository", ""), request.get("branch", ""), request.get("expected_head_sha", ""), "running", time.time()),
    )
    db.commit()
    return operation_id, None


def _idempotent_finish(operation_id: str, status: str, commit_sha: str | None = None, error_code: str | None = None) -> None:
    if not operation_id or operation_id == "replay":
        return
    db = _db()
    db.execute("UPDATE mygithub10_operations SET status=?,result_commit_sha=?,finished_at=?,error_code=? WHERE operation_id=?", (status, commit_sha, time.time(), error_code, operation_id))
    db.commit()


def _repo(client, repository: str):
    raw = client.client if hasattr(client, "client") else client
    return raw._pygithub.get_repo(repository)


def _resolve_commit(repo, ref: str):
    try:
        commit = repo.get_commit(ref or repo.default_branch)
        return commit.sha, commit
    except Exception as exc:
        raise MyGithub10Error("FILE_NOT_FOUND", "ref could not be resolved") from exc


def _read_blob(repo, path: str, ref: str) -> tuple[bytes, str, str]:
    _safe_path(path)
    commit_sha, commit = _resolve_commit(repo, ref)
    try:
        entry = repo.get_contents(path, ref=commit_sha)
        if isinstance(entry, list):
            raise MyGithub10Error("FILE_NOT_FOUND", "path is a directory")
        blob_sha = entry.sha
        blob = repo.get_git_blob(blob_sha)
        data = base64.b64decode(blob.content or "") if getattr(blob, "encoding", "base64") == "base64" else (blob.content or "").encode()
        return data, blob_sha, commit_sha
    except MyGithub10Error:
        raise
    except Exception as exc:
        raise MyGithub10Error("FILE_NOT_FOUND", f"file not found: {path}") from exc


def _text(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MyGithub10Error("FILE_BINARY_UNSUPPORTED", "only UTF-8 text is supported") from exc


def file_manifest(client, repository: str, path: str, ref: str = "") -> dict[str, Any]:
    data, blob_sha, commit_sha = _read_blob(_repo(client, repository), path, ref)
    content = _text(data)
    eol = "CRLF" if b"\r\n" in data else ("LF" if b"\n" in data else "NONE")
    return {
        "repository": repository, "path": path, "resolved_commit_sha": commit_sha,
        "blob_sha": blob_sha, "size_bytes": len(data), "character_count": len(content),
        "line_count": len(content.splitlines()), "encoding": "utf-8", "eol": eol,
        "ends_with_newline": data.endswith(b"\n"), "is_binary": False,
        "content_sha256": _sha256(data), "recommended_chunk_bytes": MAX_FILE_CHUNK_BYTES,
    }


def file_chunk(client, repository: str, path: str, ref: str, offset_bytes: int = 0, limit_bytes: int = MAX_FILE_CHUNK_BYTES, expected_blob_sha: str = "") -> dict[str, Any]:
    data, blob_sha, commit_sha = _read_blob(_repo(client, repository), path, ref)
    if expected_blob_sha and expected_blob_sha != blob_sha:
        raise MyGithub10Error("FILE_BLOB_SHA_MISMATCH", "blob SHA differs from expected", {"expected": expected_blob_sha, "actual": blob_sha})
    if offset_bytes < 0 or offset_bytes > len(data):
        raise MyGithub10Error("FILE_CHUNK_OUT_OF_RANGE", "offset is outside the file")
    if limit_bytes <= 0 or limit_bytes > MAX_FILE_CHUNK_BYTES:
        raise MyGithub10Error("FILE_CHUNK_LIMIT_EXCEEDED", "limit exceeds MyGithub10 chunk limit")
    content = _text(data)
    try:
        data[:offset_bytes].decode("utf-8")
        data[offset_bytes:].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MyGithub10Error("FILE_UTF8_BOUNDARY_INVALID", "offset is not a UTF-8 boundary") from exc
    end = min(offset_bytes + limit_bytes, len(data))
    while end < len(data):
        try:
            data[offset_bytes:end].decode("utf-8")
            break
        except UnicodeDecodeError:
            end -= 1
    chunk = data[offset_bytes:end]
    eof = end == len(data)
    return {
        "repository": repository, "path": path, "resolved_commit_sha": commit_sha,
        "blob_sha": blob_sha, "content_sha256": _sha256(data), "offset_from": offset_bytes,
        "offset_to": end, "next_offset": None if eof else end, "total_size_bytes": len(data),
        "eof": eof, "complete": eof and offset_bytes == 0, "truncated": not eof,
        "chunk_sha256": _sha256(chunk), "content": chunk.decode("utf-8"),
    }


def _parse_patch(patch: str) -> list[tuple[str, str, bytes | None]]:
    if not patch or len(patch.encode()) > MAX_PATCH_BYTES:
        raise MyGithub10Error("PATCH_INVALID_FORMAT", "patch is empty or exceeds the patch limit")
    lines = patch.splitlines(keepends=True)
    result: list[tuple[str, str, bytes | None]] = []
    index = 0
    while index < len(lines):
        if not lines[index].startswith("--- "):
            index += 1
            continue
        if index + 1 >= len(lines) or not lines[index + 1].startswith("+++ "):
            raise MyGithub10Error("PATCH_INVALID_FORMAT", "unified diff header is incomplete")
        old_path, new_path = lines[index][4:].strip().split("\t", 1)[0], lines[index + 1][4:].strip().split("\t", 1)[0]
        if old_path.startswith("a/"): old_path = old_path[2:]
        if new_path.startswith("b/"): new_path = new_path[2:]
        if old_path == "/dev/null": path, operation = new_path, "add"
        elif new_path == "/dev/null": path, operation = old_path, "delete"
        else: path, operation = new_path, "modify"
        _safe_path(path)
        if old_path != "/dev/null" and new_path != "/dev/null" and old_path != new_path:
            raise MyGithub10Error("PATCH_RENAME_UNSUPPORTED", "rename patches are not supported")
        index += 2
        old_lines: list[str] = []
        new_lines: list[str] = []
        while index < len(lines) and lines[index].startswith("@@ "):
            match = _HUNK_RE.match(lines[index])
            if not match:
                raise MyGithub10Error("PATCH_INVALID_FORMAT", "invalid unified diff hunk")
            index += 1
            while index < len(lines) and not lines[index].startswith(("--- ", "@@ ")):
                line = lines[index]
                if line.startswith("\\ No newline at end of file"):
                    index += 1
                    continue
                if not line or line[0] not in " +-":
                    raise MyGithub10Error("PATCH_INVALID_FORMAT", "invalid unified diff line")
                if line[0] in " -": old_lines.append(line[1:])
                if line[0] in " +": new_lines.append(line[1:])
                index += 1
        result.append((path, operation, ("".join(old_lines), "".join(new_lines))))
    if not result:
        raise MyGithub10Error("PATCH_EMPTY", "no file patch found")
    return result


def _apply_file_patch(old: bytes, hunks: list[tuple[str, str]]) -> bytes:
    source = _text(old).splitlines(keepends=True) if old else []
    cursor = 0
    output: list[str] = []
    for old_text, new_text in hunks:
        expected = old_text.splitlines(keepends=True)
        try:
            position = next(i for i in range(cursor, len(source) - len(expected) + 1) if source[i:i + len(expected)] == expected)
        except StopIteration as exc:
            raise MyGithub10Error("PATCH_DOES_NOT_APPLY", "patch context does not match exactly") from exc
        if position != cursor:
            raise MyGithub10Error("PATCH_DOES_NOT_APPLY", "fuzzy patch application is disabled")
        output.extend(source[cursor:position])
        output.extend(new_text.splitlines(keepends=True))
        cursor = position + len(expected)
    output.extend(source[cursor:])
    return "".join(output).encode("utf-8")


def _commit_files(client, repository: str, branch: str, expected_head_sha: str, changed: dict[str, bytes | None], expected_blob_shas: dict[str, str], message: str) -> dict[str, Any]:
    service = client
    service._check_repository_allowed(repository)
    service._check_default_branch_write(repository, branch)
    repo = _repo(service.client, repository) if hasattr(service, "client") else _repo(service, repository)
    ref = repo.get_git_ref(f"heads/{branch}")
    actual_head = ref.object.sha
    if expected_head_sha and actual_head != expected_head_sha:
        raise MyGithub10Error("PATCH_HEAD_CHANGED", "branch HEAD changed", {"expected": expected_head_sha, "actual": actual_head})
    elements = []
    old_shas = {}
    for path, content in changed.items():
        _safe_path(path)
        try:
            entry = repo.get_contents(path, ref=actual_head)
            old_sha = None if isinstance(entry, list) else entry.sha
        except Exception:
            old_sha = None
        old_shas[path] = old_sha
        expected = expected_blob_shas.get(path, "")
        if expected and expected != (old_sha or ""):
            raise MyGithub10Error("PATCH_FILE_CHANGED", f"file blob changed: {path}", {"expected": expected, "actual": old_sha})
        if content is None:
            elements.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
        else:
            blob = service.create_blob(repository, content.decode("utf-8"))
            elements.append({"path": path, "mode": "100644", "type": "blob", "sha": blob.sha})
    base_tree = repo.get_git_commit(actual_head).commit.tree.sha
    tree = service.create_git_tree(repository, elements, base_tree)
    commit = service.create_commit(repository, message, tree.sha, [actual_head])
    try:
        ref.edit(sha=commit.sha, force=False)
    except Exception as exc:
        raise MyGithub10Error("PATCH_HEAD_CHANGED", "branch changed while committing") from exc
    return {"commit_sha": commit.sha, "tree_sha": tree.sha, "branch": branch, "repository": repository, "changed_files": [{"path": path, "operation": "delete" if content is None else ("modify" if old_shas[path] else "add")} for path, content in changed.items()]}


def apply_patch(client, repository: str, branch: str, expected_head_sha: str, expected_blob_shas_json: str, patch: str, commit_message: str, dry_run: bool, idempotency_key: str = "") -> dict[str, Any]:
    parsed = _parse_patch(patch)
    expected = json.loads(expected_blob_shas_json or "{}")
    repo = _repo(client, repository)
    changed: dict[str, bytes | None] = {}
    previews = []
    for path, operation, hunks in parsed:
        old = b""
        old_sha = None
        if operation != "add":
            old, old_sha, _ = _read_blob(repo, path, branch)
        new = None if operation == "delete" else _apply_file_patch(old, [hunks])
        changed[path] = new
        previews.append({"path": path, "operation": operation, "old_blob_sha": old_sha, "new_content_sha256": _sha256(new or b""), "added_lines": 0, "deleted_lines": 0})
    fingerprint = _sha256(_json({"repository": repository, "branch": branch, "expected_head_sha": expected_head_sha, "patch": patch}).encode())
    result = {"ok": True, "dry_run": dry_run, "repository": repository, "branch": branch, "expected_head_sha": expected_head_sha, "changed_files": previews, "diff_preview": patch[:MAX_INLINE_RESPONSE_BYTES], "diff_truncated": len(patch.encode()) > MAX_INLINE_RESPONSE_BYTES, "operation_fingerprint": fingerprint}
    if dry_run:
        return result
    operation_id, replay = _idempotent_start("apply_github_patch", idempotency_key, {"repository": repository, "branch": branch, "expected_head_sha": expected_head_sha, "patch_sha256": _sha256(patch.encode())})
    if replay:
        return {**result, "replayed_commit_sha": replay}
    try:
        result.update(_commit_files(client, repository, branch, expected_head_sha, changed, expected, commit_message))
        _idempotent_finish(operation_id, "succeeded", result["commit_sha"])
        return result
    except MyGithub10Error as exc:
        _idempotent_finish(operation_id, "failed", error_code=exc.code)
        raise


def edit_ranges(client, repository: str, branch: str, expected_head_sha: str, operations_json: str, commit_message: str, dry_run: bool, idempotency_key: str = "") -> dict[str, Any]:
    operations = json.loads(operations_json or "[]")
    repo = _repo(client, repository)
    changed = {}
    expected = {}
    for path in sorted({item["path"] for item in operations}):
        data, blob_sha, _ = _read_blob(repo, path, branch)
        text = _text(data)
        lines = text.splitlines(keepends=True)
        items = [item for item in operations if item["path"] == path]
        positions = []
        for item in items:
            start, end = int(item["start_line"]), int(item.get("end_line", item["start_line"]))
            if start < 1 or end < start or end > len(lines) + 1:
                raise MyGithub10Error("PATCH_SCOPE_EXCEEDED", "line range is outside the file")
            if item["operation"] in ("replace", "delete"):
                old_text = "".join(lines[start - 1:end])
                if _sha256(old_text.encode()) != item.get("expected_old_text_sha256", ""):
                    raise MyGithub10Error("PATCH_FILE_CHANGED", "old range text hash does not match")
            positions.append((start, end, item))
        ordered = sorted(positions, key=lambda item: (item[0], item[1]))
        if any(previous[1] > current[0] for previous, current in zip(ordered, ordered[1:])):
            raise MyGithub10Error("PATCH_SCOPE_EXCEEDED", "overlapping ranges are not allowed")
        for start, end, item in sorted(positions, key=lambda item: (item[0], item[1]), reverse=True):
            replacement = item.get("replacement", "")
            if item["operation"] == "insert_before": index = start - 1
            elif item["operation"] == "insert_after": index = end
            else: index = start - 1; end = end
            if item["operation"] == "delete": replacement = ""
            lines[index:end] = [replacement] if replacement else []
        changed[path] = "".join(lines).encode()
        expected[path] = blob_sha
    result = {"ok": True, "dry_run": dry_run, "changed_files": [{"path": p, "operation": "modify"} for p in changed], "new_content_sha256": {p: _sha256(v) for p, v in changed.items()}}
    if dry_run: return result
    operation_id, replay = _idempotent_start("edit_github_file_ranges", idempotency_key, {"repository": repository, "branch": branch, "expected_head_sha": expected_head_sha, "operations_sha256": _sha256(operations_json.encode())})
    if replay: return {**result, "replayed_commit_sha": replay}
    try:
        result.update(_commit_files(client, repository, branch, expected_head_sha, changed, expected, commit_message))
        _idempotent_finish(operation_id, "succeeded", result["commit_sha"])
        return result
    except MyGithub10Error as exc:
        _idempotent_finish(operation_id, "failed", error_code=exc.code)
        raise


def capabilities(build_sha: str = "unknown") -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{40}", build_sha or ""):
        try:
            build_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parents[3], text=True, stderr=subprocess.DEVNULL).strip()
        except (OSError, subprocess.CalledProcessError):
            build_sha = "unknown"
    return {"name": "MyGithub10", "version": "10.0.0", "build_sha": build_sha, "source_repository": "frankichen/github_mcp", "max_inline_response_bytes": MAX_INLINE_RESPONSE_BYTES, "max_file_chunk_bytes": MAX_FILE_CHUNK_BYTES, "max_patch_bytes": MAX_PATCH_BYTES, "max_upload_chunk_bytes": MAX_UPLOAD_CHUNK_BYTES, "supports_file_manifest": True, "supports_byte_chunks": True, "supports_mcp_resources": True, "supports_incremental_patch": True, "supports_range_edit": True, "supports_chunked_upload": True, "supports_dry_run": True, "supports_expected_head_sha": True, "supports_expected_blob_sha": True, "supports_idempotency_key": True, "supports_operation_audit": True, "supports_tree_attestation": True, "supports_artifact_deployment": False, "supports_gofmt_autofix": True, "supports_real_ci_performance_validation": False, "deprecated_tools": [{"name": "get_github_file", "deprecated": True, "replacement": "get_github_file_manifest + read_github_file_chunk"}, {"name": "commit_github_files", "deprecated": True, "replacement": "apply_github_patch or commit_github_uploaded_files"}, {"name": "get_test_deployment_logs", "deprecated": True, "replacement": "get_test_deployment_log_tail"}]}


_UPLOAD_ROOT = Path(os.environ.get("MYGITHUB10_UPLOAD_DIR", tempfile.gettempdir())) / "mygithub10-uploads"


def _upload_paths(upload_id: str) -> tuple[Path, Path]:
    if not re.fullmatch(r"[0-9a-f-]{36}", upload_id):
        raise MyGithub10Error("PATCH_UNSAFE_PATH", "invalid upload id")
    return _UPLOAD_ROOT / f"{upload_id}.bin", _UPLOAD_ROOT / f"{upload_id}.json"


def begin_upload() -> dict[str, Any]:
    _UPLOAD_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    upload_id = str(uuid.uuid4())
    data_path, meta_path = _upload_paths(upload_id)
    fd = os.open(data_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    meta_path.write_text(_json({"upload_id": upload_id, "created_at": time.time(), "expires_at": time.time() + UPLOAD_TTL_SECONDS, "size": 0, "sha256": None, "finalized": False}), encoding="utf-8")
    os.chmod(meta_path, 0o600)
    return {"upload_id": upload_id, "expires_at": time.time() + UPLOAD_TTL_SECONDS, "max_chunk_bytes": MAX_UPLOAD_CHUNK_BYTES}


def _load_upload(upload_id: str) -> tuple[Path, Path, dict[str, Any]]:
    data_path, meta_path = _upload_paths(upload_id)
    if not data_path.exists() or not meta_path.exists():
        raise MyGithub10Error("UPLOAD_NOT_FOUND", "upload does not exist")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if time.time() > meta["expires_at"]:
        abort_upload(upload_id)
        raise MyGithub10Error("UPLOAD_EXPIRED", "upload has expired")
    return data_path, meta_path, meta


def append_upload(upload_id: str, offset: int, content: bytes, chunk_sha256: str, idempotency_key: str = "") -> dict[str, Any]:
    if len(content) > MAX_UPLOAD_CHUNK_BYTES:
        raise MyGithub10Error("UPLOAD_CHUNK_LIMIT_EXCEEDED", "upload chunk exceeds limit")
    if _sha256(content) != chunk_sha256:
        raise MyGithub10Error("UPLOAD_CHUNK_SHA_MISMATCH", "chunk SHA256 mismatch")
    data_path, meta_path, meta = _load_upload(upload_id)
    if offset != meta["size"]:
        raise MyGithub10Error("UPLOAD_OFFSET_MISMATCH", "upload offset is not contiguous")
    if meta["size"] + len(content) > MAX_UPLOAD_BYTES:
        raise MyGithub10Error("UPLOAD_SIZE_EXCEEDED", "upload exceeds configured size limit")
    with data_path.open("ab") as handle:
        handle.write(content)
    meta["size"] += len(content)
    meta_path.write_text(_json(meta), encoding="utf-8")
    return {"upload_id": upload_id, "offset": offset, "next_offset": meta["size"], "chunk_sha256": chunk_sha256}


def finalize_upload(upload_id: str, expected_size_bytes: int, expected_sha256: str) -> dict[str, Any]:
    data_path, meta_path, meta = _load_upload(upload_id)
    data = data_path.read_bytes()
    if len(data) != expected_size_bytes:
        raise MyGithub10Error("UPLOAD_SIZE_MISMATCH", "final upload size differs from expected")
    actual = _sha256(data)
    if actual != expected_sha256:
        raise MyGithub10Error("UPLOAD_SHA_MISMATCH", "final upload SHA256 differs from expected")
    meta.update({"size": len(data), "sha256": actual, "finalized": True})
    meta_path.write_text(_json(meta), encoding="utf-8")
    return {"upload_id": upload_id, "size_bytes": len(data), "sha256": actual, "finalized": True}


def commit_upload(client, repository: str, branch: str, expected_head_sha: str, path: str, expected_blob_sha: str, upload_id: str, commit_message: str, idempotency_key: str = "") -> dict[str, Any]:
    data_path, _, meta = _load_upload(upload_id)
    if not meta.get("finalized"):
        raise MyGithub10Error("UPLOAD_NOT_FINALIZED", "finalize the upload before committing")
    _safe_path(path)
    data = data_path.read_bytes()
    operation_id, replay = _idempotent_start("commit_github_uploaded_files", idempotency_key, {"repository": repository, "branch": branch, "expected_head_sha": expected_head_sha, "path": path, "upload_sha256": meta["sha256"]})
    if replay:
        return {"ok": True, "replayed_commit_sha": replay, "upload_id": upload_id}
    try:
        result = _commit_files(client, repository, branch, expected_head_sha, {path: data}, {path: expected_blob_sha}, commit_message)
        _idempotent_finish(operation_id, "succeeded", result["commit_sha"])
        abort_upload(upload_id)
        return {"ok": True, **result, "upload_id": upload_id}
    except MyGithub10Error as exc:
        _idempotent_finish(operation_id, "failed", error_code=exc.code)
        raise


def abort_upload(upload_id: str) -> dict[str, Any]:
    data_path, meta_path = _upload_paths(upload_id)
    data_path.unlink(missing_ok=True)
    meta_path.unlink(missing_ok=True)
    return {"ok": True, "upload_id": upload_id, "aborted": True}
