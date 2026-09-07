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
) -> dict[str, Any]:
    """Converge exact-head analysis and CI without merge, deploy, rollback, or branch movement."""
    if mode not in {"fast", "full"}:
        raise MyGithub12Error(
            "DEVELOPMENT_SESSION_STATE_INVALID",
            "convergence mode must be fast or full",
            {"mode": mode},
        )

    # AC-CONV-02: the caller must hold the exact Session revision. Recovery may
    # happen only after this CAS gate, never as a stale-revision bypass.
    sessions._require_revision(development_session_id, expected_session_revision)
    session = sessions.get_session(development_session_id)
    maintenance = await github_call(
        dx.maybe_auto_renew_session_workspace,
        service,
        development_session_id,
        expected_session_revision,
        int(session["workspace_revision"]),
        session["head_commit_sha"],
        idempotency_key,
    )
    session = maintenance["session"]
    workspace = maintenance["workspace"]
    effective_session_revision = int(session["session_revision"])
    await github_call(
        mygithub12.workspace_write_preflight,
        service,
        session["repository"],
        session["branch"],
        session["head_commit_sha"],
        session["workspace_id"],
        int(workspace["revision"]),
    )
    resolved_base = base_sha or session["base_commit_sha"]
    analysis = await github_call(
        convergence_analysis,
        service,
        session,
        resolved_base,
        index_wait_seconds,
        idempotency_key,
    )
    prepared = await github_call(dx.validation_preflight, service, session, mode, resolved_base)
    if mode == "full" and prepared.get("profile") != "repo-auto-check":
        raise MyGithub12Error(
            "CI_PROFILE_DISCOVERY_MISMATCH",
            "full convergence must use repo-auto-check",
            {"actual_profile": prepared.get("profile")},
        )

    lease_maintenance = {
        "renewed": bool(maintenance.get("renewed")),
        "remaining_seconds": maintenance.get("remaining_seconds"),
        "audit": maintenance.get("audit"),
        "recovery": maintenance.get("recovery"),
    }
    phase = "validating_fast" if mode == "fast" else "validating_full"
    phase_session = await github_call(
        sessions.transition,
        development_session_id,
        effective_session_revision,
        phase,
        event_type="convergence_validation_started",
        allowed_from={"active", "pr_ready", "validating_fast", "validating_full"},
    )
    try:
        job, selection = await github_call(
            dx.start_validation_job,
            service,
            phase_session,
            mode,
            resolved_base,
            force_rerun,
            supersede_previous,
            prepared,
        )
    except Exception as start_exc:
        rollback = None
        rollback_error = None
        try:
            rollback = await github_call(
                sessions.transition,
                development_session_id,
                phase_session["session_revision"],
                session["status"],
                event_type="convergence_validation_start_failed",
                allowed_from={phase},
            )
        except Exception as rollback_exc:
            rollback_error = type(rollback_exc).__name__
        if isinstance(start_exc, MyGithub12Error):
            start_exc.details.update(
                {
                    "validation_state_rolled_back": bool(rollback),
                    "rollback_error_type": rollback_error,
                }
            )
            raise
        raise MyGithub12Error(
            "PRIVATE_CI_UNAVAILABLE",
            "convergence could not start private CI",
            {
                "validation_state_rolled_back": bool(rollback),
                "rollback_error_type": rollback_error,
                "cause_type": type(start_exc).__name__,
            },
        ) from start_exc

    try:
        await github_call(
            sessions.record_validation,
            development_session_id,
            phase_session["session_revision"],
            mode,
            phase_session["head_commit_sha"],
            phase_session["tree_sha"],
            job_id=job["job_id"],
            status=job.get("status") or "queued",
            evidence={"selection": selection},
        )
    except Exception as correlate_exc:
        raise MyGithub12Error(
            "DEVELOPMENT_SESSION_RECOVERY_REQUIRED",
            "private CI started but its validation correlation could not be persisted",
            {
                "validation_started": True,
                "recovery_required": True,
                "failed_stage": "validation_correlate",
                "job_id": job.get("job_id"),
                "job_status": job.get("status"),
                "cause_type": type(correlate_exc).__name__,
            },
        ) from correlate_exc

    result = None
    try:
        job = await github_call(dx.wait_validation, job["job_id"], wait_seconds)
        result = await github_call(
            dx.validation_result,
            development_session_id,
            phase_session["session_revision"],
            mode,
            job,
            selection,
            include_failure_pack,
        )
        fields = {
            "last_fast_ci_job_id" if mode == "fast" else "last_full_ci_job_id": job["job_id"]
        }
        if analysis.get("index", {}).get("ready"):
            fields["index_commit_sha"] = session["head_commit_sha"]
        if isinstance(result.get("attestation"), dict) and result["attestation"].get("attestation_id"):
            fields["last_attestation_id"] = result["attestation"]["attestation_id"]
        if isinstance(result.get("failure_pack"), dict) and result["failure_pack"].get("resource_uri"):
            fields["last_failure_resource_uri"] = result["failure_pack"]["resource_uri"]
        next_status = (
            ("pr_ready" if result.get("merge_eligible") else "active")
            if result.get("terminal")
            else phase
        )
        final_session = await github_call(
            sessions.transition,
            development_session_id,
            phase_session["session_revision"],
            next_status,
            event_type="convergence_observed",
            allowed_from={phase},
            fields=fields,
        )
    except Exception as observe_exc:
        details = {
            "validation_started": True,
            "recovery_required": True,
            "job_id": job.get("job_id") if isinstance(job, dict) else None,
            "job_status": job.get("status") if isinstance(job, dict) else None,
            "validation_result": result,
            "cause_type": type(observe_exc).__name__,
        }
        if isinstance(observe_exc, MyGithub12Error):
            observe_exc.details.update(details)
            raise
        raise MyGithub12Error(
            "DEVELOPMENT_SESSION_RECOVERY_REQUIRED",
            "convergence validation completed but observation could not be finalized",
            details,
        ) from observe_exc

    worker_final = await github_call(
        wait_worker_final_state,
        job["job_id"],
        5 if result.get("terminal") else 0,
    )
    merge_eligibility: dict[str, Any] = {
        "ci_merge_eligible": bool(result.get("merge_eligible")),
        "ready": False,
        "blocking_reasons": [],
        "readiness": None,
    }
    if final_session.get("pull_number"):
        try:
            readiness = await github_call(
                github_utils.get_github_pull_request_merge_readiness,
                final_session["repository"],
                int(final_session["pull_number"]),
                final_session["head_commit_sha"],
                job["job_id"] if mode == "full" and result.get("merge_eligible") else "",
                final_session["base_branch"],
            )
            merge_eligibility.update(
                {
                    "ready": bool(readiness.get("ready")),
                    "blocking_reasons": list(
                        readiness.get("blocking") or readiness.get("blocking_reasons") or []
                    ),
                    "readiness": readiness,
                }
            )
        except Exception as exc:
            merge_eligibility["blocking_reasons"] = ["READINESS_UNAVAILABLE"]
            merge_eligibility["readiness_error"] = _error_evidence("readiness", exc)
    else:
        merge_eligibility["blocking_reasons"] = ["PULL_REQUEST_REQUIRED"]

    terminal_pass = bool(result.get("terminal") and result.get("job", {}).get("status") == "passed")
    converged = bool(terminal_pass and not analysis.get("degraded") and worker_final.get("released"))
    if not result.get("terminal"):
        next_allowed_actions = ["converge_development_task"]
    elif result.get("job", {}).get("status") != "passed":
        next_allowed_actions = ["inspect_failure_pack"]
    elif analysis.get("degraded"):
        next_allowed_actions = ["inspect_convergence_resource", "converge_development_task"]
    elif mode == "fast":
        next_allowed_actions = ["run_full_convergence"]
    elif not final_session.get("pull_number"):
        next_allowed_actions = ["prepare_pr"]
    else:
        next_allowed_actions = ["readiness"]

    return {
        "ok": True,
        "converged": converged,
        "mode": mode,
        "development_session": final_session,
        "lease_maintenance": lease_maintenance,
        "exact_head": analysis["identity"],
        "analysis": analysis,
        "validation": result,
        "worker_final_state": worker_final,
        "merge_eligibility": merge_eligibility,
        "next_allowed_actions": next_allowed_actions,
        "safety": {
            "merge_performed": False,
            "deploy_performed": False,
            "rollback_performed": False,
            "branch_moved": False,
        },
    }
