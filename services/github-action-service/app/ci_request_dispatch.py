"""Durable DEV-003 CI Request preflight and background dispatch."""

from __future__ import annotations

import hashlib
import json
import logging
import queue
import re
import threading
import uuid

from app import mygithub12
from app.ci_repository_config import get_repository_config, get_repository_policy_source
from app.ci_request_store import (
    CIRequestRevisionConflictError,
    dispatch_ci_request,
    get_ci_request,
    get_ci_request_payload,
    list_pending_ci_requests,
    transition_ci_request,
)
from app.github_utils import get_github_changed_files_result

logger = logging.getLogger(__name__)
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_QUEUE: queue.Queue[str] = queue.Queue()
_SCHEDULED: set[str] = set()
_SCHEDULE_LOCK = threading.Lock()
_WORKERS_STARTED = False
_WORKER_COUNT = 2


class CIPreflightFailure(RuntimeError):
    """A queue-before-Worker validation failure with durable public semantics."""

    def __init__(self, code: str, reason: str):
        super().__init__(reason)
        self.code = code
        self.reason = reason


def effective_ci_config_digest(repository: str) -> str:
    """Hash local CI repository policy/config without GitHub or network access."""
    canonical = json.dumps(
        {
            "policy_source": get_repository_policy_source(repository),
            "repository_config": get_repository_config(repository),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _get_github_service():
    # Lazy import avoids the mcp_server -> ci_mcp import cycle at process start.
    from app.mcp_server import _service

    return _service


def _prefixed_code(code: str) -> str:
    safe = re.sub(r"[^A-Z0-9_]+", "_", str(code or "UNKNOWN").upper()).strip("_")
    return f"CI_PREFLIGHT_{safe or 'UNKNOWN'}"


def _perform_ci_request_preflight(request: dict) -> dict:
    """Run GitHub/Manifest/workspace/config checks only after canonical start."""
    service = _get_github_service()
    try:
        plan = mygithub12.plan_private_ci_job(
            service,
            request["repository"],
            request["commit_sha"],
            request["profile"],
        )
    except mygithub12.MyGithub12Error as exc:
        raise CIPreflightFailure(
            _prefixed_code(exc.code), "exact_git_identity_unavailable"
        ) from exc

    if not plan.get("applicable"):
        reason = str(plan.get("reason") or "not_applicable")
        raise CIPreflightFailure(_prefixed_code(reason), reason)
    if plan.get("commit_sha") != request["commit_sha"]:
        raise CIPreflightFailure(
            "CI_PREFLIGHT_COMMIT_MISMATCH", "exact_commit_mismatch"
        )
    tree_sha = str(plan.get("tree_sha") or "")
    if not _SHA_RE.fullmatch(tree_sha):
        raise CIPreflightFailure(
            "CI_PREFLIGHT_TREE_UNAVAILABLE", "exact_tree_unavailable"
        )

    try:
        branch_identity = mygithub12.resolve_identity(
            service,
            request["repository"],
            ref=request["branch"],
        )
    except mygithub12.MyGithub12Error as exc:
        raise CIPreflightFailure(
            _prefixed_code(exc.code), "branch_identity_unavailable"
        ) from exc
    if branch_identity["commit_sha"] != request["commit_sha"]:
        raise CIPreflightFailure(
            "CI_PREFLIGHT_BRANCH_HEAD_MISMATCH",
            "branch_no_longer_points_to_exact_commit",
        )
    if branch_identity["tree_sha"] != tree_sha:
        raise CIPreflightFailure(
            "CI_PREFLIGHT_TREE_MISMATCH", "branch_tree_identity_mismatch"
        )

    payload = get_ci_request_payload(request["request_id"])
    base_sha = str(payload.get("base_sha") or "")
    if base_sha:
        try:
            mygithub12.resolve_identity(
                service,
                request["repository"],
                commit_sha=base_sha,
            )
        except mygithub12.MyGithub12Error as exc:
            raise CIPreflightFailure(
                _prefixed_code(exc.code), "base_identity_unavailable"
            ) from exc

    if effective_ci_config_digest(request["repository"]) != request["effective_config_digest"]:
        raise CIPreflightFailure(
            "CI_PREFLIGHT_CONFIG_CHANGED", "effective_ci_config_changed"
        )

    changed = {
        "ok": True,
        "changed_files": [],
        "total_count": 0,
        "truncated": False,
    }
    if request["profile"] in {"repo-auto-check", "repo-fast-check"}:
        changed = get_github_changed_files_result(
            request["repository"],
            base_sha,
            request["commit_sha"],
        )
        if not changed.get("ok"):
            raise CIPreflightFailure(
                _prefixed_code(str(changed.get("error_code") or "CHANGED_FILES_FAILED")),
                "changed_files_preflight_failed",
            )

    return {
        "tree_sha": tree_sha,
        "changed_files": list(changed.get("changed_files") or []),
        "changed_files_total": int(changed.get("total_count") or 0),
        "changed_files_truncated": bool(changed.get("truncated")),
        "event_data": {
            "policy_source": plan.get("policy_source"),
            "detected_stacks": list(plan.get("detected_stacks") or []),
            "selected_profiles": list(plan.get("selected_profiles") or []),
            "workspaces_total": len(plan.get("workspaces") or []),
        },
    }


def _mark_preflight_failed(request: dict, failure: CIPreflightFailure) -> dict:
    error_id = f"ci_preflight_{uuid.uuid4().hex[:20]}"
    try:
        return transition_ci_request(
            request["request_id"],
            request["revision"],
            "terminal",
            "preflight_failed",
            preflight_error_id=error_id,
            preflight_error_code=failure.code,
            terminal_reason=failure.reason,
            event_data={
                "preflight_error_id": error_id,
                "preflight_error_code": failure.code,
            },
        )
    except CIRequestRevisionConflictError:
        return get_ci_request(request["request_id"])


def process_ci_request(request_id: str) -> dict | None:
    """Advance one durable Request; database state is the correctness gate."""
    request = get_ci_request(request_id)
    if not request:
        return None
    if request["phase"] == "accepted":
        try:
            request = transition_ci_request(
                request_id,
                request["revision"],
                "preparing",
                "preparing",
                event_data={"source": "durable_preflight_dispatcher"},
            )
        except CIRequestRevisionConflictError:
            request = get_ci_request(request_id)
    if not request or request["phase"] != "preparing":
        return request

    try:
        preflight = _perform_ci_request_preflight(request)
    except CIPreflightFailure as failure:
        return _mark_preflight_failed(request, failure)
    except Exception:
        logger.exception("CI Request preflight failed unexpectedly: %s", request_id)
        return _mark_preflight_failed(
            request,
            CIPreflightFailure(
                "CI_PREFLIGHT_INTERNAL_ERROR", "preflight_internal_error"
            ),
        )

    try:
        return dispatch_ci_request(
            request_id,
            expected_revision=request["revision"],
            tree_sha=preflight["tree_sha"],
            changed_files=preflight["changed_files"],
            changed_files_total=preflight["changed_files_total"],
            changed_files_truncated=preflight["changed_files_truncated"],
            event_data=preflight["event_data"],
        )
    except CIRequestRevisionConflictError:
        return get_ci_request(request_id)
    except Exception:
        # Worker INSERT/bind/event is one transaction; rollback leaves the
        # Request preparing and restart/replay can safely retry it.
        logger.exception(
            "CI Request dispatch remains preparing for durable retry: %s", request_id
        )
        return get_ci_request(request_id)


def _worker_loop() -> None:
    while True:
        request_id = _QUEUE.get()
        try:
            process_ci_request(request_id)
        except Exception:
            logger.exception("Durable CI Request worker failed: %s", request_id)
        finally:
            with _SCHEDULE_LOCK:
                _SCHEDULED.discard(request_id)
            _QUEUE.task_done()


def _ensure_workers() -> None:
    global _WORKERS_STARTED
    with _SCHEDULE_LOCK:
        if _WORKERS_STARTED:
            return
        _WORKERS_STARTED = True
        for index in range(_WORKER_COUNT):
            threading.Thread(
                target=_worker_loop,
                name=f"ci-request-preflight-{index + 1}",
                daemon=True,
            ).start()


def schedule_ci_request_preparation(request_id: str) -> bool:
    """Wake background preparation; durable Request state remains authoritative."""
    _ensure_workers()
    with _SCHEDULE_LOCK:
        if request_id in _SCHEDULED:
            return False
        _SCHEDULED.add(request_id)
        _QUEUE.put(request_id)
        return True


def recover_pending_ci_requests(limit: int = 1000) -> dict:
    """Reschedule accepted/preparing Requests after Controller restart."""
    pending = list_pending_ci_requests(limit=limit)
    scheduled = sum(
        schedule_ci_request_preparation(item["request_id"]) for item in pending
    )
    return {"pending": len(pending), "scheduled": scheduled}
