"""Durable, non-blocking convergence orchestration for Development Sessions.

The convergence run is the durable cursor for this operation. A call may create
or reuse the run, request an Index, perform analysis whose dependencies are
already ready, create/reuse one durable CI Request, and consume one current CI
Request snapshot. It never waits for an Index or CI lifecycle to change.
"""
from __future__ import annotations

import hashlib
import inspect
import json
from typing import Any, Awaitable, Callable, Mapping

from app import ci_database
from app import ci_request_store
from app import development_convergence_store as convergence_store
from app import observability
from app import development_session_store as sessions
from app import mygithub12
from app.ci_models import effective_priority
from app.ci_repository_config import get_max_timeout
from app.ci_request_dispatch import (
    effective_ci_config_digest,
    schedule_ci_request_preparation,
)
from app.mcp_response import store_response_resource
from app.ci_database import get_job, get_workers, reconcile_stale_workers


MyGithub12Error = mygithub12.MyGithub12Error

_TERMINAL_CI = {
    "passed",
    "failed",
    "timed_out",
    "cancelled",
    "superseded",
    "worker_lost",
    "internal_error",
    "preflight_failed",
}
_CI_REQUEST_PHASES = {"accepted", "preparing", "queued", "running", "terminal"}
_ANALYSIS_CALLBACKS: tuple[tuple[str, str], ...] = (
    ("change_context", "change_context"),
    ("change_impact", "impact"),
    ("contract_detection", "contracts"),
    ("affected_tests", "affected_tests"),
)
_PHASE_RANK = {
    "accepted": 0,
    "index_requested": 1,
    "analysis_pending": 2,
    "ci_requested": 3,
    "ci_running": 4,
    "post_ci_finalize": 5,
    "passed": 6,
    "failed": 6,
    "blocked": 6,
}


def _error_evidence(stage: str, exc: Exception) -> dict[str, Any]:
    """Convert one analysis error into bounded, truthful evidence."""
    return {
        "stage": stage,
        "code": str(getattr(exc, "code", "INTERNAL_ERROR")),
        "message": str(getattr(exc, "message", str(exc)))[:1000],
        "type": type(exc).__name__,
    }


def _index_is_exact_ready(
    index_status: Mapping[str, Any], head_sha: str, tree_sha: str
) -> bool:
    """Only an exact commit/tree result may unlock dependent analysis."""
    return bool(
        index_status.get("status") in {"ready", "completed"}
        and index_status.get("commit_sha") == head_sha
        and index_status.get("tree_sha") == tree_sha
    )


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _analysis_failure_state(result: Mapping[str, Any]) -> str:
    if result.get("ok") is False:
        return "failed"
    if result.get("complete") is False:
        return "degraded"
    return "ready"


def _analysis_result_details(
    details: Mapping[str, Any],
    *,
    repository: str = "",
    index_status: Mapping[str, Any],
    index_request: Mapping[str, Any] | None,
    head_sha: str,
    tree_sha: str,
    base_sha: str,
    degraded_reasons: list[dict[str, Any]],
    pending_reasons: list[dict[str, Any]],
    analysis_resource: Mapping[str, Any] | None,
    analysis_states: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the compact public analysis view shared by direct and durable calls."""
    change_context = (
        details.get("change_context")
        if isinstance(details.get("change_context"), dict)
        else {}
    )
    impact = details.get("impact") if isinstance(details.get("impact"), dict) else {}
    contracts = (
        details.get("contracts") if isinstance(details.get("contracts"), dict) else {}
    )
    affected = (
        details.get("affected_tests")
        if isinstance(details.get("affected_tests"), dict)
        else {}
    )
    compact_index_request = None
    if isinstance(index_request, Mapping):
        compact_index_request = {
            key: index_request.get(key)
            for key in (
                "job_id",
                "status",
                "strategy",
                "revision",
                "step",
                "deduplicated",
            )
            if key in index_request
        }
    analysis_states = analysis_states or {}
    index_ready = _index_is_exact_ready(index_status, head_sha, tree_sha)
    impact_state = str((analysis_states.get("change_impact") or {}).get("state") or "")
    impact_ok = impact.get("ok") is not False and impact_state != "failed"
    impact_complete = impact.get("complete") is True or impact_state == "ready"
    context_state = str((analysis_states.get("change_context") or {}).get("state") or "")
    contracts_state = str(
        (analysis_states.get("contract_detection") or {}).get("state") or ""
    )
    affected_state = str((analysis_states.get("affected_tests") or {}).get("state") or "")
    pending = bool(pending_reasons) or not index_ready
    return {
        "identity": {
            "repository": repository or index_status.get("repository"),
            "commit_sha": head_sha,
            "tree_sha": tree_sha,
        },
        "base_sha": base_sha,
        "index": {
            "ready": index_ready,
            "status": index_status.get("status"),
            "commit_sha": index_status.get("commit_sha"),
            "tree_sha": index_status.get("tree_sha"),
            "index_version": index_status.get("index_version"),
            "request": compact_index_request,
        },
        "change_context": {
            "ok": change_context.get("ok") is not False and context_state != "failed",
            "items_total": len(change_context.get("items") or []),
            "omitted_count": int(change_context.get("omitted_count", 0) or 0),
        },
        "impact": {
            "ok": impact_ok,
            "complete": impact_complete,
            "changed_paths": list(impact.get("changed_paths") or [])[:100],
            "affected_modules": list(impact.get("affected_modules") or [])[:100],
            "affected_test_count": len(impact.get("affected_tests") or []),
            "contract_change_count": len(impact.get("contract_changes") or []),
        },
        "contracts": {
            "ok": contracts.get("ok") is not False and contracts_state != "failed",
            "summary": contracts.get("summary") or {},
            "changes": list(contracts.get("changes") or [])[:50],
        },
        "affected_tests": {
            "ok": affected.get("ok") is not False and affected_state != "failed",
            "authoritative": bool(affected.get("authoritative", False)),
            "tests": list(affected.get("tests") or [])[:100],
        },
        "details": dict(details),
        "pending": pending,
        "pending_reasons": list(pending_reasons),
        "degraded": bool(degraded_reasons),
        "degraded_reasons": list(degraded_reasons),
        "conservative_ci_required": bool(
            degraded_reasons or pending or not index_ready
        ),
        "analysis_resource": dict(analysis_resource) if analysis_resource else None,
    }


def _run_analysis_callbacks(
    service: Any,
    session: Mapping[str, Any],
    base_sha: str,
    *,
    existing_states: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run only analysis stages still pending after exact Index readiness."""
    repository = str(session["repository"])
    head_sha = str(session["head_commit_sha"])
    task = str(
        (session.get("metadata") or {}).get("task_name")
        or "development task convergence"
    )
    callbacks: dict[str, Callable[[], Any]] = {
        "change_context": lambda: mygithub12.change_context_pack(
            service, repository, base_sha, head_sha, task, 50, 1024 * 1024
        ),
        "change_impact": lambda: mygithub12.change_impact(
            service, repository, base_sha, head_sha
        ),
        "contract_detection": lambda: mygithub12.contract_changes(
            service, repository, base_sha, head_sha
        ),
        "affected_tests": lambda: mygithub12.affected_tests(
            service, repository, head_sha, base_sha
        ),
    }
    details: dict[str, Any] = {}
    degraded_reasons: list[dict[str, Any]] = []
    existing_states = existing_states or {}
    for stage, public_key in _ANALYSIS_CALLBACKS:
        state = str((existing_states.get(stage) or {}).get("state") or "pending")
        if state != "pending":
            continue
        try:
            result = callbacks[stage]()
            if not isinstance(result, dict):
                result = {"ok": True, "value": result}
            details[public_key] = result
            result_state = _analysis_failure_state(result)
            if result_state != "ready":
                degraded_reasons.append(
                    {
                        "stage": stage,
                        "code": (
                            "IMPACT_ANALYSIS_INCOMPLETE"
                            if stage == "change_impact"
                            else "ANALYSIS_STAGE_INCOMPLETE"
                        ),
                        "message": f"{stage} returned incomplete evidence",
                    }
                )
        except Exception as exc:
            error = _error_evidence(stage, exc)
            details[public_key] = {"ok": False, "error": error}
            degraded_reasons.append(error)

    impact = details.get("impact") if isinstance(details.get("impact"), dict) else {}
    if impact and impact.get("ok") is not False and impact.get("complete") is not True:
        reason = {
            "stage": "change_impact",
            "code": "IMPACT_ANALYSIS_INCOMPLETE",
            "message": "change impact is incomplete; full CI must remain conservative",
        }
        if reason not in degraded_reasons:
            degraded_reasons.append(reason)
    return details, degraded_reasons


def _analysis_readiness_reasons(
    snapshot: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Expose durable analysis failures/pending states on every replay."""
    pending: list[dict[str, Any]] = []
    degraded: list[dict[str, Any]] = []
    for stage in convergence_store.ANALYSIS_STAGES:
        state = (snapshot.get("analysis") or {}).get(stage) or {}
        state_name = str(state.get("state") or "pending")
        if state_name == "pending":
            pending.append(
                {
                    "stage": stage,
                    "code": "INDEX_NOT_READY" if stage == "index" else "ANALYSIS_NOT_READY",
                    "message": (
                        "exact HEAD Repository Index is not ready; analysis remains pending"
                        if stage == "index"
                        else f"{stage} evidence is not ready"
                    ),
                }
            )
        elif state_name in {"failed", "degraded"}:
            degraded.append(
                {
                    "stage": stage,
                    "code": str(
                        state.get("error_code")
                        or (
                            "IMPACT_ANALYSIS_INCOMPLETE"
                            if stage == "change_impact"
                            else "ANALYSIS_STAGE_INCOMPLETE"
                        )
                    ),
                    "message": str(
                        state.get("error_message")
                        or f"{stage} evidence is {state_name}"
                    ),
                }
            )
    return pending, degraded


def _analysis_resource(
    *,
    session: Mapping[str, Any],
    base_sha: str,
    index_status: Mapping[str, Any],
    index_request: Mapping[str, Any] | None,
    details: Mapping[str, Any],
    degraded_reasons: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not _index_is_exact_ready(
        index_status, str(session["head_commit_sha"]), str(session["tree_sha"])
    ):
        return None
    evidence = {
        "identity": {
            "repository": session["repository"],
            "commit_sha": session["head_commit_sha"],
            "tree_sha": session["tree_sha"],
        },
        "base_sha": base_sha,
        "index": {"status": dict(index_status), "request": dict(index_request or {})},
        "analysis": dict(details),
        "degraded_reasons": list(degraded_reasons),
    }
    try:
        resource = store_response_resource(evidence)
    except Exception as exc:
        degraded_reasons.append(_error_evidence("analysis_resource", exc))
        return None
    if not isinstance(resource, dict):
        degraded_reasons.append(
            {
                "stage": "analysis_resource",
                "code": "ANALYSIS_RESOURCE_UNAVAILABLE",
                "message": "analysis evidence resource was not returned",
            }
        )
        return None
    return {
        "resource_uri": resource.get("resource_uri"),
        "total_bytes": resource.get("total_bytes"),
        "content_sha256": resource.get("sha256"),
    }


def convergence_analysis(
    service: Any,
    session: dict[str, Any],
    base_sha: str = "",
    index_wait_seconds: int = 55,
    idempotency_key: str = "",
) -> dict[str, Any]:
    """Perform one no-wait analysis pass.

    ``index_wait_seconds`` remains in the signature for compatibility only. It
    is deliberately ignored, as is any external Index lifecycle wait.
    """
    del index_wait_seconds
    repository = session["repository"]
    head_sha = session["head_commit_sha"]
    tree_sha = session["tree_sha"]
    resolved_base = base_sha or session["base_commit_sha"]
    identity = mygithub12.resolve_identity(service, repository, commit_sha=head_sha)
    if identity.get("commit_sha") not in {None, head_sha} or identity.get("tree_sha") != tree_sha:
        raise MyGithub12Error(
            "DEVELOPMENT_SESSION_RECOVERY_REQUIRED",
            "exact HEAD Tree differs from the Development Session",
            {
                "session_head": head_sha,
                "session_tree": tree_sha,
                "actual_head": identity.get("commit_sha"),
                "actual_tree": identity.get("tree_sha"),
                "recovery_required": True,
            },
        )

    index_error = None
    try:
        index_status = mygithub12.get_index_status(service, repository, head_sha)
    except Exception as exc:
        index_status = {
            "status": "unknown",
            "repository": repository,
            "commit_sha": head_sha,
            "tree_sha": None,
        }
        index_error = _error_evidence("index_status", exc)
    if not isinstance(index_status, Mapping):
        index_status = {
            "status": "unknown",
            "repository": repository,
            "commit_sha": head_sha,
            "tree_sha": None,
        }
    index_request = None
    if not _index_is_exact_ready(index_status, head_sha, tree_sha):
        index_request = mygithub12.request_index_build(
            service,
            repository,
            head_sha,
            "auto",
            resolved_base,
            "interactive",
            f"converge-index:{session['session_id']}:{head_sha}:{idempotency_key or 'default'}",
            False,
        )
        if isinstance(index_request, dict) and _index_is_exact_ready(
            index_request, head_sha, tree_sha
        ):
            index_status = index_request
        elif isinstance(index_request, dict) and index_request.get("status") in {
            "failed",
            "cancelled",
        }:
            index_error = {
                "stage": "index",
                "code": str(index_request.get("error_code") or "INDEX_BUILD_FAILED"),
                "message": str(index_request.get("error_message") or "Index build failed"),
            }
        elif isinstance(index_request, dict) and index_request.get("status"):
            index_status = index_request
            index_error = None

    details: dict[str, Any] = {}
    degraded_reasons: list[dict[str, Any]] = []
    pending_reasons: list[dict[str, Any]] = []
    if _index_is_exact_ready(index_status, head_sha, tree_sha):
        details, degraded_reasons = _run_analysis_callbacks(
            service, session, resolved_base
        )
    else:
        pending_reasons.append(
            {
                "stage": "index",
                "code": "INDEX_NOT_READY",
                "message": "exact HEAD Repository Index is not ready; analysis remains pending",
            }
        )
        if index_error:
            degraded_reasons.append(index_error)

    resource = _analysis_resource(
        session=session,
        base_sha=resolved_base,
        index_status=index_status,
        index_request=index_request,
        details=details,
        degraded_reasons=degraded_reasons,
    )
    result = _analysis_result_details(
        details,
        repository=str(repository),
        index_status=index_status,
        index_request=index_request,
        head_sha=head_sha,
        tree_sha=tree_sha,
        base_sha=resolved_base,
        degraded_reasons=degraded_reasons,
        pending_reasons=pending_reasons,
        analysis_resource=resource,
    )
    result["index_job_id"] = (
        index_request.get("job_id") if isinstance(index_request, dict) else None
    )
    return result


def _worker_snapshot(job_id: str) -> dict[str, Any]:
    """Return one local Worker snapshot; this compatibility helper never waits."""
    job = get_job(job_id)
    if not job:
        raise MyGithub12Error(
            "PRIVATE_CI_JOB_NOT_FOUND",
            "private CI job disappeared",
            {"job_id": job_id},
        )
    worker_id = job.get("worker_id")
    if not worker_id:
        return {
            "worker_id": None,
            "terminal": job.get("status") in _TERMINAL_CI,
            "released": False,
            "idle": False,
            "reason": "worker_not_recorded",
        }
    reconcile_stale_workers()
    worker = next(
        (item for item in get_workers() if item.get("worker_id") == worker_id), None
    )
    if worker is None:
        return {
            "worker_id": worker_id,
            "terminal": job.get("status") in _TERMINAL_CI,
            "released": False,
            "idle": False,
            "reason": "worker_not_registered",
        }
    released = worker.get("current_job") != job_id
    return {
        "worker_id": worker_id,
        "terminal": job.get("status") in _TERMINAL_CI,
        "online": bool(worker.get("online")),
        "status": worker.get("status"),
        "current_job": worker.get("current_job"),
        "max_concurrent": worker.get("max_concurrent"),
        "released": released,
        "idle": bool(released and worker.get("status") == "idle"),
    }


def wait_worker_final_state(job_id: str, wait_seconds: int = 5) -> dict[str, Any]:
    """Compatibility name for a single local snapshot, never a wait operation."""
    del wait_seconds
    return _worker_snapshot(job_id)


async def _invoke(
    github_call: Callable[..., Awaitable[Any]],
    fn: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    result = github_call(fn, *args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


def _session_identity(session: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "development_session_id": session.get("session_id"),
        "workspace_id": session.get("workspace_id"),
        "repository": session.get("repository"),
        "branch": session.get("branch"),
        "head_sha": session.get("head_commit_sha"),
        "tree_sha": session.get("tree_sha"),
        "base_branch": session.get("base_branch"),
        "base_sha": session.get("base_commit_sha"),
    }


def _identity_drift(
    snapshot: Mapping[str, Any], session: Mapping[str, Any], resolved_base: str
) -> dict[str, Any] | None:
    expected = {
        "development_session_id": snapshot.get("development_session_id"),
        "workspace_id": snapshot.get("workspace_id"),
        "repository": snapshot.get("repository"),
        "branch": snapshot.get("branch"),
        "head_sha": snapshot.get("head_sha"),
        "tree_sha": snapshot.get("tree_sha"),
        "base_branch": snapshot.get("base_branch"),
        "base_sha": snapshot.get("base_sha"),
    }
    actual = _session_identity(session)
    actual["base_sha"] = resolved_base
    mismatches = {
        key: {"expected": expected.get(key), "actual": actual.get(key)}
        for key in expected
        if expected.get(key) != actual.get(key)
    }
    return mismatches or None


async def _verify_current_identity(
    github_call: Callable[..., Awaitable[Any]],
    service: Any,
    session: Mapping[str, Any],
    snapshot: Mapping[str, Any],
) -> None:
    """Check Session/Workspace/HEAD/Tree without mutating or waiting."""
    resolved_base = str(snapshot.get("base_sha") or session.get("base_commit_sha") or "")
    drift = _identity_drift(snapshot, session, resolved_base)
    if drift:
        raise MyGithub12Error(
            "DEVELOPMENT_SESSION_RECOVERY_REQUIRED",
            "Development Session identity differs from the active convergence",
            {"mismatches": drift, "recovery_required": True},
        )

    # Real GitHubService instances have a client. Lightweight unit-test
    # doubles may intentionally omit it; Session evidence is still checked in
    # that case, while patched resolve_identity remains fully exercised.
    workspace_preflight = getattr(mygithub12, "workspace_write_preflight", None)
    if getattr(service, "client", None) is not None and workspace_preflight:
        workspace = await _invoke(
            github_call,
            workspace_preflight,
            service,
            session["repository"],
            session["branch"],
            session["head_commit_sha"],
            session["workspace_id"],
            int(session.get("workspace_revision") or 0),
        )
        if isinstance(workspace, Mapping):
            workspace_mismatches = {
                key: {"expected": snapshot.get(expected), "actual": workspace.get(key)}
                for key, expected in (
                    ("workspace_id", "workspace_id"),
                    ("repository", "repository"),
                    ("branch", "branch"),
                    ("head_sha", "head_sha"),
                    ("tree_sha", "tree_sha"),
                )
                if workspace.get(key) is not None
                and workspace.get(key) != snapshot.get(expected)
            }
            if workspace_mismatches:
                raise MyGithub12Error(
                    "DEVELOPMENT_SESSION_RECOVERY_REQUIRED",
                    "Workspace identity differs from the active convergence",
                    {"mismatches": workspace_mismatches, "recovery_required": True},
                )

    try:
        identity = await _invoke(
            github_call,
            mygithub12.resolve_identity,
            service,
            session["repository"],
            commit_sha=session["head_commit_sha"],
        )
    except Exception as exc:
        # A deliberately tiny fake service has no GitHub client. Do not make
        # unit-only durable state tests require a network stub; a real service
        # still fails closed with recovery evidence.
        if getattr(service, "client", None) is None and isinstance(exc, AttributeError):
            return
        raise MyGithub12Error(
            "DEVELOPMENT_SESSION_RECOVERY_REQUIRED",
            "exact HEAD/Tree identity could not be verified",
            {"cause_type": type(exc).__name__, "recovery_required": True},
        ) from exc
    if not isinstance(identity, Mapping):
        raise MyGithub12Error(
            "DEVELOPMENT_SESSION_RECOVERY_REQUIRED",
            "exact HEAD/Tree identity response was invalid",
            {"recovery_required": True},
        )
    actual_head = identity.get("commit_sha") or session["head_commit_sha"]
    actual_tree = identity.get("tree_sha")
    if actual_head != snapshot.get("head_sha") or actual_tree != snapshot.get("tree_sha"):
        raise MyGithub12Error(
            "DEVELOPMENT_SESSION_RECOVERY_REQUIRED",
            "exact HEAD/Tree differs from the active convergence",
            {
                "expected_head_sha": snapshot.get("head_sha"),
                "actual_head_sha": actual_head,
                "expected_tree_sha": snapshot.get("tree_sha"),
                "actual_tree_sha": actual_tree,
                "recovery_required": True,
            },
        )


def _phase_rank(phase: str) -> int:
    return _PHASE_RANK.get(phase, -1)


def _durable_phase_started_at(snapshot: Mapping[str, Any]) -> Any:
    current_phase = str(snapshot.get("phase") or "")
    started_at = snapshot.get("created_at")
    for event in convergence_store.list_convergence_events(
        str(snapshot["convergence_id"]), limit=500
    ):
        if (
            event.get("to_phase") == current_phase
            and event.get("from_phase") != current_phase
        ):
            started_at = event.get("created_at")
    return started_at


def _transition_if_needed(
    snapshot: dict[str, Any],
    phase: str,
    *,
    status: str = "",
    error_code: str | None = None,
    error_message: str | None = None,
    event_type: str = "phase_changed",
    metadata: Mapping[str, Any] | None = None,
    _retry_on_cas: bool = True,
) -> dict[str, Any]:
    if snapshot.get("terminal"):
        return snapshot
    current_phase = str(snapshot.get("phase") or "")
    target_status = status or phase
    should_move = _phase_rank(phase) > _phase_rank(current_phase)
    should_update_status = target_status != snapshot.get("status")
    should_update_error = (
        error_code is not None and error_code != snapshot.get("error_code")
    ) or (
        error_message is not None and error_message != snapshot.get("error_message")
    )
    if not (should_move or should_update_status or should_update_error):
        return snapshot
    phase_started_at = _durable_phase_started_at(snapshot) if should_move else None
    try:
        updated = convergence_store.transition_convergence(
            snapshot["convergence_id"],
            int(snapshot["revision"]),
            phase if should_move else current_phase,
            status=target_status,
            error_code=error_code,
            error_message=error_message,
            event_type=event_type,
            metadata=metadata,
        )
        if should_move and updated.get("phase") != current_phase:
            observability.observe_convergence_transition(
                current_phase, str(updated.get("phase") or phase),
                phase_started_at, updated.get("updated_at"),
            )
        return updated
    except MyGithub12Error as exc:
        if exc.code != "DEVELOPMENT_CONVERGENCE_REVISION_MISMATCH" or not _retry_on_cas:
            raise
        refreshed = convergence_store.get_convergence(snapshot["convergence_id"])
        if refreshed.get("terminal"):
            return refreshed
        return _transition_if_needed(
            refreshed,
            phase,
            status=status,
            error_code=error_code,
            error_message=error_message,
            event_type=event_type,
            metadata=metadata,
            _retry_on_cas=False,
        )


def _bind_with_replay(
    snapshot: dict[str, Any],
    binder: Callable[..., dict[str, Any]],
    *args: Any,
    _retry_on_cas: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    try:
        return binder(snapshot["convergence_id"], int(snapshot["revision"]), *args, **kwargs)
    except MyGithub12Error as exc:
        if exc.code != "DEVELOPMENT_CONVERGENCE_REVISION_MISMATCH" or not _retry_on_cas:
            raise
        refreshed = convergence_store.get_convergence(snapshot["convergence_id"])
        return binder(refreshed["convergence_id"], int(refreshed["revision"]), *args, **kwargs)


def _record_analysis_with_replay(
    snapshot: dict[str, Any],
    *,
    stage: str,
    state: str,
    resource_uri: str | None = None,
    resource_identity: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    _retry_on_cas: bool = True,
) -> dict[str, Any]:
    current = (snapshot.get("analysis") or {}).get(stage) or {}
    if (
        current.get("state") == state
        and current.get("resource_uri") == resource_uri
        and current.get("resource_identity") == resource_identity
        and current.get("error_code") == error_code
        and current.get("error_message") == error_message
    ):
        return snapshot
    try:
        return convergence_store.record_analysis_state(
            snapshot["convergence_id"],
            int(snapshot["revision"]),
            stage=stage,
            state=state,
            resource_uri=resource_uri,
            resource_identity=resource_identity,
            error_code=error_code,
            error_message=error_message,
        )
    except MyGithub12Error as exc:
        if exc.code != "DEVELOPMENT_CONVERGENCE_REVISION_MISMATCH" or not _retry_on_cas:
            raise
        refreshed = convergence_store.get_convergence(snapshot["convergence_id"])
        return _record_analysis_with_replay(
            refreshed,
            stage=stage,
            state=state,
            resource_uri=resource_uri,
            resource_identity=resource_identity,
            error_code=error_code,
            error_message=error_message,
            _retry_on_cas=False,
        )


def _record_terminal_evidence_if_needed(
    snapshot: dict[str, Any],
    *,
    attestation_id: str | None,
    failure_pack_id: str | None,
    _retry_on_cas: bool = True,
) -> dict[str, Any]:
    if not attestation_id and not failure_pack_id:
        return snapshot
    if snapshot.get("attestation_id") == attestation_id and snapshot.get(
        "failure_pack_id"
    ) == failure_pack_id:
        return snapshot
    try:
        return convergence_store.record_terminal_evidence(
            snapshot["convergence_id"],
            int(snapshot["revision"]),
            attestation_id=attestation_id,
            failure_pack_id=failure_pack_id,
        )
    except MyGithub12Error as exc:
        if exc.code != "DEVELOPMENT_CONVERGENCE_REVISION_MISMATCH" or not _retry_on_cas:
            raise
        refreshed = convergence_store.get_convergence(snapshot["convergence_id"])
        return _record_terminal_evidence_if_needed(
            refreshed,
            attestation_id=attestation_id,
            failure_pack_id=failure_pack_id,
            _retry_on_cas=False,
        )


def _ci_request_status(request: Mapping[str, Any]) -> str:
    status = str(request.get("status") or "")
    phase = str(request.get("phase") or "")
    if status:
        return status
    if phase in _CI_REQUEST_PHASES:
        return "terminal" if phase == "terminal" else phase
    return "unknown"


def _ci_request_terminal(request: Mapping[str, Any]) -> bool:
    return bool(
        request.get("terminal")
        or request.get("phase") == "terminal"
        or _ci_request_status(request) in _TERMINAL_CI
    )


def _validate_ci_request_identity(
    request: Mapping[str, Any], snapshot: Mapping[str, Any], mode: str
) -> None:
    expected_profile = "repo-fast-check" if mode == "fast" else "repo-auto-check"
    mismatches: dict[str, Any] = {}
    for request_key, convergence_key in (
        ("repository", "repository"),
        ("branch", "branch"),
        ("commit_sha", "head_sha"),
        ("profile", "mode"),
    ):
        expected = (
            expected_profile
            if convergence_key == "mode"
            else snapshot.get(convergence_key)
        )
        actual = request.get(request_key)
        if actual is not None and actual != expected:
            mismatches[request_key] = {"expected": expected, "actual": actual}
    request_tree = request.get("tree_sha")
    if request_tree and request_tree != snapshot.get("tree_sha"):
        mismatches["tree_sha"] = {
            "expected": snapshot.get("tree_sha"),
            "actual": request_tree,
        }
    if mismatches:
        raise MyGithub12Error(
            "DEVELOPMENT_SESSION_RECOVERY_REQUIRED",
            "CI Request identity differs from the active convergence",
            {"mismatches": mismatches, "recovery_required": True},
        )


def _extract_evidence(request: Mapping[str, Any]) -> tuple[str | None, str | None]:
    attestation_id = request.get("attestation_id")
    failure_pack_id = request.get("failure_pack_id")
    attestation = request.get("attestation")
    if not attestation_id and isinstance(attestation, Mapping):
        attestation_id = attestation.get("attestation_id")
    failure = request.get("failure_pack")
    if not failure_pack_id and isinstance(failure, Mapping):
        failure_pack_id = failure.get("failure_pack_id") or failure.get("resource_uri")
    return (
        str(attestation_id) if attestation_id else None,
        str(failure_pack_id) if failure_pack_id else None,
    )


def _validation_snapshot(
    request: Mapping[str, Any] | None,
    *,
    ci_job_id: str | None,
    attestation_id: str | None,
    failure_pack_id: str | None,
) -> dict[str, Any]:
    if not request:
        return {
            "request_id": None,
            "request": None,
            "job": {"job_id": ci_job_id, "status": "not_found"},
            "status": "not_found",
            "phase": "unknown",
            "revision": None,
            "terminal": False,
            "merge_eligible": False,
            "attestation": None,
            "failure_pack": None,
        }
    status = _ci_request_status(request)
    terminal = _ci_request_terminal(request)
    return {
        "request_id": request.get("request_id"),
        "request": dict(request),
        "job": {
            "job_id": ci_job_id or request.get("worker_job_id"),
            "status": status,
            "profile": request.get("profile"),
            "commit_sha": request.get("commit_sha"),
            "tree_sha": request.get("tree_sha"),
        },
        "status": status,
        "phase": request.get("phase"),
        "revision": request.get("revision"),
        "terminal": terminal,
        "merge_eligible": bool(
            status == "passed" and request.get("profile") == "repo-auto-check"
        ),
        "attestation": (
            {"attestation_id": attestation_id} if attestation_id else None
        ),
        "failure_pack": (
            {"failure_pack_id": failure_pack_id} if failure_pack_id else None
        ),
    }


def _fallback_ci_digest(repository: str, profile: str) -> str:
    return _canonical_digest({"repository": repository, "profile": profile})


async def _create_ci_request(
    github_call: Callable[..., Awaitable[Any]],
    snapshot: dict[str, Any],
    session: Mapping[str, Any],
    mode: str,
    *,
    supersede_previous: bool,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Create one durable Request and wake preparation without waiting for it."""
    del github_call
    repository = str(snapshot["repository"])
    branch = str(snapshot["branch"])
    profile = "repo-fast-check" if mode == "fast" else "repo-auto-check"
    try:
        config_digest = effective_ci_config_digest(repository)
    except Exception:
        config_digest = _fallback_ci_digest(repository, profile)
    priority = effective_priority(branch, profile, 50)
    try:
        timeout_seconds = get_max_timeout(repository)
    except Exception:
        timeout_seconds = 900
    # The convergence identity, rather than the caller's window, is the CI
    # identity. This makes a new window and a repeated call share the Request.
    request_key = f"convergence:{snapshot['convergence_id']}"
    payload = {
        "schema": "development-convergence-ci-v1",
        "convergence_id": snapshot["convergence_id"],
        "repository": repository,
        "branch": branch,
        "commit_sha": snapshot["head_sha"],
        "tree_sha": snapshot["tree_sha"],
        "profile": profile,
        "timeout_seconds": int(timeout_seconds),
        "priority": int(priority),
        "base_sha": snapshot["base_sha"],
        "supersede_previous": bool(supersede_previous),
        "effective_config_digest": config_digest,
        "session_id": session.get("session_id"),
    }
    normalized_hash = ci_request_store.compute_normalized_request_hash(payload)
    ci_database.init_db()
    # A crash can occur after the durable Request commit but before the
    # convergence row binds its request_id. Reopen the Request by its stable
    # key first so a changed local config cannot manufacture a second Request.
    request = ci_request_store.get_ci_request_by_idempotency_key(request_key)
    if request is None:
        request = ci_request_store.create_or_get_ci_request(
            repository=repository,
            branch=branch,
            commit_sha=str(snapshot["head_sha"]),
            tree_sha=str(snapshot["tree_sha"]),
            profile=profile,
            effective_config_digest=config_digest,
            idempotency_key=request_key,
            normalized_request_hash=normalized_hash,
            request_payload=payload,
        )
    schedule_error = None
    try:
        # Scheduling is fire-and-forget. The Request row remains the source of
        # truth if this process exits before the preparation thread runs.
        schedule_ci_request_preparation(str(request["request_id"]))
    except Exception as exc:
        schedule_error = _error_evidence("ci_request_schedule", exc)
    return request, schedule_error


async def _advance_ci_track(
    github_call: Callable[..., Awaitable[Any]],
    snapshot: dict[str, Any],
    session: Mapping[str, Any],
    mode: str,
    *,
    supersede_previous: bool,
) -> tuple[
    dict[str, Any], dict[str, Any], dict[str, Any] | None, str | None, bool
]:
    """Create/reuse the Request, bind a known Worker ID, and read once."""
    schedule_error: dict[str, Any] | None = None
    created_or_reused = False
    if not snapshot.get("ci_request_id"):
        request, schedule_error = await _create_ci_request(
            github_call,
            snapshot,
            session,
            mode,
            supersede_previous=supersede_previous,
        )
        created_or_reused = True
        snapshot = _bind_with_replay(
            snapshot, convergence_store.bind_ci_request, request["request_id"]
        )
    request_id = snapshot.get("ci_request_id")
    request = ci_request_store.get_ci_request(str(request_id)) if request_id else None
    if request is None:
        raise MyGithub12Error(
            "DEVELOPMENT_SESSION_RECOVERY_REQUIRED",
            "durable CI Request for the convergence was not found",
            {"ci_request_id": request_id, "recovery_required": True},
        )
    _validate_ci_request_identity(request, snapshot, mode)
    if not created_or_reused and request.get("phase") in {"accepted", "preparing"}:
        try:
            # Reopen/replay may be the first process to notice an unprepared
            # Request. Scheduling is idempotent and never waits for it.
            schedule_ci_request_preparation(str(request["request_id"]))
        except Exception as exc:
            schedule_error = _error_evidence("ci_request_schedule", exc)
    worker_job_id = request.get("worker_job_id") or request.get("ci_job_id")
    if worker_job_id and not snapshot.get("ci_job_id"):
        snapshot = _bind_with_replay(
            snapshot,
            convergence_store.bind_ci_job,
            ci_request_id=str(request["request_id"]),
            ci_job_id=str(worker_job_id),
        )
    elif worker_job_id and snapshot.get("ci_job_id") != worker_job_id:
        raise MyGithub12Error(
            "DEVELOPMENT_SESSION_RECOVERY_REQUIRED",
            "convergence CI Job differs from the durable CI Request Worker Job",
            {
                "convergence_ci_job_id": snapshot.get("ci_job_id"),
                "request_ci_job_id": worker_job_id,
                "recovery_required": True,
            },
        )
    return (
        snapshot,
        request,
        schedule_error,
        str(snapshot.get("ci_job_id") or worker_job_id or "") or None,
        created_or_reused,
    )


def _next_actions(
    snapshot: Mapping[str, Any],
    validation: Mapping[str, Any],
    analysis: Mapping[str, Any],
    mode: str,
) -> list[Any]:
    if snapshot.get("phase") == "blocked":
        return ["recover_development_task"]
    if snapshot.get("phase") == "failed":
        return (
            ["inspect_failure_pack"]
            if snapshot.get("failure_pack_id")
            else ["inspect_ci_request"]
        )
    if snapshot.get("phase") == "passed":
        return ["run_full_convergence"] if mode == "fast" else ["readiness"]
    if validation.get("status") in _TERMINAL_CI and analysis.get("pending"):
        return ["converge_development_task"]
    if validation.get("status") in _TERMINAL_CI and analysis.get("degraded"):
        return ["inspect_convergence_resource", "converge_development_task"]
    return ["converge_development_task"]


def _response(
    snapshot: dict[str, Any],
    session: Mapping[str, Any],
    *,
    mode: str,
    index: Mapping[str, Any],
    analysis: Mapping[str, Any],
    request: Mapping[str, Any] | None,
    validation: Mapping[str, Any],
    schedule_error: Mapping[str, Any] | None = None,
    recovery_required: bool = False,
) -> dict[str, Any]:
    next_actions = _next_actions(snapshot, validation, analysis, mode)
    ci_status = _ci_request_status(request) if request else None
    result = {
        "ok": True,
        "converged": bool(snapshot.get("phase") == "passed"),
        "mode": mode,
        "convergence_id": snapshot.get("convergence_id"),
        "development_session_id": snapshot.get("development_session_id"),
        "phase": snapshot.get("phase"),
        "status": snapshot.get("status"),
        "revision": snapshot.get("revision"),
        "terminal": bool(snapshot.get("terminal")),
        "continuation_required": not bool(snapshot.get("terminal")),
        "recovery_required": bool(
            recovery_required or snapshot.get("phase") == "blocked"
        ),
        "convergence": snapshot,
        "development_session": dict(session),
        "exact_head": {
            "repository": snapshot.get("repository"),
            "branch": snapshot.get("branch"),
            "commit_sha": snapshot.get("head_sha"),
            "tree_sha": snapshot.get("tree_sha"),
            "base_branch": snapshot.get("base_branch"),
            "base_sha": snapshot.get("base_sha"),
        },
        "index": dict(index),
        "analysis": dict(analysis),
        "ci_request": dict(request) if request else None,
        "ci_status": ci_status,
        "ci_job_id": snapshot.get("ci_job_id"),
        "validation": dict(validation),
        "attestation_id": snapshot.get("attestation_id"),
        "failure_pack_id": snapshot.get("failure_pack_id"),
        "next_actions": next_actions,
        "next_allowed_actions": next_actions,
        "safety": {
            "merge_performed": False,
            "deploy_performed": False,
            "rollback_performed": False,
            "branch_moved": False,
        },
    }
    if schedule_error:
        result["ci_request_schedule_error"] = dict(schedule_error)
    return result


async def _advance_index_and_analysis(
    github_call: Callable[..., Awaitable[Any]],
    service: Any,
    snapshot: dict[str, Any],
    session: Mapping[str, Any],
    *,
    base_sha: str,
    idempotency_key: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    head_sha = str(snapshot["head_sha"])
    tree_sha = str(snapshot["tree_sha"])
    index_error = None
    try:
        index_status = await _invoke(
            github_call,
            mygithub12.get_index_status,
            service,
            snapshot["repository"],
            head_sha,
        )
    except Exception as exc:
        index_status = {
            "status": "unknown",
            "repository": snapshot["repository"],
            "commit_sha": head_sha,
            "tree_sha": None,
        }
        index_error = _error_evidence("index_status", exc)
    if not isinstance(index_status, dict):
        index_status = {
            "status": "unknown",
            "repository": snapshot["repository"],
            "commit_sha": head_sha,
            "tree_sha": None,
        }
    index_request = None
    if not _index_is_exact_ready(index_status, head_sha, tree_sha):
        if snapshot.get("index_job_id"):
            index_error = (
                {
                    "stage": "index",
                    "code": "INDEX_NOT_READY",
                    "message": "exact HEAD Index remains pending",
                }
                if index_status.get("status") not in {"failed", "cancelled"}
                else {
                    "stage": "index",
                    "code": str(index_status.get("error_code") or "INDEX_BUILD_FAILED"),
                    "message": str(index_status.get("error_message") or "Index build failed"),
                }
            )
        else:
            try:
                index_request = await _invoke(
                    github_call,
                    mygithub12.request_index_build,
                    service,
                    snapshot["repository"],
                    head_sha,
                    "auto",
                    base_sha,
                    "interactive",
                    f"converge-index:{snapshot['development_session_id']}:{head_sha}:{idempotency_key or 'default'}",
                    False,
                )
            except Exception as exc:
                index_error = _error_evidence("index", exc)
            if isinstance(index_request, dict):
                index_job_id = index_request.get("job_id")
                if index_job_id:
                    snapshot = _bind_with_replay(
                        snapshot,
                        convergence_store.bind_index_job,
                        str(index_job_id),
                    )
                if _index_is_exact_ready(index_request, head_sha, tree_sha):
                    index_status = index_request
                elif index_request.get("status") in {"failed", "cancelled"}:
                    index_error = {
                        "stage": "index",
                        "code": str(index_request.get("error_code") or "INDEX_BUILD_FAILED"),
                        "message": str(index_request.get("error_message") or "Index build failed"),
                    }
                elif index_request.get("status"):
                    # The request snapshot is the best immediate fact when
                    # the previous status read failed; keep it pending rather
                    # than falsely recording an Index failure.
                    index_status = index_request
                    index_error = None
    exact_ready = _index_is_exact_ready(index_status, head_sha, tree_sha)
    if exact_ready:
        snapshot = _record_analysis_with_replay(
            snapshot,
            stage="index",
            state="ready",
            resource_identity=_canonical_digest(
                {
                    "repository": snapshot["repository"],
                    "commit_sha": head_sha,
                    "tree_sha": tree_sha,
                    "index_version": index_status.get("index_version"),
                }
            ),
        )
    elif index_error and index_error.get("code") != "INDEX_NOT_READY":
        snapshot = _record_analysis_with_replay(
            snapshot,
            stage="index",
            state="failed",
            error_code=str(index_error.get("code") or "INDEX_BUILD_FAILED"),
            error_message=str(index_error.get("message") or "Index build failed"),
        )

    if not exact_ready:
        if _phase_rank(str(snapshot.get("phase") or "")) < _phase_rank("index_requested"):
            snapshot = _transition_if_needed(
                snapshot,
                "index_requested",
                status="pending",
                event_type="index_requested" if index_request else "index_pending",
                metadata={
                    "index_job_id": snapshot.get("index_job_id"),
                    "index_status": index_status.get("status"),
                },
            )
        pending = [
            {
                "stage": "index",
                "code": "INDEX_NOT_READY",
                "message": "exact HEAD Repository Index is not ready; analysis remains pending",
            }
        ]
        degraded = (
            [index_error]
            if index_error and index_error.get("code") != "INDEX_NOT_READY"
            else []
        )
        if _phase_rank(str(snapshot.get("phase") or "")) < _phase_rank("analysis_pending"):
            snapshot = _transition_if_needed(
                snapshot,
                "analysis_pending",
                status="degraded" if degraded else "pending",
                event_type="analysis_pending",
                metadata={"index_status": index_status.get("status")},
            )
        analysis = _analysis_result_details(
            {},
            repository=str(snapshot["repository"]),
            index_status=index_status,
            index_request=index_request,
            head_sha=head_sha,
            tree_sha=tree_sha,
            base_sha=base_sha,
            degraded_reasons=degraded,
            pending_reasons=pending,
            analysis_resource=None,
            analysis_states=snapshot.get("analysis") or {},
        )
        index = {
            "ready": False,
            "status": index_status.get("status"),
            "job_id": snapshot.get("index_job_id"),
            "commit_sha": index_status.get("commit_sha"),
            "tree_sha": index_status.get("tree_sha"),
            "request": index_request,
        }
        return snapshot, index, analysis

    existing_states = snapshot.get("analysis") or {}
    details, degraded_reasons = _run_analysis_callbacks(
        service,
        session,
        base_sha,
        existing_states=existing_states,
    )
    resource = _analysis_resource(
        session=session,
        base_sha=base_sha,
        index_status=index_status,
        index_request=index_request,
        details=details,
        degraded_reasons=degraded_reasons,
    )
    for stage, public_key in _ANALYSIS_CALLBACKS:
        if public_key not in details:
            continue
        stage_result = details.get(public_key)
        if not isinstance(stage_result, dict):
            stage_result = {"ok": True, "value": stage_result}
        state = _analysis_failure_state(stage_result)
        error = (
            stage_result.get("error")
            if isinstance(stage_result.get("error"), dict)
            else {}
        )
        snapshot = _record_analysis_with_replay(
            snapshot,
            stage=stage,
            state=state,
            resource_uri=(resource or {}).get("resource_uri") if resource else None,
            resource_identity=_canonical_digest(stage_result),
            error_code=(
                str(error.get("code"))
                if state == "failed" and error.get("code")
                else None
            ),
            error_message=(
                str(error.get("message"))
                if state == "failed" and error.get("message")
                else None
            ),
        )
    all_analysis_ready = all(
        (snapshot.get("analysis") or {}).get(stage, {}).get("state") == "ready"
        for stage in convergence_store.ANALYSIS_STAGES
    )
    # Once the validation track has its own phase, keep ``status`` truthful to
    # the durable CI Request lifecycle. Analysis readiness remains in its
    # per-stage durable rows and the public analysis snapshot; it must not
    # briefly overwrite ``accepted``/``running`` and then be reset by CI
    # observation in the same call.
    if _phase_rank(str(snapshot.get("phase") or "")) < _phase_rank("ci_requested"):
        snapshot = _transition_if_needed(
            snapshot,
            "analysis_pending",
            status="ready" if all_analysis_ready else (
                "degraded" if degraded_reasons else "pending"
            ),
            event_type="analysis_ready" if all_analysis_ready else "analysis_pending",
            metadata={"required_stages": list(convergence_store.ANALYSIS_STAGES)},
        )
    current_pending, durable_degraded = _analysis_readiness_reasons(snapshot)
    for reason in durable_degraded:
        if reason not in degraded_reasons:
            degraded_reasons.append(reason)
    analysis = _analysis_result_details(
        details,
        repository=str(snapshot["repository"]),
        index_status=index_status,
        index_request=index_request,
        head_sha=head_sha,
        tree_sha=tree_sha,
        base_sha=base_sha,
        degraded_reasons=degraded_reasons,
        pending_reasons=current_pending,
        analysis_resource=resource,
        analysis_states=snapshot.get("analysis") or {},
    )
    index = {
        "ready": True,
        "status": index_status.get("status"),
        "job_id": snapshot.get("index_job_id"),
        "commit_sha": index_status.get("commit_sha"),
        "tree_sha": index_status.get("tree_sha"),
        "index_version": index_status.get("index_version"),
        "request": index_request,
    }
    return snapshot, index, analysis


async def _finalize_ci_state(
    snapshot: dict[str, Any],
    session: Mapping[str, Any],
    mode: str,
    request: Mapping[str, Any],
    validation: dict[str, Any],
    analysis: Mapping[str, Any],
    *,
    include_failure_pack: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    del session, include_failure_pack
    status = _ci_request_status(request)
    if not _ci_request_terminal(request):
        target = "ci_running" if status == "running" else "ci_requested"
        snapshot = _transition_if_needed(
            snapshot,
            target,
            status=status,
            event_type="ci_request_observed",
            metadata={
                "ci_request_id": request.get("request_id"),
                "ci_request_phase": request.get("phase"),
                "ci_request_revision": request.get("revision"),
            },
        )
        validation = _validation_snapshot(
            request,
            ci_job_id=snapshot.get("ci_job_id"),
            attestation_id=None,
            failure_pack_id=None,
        )
        return snapshot, validation

    attestation_id, failure_pack_id = _extract_evidence(request)
    if status == "passed" and mode == "full" and not attestation_id:
        # A terminal Request without its required full-gate attestation is a
        # terminal CI fact, not a convergence success. The next call can
        # consume a controller retry/materialized evidence without rerunning CI.
        snapshot = _transition_if_needed(
            snapshot,
            "post_ci_finalize",
            status="pending",
            event_type="post_ci_evidence_pending",
            metadata={"reason": "FULL_ATTESTATION_REQUIRED"},
        )
        validation = _validation_snapshot(
            request,
            ci_job_id=snapshot.get("ci_job_id"),
            attestation_id=None,
            failure_pack_id=failure_pack_id,
        )
        return snapshot, validation

    if status != "passed":
        if failure_pack_id:
            snapshot = _record_terminal_evidence_if_needed(
                snapshot,
                attestation_id=None,
                failure_pack_id=failure_pack_id,
            )
        snapshot = _transition_if_needed(
            snapshot,
            "failed",
            status="failed",
            error_code=str(request.get("preflight_error_code") or "CI_FAILED"),
            error_message=str(
                request.get("terminal_reason")
                or f"CI Request reached terminal status {status}"
            ),
            event_type="ci_failed",
            metadata={
                "ci_request_id": request.get("request_id"),
                "ci_status": status,
            },
        )
        validation = _validation_snapshot(
            request,
            ci_job_id=snapshot.get("ci_job_id"),
            attestation_id=None,
            failure_pack_id=failure_pack_id,
        )
        return snapshot, validation

    # A passed fast CI needs no attestation; full CI does, and the branch above
    # has already fail-closed if it is absent.
    snapshot = _record_terminal_evidence_if_needed(
        snapshot,
        attestation_id=attestation_id,
        failure_pack_id=failure_pack_id,
    )
    analysis_complete = bool(
        analysis.get("index", {}).get("ready")
        and not analysis.get("pending")
        and not analysis.get("degraded")
        and all(
            (snapshot.get("analysis") or {}).get(stage, {}).get("state") == "ready"
            for stage in convergence_store.ANALYSIS_STAGES
        )
    )
    if not analysis_complete:
        snapshot = _transition_if_needed(
            snapshot,
            "post_ci_finalize",
            status="degraded" if analysis.get("degraded") else "pending",
            event_type="post_ci_analysis_pending",
            metadata={
                "analysis_pending": bool(analysis.get("pending")),
                "analysis_degraded": bool(analysis.get("degraded")),
            },
        )
    else:
        snapshot = _transition_if_needed(
            snapshot,
            "post_ci_finalize",
            status="finalizing",
            event_type="post_ci_finalize",
            metadata={"ci_request_id": request.get("request_id")},
        )
        snapshot = _transition_if_needed(
            snapshot,
            "passed",
            status="passed",
            event_type="convergence_passed",
            metadata={
                "ci_request_id": request.get("request_id"),
                "attestation_id": attestation_id,
            },
        )
    validation = _validation_snapshot(
        request,
        ci_job_id=snapshot.get("ci_job_id"),
        attestation_id=attestation_id,
        failure_pack_id=failure_pack_id,
    )
    return snapshot, validation


async def converge_task(
    github_call: Callable[..., Awaitable[Any]],
    service: Any,
    development_session_id: str,
    expected_session_revision: int,
    mode: str = "full",
    base_sha: str = "",
    index_wait_seconds: int = 55,
    wait_seconds: int = 55,
    force_rerun: bool = False,
    supersede_previous: bool = True,
    include_failure_pack: bool = True,
    idempotency_key: str = "",
    convergence_id: str = "",
) -> dict[str, Any]:
    """Advance one durable convergence without waiting for external work."""
    del index_wait_seconds, wait_seconds, force_rerun
    if mode not in convergence_store.VALID_MODES:
        raise MyGithub12Error(
            "DEVELOPMENT_SESSION_STATE_INVALID",
            "convergence mode must be fast or full",
            {"mode": mode},
        )

    # Preserve the explicit Session CAS gate. A stale caller must not create
    # or mutate a convergence under a newer Session revision.
    sessions._require_revision(development_session_id, expected_session_revision)
    session = sessions.get_session(development_session_id)
    resolved_base = base_sha or session["base_commit_sha"]
    # The public wrapper makes the caller key optional. A deterministic
    # session/mode key keeps ordinary cross-window retries on one Run while
    # making a new HEAD/Tree or Session/Workspace identity an explicit
    # idempotency conflict that can block the old Run for recovery.
    convergence_key = idempotency_key or f"session:{development_session_id}:{mode}"

    if convergence_id:
        snapshot = convergence_store.get_convergence(convergence_id)
    else:
        try:
            snapshot = convergence_store.create_or_get_convergence(
                repository=session["repository"],
                branch=session["branch"],
                development_session_id=development_session_id,
                workspace_id=session["workspace_id"],
                session_revision=int(session["session_revision"]),
                workspace_revision=int(session["workspace_revision"]),
                head_sha=session["head_commit_sha"],
                tree_sha=session["tree_sha"],
                base_branch=session["base_branch"],
                base_sha=resolved_base,
                mode=mode,
                idempotency_key=convergence_key,
            )
            if snapshot.get("deduplicated"):
                observability.observe_idempotency("convergence", "reuse")
            else:
                observability.observe_convergence_phase_entry("accepted")
        except MyGithub12Error as exc:
            if exc.code == "IDEMPOTENCY_CONFLICT":
                observability.observe_idempotency("convergence", "conflict")
            # An identity conflict can be the old active run after a branch or
            # Session drift. Mark that old run blocked when its identity is
            # available instead of silently starting a new run under the key.
            old_id = (exc.details or {}).get("convergence_id")
            if old_id and exc.code in {
                "DEVELOPMENT_CONVERGENCE_IDENTITY_MISMATCH",
                "IDEMPOTENCY_CONFLICT",
            }:
                snapshot = convergence_store.get_convergence(str(old_id))
                if not snapshot.get("terminal"):
                    snapshot = _transition_if_needed(
                        snapshot,
                        "blocked",
                        status="blocked",
                        error_code="DEVELOPMENT_SESSION_RECOVERY_REQUIRED",
                        error_message="convergence request identity drifted",
                        event_type="convergence_blocked_drift",
                        metadata={"cause_code": exc.code, "cause_details": exc.details},
                    )
                return _response(
                    snapshot,
                    session,
                    mode=mode,
                    index={},
                    analysis={"pending": True, "degraded": False},
                    request=None,
                    validation=_validation_snapshot(
                        None,
                        ci_job_id=snapshot.get("ci_job_id"),
                        attestation_id=snapshot.get("attestation_id"),
                        failure_pack_id=snapshot.get("failure_pack_id"),
                    ),
                    recovery_required=True,
                )
            raise

    if snapshot.get("development_session_id") != development_session_id:
        raise MyGithub12Error(
            "DEVELOPMENT_SESSION_RECOVERY_REQUIRED",
            "convergence belongs to a different Development Session",
            {"recovery_required": True},
        )
    # Once a Run exists, its durable base is authoritative for every replay;
    # a later caller cannot silently analyze or validate against another base.
    resolved_base = str(snapshot.get("base_sha") or resolved_base)
    if snapshot.get("terminal"):
        request = (
            ci_request_store.get_ci_request(str(snapshot["ci_request_id"]))
            if snapshot.get("ci_request_id")
            else None
        )
        validation = _validation_snapshot(
            request,
            ci_job_id=snapshot.get("ci_job_id"),
            attestation_id=snapshot.get("attestation_id"),
            failure_pack_id=snapshot.get("failure_pack_id"),
        )
        return _response(
            snapshot,
            session,
            mode=mode,
            index={"job_id": snapshot.get("index_job_id")},
            analysis={"readiness": snapshot.get("analysis", {})},
            request=request,
            validation=validation,
        )

    try:
        await _verify_current_identity(github_call, service, session, snapshot)
    except MyGithub12Error as exc:
        if not snapshot.get("terminal"):
            snapshot = _transition_if_needed(
                snapshot,
                "blocked",
                status="blocked",
                error_code=exc.code,
                error_message=exc.message,
                event_type="convergence_blocked_drift",
                metadata={"details": exc.details},
            )
        return _response(
            snapshot,
            session,
            mode=mode,
            index={"job_id": snapshot.get("index_job_id")},
            analysis={"pending": True, "degraded": False},
            request=None,
            validation=_validation_snapshot(
                None,
                ci_job_id=snapshot.get("ci_job_id"),
                attestation_id=snapshot.get("attestation_id"),
                failure_pack_id=snapshot.get("failure_pack_id"),
            ),
            recovery_required=True,
        )

    snapshot, index, analysis = await _advance_index_and_analysis(
        github_call,
        service,
        snapshot,
        session,
        base_sha=resolved_base,
        idempotency_key=convergence_key,
    )
    try:
        snapshot, request, schedule_error, ci_job_id, _ = await _advance_ci_track(
            github_call,
            snapshot,
            session,
            mode,
            supersede_previous=supersede_previous,
        )
    except MyGithub12Error as exc:
        if exc.code == "DEVELOPMENT_SESSION_RECOVERY_REQUIRED":
            snapshot = _transition_if_needed(
                snapshot,
                "blocked",
                status="blocked",
                error_code=exc.code,
                error_message=exc.message,
                event_type="convergence_blocked_ci_identity",
                metadata={"details": exc.details},
            )
            return _response(
                snapshot,
                session,
                mode=mode,
                index=index,
                analysis=analysis,
                request=None,
                validation=_validation_snapshot(
                    None,
                    ci_job_id=snapshot.get("ci_job_id"),
                    attestation_id=snapshot.get("attestation_id"),
                    failure_pack_id=snapshot.get("failure_pack_id"),
                ),
                recovery_required=True,
            )
        snapshot = _transition_if_needed(
            snapshot,
            "failed",
            status="failed",
            error_code=str(getattr(exc, "code", "CI_REQUEST_CREATE_FAILED")),
            error_message=str(getattr(exc, "message", str(exc))),
            event_type="ci_request_failed",
            metadata={"cause_type": type(exc).__name__},
        )
        return _response(
            snapshot,
            session,
            mode=mode,
            index=index,
            analysis=analysis,
            request=None,
            validation=_validation_snapshot(
                None,
                ci_job_id=snapshot.get("ci_job_id"),
                attestation_id=None,
                failure_pack_id=None,
            ),
        )
    if ci_job_id and not snapshot.get("ci_job_id"):
        snapshot = _bind_with_replay(
            snapshot,
            convergence_store.bind_ci_job,
            ci_request_id=str(request["request_id"]),
            ci_job_id=str(ci_job_id),
        )
    snapshot, validation = await _finalize_ci_state(
        snapshot,
        session,
        mode,
        request,
        _validation_snapshot(
            request,
            ci_job_id=snapshot.get("ci_job_id") or ci_job_id,
            attestation_id=_extract_evidence(request)[0],
            failure_pack_id=_extract_evidence(request)[1],
        ),
        analysis,
        include_failure_pack=include_failure_pack,
    )
    return _response(
        snapshot,
        session,
        mode=mode,
        index=index,
        analysis=analysis,
        request=request,
        validation=validation,
        schedule_error=schedule_error,
    )
