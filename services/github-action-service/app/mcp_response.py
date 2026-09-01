"""Structured MCP response normalization, budgeting, and resource fallback.

Existing tool handlers historically return JSON text.  ``StructuredFastMCP`` keeps those
Python call sites intact while registering an adapter that exposes real MCP structured
content, applies a conservative inline budget, and persists oversized payloads as
short-lived resources instead of silently truncating them.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable, get_type_hints
from typing_extensions import NotRequired, TypedDict

from mcp.server.fastmcp import FastMCP

MAX_SAFE_INLINE_BYTES = 32 * 1024
# Leave room for the chunk envelope and MCP transport framing.
MAX_RESPONSE_RESOURCE_CHUNK_BYTES = 24 * 1024
RESPONSE_RESOURCE_TTL_SECONDS = 60 * 60
RESOURCE_URI_PREFIX = "mygithub12://response/"


class RuntimeFileParam(TypedDict):
    download_url: str
    file_id: str
    mime_type: NotRequired[str]
    file_name: NotRequired[str]


_IDENTITY_KEYS = {
    "ok", "job_id", "repository", "branch", "base_branch", "commit_sha",
    "base_sha", "head_sha", "head_commit_sha", "tree_sha", "git_tree_sha",
    "profile", "status", "conclusion", "exit_code", "priority", "worker_id",
    "queue_position", "current_step", "created_at", "started_at", "finished_at",
    "duration_seconds", "cancel_requested", "superseded_by_job_id", "workspace_id",
    "revision", "pull_number", "number", "mergeable", "merge_state_status",
    "draft", "state", "index_version", "strategy", "progress_current",
    "development_session_id", "session_id", "session_revision", "workspace_revision", "generation_id", "role",
    "progress_total", "terminal", "changed", "timed_out", "resource_uri",
}
_SMALL_COLLECTION_KEYS = {
    "detected_stacks", "selected_profiles", "workspaces", "steps", "warnings",
    "errors", "diagnostics", "checks", "required_checks", "blocking_reasons",
}


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")


def response_size_bytes(value: Any) -> int:
    return len(json_bytes(value))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _resource_root() -> Path:
    root = Path(os.environ.get("MYGITHUB12_SHARED_RESOURCE_DIR") or os.environ.get("MCP_RESPONSE_RESOURCE_DIR", "/data/mcp-response-resources"))
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass
    return root


def _resource_paths(resource_id: str) -> tuple[Path, Path]:
    try:
        parsed = uuid.UUID(hex=resource_id)
    except (ValueError, AttributeError) as exc:
        raise ValueError("invalid MCP response resource id") from exc
    normalized = parsed.hex
    root = _resource_root().resolve()
    payload = (root / f"{normalized}.json").resolve()
    metadata = (root / f"{normalized}.meta.json").resolve()
    if payload.parent != root or metadata.parent != root:
        raise ValueError("invalid MCP response resource path")
    return payload, metadata


def cleanup_expired_response_resources(now: float | None = None) -> int:
    if os.environ.get("MYGITHUB12_RUNTIME_ROLE"):
        try:
            from app.runtime_generation import is_cleanup_leader
            if not is_cleanup_leader():
                return 0
        except Exception:
            return 0
    root = _resource_root()
    now = time.time() if now is None else now
    removed = 0
    for meta_path in root.glob("*.meta.json"):
        resource_id = meta_path.name.removesuffix(".meta.json")
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            expired = float(meta.get("expires_at", 0)) <= now
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            expired = True
        if not expired:
            continue
        try:
            payload_path, checked_meta_path = _resource_paths(resource_id)
        except ValueError:
            continue
        payload_path.unlink(missing_ok=True)
        checked_meta_path.unlink(missing_ok=True)
        removed += 1
    return removed


def store_response_resource(value: Any) -> dict[str, Any]:
    cleanup_expired_response_resources()
    data = json_bytes(value)
    resource_id = uuid.uuid4().hex
    payload_path, meta_path = _resource_paths(resource_id)
    now = time.time()
    digest = _sha256(data)
    payload_tmp = payload_path.with_suffix(".json.tmp")
    meta_tmp = meta_path.with_suffix(".json.tmp")
    fd = os.open(payload_tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    meta_text = json.dumps(
        {
            "resource_id": resource_id,
            "created_at": now,
            "expires_at": now + RESPONSE_RESOURCE_TTL_SECONDS,
            "total_bytes": len(data),
            "sha256": digest,
            "mime_type": "application/json",
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    fd = os.open(meta_tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(meta_text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(payload_tmp, payload_path)
    os.replace(meta_tmp, meta_path)
    return {
        "resource_uri": f"{RESOURCE_URI_PREFIX}{resource_id}",
        "total_bytes": len(data),
        "sha256": digest,
        "expires_at": now + RESPONSE_RESOURCE_TTL_SECONDS,
    }


def _load_resource(resource_uri: str) -> tuple[bytes, dict[str, Any]]:
    if not resource_uri.startswith(RESOURCE_URI_PREFIX):
        raise ValueError("unsupported MCP response resource URI")
    resource_id = resource_uri.removeprefix(RESOURCE_URI_PREFIX)
    payload_path, meta_path = _resource_paths(resource_id)
    if not payload_path.is_file() or not meta_path.is_file():
        raise FileNotFoundError("MCP response resource not found")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if float(meta.get("expires_at", 0)) <= time.time():
        payload_path.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)
        raise FileNotFoundError("MCP response resource expired")
    data = payload_path.read_bytes()
    if len(data) != int(meta.get("total_bytes", -1)) or _sha256(data) != meta.get("sha256"):
        raise ValueError("MCP response resource integrity check failed")
    return data, meta


def read_response_resource_text(resource_uri: str) -> str:
    data, _ = _load_resource(resource_uri)
    return data.decode("utf-8")


def read_response_resource_chunk(
    resource_uri: str, offset_bytes: int = 0, limit_bytes: int = MAX_RESPONSE_RESOURCE_CHUNK_BYTES,
) -> dict[str, Any]:
    data, meta = _load_resource(resource_uri)
    total = len(data)
    offset = int(offset_bytes)
    limit = min(max(int(limit_bytes), 1), MAX_RESPONSE_RESOURCE_CHUNK_BYTES)
    if offset < 0 or offset > total:
        raise ValueError("resource offset is outside the payload")
    end = min(offset + limit, total)
    while end > offset:
        try:
            content = data[offset:end].decode("utf-8")
            break
        except UnicodeDecodeError as exc:
            if exc.start == 0:
                raise ValueError("resource offset is not a UTF-8 boundary") from exc
            end -= 1
    else:
        content = ""
    chunk = data[offset:end]
    eof = end >= total
    return {
        "ok": True,
        "resource_uri": resource_uri,
        "offset_from": offset,
        "offset_to": end,
        "next_offset": None if eof else end,
        "total_bytes": total,
        "has_more": not eof,
        "eof": eof,
        "content_sha256": meta["sha256"],
        "chunk_sha256": _sha256(chunk),
        "content": content,
    }


def _truncate_text(value: str, max_bytes: int = 2048) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    boundary = encoded[:max_bytes]
    while boundary:
        try:
            return boundary.decode("utf-8") + "…"
        except UnicodeDecodeError:
            boundary = boundary[:-1]
    return "…"


def _compact_collection(value: Any) -> Any:
    if isinstance(value, str):
        return _truncate_text(value)
    if isinstance(value, list):
        out = []
        for item in value[:100]:
            if isinstance(item, dict):
                out.append({key: _compact_collection(val) for key, val in item.items() if not isinstance(val, (bytes, bytearray))})
            else:
                out.append(_compact_collection(item))
        return out
    if isinstance(value, dict):
        return {key: _compact_collection(val) for key, val in value.items()}
    return value


def compact_large_payload(value: dict[str, Any]) -> dict[str, Any]:
    """Return bounded decision metadata while the complete payload moves to a resource."""
    summary: dict[str, Any] = {}
    for key, item in value.items():
        if key in {"response_meta", "_mcp_response_mode"}:
            continue
        if key in _IDENTITY_KEYS and not isinstance(item, (dict, list, bytes, bytearray)):
            summary[key] = _truncate_text(item) if isinstance(item, str) else item
            continue
        if key in {"error", "status_summary"} and isinstance(item, dict):
            summary[key] = _compact_collection(item)
            continue
        if key in _SMALL_COLLECTION_KEYS:
            compacted = _compact_collection(item)
            if response_size_bytes(compacted) <= 8192:
                summary[key] = compacted
                continue
        if isinstance(item, list):
            total_key = f"{key}_total"
            truncated_key = f"{key}_truncated"
            if total_key not in value:
                summary[total_key] = len(item)
            if truncated_key not in value:
                summary[truncated_key] = bool(item)
        elif isinstance(item, dict):
            summary[f"{key}_available"] = bool(item)
        elif isinstance(item, str) and len(item.encode("utf-8")) > 2048:
            summary[f"{key}_bytes"] = len(item.encode("utf-8"))
    if "ok" not in summary and "ok" in value:
        summary["ok"] = value["ok"]
    return summary


def _attach_meta(base: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    result["response_meta"] = dict(meta)
    for _ in range(3):
        size = response_size_bytes(result)
        if result["response_meta"].get("inline_bytes") == size:
            break
        result["response_meta"]["inline_bytes"] = size
    return result


def prepare_tool_response(value: dict[str, Any]) -> dict[str, Any]:
    payload = dict(value)
    requested_mode = str(payload.pop("_mcp_response_mode", "full"))
    payload.pop("response_meta", None)
    raw = json_bytes(payload)
    digest = _sha256(raw)
    inline_meta = {
        "mode": requested_mode,
        "inline_bytes": 0,
        "total_bytes": len(raw),
        "truncated": False,
        "resource_uri": None,
        "has_more": False,
        "content_sha256": digest,
    }
    inline = _attach_meta(payload, inline_meta)
    if response_size_bytes(inline) <= MAX_SAFE_INLINE_BYTES:
        return inline

    resource = store_response_resource(payload)
    compact = compact_large_payload(payload)
    resource_meta = {
        "mode": "resource",
        "requested_mode": requested_mode,
        "inline_bytes": 0,
        "total_bytes": resource["total_bytes"],
        "truncated": True,
        "resource_uri": resource["resource_uri"],
        "has_more": True,
        "content_sha256": resource["sha256"],
        "resource_expires_at": resource["expires_at"],
    }
    result = _attach_meta(compact, resource_meta)
    if response_size_bytes(result) > MAX_SAFE_INLINE_BYTES:
        minimal = {key: compact[key] for key in compact if key in _IDENTITY_KEYS or key == "error"}
        result = _attach_meta(minimal, resource_meta)
    return result


def normalize_json_tool_result(result: Any) -> dict[str, Any]:
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except json.JSONDecodeError:
            parsed = {"result": result}
    else:
        parsed = result
    if not isinstance(parsed, dict):
        parsed = {"result": parsed}
    return prepare_tool_response(parsed)


def _structured_wrapper(function: Callable[..., Any]) -> Callable[..., Any]:
    signature = inspect.signature(function)
    try:
        annotations = dict(get_type_hints(function, include_extras=True))
    except Exception:
        annotations = dict(getattr(function, "__annotations__", {}))
    annotations["return"] = dict[str, Any]

    if inspect.iscoroutinefunction(function):
        async def wrapped(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return normalize_json_tool_result(await function(*args, **kwargs))
    else:
        def wrapped(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return normalize_json_tool_result(function(*args, **kwargs))

    wrapped.__name__ = function.__name__
    wrapped.__qualname__ = function.__qualname__
    wrapped.__module__ = function.__module__
    wrapped.__doc__ = function.__doc__
    wrapped.__annotations__ = annotations
    wrapped.__signature__ = signature.replace(return_annotation=dict[str, Any])
    return wrapped


class StructuredFastMCP(FastMCP):
    """FastMCP adapter that keeps compatibility handlers while exposing a canonical schema."""

    _DEPRECATED_TOOL_NAMES = frozenset({
        "get_github_file",
        "commit_github_files",
        "get_test_deployment_logs",
        "begin_github_file_upload",
        "append_github_file_upload_chunk",
        "finalize_github_file_upload",
        "commit_github_uploaded_files",
        "put_github_file",
        "put_github_files",
        "put_github_file_from_local_candidate",
    })

    @staticmethod
    def deprecated_tools_exposed() -> bool:
        """Expose deprecated compatibility tools only when explicitly requested outside production."""
        configured = os.environ.get("MYGITHUB12_EXPOSE_DEPRECATED_TOOLS", "").strip().lower()
        if configured:
            return configured in {"1", "true", "yes", "on"}
        runtime_mode = os.environ.get(
            "MYGITHUB12_RUNTIME_MODE",
            os.environ.get("MYGITHUB10_RUNTIME_MODE", "development"),
        ).strip().lower()
        return runtime_mode != "production"

    async def list_tools(self):
        tools = await super().list_tools()
        if self.deprecated_tools_exposed():
            return tools
        return [tool for tool in tools if tool.name not in self._DEPRECATED_TOOL_NAMES]

    async def tool_schema_identity(self, tools: list[Any] | None = None) -> dict[str, Any]:
        """Return a stable fingerprint for the exact tool schema visible to this connector."""
        def jsonable(value: Any) -> Any:
            if value is None:
                return None
            if hasattr(value, "model_dump"):
                return value.model_dump(mode="json", exclude_none=True)
            return value

        registered = await super().list_tools()
        visible = tools if tools is not None else await self.list_tools()
        canonical = []
        for tool in sorted(visible, key=lambda item: item.name):
            canonical.append({
                "name": tool.name,
                "description": tool.description or "",
                "input_schema": tool.inputSchema,
                "output_schema": getattr(tool, "outputSchema", None),
                "annotations": jsonable(getattr(tool, "annotations", None)),
            })
        encoded = json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        visible_names = {tool.name for tool in visible}
        hidden_deprecated = sorted(
            tool.name
            for tool in registered
            if tool.name in self._DEPRECATED_TOOL_NAMES and tool.name not in visible_names
        )
        return {
            "tool_count": len(visible),
            "tool_manifest_count": len(visible),
            "tool_schema_sha256": digest,
            "schema_generation_id": f"schema-v1:{digest[:16]}",
            "deprecated_tools_exposed": self.deprecated_tools_exposed(),
            "hidden_deprecated_tool_count": len(hidden_deprecated),
            "hidden_deprecated_tools": hidden_deprecated,
            "compatibility_tool_count": len(registered),
        }

    def tool(self, *args: Any, **kwargs: Any):
        register = super().tool(*args, **kwargs)

        def decorator(function: Callable[..., Any]):
            return_annotation = inspect.signature(function).return_annotation
            if return_annotation is str or return_annotation == "str":
                register(_structured_wrapper(function))
                # Preserve direct-import compatibility for existing tests and internal callers.
                return function
            return register(function)

        return decorator
