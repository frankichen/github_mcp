"""Stable, redacted and bounded evidence for failed Web CI jobs.

The durable payload is stored by :mod:`development_failure_pack_store`.  A
response resource is materialized from that payload for transport and paging,
but its URI is never used as the pack identity and is safe to lose after its
TTL expires.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

from app import development_failure_pack_store as failure_pack_store
from app.ci_database import encode_step_log_cursor, get_log_tail, get_steps
from app.mcp_response import MAX_RESPONSE_RESOURCE_CHUNK_BYTES, store_response_resource


FAILURE_PACK_SCHEMA_VERSION = 1
FAILURE_CLASSIFICATIONS = (
    "code",
    "test",
    "dependency",
    "runner",
    "network",
    "permission",
    "infrastructure",
    "timeout",
    "cancelled",
    "unknown",
)

MAX_FAILED_STEPS = 20
MAX_FAILED_TESTS = 100
MAX_PRIMARY_ERRORS = 20
MAX_CHANGED_FILES = 100
MAX_LOG_EXCERPT_BYTES = 32 * 1024
MAX_INLINE_LOG_PREVIEW_BYTES = 8 * 1024
MAX_COMMAND_BYTES = 8 * 1024
MAX_ERROR_BYTES = 8 * 1024
MAX_AFFECTED_BYTES = 64 * 1024

_FAILED_STEP_STATUSES = frozenset(
    {
        "failed",
        "timed_out",
        "timeout",
        "configuration_error",
        "blocked_by_setup",
        "cancelled",
    }
)
_TERMINAL_FAILURE_STATUSES = frozenset(
    {"failed", "timed_out", "cancelled", "superseded", "worker_lost", "internal_error"}
)
_ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_PEM_RE = re.compile(
    r"-----BEGIN [^-]+-----.*?-----END [^-]+-----", re.IGNORECASE | re.DOTALL
)
_AUTH_RE = re.compile(
    r"(?i)(\b(?:proxy-)?authorization\s*[:=]\s*(?:bearer|basic)\s+)([^\s,;\"']+)"
)
_HEADER_RE = re.compile(
    r"(?i)(--(?:header|http-header)\s+)(?:\"[^\"]*\"|'[^']*'|[^\s]+)"
)
_HEADER_ASSIGNMENT_RE = re.compile(r"(?im)(\bheaders?\s*[:=]\s*).+$")
_SENSITIVE_OPTION_RE = re.compile(
    r"(?i)(--?(?:token|password|passwd|secret|api[-_]?key|access[-_]?key|credential|client[-_]?secret|private[-_]?key|auth)(?:=|\s+))([^\s,;\"']+)"
)
_SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)(\b[\w.-]*(?:token|password|passwd|secret|(?:api|access|client)[_-]?key|credential|private[_-]?key|authorization|headers?)[\w.-]*\s*[:=]\s*)([^\s,;\"']+)"
)
_QUOTED_SENSITIVE_RE = re.compile(
    r"(?i)([\"'](?:token|password|passwd|secret|(?:api|access|client)[_-]?key|credential|private[_-]?key|authorization|headers?)[\"']\s*[:=]\s*)([\"'])(.*?)([\"'])"
)
_URL_CREDENTIAL_RE = re.compile(r"(?i)(://[^\s/@:]+:)([^\s/@]+)(@)")
_KNOWN_TOKEN_RE = re.compile(
    r"(?i)(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|xox[baprs]-[A-Za-z0-9-]+|sk-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16})"
)
_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(?:token|secret|password|passwd|api[_-]?key|access[_-]?key|credential|authorization|private[_-]?key|headers?)"
)

_LOCATION_RE = re.compile(
    r"(?P<file>(?:[A-Za-z]:[\\/]|/|\./|\.\./)?[^()\s:]+):"
    r"(?P<line>\d+)(?::(?P<column>\d+))?"
)
_PYTEST_FAILED_RE = re.compile(r"^\s*FAILED\s+(?P<target>\S+?)(?:\s+-\s+(?P<message>.*))?\s*$")
_GO_FAILED_RE = re.compile(r"^\s*---\s+FAIL:\s+(?P<name>.+?)(?:\s+\([^)]*\))?\s*$")
_NODE_BULLET_RE = re.compile(r"^\s*[●✕×]\s+(?P<name>.+?)\s*$")
_NODE_NOT_OK_RE = re.compile(r"^\s*not\s+ok(?:\s+\d+)?\s*(?:-\s*)?(?P<name>.+?)\s*$", re.I)
_MOCHA_FAILED_RE = re.compile(r"^\s*\d+\)\s+(?P<name>.+?)\s*$")
_UNITTEST_FAILED_RE = re.compile(
    r"^\s*(?:FAIL|ERROR):\s+(?P<name>[^\s].*?)\s*$", re.I
)
_ERROR_LINE_RE = re.compile(
    r"(?i)(?:\b(?:error|exception|failure|failed|fatal|panic|traceback)\b|\bnpm\s+err!|^\s*e\s{1,3}|^\s*not\s+ok|^\s*---\s+fail:)"
)


def _truncate_utf8(value: str, max_bytes: int, *, from_end: bool = False) -> tuple[str, bool]:
    text = str(value)
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    marker = "…"
    budget = max(0, int(max_bytes) - len(marker.encode("utf-8")))
    if from_end:
        text = encoded[-budget:].decode("utf-8", errors="ignore") if budget else ""
        return marker + text, True
    text = encoded[:budget].decode("utf-8", errors="ignore") if budget else ""
    return text + marker, True


def redact_text(value: Any) -> str:
    """Redact credentials from commands, logs, errors and other text evidence."""
    text = _ANSI_RE.sub("", str(value))
    text = _PEM_RE.sub("[REDACTED]", text)
    text = _AUTH_RE.sub(lambda match: match.group(1) + "[REDACTED]", text)
    text = _HEADER_RE.sub(lambda match: match.group(1) + "[REDACTED]", text)
    text = _HEADER_ASSIGNMENT_RE.sub(lambda match: match.group(1) + "[REDACTED]", text)
    text = _QUOTED_SENSITIVE_RE.sub(
        lambda match: match.group(1) + match.group(2) + "[REDACTED]" + match.group(4), text
    )
    text = _SENSITIVE_OPTION_RE.sub(lambda match: match.group(1) + "[REDACTED]", text)
    text = _SENSITIVE_ASSIGNMENT_RE.sub(lambda match: match.group(1) + "[REDACTED]", text)
    text = _URL_CREDENTIAL_RE.sub(lambda match: match.group(1) + "[REDACTED]" + match.group(3), text)
    return _KNOWN_TOKEN_RE.sub("[REDACTED]", text)


def redact_command(command: Any) -> str | None:
    """Return a bounded redacted command, or ``None`` when no command exists."""
    if command is None:
        return None
    if isinstance(command, (list, tuple)):
        command = " ".join(str(item) for item in command)
    text = redact_text(command).strip()
    if not text:
        return None
    return _truncate_utf8(text, MAX_COMMAND_BYTES)[0]


def _safe_text(value: Any, max_bytes: int = 4096) -> str:
    return _truncate_utf8(redact_text(value), max_bytes)[0]


def _safe_value(value: Any, *, depth: int = 0, max_items: int = 100, max_bytes: int = 4096) -> Any:
    if depth > 4:
        return "[DEPTH_LIMIT]"
    if isinstance(value, str):
        return _safe_text(value, max_bytes)
    if isinstance(value, (bytes, bytearray)):
        return "[BINARY_REDACTED]"
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for raw_key, raw_value in list(value.items())[:max_items]:
            key = _safe_text(raw_key, 256)
            if _SENSITIVE_KEY_RE.search(key):
                output[key] = "[REDACTED]"
            else:
                output[key] = _safe_value(
                    raw_value,
                    depth=depth + 1,
                    max_items=max_items,
                    max_bytes=max_bytes,
                )
        if len(value) > max_items:
            output["_truncated"] = True
        return output
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        output = [
            _safe_value(item, depth=depth + 1, max_items=max_items, max_bytes=max_bytes)
            for item in items[:max_items]
        ]
        if len(items) > max_items:
            output.append("[TRUNCATED]")
        return output
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _safe_text(value, max_bytes)


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _location(file: str | None = None, line: Any = None, column: Any = None) -> dict[str, Any]:
    file_value = _safe_text(file, 1024).strip() if file else None
    line_value = _as_int(line)
    column_value = _as_int(column)
    if file_value and "://" in file_value:
        file_value = None
    state = "complete" if file_value and line_value is not None and column_value is not None else (
        "partial" if file_value or line_value is not None or column_value is not None else "unavailable"
    )
    return {"status": state, "file": file_value, "line": line_value, "column": column_value}


def parse_failure_location(text: Any) -> dict[str, Any]:
    """Parse ``file:line[:column]`` and always return an explicit status."""
    raw = redact_text(text)
    for match in _LOCATION_RE.finditer(raw):
        file_value = match.group("file").rstrip(".,;])}>")
        if "://" in file_value:
            continue
        return _location(file_value, match.group("line"), match.group("column"))
    return _location()


def _record_test(
    records: list[dict[str, Any]],
    *,
    name: str,
    framework: str,
    index: int,
    file: str | None = None,
    location: dict[str, Any] | None = None,
) -> None:
    name = _safe_text(name, 2048).strip()
    if not name:
        return
    target_location = location or _location(file)
    records.append(
        {
            "name": name,
            "test_name": name,
            "framework": framework,
            "location": target_location,
            "file": target_location["file"],
            "line": target_location["line"],
            "column": target_location["column"],
            "location_status": target_location["status"],
            "_index": index,
        }
    )


def parse_failed_tests(log_text: Any) -> list[dict[str, Any]]:
    """Parse common pytest, Go, Node/Jest/Mocha and unittest failure markers."""
    text = redact_text(log_text or "")
    lines = text.splitlines()
    records: list[dict[str, Any]] = []
    for index, raw_line in enumerate(lines):
        match = _PYTEST_FAILED_RE.match(raw_line)
        if match:
            target = match.group("target")
            if "::" in target:
                file_value, test_name = target.split("::", 1)
                _record_test(
                    records,
                    name=test_name,
                    framework="pytest",
                    index=index,
                    file=file_value,
                )
            continue
        match = _GO_FAILED_RE.match(raw_line)
        if match:
            _record_test(records, name=match.group("name"), framework="go", index=index)
            continue
        match = _NODE_BULLET_RE.match(raw_line)
        if match:
            name = match.group("name")
            _record_test(
                records,
                name=name,
                framework="node",
                index=index,
                location=parse_failure_location(name),
            )
            continue
        match = _NODE_NOT_OK_RE.match(raw_line)
        if match:
            _record_test(records, name=match.group("name"), framework="node", index=index)
            continue
        match = _MOCHA_FAILED_RE.match(raw_line)
        if match:
            _record_test(records, name=match.group("name"), framework="node", index=index)
            continue
        match = _UNITTEST_FAILED_RE.match(raw_line)
        if match:
            _record_test(records, name=match.group("name"), framework="unittest", index=index)

    # A traceback/location line commonly follows a marker (Go/Jest) or
    # precedes pytest's final short-summary line.  Attach the nearest useful
    # location without treating an absent column as a complete location.
    locations = []
    for index, raw_line in enumerate(lines):
        location = parse_failure_location(raw_line)
        if location["status"] != "unavailable":
            locations.append((index, location))
    for record in records:
        if record["location_status"] == "complete":
            continue
        candidates = [
            (abs(index - record["_index"]), location)
            for index, location in locations
            if abs(index - record["_index"]) <= 120
        ]
        if not record["location"]["file"]:
            # For markers such as Go's ``--- FAIL`` and Jest's ``●``, the
            # useful stack location normally follows the marker.  The
            # absolute-distance fallback below still covers logs that place a
            # traceback before a final pytest summary line.
            after = [
                (index - record["_index"], location)
                for index, location in locations
                if record["_index"] <= index <= record["_index"] + 120
            ]
            if after:
                after.sort(key=lambda item: item[0])
                candidates = after
        if record["location"]["file"]:
            expected = str(record["location"]["file"])
            matching = [
                item for item in candidates
                if item[1]["file"] and (
                    str(item[1]["file"]) == expected
                    or str(item[1]["file"]).endswith("/" + expected.lstrip("./"))
                    or expected.endswith("/" + str(item[1]["file"]).lstrip("./"))
                )
            ]
            candidates = matching
        if candidates:
            candidates.sort(key=lambda item: item[0])
            location = candidates[0][1]
            current = record["location"]
            if current["status"] != "complete" and (
                current["file"] is None or location["line"] is not None
            ):
                record["location"] = _location(
                    current["file"] or location["file"],
                    current["line"] or location["line"],
                    current["column"] or location["column"],
                )
                record["file"] = record["location"]["file"]
                record["line"] = record["location"]["line"]
                record["column"] = record["location"]["column"]
                record["location_status"] = record["location"]["status"]

    output: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for record in records:
        record.pop("_index", None)
        identity = (
            record["framework"],
            record["name"],
            record["file"],
            record["line"],
            record["column"],
        )
        if identity in seen:
            continue
        seen.add(identity)
        output.append(record)
        if len(output) >= MAX_FAILED_TESTS:
            break
    return output


def _normalize_step(step: Mapping[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {
        "step_name": _safe_text(step.get("step_name") or step.get("name") or "", 512) or None,
        "status": _safe_text(step.get("status") or "", 128) or None,
        "exit_code": _as_int(step.get("exit_code")),
    }
    step_id = _as_int(step.get("step_id", step.get("id")))
    if step_id is not None:
        output["step_id"] = step_id
    for key in ("duration_seconds", "log_start_offset", "log_end_offset"):
        if key in step:
            value = _as_int(step.get(key)) if key.endswith("offset") else step.get(key)
            if value is not None:
                output[key] = value
    command = None
    for key in ("command", "command_line", "cmd", "run", "script"):
        if step.get(key) is not None:
            command = step.get(key)
            break
    redacted = redact_command(command)
    if redacted:
        output["redacted_command"] = redacted
    return output


def _merge_steps(job: Mapping[str, Any], persisted_steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary = job.get("summary") if isinstance(job.get("summary"), Mapping) else {}
    candidates = summary.get("steps") if isinstance(summary.get("steps"), list) else None
    if not candidates:
        candidates = job.get("steps") if isinstance(job.get("steps"), list) else []
    logical = [item for item in candidates if isinstance(item, Mapping)]
    persisted_by_name: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for index, item in enumerate(persisted_steps):
        if isinstance(item, Mapping):
            persisted_by_name.setdefault(str(item.get("step_name") or ""), []).append((index, dict(item)))
    merged: list[dict[str, Any]] = []
    consumed: set[int] = set()
    for item in logical:
        normalized = _normalize_step(item)
        options = persisted_by_name.get(str(normalized.get("step_name") or ""), [])
        persisted_item = None
        if normalized.get("step_id") is not None:
            for position, candidate in enumerate(options):
                candidate_id = _as_int(candidate[1].get("step_id", candidate[1].get("id")))
                if candidate_id == normalized["step_id"]:
                    persisted_item = options.pop(position)
                    break
        elif len(options) == 1:
            persisted_item = options.pop(0)
        elif options:
            # A status/exit-code match can disambiguate a summary step when
            # names repeat.  If it cannot, leave the summary step without a
            # persisted identity rather than silently selecting the wrong row.
            status = str(normalized.get("status") or "").lower()
            status_matches = [
                candidate for candidate in options
                if str(candidate[1].get("status") or "").lower() == status
            ] if status else []
            exit_code = _as_int(normalized.get("exit_code"))
            if exit_code is not None:
                exact_exit_matches = [
                    candidate for candidate in status_matches
                    if _as_int(candidate[1].get("exit_code")) == exit_code
                ]
                if len(exact_exit_matches) == 1:
                    status_matches = exact_exit_matches
            if len(status_matches) == 1:
                candidate = status_matches[0]
                options.remove(candidate)
                persisted_item = candidate
        if persisted_item is not None:
            persisted_index, persisted = persisted_item
            consumed.add(persisted_index)
            for key in ("step_id", "log_start_offset", "log_end_offset", "duration_seconds"):
                if key in persisted and key not in normalized:
                    normalized[key] = persisted[key]
            if normalized.get("status") is None:
                normalized["status"] = _safe_text(persisted.get("status") or "", 128) or None
            if normalized.get("exit_code") is None:
                normalized["exit_code"] = _as_int(persisted.get("exit_code"))
        merged.append(normalized)
    for index, item in enumerate(persisted_steps):
        if isinstance(item, Mapping) and index not in consumed:
            merged.append(_normalize_step(item))
    return merged[:100]


def _load_persisted_steps(job: Mapping[str, Any]) -> list[dict[str, Any]]:
    job_id = str(job.get("job_id") or "")
    if not job_id:
        return []
    try:
        result = get_steps(job_id)
    except Exception:
        return []
    return result if isinstance(result, list) else []


def _extract_command(job: Mapping[str, Any], steps: list[dict[str, Any]]) -> str | None:
    for key in ("command", "command_line", "cmd", "run", "script"):
        command = redact_command(job.get(key))
        if command:
            return command
    for step in steps:
        command = redact_command(step.get("redacted_command"))
        if command:
            return command
    summary = job.get("summary") if isinstance(job.get("summary"), Mapping) else {}
    for key in ("command", "command_line", "cmd", "run", "script"):
        command = redact_command(summary.get(key))
        if command:
            return command
    return None


def _load_log(job: Mapping[str, Any], supplied: Any) -> dict[str, Any]:
    source = "argument"
    source_meta: Mapping[str, Any] = {}
    raw = ""
    if supplied:
        if isinstance(supplied, Mapping):
            source_meta = supplied
            if isinstance(supplied.get("lines"), list):
                raw = "\n".join(str(item) for item in supplied["lines"])
            else:
                raw = str(supplied.get("content") or supplied.get("log") or "")
        else:
            raw = str(supplied)
    else:
        summary = job.get("summary") if isinstance(job.get("summary"), Mapping) else {}
        for key in ("log_tail", "log", "logs"):
            if summary.get(key):
                raw = str(summary[key])
                source = "job_summary"
                break
        if not raw and job.get("job_id"):
            try:
                result = get_log_tail(
                    str(job["job_id"]),
                    lines=200,
                    max_scan_bytes=4 * 1024 * 1024,
                )
                if isinstance(result, Mapping):
                    source_meta = result
                    raw = "\n".join(str(item) for item in result.get("lines", []))
                    source = "ci_job_log_tail"
            except Exception:
                source = "unavailable"

    redacted = redact_text(raw)
    bounded, excerpt_truncated = _truncate_utf8(
        redacted,
        MAX_LOG_EXCERPT_BYTES,
        from_end=True,
    )
    raw_bytes = len(redacted.encode("utf-8"))
    total_bytes = _as_int(source_meta.get("total_bytes")) or raw_bytes
    source_truncated = bool(
        source_meta.get("truncated")
        or source_meta.get("max_bytes_reached")
        or source_meta.get("first_line_partial")
    )
    excerpt_bytes = len(bounded.encode("utf-8"))
    if not bounded:
        state = "unavailable"
    elif excerpt_truncated or source_truncated or total_bytes > excerpt_bytes:
        state = "partial"
    else:
        state = "available"
    job_id = str(job.get("job_id") or "") or None
    return {
        "status": state,
        "content": bounded,
        "excerpt_bytes": excerpt_bytes,
        "total_bytes": max(total_bytes, excerpt_bytes),
        "truncated": bool(excerpt_truncated or source_truncated or total_bytes > excerpt_bytes),
        "source": source if bounded else "unavailable",
        "job_id": job_id,
        "continuation": {
            "status": "available" if job_id and total_bytes > excerpt_bytes else (
                "partial" if bounded else "unavailable"
            ),
            "method": "get_private_ci_logs" if job_id else None,
            "tail_method": "get_private_ci_log_tail" if job_id else None,
            "job_id": job_id,
            "total_bytes": max(total_bytes, excerpt_bytes),
            "truncated": bool(excerpt_truncated or source_truncated),
        },
    }


def _failure_pack_log_continuation(
    base: Mapping[str, Any],
    job_id: str | None,
    failed_step: Mapping[str, Any],
) -> dict[str, Any]:
    """Add a durable precise-step continuation descriptor to the pack."""
    continuation = dict(base)
    step_id = _as_int(failed_step.get("step_id"))
    range_start = _as_int(failed_step.get("log_start_offset"))
    range_end = _as_int(failed_step.get("log_end_offset"))
    valid_range = (
        bool(job_id)
        and step_id is not None
        and range_start is not None
        and range_end is not None
        and range_start >= 0
        and range_end >= range_start
    )
    continuation.update({
        "method": "get_private_ci_logs" if job_id else None,
        "job_id": job_id,
        "failed_step": {
            "step_id": step_id,
            "step_name": _safe_text(failed_step.get("step_name") or "", 512) or None,
            "status": _safe_text(failed_step.get("status") or "", 128) or None,
            "exit_code": _as_int(failed_step.get("exit_code")),
        },
        "step_selector": {"step_id": step_id} if step_id is not None else None,
        "step_log_range": (
            {
                "start_offset": range_start,
                "end_offset": range_end,
                "end_exclusive": True,
                "status": "available",
            }
            if valid_range
            else {
                "start_offset": range_start,
                "end_offset": range_end,
                "end_exclusive": True,
                "status": "partial" if step_id is not None else "unavailable",
            }
        ),
        # No step page has been consumed by Failure Pack construction.  The
        # first page cursor is therefore the deterministic next position.
        "cursor": None,
        "next_cursor": (
            encode_step_log_cursor(
                str(job_id), step_id, range_start, range_end, range_start
            )
            if valid_range and range_end > range_start
            else None
        ),
        "has_more": bool(valid_range and range_end > range_start),
        "resource_independent": True,
        "rerun_ci": False,
    })
    return continuation


def _extract_changed_files(job: Mapping[str, Any], affected: Mapping[str, Any]) -> tuple[list[Any], dict[str, Any]]:
    raw = job.get("changed_files")
    evidence = "job"
    if not isinstance(raw, list):
        raw = affected.get("changed_files") or affected.get("changed_paths") or []
        evidence = "affected_selection" if raw else "unavailable"
    items = [_safe_value(item, max_items=20, max_bytes=2048) for item in raw[:MAX_CHANGED_FILES]]
    total = _as_int(job.get("changed_files_total"))
    if total is None:
        total = _as_int(affected.get("changed_files_total"))
    total = max(int(total if total is not None else len(raw)), len(items))
    truncated = bool(job.get("changed_files_truncated") or affected.get("changed_files_truncated") or total > len(items))
    return items, {
        "status": "available" if items else "unavailable",
        "evidence": evidence,
        "total": total,
        "returned": len(items),
        "truncated": truncated,
    }


def _extract_affected_tests(affected: Mapping[str, Any]) -> tuple[list[Any], dict[str, Any]]:
    raw: Any = None
    source = "unavailable"
    for key in ("selected_tests", "affected_tests", "tests", "test_names"):
        if isinstance(affected.get(key), list):
            raw = affected[key]
            source = key
            break
    if not isinstance(raw, list):
        raw = []
    items = [_safe_value(item, max_items=20, max_bytes=2048) for item in raw[:MAX_FAILED_TESTS]]
    return items, {
        "status": "available" if items else "unavailable",
        "evidence": source,
        "total": len(raw),
        "returned": len(items),
        "truncated": len(raw) > len(items),
    }


def _job_identity(job: Mapping[str, Any]) -> dict[str, Any]:
    identity: dict[str, Any] = {}
    for key in (
        "job_id",
        "repository",
        "branch",
        "commit_sha",
        "base_sha",
        "profile",
        "profile_version",
        "attempt",
        "attempts",
        "run_id",
        "workflow_id",
    ):
        if key in job and job.get(key) is not None:
            identity[key] = _safe_text(job[key], 1024)
    identity.setdefault("job_id", None)
    return identity


def _classification_signal(code: str, reason: str) -> dict[str, Any]:
    return {"code": code, "reason": _safe_text(reason, 1024), "allowed_codes": list(FAILURE_CLASSIFICATIONS)}


def _classify_failure_details(job: Mapping[str, Any], log_text: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
    status = str(job.get("status") or "").lower()
    error_code = str(job.get("error_code") or "").lower()
    error_message = str(job.get("error_message") or "")
    current_step = str(job.get("current_step") or "")
    combined = "\n".join([status, error_code, error_message, current_step, log_text, json.dumps(steps, default=str)])
    lower = combined.lower()
    signal_text = lower + "\n" + re.sub(r"[_-]+", " ", error_code)
    if status in {"cancelled", "canceled"} or "cancel_requested" in lower or re.search(r"\bcancel(?:led|ed)?\b", lower):
        return _classification_signal("cancelled", "job or log reports cancellation")
    if status in {"timed_out", "timeout", "timed-out"} or any(
        marker in lower for marker in ("timed out", "timeout", "deadline exceeded", "time limit exceeded")
    ):
        return _classification_signal("timeout", "job or log reports a timeout")
    if re.search(r"permission denied|access denied|eacces|eperm|forbidden|\b403\b|not authorized|unauthori[sz]ed", signal_text):
        return _classification_signal("permission", "permission or authorization failure signal")
    if re.search(
        r"connection (?:reset|refused|aborted)|network (?:unreachable|error)|temporary failure in name resolution|could not resolve host|dns|name or service not known|tls handshake|connection timed out|socket", signal_text
    ):
        return _classification_signal("network", "network or name-resolution failure signal")
    if re.search(
        r"no matching distribution|could not find a version|module not found|cannot find module|npm err!.*(?:eresolve|enoent|eintegrity)|peer dep|dependency|dependencies|go (?:mod|sum) (?:download|tidy)|checksum mismatch|package .* not found|lockfile", signal_text
    ):
        return _classification_signal("dependency", "package or module resolution failure signal")
    if re.search(r"worker_lost|runner|no such file or directory.*(?:docker|shell|bash)|docker daemon|runner unavailable|executor", signal_text):
        return _classification_signal("runner", "CI runner or executor failure signal")
    if re.search(r"database is locked|sqlite|controller|service unavailable|internal server error|infrastructure|preflight|configuration_error|blocked_by_setup", signal_text):
        return _classification_signal("infrastructure", "controller, service, or CI infrastructure failure signal")
    if re.search(
        r"assert(?:ionerror)?|test(?:s| suite)? failed|failed tests?|--- fail:|not ok|pytest|jest|mocha|go test|unittest|expect\(|test failure", signal_text
    ):
        return _classification_signal("test", "test runner or assertion failure signal")
    if re.search(r"\bcode (?:error|failure|does not compile)\b|syntaxerror|compile(?:r)? error|build failed|type error|undefined reference|cannot compile|lint error|parse error", signal_text):
        return _classification_signal("code", "source compilation, type, syntax, or lint failure signal")
    if status in _TERMINAL_FAILURE_STATUSES or _as_int(job.get("exit_code")) not in (None, 0):
        return _classification_signal("unknown", "no supported failure classification signal was found")
    return _classification_signal("unknown", "failure classification evidence is unavailable")


def classify_failure(job: Mapping[str, Any], log_text: str = "") -> str:
    """Return one of the stable failure classification codes."""
    return _classify_failure_details(job, log_text, []).get("code", "unknown")


def _location_fields(location: dict[str, Any]) -> dict[str, Any]:
    return {
        "location": location,
        "file": location["file"],
        "line": location["line"],
        "column": location["column"],
        "location_status": location["status"],
    }


def _primary_errors(job: Mapping[str, Any], log_text: str) -> list[dict[str, Any]]:
    candidates: list[tuple[str, str, str]] = []
    error_code = str(job.get("error_code") or "").strip()
    error_message = str(job.get("error_message") or "").strip()
    if error_code:
        candidates.append(("job", "job_error", f"{error_code}: {error_message}" if error_message else error_code))
    elif error_message:
        candidates.append(("job", "job_error", error_message))
    for raw_line in str(log_text or "").splitlines():
        line = raw_line.strip()
        if line and _ERROR_LINE_RE.search(line):
            kind = "test" if re.search(r"(?i)\b(?:failed|failure|assert|not ok|---\s+fail)\b", line) else "error"
            candidates.append(("log", kind, line))
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source, kind, raw_message in candidates:
        message = _truncate_utf8(redact_text(raw_message), MAX_ERROR_BYTES)[0].strip()
        normalized = re.sub(r"\s+", " ", message).lower()
        if not message or normalized in seen:
            continue
        seen.add(normalized)
        item = {"message": message, "kind": kind, "source": source}
        item.update(_location_fields(parse_failure_location(message)))
        output.append(item)
        if len(output) >= MAX_PRIMARY_ERRORS:
            break
    return output


def fingerprint_error(
    job: Mapping[str, Any],
    *,
    classification: str = "unknown",
    primary_errors: list[dict[str, Any]] | None = None,
    failed_tests: list[dict[str, Any]] | None = None,
    failed_step: Mapping[str, Any] | None = None,
) -> str:
    """Hash normalized redacted root-error evidence, excluding job identity."""
    errors = [
        re.sub(r"\s+", " ", redact_text(item.get("message") or "")).strip().lower()
        for item in (primary_errors or [])
    ]
    tests = [
        {
            "framework": item.get("framework"),
            "name": re.sub(r"\s+", " ", redact_text(item.get("name") or "")).strip().lower(),
            "file": item.get("file"),
            "line": item.get("line"),
            "column": item.get("column"),
        }
        for item in (failed_tests or [])
    ]
    evidence = {
        "classification": classification,
        "error_code": _safe_text(job.get("error_code") or "", 512).lower(),
        "exit_code": _as_int(job.get("exit_code")),
        "failed_step": {
            "step_name": _safe_text((failed_step or {}).get("step_name") or "", 512).lower(),
            "status": _safe_text((failed_step or {}).get("status") or "", 128).lower(),
            "exit_code": _as_int((failed_step or {}).get("exit_code")),
        },
        "errors": errors[:MAX_PRIMARY_ERRORS],
        "tests": tests[:MAX_FAILED_TESTS],
    }
    canonical = json.dumps(evidence, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _evidence_hash(payload: Mapping[str, Any]) -> str:
    evidence = dict(payload)
    evidence.pop("failure_pack_id", None)
    return hashlib.sha256(
        json.dumps(evidence, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _public_pack(
    payload: Mapping[str, Any],
    record: Mapping[str, Any],
    resource: Mapping[str, Any] | None,
    resource_error: str | None = None,
) -> dict[str, Any]:
    resource_uri = resource.get("resource_uri") if resource else None
    resource_bytes = int(resource.get("total_bytes", 0)) if resource else int(record.get("payload_bytes", 0))
    resource_continuation = {
        "status": "available" if resource else "unavailable",
        "method": "read_response_resource_chunk" if resource else None,
        "resource_uri": resource_uri,
        "offset_bytes": 0,
        "next_offset": 0 if resource else None,
        "chunk_limit_bytes": MAX_RESPONSE_RESOURCE_CHUNK_BYTES,
        "total_bytes": resource_bytes,
        "has_more": bool(resource and resource_bytes > MAX_RESPONSE_RESOURCE_CHUNK_BYTES),
        "rematerialize_method": "materialize_failure_pack",
        "rematerialize_without_ci": True,
    }
    log_excerpt = dict(payload.get("log_excerpt") or {})
    preview, preview_truncated = _truncate_utf8(
        str(log_excerpt.get("content") or ""),
        MAX_INLINE_LOG_PREVIEW_BYTES,
        from_end=True,
    )
    if preview_truncated:
        log_excerpt["content"] = preview
        log_excerpt["preview_truncated"] = True
        log_excerpt["preview_bytes"] = len(preview.encode("utf-8"))
    result: dict[str, Any] = {
        "failure_pack_id": payload.get("failure_pack_id"),
        "schema_version": payload.get("schema_version", FAILURE_PACK_SCHEMA_VERSION),
        "job_id": payload.get("job_identity", {}).get("job_id"),
        "repository": payload.get("job_identity", {}).get("repository"),
        "branch": payload.get("job_identity", {}).get("branch"),
        "commit_sha": payload.get("job_identity", {}).get("commit_sha"),
        "base_sha": payload.get("job_identity", {}).get("base_sha"),
        "profile": payload.get("job_identity", {}).get("profile"),
        "status": payload.get("summary", {}).get("status"),
        "exit_code": payload.get("summary", {}).get("exit_code"),
        "error_code": payload.get("summary", {}).get("error_code"),
        "error_message": payload.get("error_message"),
        "job_identity": payload.get("job_identity", {}),
        "summary": payload.get("summary", {}),
        "classification": payload.get("classification", "unknown"),
        "failure_classification": payload.get("failure_classification", {}),
        "error_fingerprint": payload.get("error_fingerprint"),
        "primary_errors": payload.get("primary_errors", []),
        "primary_errors_evidence": payload.get("primary_errors_evidence", {}),
        "failed_step": payload.get("failed_step"),
        "failed_step_evidence": payload.get("failed_step_evidence", {}),
        "failed_steps": payload.get("failed_steps", []),
        "failed_tests": payload.get("failed_tests", []),
        "failed_test_names": payload.get("failed_test_names", []),
        "failed_tests_evidence": payload.get("failed_tests_evidence", {}),
        "changed_files": payload.get("changed_files", []),
        "changed_files_evidence": payload.get("changed_files_evidence", {}),
        "affected": payload.get("affected", {}),
        "affected_tests": payload.get("affected_tests", []),
        "affected_tests_evidence": payload.get("affected_tests_evidence", {}),
        "redacted_command": payload.get("redacted_command"),
        "command_evidence": payload.get("command_evidence", {}),
        "log_excerpt": log_excerpt,
        "log_tail": log_excerpt.get("content", ""),
        "log_continuation": payload.get("log_continuation", {}),
        "resource_continuation": resource_continuation,
        "rematerialize": {
            "method": "materialize_failure_pack",
            "failure_pack_id": payload.get("failure_pack_id"),
            "rerun_ci": False,
        },
        "content_sha256": record.get("payload_sha256"),
        "total_bytes": int(record.get("payload_bytes", 0)),
        "resource_uri": resource_uri,
        "resource_sha256": resource.get("sha256") if resource else None,
        "resource_expires_at": resource.get("expires_at") if resource else None,
    }
    if resource_error:
        result["resource_error"] = resource_error
    return result


def build_failure_pack(
    job: dict[str, Any],
    *,
    affected: dict[str, Any] | None = None,
    log_tail: str | Mapping[str, Any] = "",
) -> dict[str, Any]:
    """Build or retrieve one durable failure pack for exact CI evidence."""
    if not isinstance(job, Mapping):
        raise TypeError("job must be a mapping")
    affected_map = affected if isinstance(affected, Mapping) else {}
    persisted_steps = _load_persisted_steps(job)
    steps = _merge_steps(job, persisted_steps)
    failed_steps = [
        step for step in steps if str(step.get("status") or "").lower() in _FAILED_STEP_STATUSES
    ][:MAX_FAILED_STEPS]
    status = _safe_text(job.get("status") or "", 128) or None
    job_exit_code = _as_int(job.get("exit_code"))
    current_step = _safe_text(job.get("current_step") or "", 512) or None
    failed_step_evidence: dict[str, Any]
    if failed_steps:
        failed_step = dict(failed_steps[0])
        failed_step_evidence = {
            "status": "available" if _as_int(failed_step.get("step_id")) is not None else "partial",
            "source": "ci_job_steps" if _as_int(failed_step.get("step_id")) is not None else "job_or_summary_steps",
        }
    elif current_step:
        failed_step = {
            "step_name": current_step,
            "status": status,
            "exit_code": job_exit_code,
        }
        failed_step_evidence = {"status": "partial", "source": "job_current_step"}
    else:
        failed_step = {
            "step_name": None,
            "status": None,
            "exit_code": job_exit_code,
        }
        failed_step_evidence = {"status": "unavailable", "source": "no_failed_step_identity"}
    log_excerpt = _load_log(job, log_tail)
    log_continuation = _failure_pack_log_continuation(
        log_excerpt.get("continuation", {}),
        str(job.get("job_id") or "") or None,
        failed_step,
    )
    failed_tests = parse_failed_tests(log_excerpt.get("content") or "")
    classification = _classify_failure_details(job, log_excerpt.get("content") or "", steps)
    primary_errors = _primary_errors(job, log_excerpt.get("content") or "")
    error_fingerprint = fingerprint_error(
        job,
        classification=classification["code"],
        primary_errors=primary_errors,
        failed_tests=failed_tests,
        failed_step=failed_step,
    )
    changed_files, changed_files_evidence = _extract_changed_files(job, affected_map)
    affected_tests, affected_tests_evidence = _extract_affected_tests(affected_map)
    command = _extract_command(job, steps)
    summary = job.get("summary") if isinstance(job.get("summary"), Mapping) else {}
    environment_cache = _safe_value(summary.get("environment_cache", {}), max_items=30, max_bytes=2048)
    environment_encoded = json.dumps(
        environment_cache, ensure_ascii=False, separators=(",", ":"), default=str
    ).encode("utf-8")
    if len(environment_encoded) > MAX_AFFECTED_BYTES:
        environment_cache = {
            "status": "partial",
            "truncated": True,
            "total_bytes": len(environment_encoded),
            "sha256": hashlib.sha256(environment_encoded).hexdigest(),
        }
    job_identity = _job_identity(job)
    failed_tests_evidence = {
        "status": "available" if failed_tests else "unavailable",
        "source": "log_parser" if failed_tests else "no_parseable_failed_test_name",
        "parser_families": ["pytest", "go", "node", "unittest"],
        "returned": len(failed_tests),
    }
    primary_errors_evidence = {
        "status": "available" if primary_errors else "unavailable",
        "source": "job_error_or_log" if primary_errors else "no_primary_error_text",
        "returned": len(primary_errors),
    }
    affected_payload = _safe_value(affected_map, max_items=100, max_bytes=4096)
    affected_encoded = json.dumps(affected_payload, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    if len(affected_encoded) > MAX_AFFECTED_BYTES:
        affected_payload = {
            "status": "partial",
            "truncated": True,
            "total_bytes": len(affected_encoded),
            "sha256": hashlib.sha256(affected_encoded).hexdigest(),
        }
    payload: dict[str, Any] = {
        "schema_version": FAILURE_PACK_SCHEMA_VERSION,
        "job_identity": job_identity,
        "summary": {
            "job_id": job_identity.get("job_id"),
            "repository": job_identity.get("repository"),
            "branch": job_identity.get("branch"),
            "commit_sha": job_identity.get("commit_sha"),
            "base_sha": job_identity.get("base_sha"),
            "profile": job_identity.get("profile"),
            "status": status,
            "exit_code": job_exit_code,
            "error_code": _safe_text(job.get("error_code") or "", 512) or None,
            "failed_steps": [
                {
                    "step_name": step.get("step_name"),
                    "status": step.get("status"),
                    "exit_code": step.get("exit_code"),
                }
                for step in failed_steps[:5]
            ],
        },
        "job_id": job_identity.get("job_id"),
        "repository": job_identity.get("repository"),
        "branch": job_identity.get("branch"),
        "commit_sha": job_identity.get("commit_sha"),
        "profile": job_identity.get("profile"),
        "status": status,
        "exit_code": job_exit_code,
        "error_code": _safe_text(job.get("error_code") or "", 512) or None,
        "error_message": _truncate_utf8(
            redact_text(job.get("error_message") or ""), MAX_ERROR_BYTES
        )[0] or None,
        "failure_classification": classification,
        "classification": classification["code"],
        "error_fingerprint": error_fingerprint,
        "primary_errors": primary_errors,
        "primary_errors_evidence": primary_errors_evidence,
        "failed_step": failed_step,
        "failed_step_evidence": failed_step_evidence,
        "failed_steps": failed_steps,
        "failed_tests": failed_tests,
        "failed_test_names": [item["name"] for item in failed_tests],
        "failed_tests_evidence": failed_tests_evidence,
        "changed_files": changed_files,
        "changed_files_evidence": changed_files_evidence,
        "affected": affected_payload,
        "affected_tests": affected_tests,
        "affected_tests_evidence": affected_tests_evidence,
        "redacted_command": command,
        "command_evidence": {
            "status": "available" if command else "unavailable",
            "redacted": True,
            "source": "job_or_failed_step" if command else "no_command_evidence",
        },
        "log_excerpt": log_excerpt,
        "log_tail": log_excerpt.get("content", ""),
        "log_continuation": log_continuation,
        "environment_cache": environment_cache,
    }
    evidence_sha256 = _evidence_hash(payload)
    payload["failure_pack_id"] = evidence_sha256
    record = failure_pack_store.create_or_get_failure_pack(
        payload,
        evidence_sha256=evidence_sha256,
        failure_pack_id=evidence_sha256,
    )
    durable_payload = record["payload"]
    resource = None
    resource_error = None
    try:
        resource = store_response_resource(durable_payload)
    except Exception as exc:
        # The durable record remains useful even if the response-resource
        # directory is temporarily unavailable; the next materialization can
        # retry without rerunning CI.
        resource_error = type(exc).__name__
    return _public_pack(durable_payload, record, resource, resource_error)


def read_failure_pack(failure_pack_id: str) -> dict[str, Any] | None:
    """Read the durable payload without requiring the ephemeral URI."""
    return failure_pack_store.read_failure_pack_payload(failure_pack_id)


def materialize_failure_pack(failure_pack_id: str) -> dict[str, Any]:
    """Create a fresh response resource from durable evidence, never from CI."""
    record = failure_pack_store.get_failure_pack(str(failure_pack_id or ""))
    if not record:
        raise KeyError(f"failure pack not found: {failure_pack_id}")
    resource = None
    resource_error = None
    try:
        resource = store_response_resource(record["payload"])
    except Exception as exc:
        resource_error = type(exc).__name__
    return _public_pack(record["payload"], record, resource, resource_error)


def rematerialize_failure_pack(failure_pack_id: str) -> dict[str, Any]:
    """Explicit alias emphasizing that this operation does not rerun CI."""
    return materialize_failure_pack(failure_pack_id)
