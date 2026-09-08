"""Small dependency-free request and Web-CI metrics.

All Prometheus dimensions in this module are intentionally bounded. Runtime identities
(repository, branch, job/request/workspace/session IDs, commits, resource URIs, hashes,
paths, and arbitrary error text) are never emitted as metric labels.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
import logging
import re
import threading
import time
import uuid
from typing import Any

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware


logger = logging.getLogger(__name__)
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_lock = threading.Lock()
_requests = Counter()
_duration_seconds = Counter()
_mcp_tool_requests = Counter()
_mcp_tool_duration_seconds = Counter()
_mcp_waits = Counter()
_mcp_wait_duration_seconds = Counter()
_private_ci_phase_observations = Counter()
_private_ci_lifecycle_duration_observations = Counter()
_private_ci_lifecycle_duration_seconds = Counter()
_convergence_phase_entries = Counter()
_convergence_phase_duration_observations = Counter()
_convergence_phase_duration_seconds = Counter()
_idempotency_events = Counter()
_failure_pack_builds = Counter()
_failure_pack_build_duration_seconds = Counter()
_failure_pack_payload_bytes = Counter()
_mcp_responses = Counter()
_mcp_response_bytes = Counter()
_mcp_resource_bytes = Counter()
_current_request_id: ContextVar[str] = ContextVar("mygithub_request_id", default="")
_current_mcp_observation: ContextVar[dict[str, bool] | None] = ContextVar(
    "mygithub_mcp_observation", default=None
)

_MCP_RESULTS = frozenset({"ok", "error", "exception"})
_MCP_MODES = frozenset({"ordinary", "explicit_wait"})
_WAIT_KINDS = frozenset({"private_ci_job", "validation"})
_PRIVATE_CI_PHASES = frozenset({"accepted", "preparing", "queued", "running", "terminal"})
_PRIVATE_CI_DURATION_KINDS = frozenset({"queue", "execution", "total"})
_CONVERGENCE_PHASES = frozenset({
    "accepted",
    "index_requested",
    "analysis_pending",
    "ci_requested",
    "ci_running",
    "post_ci_finalize",
    "passed",
    "failed",
    "blocked",
})
_IDEMPOTENCY_OPERATIONS = frozenset({"private_ci", "validation", "convergence"})
_IDEMPOTENCY_OUTCOMES = frozenset({"reuse", "conflict"})
_RESPONSE_MODES = frozenset({"inline", "resource"})


def current_request_id() -> str:
    return _current_request_id.get()


def monotonic() -> float:
    """Clock seam used by tests without changing production timing semantics."""
    return time.monotonic()


def _bounded(value: Any, allowed: frozenset[str]) -> str:
    normalized = str(value or "")
    return normalized if normalized in allowed else "other"


def _tool_name(value: Any) -> str:
    normalized = str(value or "")
    return normalized if _TOOL_NAME_RE.fullmatch(normalized) else "other"


def _timestamp_seconds(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def begin_mcp_tool(tool: str) -> tuple[float, dict[str, bool], object]:
    state = {"waited": False}
    token = _current_mcp_observation.set(state)
    return monotonic(), state, token


def classify_mcp_result(value: Any) -> str:
    if isinstance(value, Mapping) and (value.get("ok") is False or value.get("error")):
        return "error"
    return "ok"


def finish_mcp_tool(
    tool: str,
    started: float,
    state: Mapping[str, bool],
    token: object,
    result: str,
) -> None:
    elapsed = max(0.0, monotonic() - float(started))
    mode = "explicit_wait" if state.get("waited") else "ordinary"
    key = (_tool_name(tool), _bounded(result, _MCP_RESULTS), _bounded(mode, _MCP_MODES))
    with _lock:
        _mcp_tool_requests[key] += 1
        _mcp_tool_duration_seconds[key] += elapsed
    _current_mcp_observation.reset(token)


@contextmanager
def explicit_wait(kind: str) -> Iterator[None]:
    """Measure an actual compatibility wait, not a nominal wait_seconds argument."""
    normalized = _bounded(kind, _WAIT_KINDS)
    state = _current_mcp_observation.get()
    if state is not None:
        state["waited"] = True
    started = monotonic()
    try:
        yield
    finally:
        elapsed = max(0.0, monotonic() - started)
        with _lock:
            _mcp_waits[normalized] += 1
            _mcp_wait_duration_seconds[normalized] += elapsed


def observe_private_ci_phase(phase: str | None) -> None:
    """Observe a bounded phase from a durable Private CI snapshot."""
    with _lock:
        _private_ci_phase_observations[_bounded(phase, _PRIVATE_CI_PHASES)] += 1


def observe_private_ci_lifecycle(job: Mapping[str, Any]) -> None:
    """Observe one terminal lifecycle from durable timestamps at its commit boundary."""
    created = _timestamp_seconds(job.get("created_at"))
    queued = _timestamp_seconds(job.get("queued_at"))
    started = _timestamp_seconds(job.get("started_at"))
    finished = _timestamp_seconds(job.get("finished_at"))
    durable_execution = job.get("duration_seconds")
    durations: dict[str, float] = {}
    if queued is not None and started is not None and started >= queued:
        durations["queue"] = started - queued
    if isinstance(durable_execution, (int, float)) and not isinstance(durable_execution, bool):
        if float(durable_execution) >= 0:
            durations["execution"] = float(durable_execution)
    elif started is not None and finished is not None and finished >= started:
        durations["execution"] = finished - started
    if created is not None and finished is not None and finished >= created:
        durations["total"] = finished - created

    with _lock:
        for kind, duration in durations.items():
            normalized_kind = _bounded(kind, _PRIVATE_CI_DURATION_KINDS)
            _private_ci_lifecycle_duration_observations[normalized_kind] += 1
            _private_ci_lifecycle_duration_seconds[normalized_kind] += duration


def observe_convergence_phase_entry(phase: str) -> None:
    with _lock:
        _convergence_phase_entries[_bounded(phase, _CONVERGENCE_PHASES)] += 1


def observe_convergence_transition(
    from_phase: str,
    to_phase: str,
    phase_started_at: Any,
    transitioned_at: Any,
) -> None:
    observe_convergence_phase_entry(to_phase)
    if from_phase == to_phase:
        return
    started = _timestamp_seconds(phase_started_at)
    ended = _timestamp_seconds(transitioned_at)
    if started is None or ended is None or ended < started:
        return
    normalized = _bounded(from_phase, _CONVERGENCE_PHASES)
    with _lock:
        _convergence_phase_duration_observations[normalized] += 1
        _convergence_phase_duration_seconds[normalized] += ended - started


def observe_idempotency(operation: str, outcome: str) -> None:
    key = (
        _bounded(operation, _IDEMPOTENCY_OPERATIONS),
        _bounded(outcome, _IDEMPOTENCY_OUTCOMES),
    )
    with _lock:
        _idempotency_events[key] += 1


def observe_failure_pack_build(duration_seconds: float, payload_bytes: int) -> None:
    with _lock:
        _failure_pack_builds["build"] += 1
        _failure_pack_build_duration_seconds["build"] += max(0.0, float(duration_seconds))
        _failure_pack_payload_bytes["build"] += max(0, int(payload_bytes))


def observe_mcp_response(mode: str, response_bytes: int, resource_bytes: int = 0) -> None:
    normalized = _bounded(mode, _RESPONSE_MODES)
    with _lock:
        _mcp_responses[normalized] += 1
        _mcp_response_bytes[normalized] += max(0, int(response_bytes))
        if normalized == "resource":
            _mcp_resource_bytes["resource"] += max(0, int(resource_bytes))


def _escape_label(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _append_counter(
    lines: list[str],
    name: str,
    help_text: str,
    items: list[tuple[Any, Any]],
    labels: tuple[str, ...] = (),
    *,
    float_values: bool = False,
) -> None:
    lines.extend([f"# HELP {name} {help_text}", f"# TYPE {name} counter"])
    for raw_key, value in sorted(items, key=lambda item: str(item[0])):
        key = raw_key if isinstance(raw_key, tuple) else (raw_key,)
        label_text = ""
        if labels:
            label_text = "{" + ",".join(
                f'{label}="{_escape_label(component)}"'
                for label, component in zip(labels, key, strict=True)
            ) + "}"
        formatted = f"{float(value):.6f}" if float_values else str(int(value))
        lines.append(f"{name}{label_text} {formatted}")


class RequestObservabilityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        supplied = request.headers.get("X-Request-ID", "")
        request_id = supplied if _REQUEST_ID_RE.fullmatch(supplied) else uuid.uuid4().hex
        started = monotonic()
        status_code = 500
        request_token = _current_request_id.set(request_id)
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            elapsed = monotonic() - started
            route_object = request.scope.get("route")
            route = getattr(route_object, "path", request.url.path)
            key = (request.method, route, str(status_code))
            with _lock:
                _requests[key] += 1
                _duration_seconds[(request.method, route)] += elapsed
            logger.info(
                "request_completed request_id=%s method=%s path=%s status=%d duration_ms=%.1f",
                request_id,
                request.method,
                route,
                status_code,
                elapsed * 1000,
            )
            _current_request_id.reset(request_token)


def _reset_metrics_for_tests() -> None:
    """Test-only reset seam for process-local counters."""
    with _lock:
        for counter in (
            _requests,
            _duration_seconds,
            _mcp_tool_requests,
            _mcp_tool_duration_seconds,
            _mcp_waits,
            _mcp_wait_duration_seconds,
            _private_ci_phase_observations,
            _private_ci_lifecycle_duration_observations,
            _private_ci_lifecycle_duration_seconds,
            _convergence_phase_entries,
            _convergence_phase_duration_observations,
            _convergence_phase_duration_seconds,
            _idempotency_events,
            _failure_pack_builds,
            _failure_pack_build_duration_seconds,
            _failure_pack_payload_bytes,
            _mcp_responses,
            _mcp_response_bytes,
            _mcp_resource_bytes,
        ):
            counter.clear()
    _current_mcp_observation.set(None)


def prometheus_metrics() -> str:
    lines: list[str] = []
    with _lock:
        snapshots = {
            "http_requests": list(_requests.items()),
            "http_duration": list(_duration_seconds.items()),
            "mcp_requests": list(_mcp_tool_requests.items()),
            "mcp_duration": list(_mcp_tool_duration_seconds.items()),
            "waits": list(_mcp_waits.items()),
            "wait_duration": list(_mcp_wait_duration_seconds.items()),
            "ci_phases": list(_private_ci_phase_observations.items()),
            "ci_duration_observations": list(_private_ci_lifecycle_duration_observations.items()),
            "ci_duration": list(_private_ci_lifecycle_duration_seconds.items()),
            "convergence_entries": list(_convergence_phase_entries.items()),
            "convergence_duration_observations": list(_convergence_phase_duration_observations.items()),
            "convergence_duration": list(_convergence_phase_duration_seconds.items()),
            "idempotency": list(_idempotency_events.items()),
            "failure_builds": list(_failure_pack_builds.items()),
            "failure_duration": list(_failure_pack_build_duration_seconds.items()),
            "failure_bytes": list(_failure_pack_payload_bytes.items()),
            "responses": list(_mcp_responses.items()),
            "response_bytes": list(_mcp_response_bytes.items()),
            "resource_bytes": list(_mcp_resource_bytes.items()),
        }

    _append_counter(lines, "mygithub_http_requests_total", "HTTP requests handled by the controller.", snapshots["http_requests"], ("method", "route", "status"))
    _append_counter(lines, "mygithub_http_request_duration_seconds_total", "Cumulative request duration.", snapshots["http_duration"], ("method", "route"), float_values=True)
    _append_counter(lines, "mygithub_mcp_tool_requests_total", "Canonical MCP tool handler observations.", snapshots["mcp_requests"], ("tool", "result", "mode"))
    _append_counter(lines, "mygithub_mcp_tool_duration_seconds_total", "Cumulative canonical MCP tool handler duration.", snapshots["mcp_duration"], ("tool", "result", "mode"), float_values=True)
    _append_counter(lines, "mygithub_mcp_explicit_waits_total", "Explicit compatibility wait observations.", snapshots["waits"], ("kind",))
    _append_counter(lines, "mygithub_mcp_explicit_wait_duration_seconds_total", "Cumulative explicit compatibility wait duration.", snapshots["wait_duration"], ("kind",), float_values=True)
    _append_counter(lines, "mygithub_private_ci_phase_observations_total", "Private CI lifecycle phase snapshot observations.", snapshots["ci_phases"], ("phase",))
    _append_counter(lines, "mygithub_private_ci_lifecycle_duration_observations_total", "Private CI durable lifecycle duration observations.", snapshots["ci_duration_observations"], ("kind",))
    _append_counter(lines, "mygithub_private_ci_lifecycle_duration_seconds_total", "Cumulative Private CI durations derived from durable lifecycle timestamps.", snapshots["ci_duration"], ("kind",), float_values=True)
    _append_counter(lines, "mygithub_convergence_phase_entries_total", "Development Convergence durable phase entries.", snapshots["convergence_entries"], ("phase",))
    _append_counter(lines, "mygithub_convergence_phase_duration_observations_total", "Completed Development Convergence phase duration observations.", snapshots["convergence_duration_observations"], ("phase",))
    _append_counter(lines, "mygithub_convergence_phase_duration_seconds_total", "Cumulative Development Convergence phase duration from durable transition timestamps.", snapshots["convergence_duration"], ("phase",), float_values=True)
    _append_counter(lines, "mygithub_idempotency_events_total", "Bounded idempotency reuse and conflict observations.", snapshots["idempotency"], ("operation", "outcome"))
    _append_counter(lines, "mygithub_failure_pack_builds_total", "Successful Failure Pack build observations.", snapshots["failure_builds"], ("operation",))
    _append_counter(lines, "mygithub_failure_pack_build_duration_seconds_total", "Cumulative successful Failure Pack build duration.", snapshots["failure_duration"], ("operation",), float_values=True)
    _append_counter(lines, "mygithub_failure_pack_payload_bytes_total", "Cumulative durable Failure Pack payload bytes.", snapshots["failure_bytes"], ("operation",))
    _append_counter(lines, "mygithub_mcp_responses_total", "MCP response inline/resource fallback observations.", snapshots["responses"], ("mode",))
    _append_counter(lines, "mygithub_mcp_response_bytes_total", "Cumulative returned MCP response envelope bytes.", snapshots["response_bytes"], ("mode",))
    _append_counter(lines, "mygithub_mcp_resource_bytes_total", "Cumulative payload bytes persisted for MCP response resources.", snapshots["resource_bytes"], ("mode",))
    return "\n".join(lines) + "\n"
