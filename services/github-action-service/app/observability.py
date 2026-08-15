"""Small dependency-free request metrics and correlation middleware."""

from __future__ import annotations

from collections import Counter
from contextvars import ContextVar
import logging
import re
import threading
import time
import uuid

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware


logger = logging.getLogger(__name__)
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_lock = threading.Lock()
_requests = Counter()
_duration_seconds = Counter()
_current_request_id: ContextVar[str] = ContextVar("mygithub_request_id", default="")


def current_request_id() -> str:
    return _current_request_id.get()


class RequestObservabilityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        supplied = request.headers.get("X-Request-ID", "")
        request_id = supplied if _REQUEST_ID_RE.fullmatch(supplied) else uuid.uuid4().hex
        started = time.monotonic()
        status_code = 500
        request_token = _current_request_id.set(request_id)
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            elapsed = time.monotonic() - started
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


def prometheus_metrics() -> str:
    lines = [
        "# HELP mygithub_http_requests_total HTTP requests handled by the controller.",
        "# TYPE mygithub_http_requests_total counter",
    ]
    with _lock:
        request_items = list(_requests.items())
        duration_items = list(_duration_seconds.items())
    for (method, route, status), value in sorted(request_items):
        lines.append(
            f'mygithub_http_requests_total{{method="{method}",route="{route}",status="{status}"}} {value}'
        )
    lines.extend(
        [
            "# HELP mygithub_http_request_duration_seconds_total Cumulative request duration.",
            "# TYPE mygithub_http_request_duration_seconds_total counter",
        ]
    )
    for (method, route), value in sorted(duration_items):
        lines.append(
            f'mygithub_http_request_duration_seconds_total{{method="{method}",route="{route}"}} {value:.6f}'
        )
    return "\n".join(lines) + "\n"
