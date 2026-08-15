"""Bootstrap fixes for MyGithub10 strict write primitives.

This module exists because the deployed strict writer cannot update its own
implementation: every strict write path shares the same broken tree lookup.
It patches the already-imported ``app.mygithub10`` module without changing the
MCP tool names or schemas.
"""

from __future__ import annotations

import json
import re
from typing import Any

_BASE = None


def _base():
    if _BASE is None:
        raise RuntimeError("MyGithub10 runtime fixes are not installed")
    return _BASE


def _commit_files(client, repository: str, branch: str, expected_head_sha: str,
                  changed: dict[str, bytes | None], expected_blob_shas: dict[str, str],
                  message: str) -> dict[str, Any]:
    return _ORIGINAL_COMMIT_FILES(
        client, repository, branch, expected_head_sha, changed,
        expected_blob_shas, message,
    )


def _load_operations(operations_json: str) -> list[dict[str, Any]]:
    base = _base()
    try:
        operations = json.loads(operations_json or "[]")
    except (TypeError, json.JSONDecodeError) as exc:
        raise base.MyGithub10Error("PATCH_INVALID_FORMAT", "operations_json must be valid JSON") from exc
    if not isinstance(operations, list) or not operations:
        raise base.MyGithub10Error("PATCH_EMPTY", "at least one range edit operation is required")
    if len(operations) > 1000:
        raise base.MyGithub10Error("PATCH_SCOPE_EXCEEDED", "too many range edit operations")

    allowed = {"replace", "delete", "insert_before", "insert_after"}
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(operations):
        if not isinstance(raw, dict):
            raise base.MyGithub10Error("PATCH_INVALID_FORMAT", f"operation {index} must be an object")
        path = raw.get("path")
        operation = raw.get("operation")
        if not isinstance(path, str):
            raise base.MyGithub10Error("PATCH_INVALID_FORMAT", f"operation {index} path must be a string")
        base._safe_path(path)
        if operation not in allowed:
            raise base.MyGithub10Error("PATCH_INVALID_FORMAT", f"operation {index} has an unsupported operation")
        start_value = raw.get("start_line")
        end_value = raw.get("end_line", start_value)
        if isinstance(start_value, bool) or isinstance(end_value, bool):
            raise base.MyGithub10Error("PATCH_INVALID_FORMAT", f"operation {index} line numbers must be integers")
        try:
            start = int(start_value)
            end = int(end_value)
        except (TypeError, ValueError) as exc:
            raise base.MyGithub10Error("PATCH_INVALID_FORMAT", f"operation {index} line numbers must be integers") from exc
        replacement = raw.get("replacement", "")
        if operation != "delete" and not isinstance(replacement, str):
            raise base.MyGithub10Error("PATCH_INVALID_FORMAT", f"operation {index} replacement must be a string")
        expected_hash = raw.get("expected_old_text_sha256", "")
        if operation in {"replace", "delete"} and not re.fullmatch(r"[0-9a-f]{64}", expected_hash or ""):
            raise base.MyGithub10Error(
                "PATCH_INVALID_FORMAT",
                f"operation {index} requires a lowercase SHA256 old-text hash",
            )
        normalized.append({
            "path": path,
            "operation": operation,
            "start_line": start,
            "end_line": end,
            "replacement": replacement,
            "expected_old_text_sha256": expected_hash,
            "order": index,
        })
    return normalized


def _splice(item: dict[str, Any], line_count: int) -> tuple[int, int]:
    base = _base()
    start = item["start_line"]
    end = item["end_line"]
    operation = item["operation"]
    if operation in {"replace", "delete"}:
        if start < 1 or end < start or end > line_count:
            raise base.MyGithub10Error("PATCH_SCOPE_EXCEEDED", "replace/delete range is outside the file")
        return start - 1, end
    if operation == "insert_before":
        if start < 1 or end != start or start > line_count + 1:
            raise base.MyGithub10Error("PATCH_SCOPE_EXCEEDED", "insert_before requires one valid anchor line")
        return start - 1, start - 1
    if start < 1 or end < start or end > line_count:
        raise base.MyGithub10Error("PATCH_SCOPE_EXCEEDED", "insert_after range is outside the file")
    return end, end


def _ensure_non_overlapping(splices: list[tuple[int, int, int, dict[str, Any]]]) -> None:
    base = _base()
    ordered = sorted(splices, key=lambda value: (value[0], value[1], value[2]))
    for index, left in enumerate(ordered):
        left_start, left_end = left[0], left[1]
        for right in ordered[index + 1:]:
            right_start, right_end = right[0], right[1]
            if right_start > left_end:
                break
            left_empty = left_start == left_end
            right_empty = right_start == right_end
            if left_empty and right_empty:
                conflict = left_start == right_start
            elif left_empty:
                conflict = right_start < left_start < right_end
            elif right_empty:
                conflict = left_start < right_start < left_end
            else:
                conflict = max(left_start, right_start) < min(left_end, right_end)
            if conflict:
                raise base.MyGithub10Error(
                    "PATCH_SCOPE_EXCEEDED", "overlapping or ambiguous ranges are not allowed"
                )


def edit_ranges(client, repository: str, branch: str, expected_head_sha: str,
                operations_json: str, commit_message: str, dry_run: bool,
                idempotency_key: str = "", audit_context: dict[str, Any] | None = None) -> dict[str, Any]:
    return _ORIGINAL_EDIT_RANGES(
        client, repository, branch, expected_head_sha, operations_json,
        commit_message, dry_run, idempotency_key, audit_context,
    )


def apply_patch(client, repository: str, branch: str, expected_head_sha: str,
                expected_blob_shas_json: str, patch: str, commit_message: str,
                dry_run: bool, idempotency_key: str = "", audit_context: dict[str, Any] | None = None) -> dict[str, Any]:
    return _ORIGINAL_APPLY_PATCH(
        client, repository, branch, expected_head_sha, expected_blob_shas_json,
        patch, commit_message, dry_run, idempotency_key, audit_context,
    )


_ORIGINAL_APPLY_PATCH = None
_ORIGINAL_COMMIT_FILES = None
_ORIGINAL_EDIT_RANGES = None


def install(module) -> None:
    global _BASE, _ORIGINAL_APPLY_PATCH, _ORIGINAL_COMMIT_FILES, _ORIGINAL_EDIT_RANGES
    if getattr(module, "_runtime_strict_write_fix_installed", False):
        return
    _BASE = module
    _ORIGINAL_APPLY_PATCH = module.apply_patch
    _ORIGINAL_COMMIT_FILES = module._commit_files
    _ORIGINAL_EDIT_RANGES = module.edit_ranges
    module._commit_files = _commit_files
    module.edit_ranges = edit_ranges
    module.apply_patch = apply_patch
    module._runtime_strict_write_fix_installed = True
