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
import time
import uuid
import logging
import fcntl
from pathlib import Path
from typing import Any

from app.config import settings
from app.exceptions import ValidationError
from app.version import SERVICE_VERSION
from app.github_write_verify import WriteVerificationError, post_write_verify
from app.observability import current_request_id

MAX_INLINE_RESPONSE_BYTES = 65536
MAX_FILE_CHUNK_BYTES = 65536
MAX_PATCH_BYTES = 262144
MAX_TEXT_EDIT_FILE_BYTES = 4 * 1024 * 1024
MAX_UPLOAD_CHUNK_BYTES = 131072
MAX_UPLOAD_BYTES = 1048576
UPLOAD_TTL_SECONDS = 3600
MAX_FILE_EDIT_OPERATIONS = 1000
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?(?:\r?\n)?$")
logger = logging.getLogger(__name__)


class MyGithub10Error(Exception):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.trace_id = str(uuid.uuid4())


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git_blob_sha(data: bytes) -> str:
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


def _safe_path(path: str) -> str:
    if not path or path.startswith("/") or "\\" in path or any(part in ("", ".", "..") for part in path.split("/")):
        raise MyGithub10Error("PATCH_UNSAFE_PATH", "path must be a relative repository path")
    if any(ord(char) < 32 for char in path):
        raise MyGithub10Error("PATCH_UNSAFE_PATH", "path contains control characters")
    return path


def _truncate_utf8(value: str, limit_bytes: int | None = None) -> tuple[str, bool]:
    limit_bytes = MAX_INLINE_RESPONSE_BYTES if limit_bytes is None else limit_bytes
    encoded = value.encode("utf-8")
    if len(encoded) <= limit_bytes:
        return value, False
    boundary = encoded[:limit_bytes]
    while boundary:
        try:
            return boundary.decode("utf-8"), True
        except UnicodeDecodeError:
            boundary = boundary[:-1]
    return "", True


def _format_unified_range(start: int, stop: int) -> str:
    length = stop - start
    beginning = start + 1
    if length == 0:
        beginning -= 1
    if length == 1:
        return str(beginning)
    return f"{beginning},{length}"


def _diff_line(prefix: str, line: str) -> list[str]:
    if line.endswith("\n"):
        return [prefix + line]
    return [prefix + line + "\n", "\\ No newline at end of file\n"]


def _minimal_unified_diff(path: str, old_text: str, new_text: str, context: int = 3) -> tuple[str, int, int]:
    """Return a bounded-context unified diff that preserves exact line bytes."""
    _safe_path(path)
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    groups = list(matcher.get_grouped_opcodes(context))
    if not groups:
        return "", 0, 0
    output = [f"--- a/{path}\n", f"+++ b/{path}\n"]
    added = 0
    deleted = 0
    for group in groups:
        old_start, old_stop = group[0][1], group[-1][2]
        new_start, new_stop = group[0][3], group[-1][4]
        output.append(
            f"@@ -{_format_unified_range(old_start, old_stop)} "
            f"+{_format_unified_range(new_start, new_stop)} @@\n"
        )
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                for line in old_lines[i1:i2]:
                    output.extend(_diff_line(" ", line))
            if tag in {"replace", "delete"}:
                deleted += i2 - i1
                for line in old_lines[i1:i2]:
                    output.extend(_diff_line("-", line))
            if tag in {"replace", "insert"}:
                added += j2 - j1
                for line in new_lines[j1:j2]:
                    output.extend(_diff_line("+", line))
    return "".join(output), added, deleted


def _db() -> sqlite3.Connection:
    path = settings.IDEMPOTENCY_DB_PATH
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=15, isolation_level="IMMEDIATE")
    db.execute("PRAGMA busy_timeout=15000")
    db.execute(
        """CREATE TABLE IF NOT EXISTS mygithub10_operations (
            operation_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
            request_sha256 TEXT NOT NULL, tool_name TEXT NOT NULL, repository TEXT NOT NULL,
            branch TEXT NOT NULL, expected_head_sha TEXT NOT NULL, status TEXT NOT NULL,
            result_commit_sha TEXT, created_at REAL NOT NULL, finished_at REAL,
            error_code TEXT, result_json TEXT, request_json TEXT,
            caller_idempotency_key TEXT, request_trace_id TEXT, workspace_id TEXT,
            workspace_revision INTEGER, previous_head_sha TEXT, computed_commit_sha TEXT,
            expected_tree_sha TEXT, observed_branch_head TEXT, observed_commit_sha TEXT,
            observed_tree_sha TEXT, failed_stage TEXT
        )"""
    )
    columns = {item[1] for item in db.execute("PRAGMA table_info(mygithub10_operations)")}
    additions = (
        ("result_json", "TEXT"), ("request_json", "TEXT"),
        ("caller_idempotency_key", "TEXT"), ("request_trace_id", "TEXT"),
        ("workspace_id", "TEXT"), ("workspace_revision", "INTEGER"),
        ("previous_head_sha", "TEXT"), ("computed_commit_sha", "TEXT"),
        ("expected_tree_sha", "TEXT"), ("observed_branch_head", "TEXT"),
        ("observed_commit_sha", "TEXT"), ("observed_tree_sha", "TEXT"),
        ("failed_stage", "TEXT"),
    )
    for name, definition in additions:
        if name not in columns:
            db.execute(f"ALTER TABLE mygithub10_operations ADD COLUMN {name} {definition}")
    db.execute("CREATE INDEX IF NOT EXISTS idx_mygithub10_operations_trace ON mygithub10_operations(request_trace_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_mygithub10_operations_workspace ON mygithub10_operations(workspace_id)")
    db.commit()
    return db


def _operation_columns(db: sqlite3.Connection) -> list[str]:
    return [item[1] for item in db.execute("PRAGMA table_info(mygithub10_operations)")]


def _idempotent_start(
    tool_name: str,
    key: str,
    request: dict[str, Any],
    audit_context: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any] | None]:
    request_hash = _sha256(_json(request).encode())
    db = _db()
    audit_context = audit_context or {}
    if key:
        row = db.execute(
            "SELECT * FROM mygithub10_operations WHERE caller_idempotency_key=? OR (caller_idempotency_key IS NULL AND idempotency_key=?) ORDER BY created_at DESC LIMIT 1",
            (key, key),
        ).fetchone()
        if row:
            old = dict(zip(_operation_columns(db), row))
            if old["request_sha256"] != request_hash:
                raise MyGithub10Error("IDEMPOTENCY_CONFLICT", "idempotency key was used for a different request")
            status = old["status"]
            if status == "success_verified":
                if old.get("result_json"):
                    result = json.loads(old["result_json"])
                    result["replayed"] = True
                    result["operation_id"] = old["operation_id"]
                    return "replay", result
                raise MyGithub10Error("IDEMPOTENCY_RESULT_UNAVAILABLE", "the verified idempotent result is unavailable")
            if status == "succeeded":
                raise MyGithub10Error(
                    "IDEMPOTENCY_RESULT_UNVERIFIED",
                    "legacy idempotent success was not durably verified against GitHub",
                    {"operation_id": old["operation_id"], "recovery_required": True},
                )
            if status in {"running", "in_progress"}:
                if time.time() - float(old.get("created_at") or 0) > 300:
                    db.execute(
                        "UPDATE mygithub10_operations SET status='indeterminate',finished_at=?,error_code=? WHERE operation_id=?",
                        (time.time(), "IDEMPOTENCY_INDETERMINATE", old["operation_id"]),
                    )
                    db.commit()
                    status = "indeterminate"
                else:
                    raise MyGithub10Error(
                        "IDEMPOTENCY_IN_PROGRESS",
                        "operation with this key is still running",
                        {"operation_id": old["operation_id"]},
                    )
            if status in {"git_verified", "indeterminate"}:
                raise MyGithub10Error(
                    "IDEMPOTENCY_INDETERMINATE",
                    "previous operation did not reach durable application success; inspect GitHub and Workspace state before retry",
                    {"operation_id": old["operation_id"], "recovery_required": True},
                )
            raise MyGithub10Error(old.get("error_code") or "IDEMPOTENCY_FAILED", "previous operation failed")

    operation_id = str(uuid.uuid4())
    storage_key = key or f"__audit__:{operation_id}"
    trace_id = str(audit_context.get("request_trace_id") or current_request_id() or operation_id)
    workspace_id = str(audit_context.get("workspace_id") or "")
    workspace_revision = int(audit_context.get("workspace_revision") or 0)
    try:
        db.execute(
            """INSERT INTO mygithub10_operations(
                operation_id,idempotency_key,caller_idempotency_key,request_sha256,tool_name,repository,branch,
                expected_head_sha,status,created_at,request_json,request_trace_id,workspace_id,workspace_revision
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                operation_id, storage_key, key or None, request_hash, tool_name, request.get("repository", ""),
                request.get("branch", ""), request.get("expected_head_sha", ""), "in_progress", time.time(),
                _json(request), trace_id, workspace_id, workspace_revision,
            ),
        )
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        raise MyGithub10Error("IDEMPOTENCY_IN_PROGRESS", "operation with this key is already running")
    return operation_id, None


def _result_audit_fields(result: dict[str, Any] | None) -> tuple[Any, ...]:
    result = result or {}
    error = result.get("error") if isinstance(result.get("error"), dict) else {}
    details = error.get("details") if isinstance(error.get("details"), dict) else {}
    return (
        result.get("old_head_sha") or result.get("previous_head_sha") or details.get("expected_previous_head"),
        result.get("commit_sha") or result.get("new_head_sha") or details.get("new_commit_sha"),
        result.get("tree_sha") or details.get("expected_tree_sha"),
        result.get("verified_branch_head_sha") or details.get("observed_branch_head"),
        result.get("verified_commit_sha") or details.get("observed_commit_sha"),
        result.get("verified_tree_sha") or details.get("observed_tree_sha"),
        result.get("failed_stage") or details.get("failed_stage"),
    )


def _idempotent_finish(
    operation_id: str,
    status: str,
    commit_sha: str | None = None,
    error_code: str | None = None,
    result: dict[str, Any] | None = None,
) -> None:
    if not operation_id or operation_id == "replay":
        return
    if status == "succeeded":
        status = "success_verified"
    db = _db()
    previous_head, computed_commit, expected_tree, observed_branch, observed_commit, observed_tree, failed_stage = _result_audit_fields(result)
    db.execute(
        """UPDATE mygithub10_operations SET
            status=?,result_commit_sha=?,finished_at=?,error_code=?,result_json=?,
            previous_head_sha=COALESCE(?,previous_head_sha),computed_commit_sha=COALESCE(?,computed_commit_sha),
            expected_tree_sha=COALESCE(?,expected_tree_sha),observed_branch_head=COALESCE(?,observed_branch_head),
            observed_commit_sha=COALESCE(?,observed_commit_sha),observed_tree_sha=COALESCE(?,observed_tree_sha),
            failed_stage=COALESCE(?,failed_stage)
        WHERE operation_id=?""",
        (
            status, commit_sha or computed_commit, time.time() if status in {"success_verified", "failed", "indeterminate"} else None,
            error_code, _json(result) if result is not None else None, previous_head, computed_commit, expected_tree,
            observed_branch, observed_commit, observed_tree, failed_stage, operation_id,
        ),
    )
    db.commit()


def _idempotent_mark_git_verified(operation_id: str, result: dict[str, Any]) -> None:
    _idempotent_finish(operation_id, "git_verified", result.get("commit_sha"), result=result)


def _idempotent_existing(key: str) -> dict[str, Any] | None:
    """Read an existing caller idempotency operation without remote state."""
    if not key:
        return None
    db = _db()
    row = db.execute(
        "SELECT * FROM mygithub10_operations WHERE caller_idempotency_key=? OR (caller_idempotency_key IS NULL AND idempotency_key=?) ORDER BY created_at DESC LIMIT 1",
        (key, key),
    ).fetchone()
    if not row:
        return None
    return dict(zip(_operation_columns(db), row))

def _idempotent_existing_by_operation(operation_id: str) -> dict[str, Any] | None:
    if not operation_id:
        return None
    db = _db()
    row = db.execute("SELECT * FROM mygithub10_operations WHERE operation_id=?", (operation_id,)).fetchone()
    if not row:
        return None
    return dict(zip(_operation_columns(db), row))



def _repo(client, repository: str):
    raw = client.client if hasattr(client, "client") else client
    return raw._pygithub.get_repo(repository)


def _resolve_commit(repo, ref: str):
    try:
        commit = repo.get_commit(ref or repo.default_branch)
        return commit.sha, commit
    except Exception as exc:
        status = getattr(exc, "status", None)
        if status == 404:
            raise MyGithub10Error("REF_NOT_FOUND", f"ref could not be resolved: {ref or repo.default_branch}", {"ref": ref or repo.default_branch, "retryable": False}) from exc
        raise MyGithub10Error("GITHUB_READ_FAILED", "GitHub failed while resolving ref", {"ref": ref or repo.default_branch, "retryable": True}) from exc


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
        status = getattr(exc, "status", None)
        if status == 404:
            raise MyGithub10Error("FILE_NOT_FOUND", f"file not found: {path}", {"path": path, "ref": ref, "retryable": False}) from exc
        raise MyGithub10Error("GITHUB_READ_FAILED", f"GitHub failed while reading {path}", {"path": path, "ref": ref, "retryable": True}) from exc


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


def _parse_patch_details(patch: str) -> tuple[list[tuple[str, str, list[tuple[int, int, list[str], list[str]]]]], dict[str, Any]]:
    if not patch:
        raise MyGithub10Error("PATCH_INVALID_FORMAT", "patch is empty")
    if len(patch.encode()) > MAX_PATCH_BYTES:
        raise MyGithub10Error("PATCH_TOO_LARGE", "patch exceeds the patch limit")
    lines = patch.splitlines(keepends=True)
    metadata_only_deletes: list[tuple[str, str, list[tuple[int, int, list[str], list[str]]]]] = []
    block_index = 0
    while block_index < len(lines):
        if not lines[block_index].startswith("diff --git "):
            block_index += 1
            continue
        block_end = block_index + 1
        while block_end < len(lines) and not lines[block_end].startswith("diff --git "):
            block_end += 1
        block = lines[block_index:block_end]
        has_delete_mode = any(line.startswith("deleted file mode ") for line in block)
        has_text_headers = any(line.startswith("--- ") for line in block)
        if has_delete_mode and not has_text_headers:
            header = lines[block_index].rstrip("\r\n")
            match = re.fullmatch(r"diff --git a/(.+) b/\1", header)
            if not match:
                raise MyGithub10Error("PATCH_INVALID_FORMAT", "metadata-only delete must use matching a/ and b/ paths")
            path = match.group(1)
            _safe_path(path)
            metadata_only_deletes.append((path, "delete", []))
        block_index = block_end
    result: list[tuple[str, str, list[tuple[int, int, list[str], list[str]]]]] = []
    normalized_hunks = []
    normalization_warnings = []
    index = 0
    while index < len(lines):
        if not lines[index].startswith("--- "):
            if result and lines[index].strip() and not lines[index].startswith(("diff --git ", "index ", "new file mode ", "deleted file mode ", "old mode ", "new mode ", "similarity index ", "rename from ", "rename to ")):
                raise MyGithub10Error("PATCH_INVALID_FORMAT", "unexpected content outside unified diff hunk")
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
        hunks: list[tuple[int, int, list[str], list[str]]] = []
        while index < len(lines) and lines[index].startswith("@@ "):
            raw_header = lines[index]
            match = _HUNK_RE.match(raw_header)
            if not match:
                raise MyGithub10Error("PATCH_INVALID_FORMAT", "invalid unified diff hunk")
            old_start = int(match.group(1))
            new_start = int(match.group(3))
            old_lines: list[str] = []
            new_lines: list[str] = []
            last_side = ""
            index += 1
            while index < len(lines) and not lines[index].startswith(("--- ", "@@ ")):
                line = lines[index]
                if line.startswith("\\ No newline at end of file"):
                    if last_side == "old" and old_lines:
                        old_lines[-1] = old_lines[-1].removesuffix("\n").removesuffix("\r")
                    elif last_side == "new" and new_lines:
                        new_lines[-1] = new_lines[-1].removesuffix("\n").removesuffix("\r")
                    elif last_side == "both":
                        if old_lines:
                            old_lines[-1] = old_lines[-1].removesuffix("\n").removesuffix("\r")
                        if new_lines:
                            new_lines[-1] = new_lines[-1].removesuffix("\n").removesuffix("\r")
                    index += 1
                    continue
                if not line or line[0] not in " +-":
                    raise MyGithub10Error("PATCH_INVALID_FORMAT", "invalid unified diff line")
                if line[0] in " -": old_lines.append(line[1:])
                if line[0] in " +": new_lines.append(line[1:])
                last_side = "old" if line[0] == "-" else ("new" if line[0] == "+" else "both")
                index += 1
            old_count = int(match.group(2) or "1")
            new_count = int(match.group(4) or "1")
            actual_old_count = len(old_lines)
            actual_new_count = len(new_lines)
            normalized_header = f"@@ -{old_start},{actual_old_count} +{new_start},{actual_new_count} @@"
            hunk_index = len(hunks) + 1
            if old_count != actual_old_count or new_count != actual_new_count:
                normalization_warnings.append({
                    "path": path, "hunk_index": hunk_index,
                    "message": "hunk line counts were corrected from parsed content",
                })
            normalized_hunks.append({"path": path, "hunk_index": hunk_index,
                                     "original": raw_header.rstrip("\r\n"),
                                     "normalized": normalized_header})
            hunks.append((old_start, new_start, old_lines, new_lines))
        if not hunks:
            if operation != "delete":
                raise MyGithub10Error("PATCH_INVALID_FORMAT", f"file patch for {path} has no hunks")
            result.append((path, operation, []))
            continue
        result.append((path, operation, hunks))
    result.extend(metadata_only_deletes)
    if not result:
        raise MyGithub10Error("PATCH_EMPTY", "no file patch found")
    return result, {"patch_normalized": bool(normalization_warnings),
                    "normalized_hunks": normalized_hunks,
                    "normalization_warnings": normalization_warnings}


def _parse_patch(patch: str) -> list[tuple[str, str, list[tuple[int, int, list[str], list[str]]]]]:
    return _parse_patch_details(patch)[0]


def _canonical_patch_request(
    repository: str,
    branch: str,
    expected_head_sha: str,
    expected_blob_shas: dict[str, str],
    parsed: list[tuple[str, str, list[tuple[int, int, list[str], list[str]]]]],
    commit_message: str,
) -> dict[str, Any]:
    files = []
    for path, operation, hunks in parsed:
        files.append({
            "path": path,
            "operation": operation,
            "expected_blob_sha": expected_blob_shas.get(path, ""),
            "hunks": [
                {
                    "old_start": old_start,
                    "new_start": new_start,
                    "old_lines": old_lines,
                    "new_lines": new_lines,
                }
                for old_start, new_start, old_lines, new_lines in hunks
            ],
        })
    return {
        "tool_name": "apply_github_patch",
        "repository": repository,
        "branch": branch,
        "expected_head_sha": expected_head_sha,
        "commit_message": commit_message,
        "files": sorted(files, key=lambda item: item["path"]),
    }


def _patch_mismatch_details(
    source: list[str],
    position: int,
    expected: list[str],
    old_start: int,
    hunk_index: int,
    path: str,
) -> dict[str, Any]:
    candidates = [index for index in range(0, len(source) - len(expected) + 1)
                  if source[index:index + len(expected)] == expected]
    relative = 1
    actual = ""
    for relative, line in enumerate(expected, 1):
        actual_line = source[position + relative - 1] if position + relative - 1 < len(source) else ""
        if actual_line != line:
            actual = actual_line
            break
    details = {
        "path": path,
        "hunk_index": hunk_index,
        "expected_old_start": old_start,
        "nearest_candidate_start": (candidates[0] + 1 if len(candidates) == 1 else None),
        "exact_match_count": len(candidates),
        "mismatch": {"relative_line": relative, "expected": expected[relative - 1] if expected else "", "actual": actual},
        "suggested_action": "REFETCH_FILE_OR_USE_RANGE_EDIT",
    }
    if len(candidates) > 1:
        details["code"] = "PATCH_CONTEXT_AMBIGUOUS"
    return details


def _apply_file_patch(
    old: bytes,
    hunks: list[tuple[int, int, list[str], list[str]]],
    allow_empty_old: bool = False,
    path: str = "",
) -> bytes:
    source = _text(old).splitlines(keepends=True) if old else []
    cursor = 0
    output: list[str] = []
    for hunk_index, (old_start, _, expected, replacement) in enumerate(hunks, 1):
        if allow_empty_old:
            if old or old_start != 0 or expected:
                raise MyGithub10Error("PATCH_INVALID_FORMAT", "new-file patch must start at old line 0 with an empty old file")
            position = 0
        else:
            if old_start < 1:
                raise MyGithub10Error("PATCH_INVALID_FORMAT", "old line 0 is valid only for a new-file patch")
            position = old_start - 1
        if position < cursor or position > len(source) or source[position:position + len(expected)] != expected:
            details = _patch_mismatch_details(source, position, expected, old_start, hunk_index, path)
            code = details.pop("code", "PATCH_DOES_NOT_APPLY")
            raise MyGithub10Error(code, f"hunk at old line {old_start} does not match exactly", details)
        output.extend(source[cursor:position])
        output.extend(replacement)
        cursor = position + len(expected)
    output.extend(source[cursor:])
    return "".join(output).encode("utf-8")


def _commit_files(client, repository: str, branch: str, expected_head_sha: str, changed: dict[str, bytes | None], expected_blob_shas: dict[str, str], message: str) -> dict[str, Any]:
    service = client
    service._check_repository_allowed(repository)
    service._check_default_branch_write(repository, branch)
    gh = service.client if hasattr(service, "client") else service
    repo = _repo(gh, repository)
    ref = repo.get_git_ref(f"heads/{branch}")
    actual_head = ref.object.sha
    if expected_head_sha and actual_head != expected_head_sha:
        raise MyGithub10Error("PATCH_HEAD_CHANGED", f"branch HEAD changed before write for {repository}:{branch}", {"expected": expected_head_sha, "actual": actual_head, "repository": repository, "branch": branch, "phase": "before_write", "error_code": "HEAD_CHANGED"})
    elements = []
    old_shas = {}
    new_shas = {}
    for path, content in changed.items():
        _safe_path(path)
        try:
            entry = repo.get_contents(path, ref=actual_head)
            old_sha = None if isinstance(entry, list) else entry.sha
        except Exception as exc:
            if getattr(exc, "status", None) != 404:
                raise MyGithub10Error("GITHUB_READ_FAILED", f"GitHub failed while checking {path} before write", {"path": path, "repository": repository, "branch": branch, "retryable": True}) from exc
            old_sha = None
        old_shas[path] = old_sha
        expected = expected_blob_shas.get(path, "")
        if expected and expected != (old_sha or ""):
            raise MyGithub10Error("BLOB_CHANGED", f"file blob changed before write: {path}", {"expected": expected, "actual": old_sha, "repository": repository, "branch": branch, "path": path})
        if content is None:
            elements.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
        else:
            blob = gh.create_blob(repository, content.decode("utf-8"))
            new_shas[path] = blob.sha
            elements.append({"path": path, "mode": "100644", "type": "blob", "sha": blob.sha})
    git_commit = repo.get_git_commit(actual_head)
    base_tree = getattr(getattr(git_commit, "tree", None), "sha", None) or git_commit.commit.tree.sha
    tree = gh.create_git_tree(repository, elements, base_tree)
    commit = gh.create_commit(repository, message, tree.sha, [actual_head])

    try:
        ref.edit(sha=commit.sha, force=False)
    except Exception as exc:
        latest = None
        try:
            latest = gh.get_branch_head_fresh(repository, branch)
        except Exception:
            pass
        if latest != commit.sha:
            code = "PATCH_HEAD_CHANGED" if latest and latest != actual_head else "WRITE_VERIFY_FAILED"
            details = {
                "repository": repository, "branch": branch,
                "expected_previous_head": actual_head, "new_commit_sha": commit.sha,
                "observed_branch_head": latest or "", "expected_tree_sha": tree.sha,
                "observed_tree_sha": "", "failed_stage": "branch_ref_update",
            }
            raise MyGithub10Error(
                code,
                f"GitHub branch ref update was not durably confirmed for {repository}:{branch}",
                details,
            ) from exc
        # The update response may have been lost after GitHub applied it.  A
        # fresh branch read already sees the intended commit, so continue into
        # the full durable verification sequence instead of guessing failure.

    expected_paths = {path: (None if content is None else new_shas[path]) for path, content in changed.items()}
    try:
        evidence = post_write_verify(
            gh, repository, branch, actual_head, commit.sha, tree.sha, expected_paths
        )
    except WriteVerificationError as exc:
        raise MyGithub10Error("WRITE_VERIFY_FAILED", exc.message, exc.details) from exc

    file_results = []
    for path, content in changed.items():
        if content is None:
            file_results.append({"path": path, "operation": "delete", "old_blob_sha": old_shas[path], "new_blob_sha": None, "content_sha256": None, "size_bytes": 0})
            continue
        try:
            actual_text, actual_blob, actual_size = gh.get_file(repository, path, commit.sha)
            actual_bytes = actual_text.encode("utf-8") if actual_text is not None else b""
        except Exception as exc:
            raise MyGithub10Error("WRITE_VERIFY_FAILED", f"could not read back {path} after commit", {**evidence, "path": path, "failed_stage": "path_content_readback"}) from exc
        expected_content_sha = _sha256(content)
        if actual_blob != new_shas[path] or actual_bytes != content:
            raise MyGithub10Error("WRITE_VERIFY_FAILED", f"read-back bytes differ for {path}", {**evidence, "path": path, "failed_stage": "path_content_readback", "expected_content_sha256": expected_content_sha, "actual_content_sha256": _sha256(actual_bytes), "expected_blob_sha": new_shas[path], "actual_blob_sha": actual_blob})
        file_results.append({"path": path, "operation": "modify" if old_shas[path] else "add", "old_blob_sha": old_shas[path], "new_blob_sha": actual_blob, "content_sha256": expected_content_sha, "size_bytes": actual_size})
    logger.info("mygithub10 verified_write repository=%s branch=%s expected_head_sha=%s old_head_sha=%s new_head_sha=%s tree_sha=%s files=%s", repository, branch, expected_head_sha, actual_head, commit.sha, tree.sha, [item["path"] for item in file_results])
    return {"commit_sha": commit.sha, "new_head_sha": commit.sha, "old_head_sha": actual_head, "tree_sha": tree.sha, "branch": branch, "repository": repository, "changed_files": file_results, **evidence}


def apply_patch(client, repository: str, branch: str, expected_head_sha: str, expected_blob_shas_json: str, patch: str, commit_message: str, dry_run: bool, idempotency_key: str = "", audit_context: dict[str, Any] | None = None) -> dict[str, Any]:
    parsed, patch_metadata = _parse_patch_details(patch)
    try:
        expected = json.loads(expected_blob_shas_json or "{}")
    except (TypeError, json.JSONDecodeError) as exc:
        raise MyGithub10Error("PATCH_INVALID_FORMAT", "expected_blob_shas_json must be valid JSON") from exc
    if not isinstance(expected, dict):
        raise MyGithub10Error("PATCH_INVALID_FORMAT", "expected_blob_shas_json must be an object")
    request = _canonical_patch_request(
        repository,
        branch,
        expected_head_sha,
        expected,
        parsed,
        commit_message,
    )
    operation_id, replay = ("", None) if dry_run else _idempotent_start("apply_github_patch", idempotency_key, request, audit_context)
    if replay:
        return replay
    repo = _repo(client, repository)
    actual_head, _ = _resolve_commit(repo, branch)
    if expected_head_sha and actual_head != expected_head_sha:
        if operation_id:
            _idempotent_finish(operation_id, "failed", error_code="HEAD_CHANGED")
        raise MyGithub10Error("PATCH_HEAD_CHANGED", f"branch HEAD changed before patch for {repository}:{branch}", {"expected": expected_head_sha, "actual": actual_head, "repository": repository, "branch": branch, "phase": "before_write", "error_code": "HEAD_CHANGED"})
    changed: dict[str, bytes | None] = {}
    previews = []
    for path, operation, hunks in parsed:
        old = b""
        old_sha = None
        if operation == "add":
            try:
                existing = repo.get_contents(path, ref=actual_head)
                if existing is not None:
                    raise MyGithub10Error("PATCH_TARGET_EXISTS", f"new-file patch target already exists: {path}", {"path": path, "error_code": "FILE_ALREADY_EXISTS"})
            except MyGithub10Error:
                raise
            except Exception as exc:
                if getattr(exc, "status", None) != 404:
                    raise MyGithub10Error("GITHUB_READ_FAILED", f"GitHub failed while checking patch target {path}", {"path": path, "retryable": True}) from exc
        else:
            old, old_sha, _ = _read_blob(repo, path, actual_head)
            if len(old) > MAX_TEXT_EDIT_FILE_BYTES:
                raise MyGithub10Error("FILE_TOO_LARGE", f"text edit target exceeds the file limit: {path}", {"path": path, "limit_bytes": MAX_TEXT_EDIT_FILE_BYTES})
            if expected.get(path) and expected[path] != old_sha:
                raise MyGithub10Error("BLOB_CHANGED", f"file blob changed before patch: {path}", {"expected": expected[path], "actual": old_sha, "repository": repository, "branch": branch, "path": path})
        if operation == "delete":
            if hunks:
                deleted = _apply_file_patch(
                    old,
                    hunks,
                    allow_empty_old=not old,
                    path=path,
                )
                if deleted:
                    raise MyGithub10Error("PATCH_INVALID_FORMAT", f"delete patch must remove all file content: {path}")
            elif old:
                raise MyGithub10Error(
                    "PATCH_INVALID_FORMAT",
                    f"metadata-only delete is valid only for an empty tracked file: {path}",
                )
            new = None
        else:
            new = _apply_file_patch(
                old,
                hunks,
                allow_empty_old=operation == "add",
                path=path,
            )
        changed[path] = new
        previews.append({"path": path, "operation": operation, "old_blob_sha": old_sha,
                         "new_blob_sha": None if new is None else _git_blob_sha(new),
                         "new_content_sha256": None if new is None else _sha256(new),
                         "added_lines": sum(len(h[3]) for h in hunks),
                         "deleted_lines": sum(len(h[2]) for h in hunks)})
    fingerprint = _sha256(_json(request).encode())
    diff_preview, diff_truncated = _truncate_utf8(patch)
    result = {"ok": True, "dry_run": dry_run, "repository": repository, "branch": branch,
              "expected_head_sha": expected_head_sha, "changed_files": previews,
              "diff_preview": diff_preview, "diff_truncated": diff_truncated,
              "operation_fingerprint": fingerprint, **patch_metadata}
    if dry_run:
        return result
    try:
        result.update(_commit_files(client, repository, branch, expected_head_sha, changed, expected, commit_message))
        _idempotent_mark_git_verified(operation_id, result)
        result["_operation_id"] = operation_id
        return result
    except MyGithub10Error as exc:
        _idempotent_finish(operation_id, "failed", error_code=exc.code, result={"failed_stage": exc.details.get("failed_stage"), "error": {"code": exc.code, "details": exc.details}})
        raise


def edit_ranges(client, repository: str, branch: str, expected_head_sha: str, operations_json: str, commit_message: str, dry_run: bool, idempotency_key: str = "", audit_context: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        operations = json.loads(operations_json or "[]")
    except (TypeError, json.JSONDecodeError) as exc:
        raise MyGithub10Error("PATCH_INVALID_FORMAT", "operations_json must be valid JSON") from exc
    if not isinstance(operations, list) or not operations:
        raise MyGithub10Error("PATCH_EMPTY", "at least one range edit operation is required")
    if len(operations) > MAX_FILE_EDIT_OPERATIONS:
        raise MyGithub10Error("PATCH_SCOPE_EXCEEDED", "too many range edit operations")
    request = {"tool_name": "edit_github_file_ranges", "repository": repository, "branch": branch, "expected_head_sha": expected_head_sha, "operations": operations, "commit_message": commit_message}
    operation_id, replay = ("", None) if dry_run else _idempotent_start("edit_github_file_ranges", idempotency_key, request, audit_context)
    if replay:
        return replay
    repo = _repo(client, repository)
    actual_head, _ = _resolve_commit(repo, branch)
    if expected_head_sha and actual_head != expected_head_sha:
        if operation_id:
            _idempotent_finish(operation_id, "failed", error_code="HEAD_CHANGED")
        raise MyGithub10Error("PATCH_HEAD_CHANGED", f"branch HEAD changed before range edit for {repository}:{branch}", {"expected": expected_head_sha, "actual": actual_head, "repository": repository, "branch": branch, "phase": "before_write", "error_code": "HEAD_CHANGED"})
    changed = {}
    expected = {}
    old_texts = {}
    if any(not isinstance(item, dict) or not isinstance(item.get("path"), str) for item in operations):
        raise MyGithub10Error("PATCH_INVALID_FORMAT", "every operation must contain a path")
    for path in sorted({item["path"] for item in operations}):
        data, blob_sha, _ = _read_blob(repo, path, actual_head)
        if len(data) > MAX_TEXT_EDIT_FILE_BYTES:
            raise MyGithub10Error("FILE_TOO_LARGE", f"text edit target exceeds the file limit: {path}", {"path": path, "limit_bytes": MAX_TEXT_EDIT_FILE_BYTES})
        blob_expectations = {item.get("expected_blob_sha") for item in operations if item["path"] == path and item.get("expected_blob_sha")}
        if blob_expectations and blob_expectations != {blob_sha}:
            raise MyGithub10Error("BLOB_CHANGED", f"file blob changed before range edit: {path}", {"path": path, "expected": sorted(blob_expectations)[0], "actual": blob_sha})
        text = _text(data)
        old_texts[path] = text
        lines = text.splitlines(keepends=True)
        items = [item for item in operations if item["path"] == path]
        positions = []
        for order, item in enumerate(items):
            if not isinstance(item, dict) or item.get("operation") not in {"replace", "delete", "insert_before", "insert_after"}:
                raise MyGithub10Error("PATCH_INVALID_FORMAT", f"operation {order} is invalid")
            try:
                start = int(item["start_line"])
                end = int(item.get("end_line", item["start_line"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise MyGithub10Error("PATCH_INVALID_FORMAT", f"operation {order} line numbers must be integers") from exc
            if item["operation"] in {"replace", "delete"} and (start < 1 or end < start or end > len(lines)):
                raise MyGithub10Error("PATCH_SCOPE_EXCEEDED", f"operation {order} range {start}-{end} is outside the file")
            if item["operation"] == "insert_before" and (start < 1 or end != start or start > len(lines) + 1):
                raise MyGithub10Error("PATCH_SCOPE_EXCEEDED", f"operation {order} insert_before anchor is outside the file")
            if item["operation"] == "insert_after" and (start < 1 or end < start or end > len(lines)):
                raise MyGithub10Error("PATCH_SCOPE_EXCEEDED", f"operation {order} insert_after range is outside the file")
            old_text = "".join(lines[start - 1:end]) if item["operation"] in {"replace", "delete"} else ""
            if item["operation"] in {"replace", "delete"}:
                has_text = "expected_old_text" in item
                has_hash = "expected_old_text_sha256" in item
                if not has_text and not has_hash:
                    raise MyGithub10Error(
                        "PATCH_INVALID_FORMAT",
                        f"operation {order} requires expected_old_text or expected_old_text_sha256",
                    )
                if has_text and not isinstance(item["expected_old_text"], str):
                    raise MyGithub10Error("PATCH_INVALID_FORMAT", f"operation {order} expected_old_text must be a string")
                if has_hash and not re.fullmatch(r"[0-9a-f]{64}", str(item["expected_old_text_sha256"])):
                    raise MyGithub10Error("PATCH_INVALID_FORMAT", f"operation {order} expected_old_text_sha256 must be a lowercase SHA-256")
                expected_old_text = item.get("expected_old_text")
                if has_text and expected_old_text != old_text:
                    expected_lines = expected_old_text.splitlines(keepends=True)
                    actual_lines = old_text.splitlines(keepends=True)
                    mismatch = next(
                        (
                            index
                            for index in range(1, max(len(expected_lines), len(actual_lines)) + 1)
                            if (
                                expected_lines[index - 1] if index <= len(expected_lines) else ""
                            ) != (
                                actual_lines[index - 1] if index <= len(actual_lines) else ""
                            )
                        ),
                        1,
                    )
                    raise MyGithub10Error(
                        "PATCH_TEXT_MISMATCH",
                        f"old range text does not match for {path}:{start}-{end}",
                        {
                            "path": path,
                            "start_line": start,
                            "end_line": end,
                            "relative_line": mismatch,
                            "expected": expected_lines[mismatch - 1] if mismatch <= len(expected_lines) else "",
                            "actual": actual_lines[mismatch - 1] if mismatch <= len(actual_lines) else "",
                        },
                    )
                if has_hash and _sha256(old_text.encode()) != item["expected_old_text_sha256"]:
                    raise MyGithub10Error(
                        "PATCH_TEXT_MISMATCH",
                        f"old range hash does not match for {path}:{start}-{end}",
                        {"path": path, "start_line": start, "end_line": end, "relative_line": 1},
                    )
            if item["operation"] == "insert_before": splice = (start - 1, start - 1)
            elif item["operation"] == "insert_after": splice = (end, end)
            else: splice = (start - 1, end)
            positions.append((splice[0], splice[1], item))
        ordered = sorted(positions, key=lambda value: (value[0], value[1], value[2].get("order", 0)))
        for previous, current in zip(ordered, ordered[1:]):
            ps, pe, _ = previous
            cs, ce, _ = current
            previous_empty = ps == pe
            current_empty = cs == ce
            conflict = (previous_empty and current_empty and ps == cs) or (previous_empty and cs < ps < ce) or (current_empty and ps < cs < pe) or (not previous_empty and not current_empty and cs < pe)
            if conflict:
                raise MyGithub10Error("PATCH_SCOPE_EXCEEDED", f"overlapping or ambiguous ranges in {path}", {"path": path, "error_code": "PATCH_RANGE_OVERLAP"})
        for splice_start, splice_end, item in sorted(positions, key=lambda value: (value[0], value[1]), reverse=True):
            replacement = item.get("replacement_text", item.get("replacement", ""))
            if not isinstance(replacement, str):
                raise MyGithub10Error("PATCH_INVALID_FORMAT", f"replacement_text for operation {item.get('order', 0)} must be a string")
            if item["operation"] == "delete": replacement = ""
            lines[splice_start:splice_end] = [replacement] if replacement else []
        changed[path] = "".join(lines).encode()
        expected[path] = blob_sha
    fingerprint = _sha256(_json({"tool_name": "edit_github_file_ranges", "repository": repository, "branch": branch, "expected_head_sha": expected_head_sha, "operations": operations, "commit_message": commit_message}).encode())
    diffs = []
    diff_stats = {}
    for path in sorted(changed):
        patch, added_lines, deleted_lines = _minimal_unified_diff(
            path,
            old_texts[path],
            changed[path].decode("utf-8"),
        )
        diffs.append(patch)
        diff_stats[path] = (added_lines, deleted_lines)
    full_diff = "".join(diffs)
    diff_preview, diff_truncated = _truncate_utf8(full_diff)
    result = {"ok": True, "dry_run": dry_run, "repository": repository, "branch": branch, "expected_head_sha": expected_head_sha, "resolved_head_sha": actual_head,
              "changed_files": [{"path": p, "operation": "modify", "old_blob_sha": expected[p], "new_blob_sha": _git_blob_sha(changed[p]), "content_sha256": _sha256(changed[p]), "size_bytes": len(changed[p]), "added_lines": diff_stats[p][0], "deleted_lines": diff_stats[p][1]} for p in changed],
              "new_content_sha256": {p: _sha256(v) for p, v in changed.items()}, "diff_preview": diff_preview, "diff_truncated": diff_truncated, "operation_count": len(operations), "operation_fingerprint": fingerprint}
    if dry_run: return result
    try:
        result.update(_commit_files(client, repository, branch, expected_head_sha, changed, expected, commit_message))
        _idempotent_mark_git_verified(operation_id, result)
        result["_operation_id"] = operation_id
        return result
    except MyGithub10Error as exc:
        _idempotent_finish(operation_id, "failed", error_code=exc.code, result={"failed_stage": exc.details.get("failed_stage"), "error": {"code": exc.code, "details": exc.details}})
        raise


def build_patch(path: str, expected_blob_sha: str, original_text: str, replacement_text: str) -> dict[str, Any]:
    """Build a deterministic, local-only unified diff; never contacts GitHub."""
    _safe_path(path)
    if not isinstance(original_text, str) or not isinstance(replacement_text, str):
        raise MyGithub10Error("PATCH_INVALID_FORMAT", "original_text and replacement_text must be strings")
    old_bytes = original_text.encode("utf-8")
    if len(old_bytes) > MAX_FILE_CHUNK_BYTES * 16 or len(replacement_text.encode("utf-8")) > MAX_FILE_CHUNK_BYTES * 16:
        raise MyGithub10Error("PATCH_TOO_LARGE", "text exceeds the patch builder limit")
    if expected_blob_sha and expected_blob_sha != _git_blob_sha(old_bytes):
        raise MyGithub10Error("BLOB_CHANGED", "expected_blob_sha does not match original_text", {"expected": expected_blob_sha, "actual": _git_blob_sha(old_bytes), "path": path})
    patch, added_lines, deleted_lines = _minimal_unified_diff(path, original_text, replacement_text)
    diff_preview, diff_truncated = _truncate_utf8(patch)
    fingerprint = _sha256(_json({"path": path, "expected_blob_sha": expected_blob_sha, "original_text": original_text, "replacement_text": replacement_text}).encode())
    return {"ok": True, "dry_run": True, "path": path, "patch": patch, "diff_preview": diff_preview, "diff_truncated": diff_truncated,
            "operation_fingerprint": fingerprint, "old_blob_sha": _git_blob_sha(old_bytes), "new_blob_sha": _git_blob_sha(replacement_text.encode()),
            "old_count": len(original_text.splitlines()), "new_count": len(replacement_text.splitlines()),
            "added_lines": added_lines, "deleted_lines": deleted_lines}


def capabilities(build_sha: str) -> dict[str, Any]:
    from app.mcp_response import MAX_RESPONSE_RESOURCE_CHUNK_BYTES, MAX_SAFE_INLINE_BYTES
    if not re.fullmatch(r"[0-9a-f]{40}", build_sha or ""):
        raise RuntimeError("build_sha must be a full lowercase 40-character Git commit SHA")
    new_build_env = bool(os.environ.get("MYGITHUB12_BUILD_SHA"))
    legacy_build_env = bool(os.environ.get("MYGITHUB10_BUILD_SHA"))
    return {
        "name": "MyGithut12",
        "version": SERVICE_VERSION,
        "tool_count": 155,
        "build_sha": build_sha,
        "build_sha_source": "environment" if new_build_env or legacy_build_env else "vcs_fallback",
        "runtime_mode": os.environ.get("MYGITHUB12_RUNTIME_MODE", os.environ.get("MYGITHUB10_RUNTIME_MODE", "development")),
        "compatibility_env_used": legacy_build_env and not new_build_env,
        "source_repository": "frankichen/github_mcp",
        "repository_index_version": "12.0.0-1",
        "supported_index_languages": ["python", "go", "typescript", "javascript", "vue", "java", "rust", "csharp", "sql"],
        "max_inline_response_bytes": MAX_SAFE_INLINE_BYTES,
        "transport_inline_hard_limit_bytes": MAX_INLINE_RESPONSE_BYTES,
        "response_resource_chunk_bytes": MAX_RESPONSE_RESOURCE_CHUNK_BYTES,
        "max_file_chunk_bytes": MAX_FILE_CHUNK_BYTES,
        "max_patch_bytes": MAX_PATCH_BYTES,
        "max_upload_chunk_bytes": MAX_UPLOAD_CHUNK_BYTES,
        "supports_file_manifest": True,
        "supports_byte_chunks": True,
        "supports_mcp_resources": True,
        "supports_response_resource_fallback": True,
        "supports_structured_content": True,
        "supports_response_meta": True,
        "supports_incremental_patch": True,
        "supports_range_edit": True,
        "range_edit_semantics": {"start_line": "1-based inclusive", "end_line": "1-based inclusive", "encoding": "UTF-8 text lines; original LF/CRLF and final-newline state are preserved"},
        "supports_chunked_upload": True,
        "supports_dry_run": True,
        "supports_expected_head_sha": True,
        "supports_expected_blob_sha": True,
        "supports_idempotency_key": True,
        "supports_operation_audit": True,
        "supports_tree_attestation": True,
        "supports_artifact_deployment": False,
        "supports_gofmt_autofix": False,
        "supports_gofmt_readonly_check": True,
        "supports_real_ci_performance_validation": False,
        "supports_repository_index_jobs": True,
        "supports_development_workspaces": True,
        "supports_workspace_leases": True,
        "supports_workspace_revision_cas": True,
        "supports_repository_tree_snapshot": True,
        "supports_repository_file_search": True,
        "supports_repository_text_search": True,
        "supports_repository_semantic_search": True,
        "supports_batch_file_read": True,
        "supports_repository_symbol_index": True,
        "supports_symbol_definition": True,
        "supports_symbol_references": True,
        "supports_symbol_call_hierarchy": True,
        "supports_symbol_implementations": True,
        "supports_symbol_type_hierarchy": True,
        "supports_symbol_diagnostics": True,
        "supports_symbol_history": True,
        "supports_repository_dependency_graph": True,
        "supports_repository_agent_instructions": True,
        "supports_repository_context_pack": True,
        "supports_change_context_pack": True,
        "supports_repository_change_impact": True,
        "supports_repository_patch_analysis": True,
        "supports_affected_test_selection": True,
        "supports_repository_contract_change_detection": True,
        "recommended_large_file_workflow": ["get_github_file_manifest", "read_github_file_chunk", "begin_github_file_upload", "append_github_file_upload_chunk", "finalize_github_file_upload", "commit_github_uploaded_files"],
        "stable_write_error_codes": ["HEAD_CHANGED", "BLOB_CHANGED", "WRITE_VERIFY_FAILED", "PATCH_DOES_NOT_APPLY", "PATCH_INVALID_FORMAT", "PATCH_TARGET_EXISTS", "IDEMPOTENCY_CONFLICT", "IDEMPOTENCY_IN_PROGRESS", "WORKSPACE_LEASE_REQUIRED", "WORKSPACE_REVISION_MISMATCH", "WORKSPACE_BRANCH_DRIFTED"],
        "deprecated_tools": [{"name": "get_github_file", "deprecated": True, "replacement": "get_github_file_manifest + read_github_file_chunk"}, {"name": "commit_github_files", "deprecated": True, "replacement": "apply_github_patch or commit_github_uploaded_files"}, {"name": "get_test_deployment_logs", "deprecated": True, "replacement": "get_test_deployment_log_tail"}],
    }


_UPLOAD_ROOT = Path(os.environ.get("MYGITHUB10_UPLOAD_DIR", tempfile.gettempdir())) / "mygithub10-uploads"


def _upload_paths(upload_id: str) -> tuple[Path, Path]:
    if not re.fullmatch(r"[0-9a-f-]{36}", upload_id):
        raise MyGithub10Error("PATCH_UNSAFE_PATH", "invalid upload id")
    return _UPLOAD_ROOT / f"{upload_id}.bin", _UPLOAD_ROOT / f"{upload_id}.json"


def _upload_lock(upload_id: str):
    _upload_paths(upload_id)
    _UPLOAD_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    handle = (_UPLOAD_ROOT / f"{upload_id}.lock").open("a+")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def cleanup_expired_uploads(now: float | None = None) -> int:
    """Remove expired upload data and orphan lock files without following links."""
    if not _UPLOAD_ROOT.is_dir():
        return 0
    now = now or time.time()
    removed = 0
    for meta_path in _UPLOAD_ROOT.glob("*.json"):
        upload_id = meta_path.stem
        if not re.fullmatch(r"[0-9a-f-]{36}", upload_id):
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if float(meta.get("expires_at", 0)) > now:
                continue
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        data_path, checked_meta_path = _upload_paths(upload_id)
        data_path.unlink(missing_ok=True)
        checked_meta_path.unlink(missing_ok=True)
        removed += 1
    return removed


def begin_upload() -> dict[str, Any]:
    _UPLOAD_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    cleanup_expired_uploads()
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
    lock = _upload_lock(upload_id)
    try:
        data_path, meta_path, meta = _load_upload(upload_id)
        if offset != meta["size"]:
            raise MyGithub10Error("UPLOAD_OFFSET_MISMATCH", "upload offset is not contiguous")
        if meta["size"] + len(content) > MAX_UPLOAD_BYTES:
            raise MyGithub10Error("UPLOAD_SIZE_EXCEEDED", "upload exceeds configured size limit")
        with data_path.open("ab") as handle:
            handle.write(content)
        meta["size"] += len(content)
        temporary_meta = meta_path.with_suffix(".json.tmp")
        temporary_meta.write_text(_json(meta), encoding="utf-8")
        os.chmod(temporary_meta, 0o600)
        os.replace(temporary_meta, meta_path)
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()
    return {"upload_id": upload_id, "offset": offset, "next_offset": meta["size"], "chunk_sha256": chunk_sha256}


def finalize_upload(upload_id: str, expected_size_bytes: int, expected_sha256: str) -> dict[str, Any]:
    lock = _upload_lock(upload_id)
    try:
        data_path, meta_path, meta = _load_upload(upload_id)
        data = data_path.read_bytes()
        if len(data) != expected_size_bytes:
            raise MyGithub10Error("UPLOAD_SIZE_MISMATCH", "final upload size differs from expected")
        actual = _sha256(data)
        if actual != expected_sha256:
            raise MyGithub10Error("UPLOAD_SHA_MISMATCH", "final upload SHA256 differs from expected")
        meta.update({"size": len(data), "sha256": actual, "finalized": True})
        temporary_meta = meta_path.with_suffix(".json.tmp")
        temporary_meta.write_text(_json(meta), encoding="utf-8")
        os.chmod(temporary_meta, 0o600)
        os.replace(temporary_meta, meta_path)
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()
    return {"upload_id": upload_id, "size_bytes": len(data), "sha256": actual, "finalized": True}


def commit_upload(client, repository: str, branch: str, expected_head_sha: str, path: str, expected_blob_sha: str, upload_id: str, commit_message: str, idempotency_key: str = "", audit_context: dict[str, Any] | None = None) -> dict[str, Any]:
    _safe_path(path)
    existing = _idempotent_existing(idempotency_key)
    if existing and existing.get("status") == "success_verified":
        stored_request = json.loads(existing.get("request_json") or "{}")
        scope = {"repository": repository, "branch": branch, "expected_head_sha": expected_head_sha, "path": path, "expected_blob_sha": expected_blob_sha, "upload_id": upload_id, "commit_message": commit_message}
        stored_scope = {key: stored_request.get(key) for key in scope}
        if stored_scope != scope:
            raise MyGithub10Error("IDEMPOTENCY_CONFLICT", "idempotency key was used for a different upload request")
        if existing.get("result_json"):
            replay = json.loads(existing["result_json"])
            replay["replayed"] = True
            replay["operation_id"] = existing["operation_id"]
            return replay
        raise MyGithub10Error("IDEMPOTENCY_RESULT_UNAVAILABLE", "the verified idempotent upload result is unavailable")
    if existing and existing.get("status") == "succeeded":
        raise MyGithub10Error(
            "IDEMPOTENCY_RESULT_UNVERIFIED",
            "legacy upload success was not durably verified against GitHub",
            {"operation_id": existing["operation_id"], "recovery_required": True},
        )
    data_path, _, meta = _load_upload(upload_id)
    if not meta.get("finalized"):
        raise MyGithub10Error("UPLOAD_NOT_FINALIZED", "finalize the upload before committing")
    data = data_path.read_bytes()
    request = {"tool_name": "commit_github_uploaded_files", "repository": repository, "branch": branch, "expected_head_sha": expected_head_sha, "path": path, "expected_blob_sha": expected_blob_sha, "upload_id": upload_id, "upload_sha256": meta["sha256"], "upload_size": meta["size"], "commit_message": commit_message}
    operation_id, replay = _idempotent_start("commit_github_uploaded_files", idempotency_key, request, audit_context)
    if replay:
        return replay
    try:
        result = _commit_files(client, repository, branch, expected_head_sha, {path: data}, {path: expected_blob_sha}, commit_message)
        result = {"ok": True, **result, "upload_id": upload_id}
        _idempotent_mark_git_verified(operation_id, result)
        result["_operation_id"] = operation_id
        result["_cleanup_upload_id"] = upload_id
        return result
    except MyGithub10Error as exc:
        _idempotent_finish(operation_id, "failed", error_code=exc.code, result={"failed_stage": exc.details.get("failed_stage"), "error": {"code": exc.code, "details": exc.details}})
        raise


def abort_upload(upload_id: str) -> dict[str, Any]:
    data_path, meta_path = _upload_paths(upload_id)
    data_path.unlink(missing_ok=True)
    meta_path.unlink(missing_ok=True)
    return {"ok": True, "upload_id": upload_id, "aborted": True}
