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
    base = _base()
    service = client
    service._check_repository_allowed(repository)
    service._check_default_branch_write(repository, branch)
    gh = service.client if hasattr(service, "client") else service
    repo = base._repo(gh, repository)
    ref = repo.get_git_ref(f"heads/{branch}")
    actual_head = ref.object.sha
    if expected_head_sha and actual_head != expected_head_sha:
        raise base.MyGithub10Error(
            "PATCH_HEAD_CHANGED", "branch HEAD changed",
            {"expected": expected_head_sha, "actual": actual_head},
        )

    elements = []
    old_shas: dict[str, str | None] = {}
    for path, content in changed.items():
        base._safe_path(path)
        try:
            entry = repo.get_contents(path, ref=actual_head)
            old_sha = None if isinstance(entry, list) else entry.sha
        except Exception:
            old_sha = None
        old_shas[path] = old_sha
        expected = expected_blob_shas.get(path, "")
        if expected and expected != (old_sha or ""):
            raise base.MyGithub10Error(
                "PATCH_FILE_CHANGED", f"file blob changed: {path}",
                {"expected": expected, "actual": old_sha},
            )
        if content is None:
            elements.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
        else:
            blob = gh.create_blob(repository, content.decode("utf-8"))
            elements.append({"path": path, "mode": "100644", "type": "blob", "sha": blob.sha})

    # PyGithub's GitCommit exposes ``tree`` directly.  The old implementation
    # incorrectly used ``.commit.tree`` and crashed before creating a tree.
    base_tree = repo.get_git_commit(actual_head).tree.sha
    tree = gh.create_git_tree(repository, elements, base_tree)
    commit = gh.create_commit(repository, message, tree.sha, [actual_head])
    try:
        ref.edit(sha=commit.sha, force=False)
    except Exception as exc:
        raise base.MyGithub10Error("PATCH_HEAD_CHANGED", "branch changed while committing") from exc
    return {
        "commit_sha": commit.sha,
        "tree_sha": tree.sha,
        "branch": branch,
        "repository": repository,
        "changed_files": [
            {
                "path": path,
                "operation": "delete" if content is None else ("modify" if old_shas[path] else "add"),
            }
            for path, content in changed.items()
        ],
    }


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
                idempotency_key: str = "") -> dict[str, Any]:
    base = _base()
    operations = _load_operations(operations_json)
    repo = base._repo(client, repository)
    actual_head, _ = base._resolve_commit(repo, branch)
    if expected_head_sha and actual_head != expected_head_sha:
        raise base.MyGithub10Error(
            "PATCH_HEAD_CHANGED", "branch HEAD changed",
            {"expected": expected_head_sha, "actual": actual_head},
        )

    changed: dict[str, bytes] = {}
    expected: dict[str, str] = {}
    for path in sorted({item["path"] for item in operations}):
        data, blob_sha, _ = base._read_blob(repo, path, actual_head)
        lines = base._text(data).splitlines(keepends=True)
        splices: list[tuple[int, int, int, dict[str, Any]]] = []
        for item in (value for value in operations if value["path"] == path):
            splice_start, splice_end = _splice(item, len(lines))
            if item["operation"] in {"replace", "delete"}:
                old_text = "".join(lines[item["start_line"] - 1:item["end_line"]])
                if base._sha256(old_text.encode("utf-8")) != item["expected_old_text_sha256"]:
                    raise base.MyGithub10Error("PATCH_FILE_CHANGED", "old range text hash does not match")
            splices.append((splice_start, splice_end, item["order"], item))
        _ensure_non_overlapping(splices)
        for splice_start, splice_end, _, item in sorted(
            splices, key=lambda value: (value[0], value[1], value[2]), reverse=True
        ):
            replacement = "" if item["operation"] == "delete" else item["replacement"]
            lines[splice_start:splice_end] = [replacement] if replacement else []
        changed[path] = "".join(lines).encode("utf-8")
        expected[path] = blob_sha

    result = {
        "ok": True,
        "dry_run": dry_run,
        "repository": repository,
        "branch": branch,
        "expected_head_sha": expected_head_sha,
        "resolved_head_sha": actual_head,
        "changed_files": [
            {"path": path, "operation": "modify", "old_blob_sha": expected[path]}
            for path in changed
        ],
        "new_content_sha256": {path: base._sha256(value) for path, value in changed.items()},
        "operation_count": len(operations),
    }
    if dry_run:
        return result

    operation_id, replay = base._idempotent_start(
        "edit_github_file_ranges", idempotency_key,
        {
            "repository": repository,
            "branch": branch,
            "expected_head_sha": expected_head_sha,
            "operations_sha256": base._sha256(operations_json.encode()),
        },
    )
    if replay:
        return {**result, "replayed_commit_sha": replay}
    try:
        result.update(_commit_files(
            client, repository, branch, expected_head_sha, changed, expected, commit_message
        ))
        base._idempotent_finish(operation_id, "succeeded", result["commit_sha"])
        return result
    except base.MyGithub10Error as exc:
        base._idempotent_finish(operation_id, "failed", error_code=exc.code)
        raise


def apply_patch(client, repository: str, branch: str, expected_head_sha: str,
                expected_blob_shas_json: str, patch: str, commit_message: str,
                dry_run: bool, idempotency_key: str = "") -> dict[str, Any]:
    base = _base()
    actual_head, _ = base._resolve_commit(base._repo(client, repository), branch)
    if expected_head_sha and actual_head != expected_head_sha:
        raise base.MyGithub10Error(
            "PATCH_HEAD_CHANGED", "branch HEAD changed",
            {"expected": expected_head_sha, "actual": actual_head},
        )
    return _ORIGINAL_APPLY_PATCH(
        client, repository, branch, expected_head_sha, expected_blob_shas_json,
        patch, commit_message, dry_run, idempotency_key,
    )


_ORIGINAL_APPLY_PATCH = None


def install(module) -> None:
    global _BASE, _ORIGINAL_APPLY_PATCH
    if getattr(module, "_runtime_strict_write_fix_installed", False):
        return
    _BASE = module
    _ORIGINAL_APPLY_PATCH = module.apply_patch
    module._commit_files = _commit_files
    module.edit_ranges = edit_ranges
    module.apply_patch = apply_patch
    module._runtime_strict_write_fix_installed = True
