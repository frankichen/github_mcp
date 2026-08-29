"""DX-2 post-write convergence orchestration for Development Sessions."""
from __future__ import annotations

import time
from typing import Any, Awaitable, Callable

from app import development_orchestrator as dx
from app import development_session_store as sessions
from app import github_utils, mygithub12
from app.ci_database import get_job, get_workers, reconcile_stale_workers
from app.mcp_response import store_response_resource


MyGithub12Error = mygithub12.MyGithub12Error
_TERMINAL_CI = {"passed", "failed", "timed_out", "cancelled", "superseded"}


def _error_evidence(stage: str, exc: Exception) -> dict[str, Any]:
    return {
        "stage": stage,
        "code": str(getattr(exc, "code", "INTERNAL_ERROR")),
        "message": str(getattr(exc, "message", "convergence analysis failed")),
        "type": type(exc).__name__,
    }


def _index_is_exact_ready(index_status: dict[str, Any], head_sha: str, tree_sha: str) -> bool:
    return bool(
        index_status.get("status") == "ready"
        and index_status.get("commit_sha") == head_sha
        and index_status.get("tree_sha") == tree_sha
    )


def convergence_analysis(
    service: Any,
    session: dict[str, Any],
    base_sha: str = "",
    index_wait_seconds: int = 55,
    idempotency_key: str = "",
) -> dict[str, Any]:
    """Collect exact-head analysis while degrading conservatively instead of narrowing full CI."""
    repository = session["repository"]
    head_sha = session["head_commit_sha"]
    tree_sha = session["tree_sha"]
    resolved_base = base_sha or session["base_commit_sha"]
    identity = mygithub12.resolve_identity(service, repository, commit_sha=head_sha)
    if identity.get("tree_sha") != tree_sha:
        raise MyGithub12Error(
            "DEVELOPMENT_SESSION_RECOVERY_REQUIRED",
            "exact HEAD Tree differs from the Development Session",
            {"session_tree": tree_sha, "actual_tree": identity.get("tree_sha"), "head_sha": head_sha},
        )

    index_status = mygithub12.get_index_status(service, repository, head_sha)
    index_request = None
    index_wait = None
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
        job_id = index_request.get("job_id") if isinstance(index_request, dict) else None
        if job_id and int(index_wait_seconds) > 0:
            index_wait = mygithub12.wait_index_job(
                job_id,
                min(max(int(index_wait_seconds), 0), 55),
                int(index_request.get("revision", 0) or 0),
                str(index_request.get("status") or ""),
                str(index_request.get("step") or ""),
            )
        index_status = mygithub12.get_index_status(service, repository, head_sha)

    degraded_reasons: list[dict[str, Any]] = []
    index_ready = _index_is_exact_ready(index_status, head_sha, tree_sha)
    if not index_ready:
        degraded_reasons.append(
            {
                "stage": "index",
                "code": "INDEX_NOT_READY",
                "message": "exact HEAD Repository Index is not ready; full CI must remain conservative",
            }
        )

    task = str((session.get("metadata") or {}).get("task_name") or "development task convergence")
    details: dict[str, Any] = {}
    stages = (
        (
            "change_context",
            lambda: mygithub12.change_context_pack(
                service, repository, resolved_base, head_sha, task, 50, 1024 * 1024
            ),
        ),
        ("impact", lambda: mygithub12.change_impact(service, repository, resolved_base, head_sha)),
        ("contracts", lambda: mygithub12.contract_changes(service, repository, resolved_base, head_sha)),
        (
            "affected_tests",
            lambda: mygithub12.affected_tests(service, repository, head_sha, resolved_base),
        ),
    )
    for stage, callback in stages:
        try:
            details[stage] = callback()
        except Exception as exc:
            error = _error_evidence(stage, exc)
            details[stage] = {"ok": False, "error": error}
            degraded_reasons.append(error)

    impact = details.get("impact") if isinstance(details.get("impact"), dict) else {}
    if impact.get("ok") is not False and impact.get("complete") is not True:
        degraded_reasons.append(
            {
                "stage": "impact",
                "code": "IMPACT_ANALYSIS_INCOMPLETE",
                "message": "change impact is incomplete; full CI must remain conservative",
            }
        )

    full_evidence = {
        "identity": {"repository": repository, "commit_sha": head_sha, "tree_sha": tree_sha},
        "base_sha": resolved_base,
        "index": {"status": index_status, "request": index_request, "wait": index_wait},
        "analysis": details,
        "degraded_reasons": degraded_reasons,
    }
    resource = None
    try:
        resource = store_response_resource(full_evidence)
    except Exception as exc:
        degraded_reasons.append(_error_evidence("analysis_resource", exc))

    change_context = details.get("change_context") if isinstance(details.get("change_context"), dict) else {}
    contracts = details.get("contracts") if isinstance(details.get("contracts"), dict) else {}
    affected = details.get("affected_tests") if isinstance(details.get("affected_tests"), dict) else {}
    compact_index_request = None
    if isinstance(index_request, dict):
        compact_index_request = {
            key: index_request.get(key)
            for key in ("job_id", "status", "strategy", "revision", "step", "deduplicated")
            if key in index_request
        }
    return {
        "identity": {"repository": repository, "commit_sha": head_sha, "tree_sha": tree_sha},
        "base_sha": resolved_base,
        "index": {
            "ready": index_ready,
            "status": index_status.get("status"),
            "commit_sha": index_status.get("commit_sha"),
            "tree_sha": index_status.get("tree_sha"),
            "index_version": index_status.get("index_version"),
            "request": compact_index_request,
        },
        "change_context": {
            "ok": change_context.get("ok") is not False,
            "items_total": len(change_context.get("items") or []),
            "omitted_count": int(change_context.get("omitted_count", 0) or 0),
        },
        "impact": {
            "ok": impact.get("ok") is not False,
            "complete": impact.get("complete") is True,
            "changed_paths": list(impact.get("changed_paths") or [])[:100],
            "affected_modules": list(impact.get("affected_modules") or [])[:100],
            "affected_test_count": len(impact.get("affected_tests") or []),
            "contract_change_count": len(impact.get("contract_changes") or []),
        },
        "contracts": {
            "ok": contracts.get("ok") is not False,
            "summary": contracts.get("summary") or {},
            "changes": list(contracts.get("changes") or [])[:50],
        },
        "affected_tests": {
            "ok": affected.get("ok") is not False,
            "authoritative": bool(affected.get("authoritative", False)),
            "tests": list(affected.get("tests") or [])[:100],
        },
        "degraded": bool(degraded_reasons),
        "degraded_reasons": degraded_reasons,
        "conservative_ci_required": bool(degraded_reasons),
        "analysis_resource": (
            {
                "resource_uri": resource["resource_uri"],
                "total_bytes": resource["total_bytes"],
                "content_sha256": resource["sha256"],
            }
            if resource
            else None
        ),
    }


def wait_worker_final_state(job_id: str, wait_seconds: int = 5) -> dict[str, Any]:
    """Boundedly prove that a terminal CI job no longer occupies its Worker."""
    job = get_job(job_id)
    if not job:
        raise MyGithub12Error("PRIVATE_CI_JOB_NOT_FOUND", "private CI job disappeared", {"job_id": job_id})
    worker_id = job.get("worker_id")
    terminal = job.get("status") in _TERMINAL_CI
    if not worker_id:
        return {
            "worker_id": None,
            "terminal": terminal,
            "released": False,
            "idle": False,
            "reason": "worker_not_recorded",
        }

    deadline = time.monotonic() + min(max(int(wait_seconds), 0), 5)
    worker = None
    while True:
        reconcile_stale_workers()
        worker = next((item for item in get_workers() if item.get("worker_id") == worker_id), None)
        if worker is None:
            break
        if worker.get("current_job") != job_id:
            break
        if not terminal or time.monotonic() >= deadline:
            break
        time.sleep(0.1)

    if worker is None:
        return {
            "worker_id": worker_id,
            "terminal": terminal,
            "released": False,
            "idle": False,
            "reason": "worker_not_registered",
        }
    released = worker.get("current_job") != job_id
    return {
        "worker_id": worker_id,
        "terminal": terminal,
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
