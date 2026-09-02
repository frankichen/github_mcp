"""DX2 resume helpers for recovering a branch or PR development context.

This module is intentionally a high-level read/recovery aggregator.  It does
not move branches, create PRs, merge, close, delete branches, or run CI.  The
only mutating behavior is guarded Session/Workspace recovery through existing
Workspace/Session CAS paths when the configured safe-recovery path permits it
and the existing DX2-SESSION guards prove the state is stale rather
than drifted.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from app import development_orchestrator as dx
from app import development_session_store as sessions
from app import attestation_registry, github_utils, mygithub12
from app.github_policy import repository_is_allowed
from app.ci_repository_config import is_private_ci_enabled, is_test_deploy_enabled, is_self_deploy_enabled
from app.ci_database import get_job as db_get_job, list_jobs as db_list_jobs
from app.ci_mcp import build_private_ci_job_list_item

MyGithub12Error = mygithub12.MyGithub12Error
ACTIVE_SESSION_STATUSES = {"active", "pr_ready"}
BLOCKED_SESSION_STATUSES = {"blocked", "drifted", "closing", "validating_fast", "validating_full"}
TRANSIENT_VALIDATION_STATUSES = {"validating_fast": "fast", "validating_full": "full"}
VALIDATION_IN_PROGRESS_STATUSES = {"queued", "leased", "downloading", "preparing", "running", "cancel_requested"}


def _raise_result_error(result: dict[str, Any], default_code: str, default_message: str) -> None:
    if isinstance(result, dict) and result.get("ok") is False:
        err = result.get("error") if isinstance(result.get("error"), dict) else {}
        raise MyGithub12Error(
            str(err.get("code") or default_code),
            str(err.get("message") or default_message),
            dict(err.get("details") or {}),
        )


def _public_session_row(row: sqlite3.Row) -> dict[str, Any]:
    # Use the public getter instead of copying private serialization details.
    return sessions.get_session(str(row["session_id"]))


def find_sessions_for_workspace(workspace_id: str, *, include_terminal: bool = False, limit: int = 20) -> list[dict[str, Any]]:
    """Return bounded sessions for one Workspace ordered by most recent update."""
    sessions.init_session_db()
    limit = max(1, min(int(limit or 20), 100))
    if include_terminal:
        sql = "SELECT * FROM development_sessions WHERE workspace_id=? ORDER BY updated_at DESC LIMIT ?"
        args = (workspace_id, limit)
    else:
        active_states = tuple(sessions.ACTIVE_STATES)
        placeholders = ",".join("?" for _ in active_states)
        sql = f"SELECT * FROM development_sessions WHERE workspace_id=? AND status IN ({placeholders}) ORDER BY updated_at DESC LIMIT ?"
        args = (workspace_id, *active_states, limit)
    with sessions._db() as db:  # The store owns this SQLite connection API.
        rows = db.execute(sql, args).fetchall()
    return [_public_session_row(row) for row in rows]


def _repository_policy(repository: str) -> dict[str, Any]:
    return {
        "ok": True,
        "repository": repository,
        "policy": {
            "github": repository_is_allowed(repository),
            "private_ci": is_private_ci_enabled(repository),
            "test_deploy": is_test_deploy_enabled(repository),
            "self_deploy": is_self_deploy_enabled(repository),
        },
    }


def _current_main(service: Any, repository: str) -> dict[str, Any]:
    repo = service.client.get_repo(repository)
    default_branch = str(repo.default_branch or "main")
    identity = mygithub12.resolve_identity(service, repository, ref=default_branch)
    return {"branch": default_branch, **identity}


def _resolve_branch(service: Any, repository: str, branch: str, base_branch: str) -> dict[str, Any]:
    branch_result = github_utils.get_github_branch(repository, branch, base_branch)
    _raise_result_error(branch_result, "BRANCH_NOT_FOUND", "branch was not found")
    identity = mygithub12.resolve_identity(service, repository, commit_sha=str(branch_result["commit_sha"]))
    return {**branch_result, "tree_sha": identity["tree_sha"]}


def _resolve_pr(repository: str, pull_number: int, branch: str) -> dict[str, Any] | None:
    if int(pull_number or 0) <= 0:
        return None
    pr = github_utils.get_github_pull_request(repository, int(pull_number))
    _raise_result_error(pr, "PULL_REQUEST_NOT_FOUND", "pull request was not found")
    if branch and pr.get("head_branch") != branch:
        raise MyGithub12Error(
            "DEVELOPMENT_RESUME_INPUT_MISMATCH",
            "branch and pull_number refer to different heads",
            {"branch": branch, "pull_number": int(pull_number), "pull_head_branch": pr.get("head_branch")},
        )
    return pr


def _discover_pr_by_branch(repository: str, branch: str, base_branch: str) -> dict[str, Any] | None:
    listing = github_utils.list_github_pull_requests(
        repository, state="open", head_branch=branch, base_branch=base_branch,
        sort="updated", direction="desc", limit=2, page=1,
    )
    _raise_result_error(listing, "PULL_REQUEST_LOOKUP_FAILED", "pull request lookup failed")
    matches = [
        item for item in listing.get("pull_requests", [])
        if item.get("head_branch") == branch and item.get("base_branch") == base_branch
    ]
    if not matches:
        return None
    pr = github_utils.get_github_pull_request(repository, int(matches[0]["pull_number"]))
    _raise_result_error(pr, "PULL_REQUEST_NOT_FOUND", "pull request was not found")
    return pr


def _select_workspace(service: Any, repository: str, branch: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    listing = mygithub12.list_workspaces(service, repository=repository, branch=branch, limit=20)
    items = [item for item in listing.get("items", []) if item.get("branch") == branch]
    if not items:
        return None, []
    # Prefer an active effective status, then the most recently updated object.
    for item in items:
        if item.get("status") == "active":
            return item, items
    return items[0], items


def _ci_summary(job: dict[str, Any] | None) -> dict[str, Any] | None:
    if not job:
        return None
    try:
        return build_private_ci_job_list_item(job)
    except Exception:
        return {key: job.get(key) for key in ("job_id", "repository", "branch", "commit_sha", "profile", "status", "exit_code") if key in job}


def _recent_ci(repository: str, branch: str, commit_sha: str) -> dict[str, Any]:
    jobs = db_list_jobs(repository=repository, branch=branch, commit_sha=commit_sha, limit=50)
    by_profile: dict[str, dict[str, Any] | None] = {"repo-fast-check": None, "repo-auto-check": None}
    all_items = []
    for job in jobs:
        summary = _ci_summary(job)
        if summary:
            all_items.append(summary)
        profile = str(job.get("profile") or "")
        if profile in by_profile and by_profile[profile] is None:
            by_profile[profile] = summary
    return {"fast": by_profile["repo-fast-check"], "full": by_profile["repo-auto-check"], "recent": all_items[:10]}


def _session_evidence(session: dict[str, Any] | None, current_head: str) -> dict[str, Any]:
    if not session:
        return {"current_head": None, "historical": None}
    exact = session.get("head_commit_sha") == current_head
    current = None
    historical = None
    evidence = {
        "last_fast_ci_job_id": session.get("last_fast_ci_job_id"),
        "last_full_ci_job_id": session.get("last_full_ci_job_id"),
        "last_attestation_id": session.get("last_attestation_id"),
        "last_failure_resource_uri": session.get("last_failure_resource_uri"),
        "head_commit_sha": session.get("head_commit_sha"),
        "validated_attestation": None,
    }
    if exact and evidence["last_attestation_id"]:
        try:
            validation = attestation_registry.validate_attestation(str(evidence["last_attestation_id"]))
            attestation = validation.get("attestation") if isinstance(validation, dict) else None
            if validation.get("ok") is True and isinstance(attestation, dict) and attestation.get("tested_commit_sha") == current_head:
                evidence["validated_attestation"] = validation
        except Exception:
            evidence["validated_attestation"] = None
    if exact:
        current = evidence
    elif any(evidence.values()):
        historical = evidence
    return {"current_head": current, "historical": historical}


def _workspace_recovery_plan(workspace: dict[str, Any] | None) -> dict[str, Any] | None:
    if not workspace:
        return None
    status = workspace.get("status")
    if status == "expired":
        return {
            "reason": "WORKSPACE_LEASE_REQUIRED",
            "action": "resume_development_workspace",
            "workspace_id": workspace.get("workspace_id"),
            "expected_workspace_revision": workspace.get("revision"),
        }
    if status == "drifted":
        return {
            "reason": "WORKSPACE_BRANCH_DRIFTED",
            "action": "recover_drifted_development_task",
            "recovery_tool": "recover_drifted_development_task",
            "manual_recovery_required": True,
            "workspace_id": workspace.get("workspace_id"),
            "drift_reason": workspace.get("drift_reason"),
        }
    return None


def _transient_recovery_failure(session: dict[str, Any], reason: str, **details: Any) -> dict[str, Any]:
    return {
        "transient_validation": True,
        "reconciled": False,
        "error_code": "DEVELOPMENT_SESSION_RECOVERY_REQUIRED",
        "reason": reason,
        "development_session_id": session.get("session_id"),
        **details,
    }


def _reconcile_transient_validation(session: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], str | None]:
    """Reconcile one exact persisted validation without starting CI or touching Git."""
    mode = TRANSIENT_VALIDATION_STATUSES[str(session["status"])]
    expected_profile = "repo-fast-check" if mode == "fast" else "repo-auto-check"
    correlations = sessions.validation_correlations(
        str(session["session_id"]),
        int(session["session_revision"]),
        mode,
        str(session["head_commit_sha"]),
        str(session["tree_sha"]),
    )
    current_revision = int(session["session_revision"])
    current_correlations = [
        item for item in correlations if int(item.get("session_revision") or 0) == current_revision
    ]
    if current_correlations:
        eligible_correlations = current_correlations
        correlation_source = "current_session_revision"
    else:
        last_job_field = "last_fast_ci_job_id" if mode == "fast" else "last_full_ci_job_id"
        observed_job_id = str(session.get(last_job_field) or "")
        eligible_correlations = [
            item for item in correlations if observed_job_id and str(item.get("job_id")) == observed_job_id
        ]
        correlation_source = "session_last_observed_job"
    by_job_id = {
        str(item.get("job_id")): item for item in eligible_correlations if item.get("job_id")
    }
    if len(by_job_id) != 1:
        recovery = _transient_recovery_failure(
            session,
            "validation_job_correlation_not_unique",
            correlation_count=len(by_job_id),
            correlation_source=correlation_source,
        )
        return session, recovery, "DEVELOPMENT_SESSION_RECOVERY_REQUIRED"

    job_id, correlation = next(iter(by_job_id.items()))
    job = db_get_job(job_id)
    if not job:
        recovery = _transient_recovery_failure(session, "validation_job_not_found", job_id=job_id)
        return session, recovery, "DEVELOPMENT_SESSION_RECOVERY_REQUIRED"
    identity_matches = (
        job.get("repository") == session.get("repository")
        and job.get("branch") == session.get("branch")
        and job.get("commit_sha") == session.get("head_commit_sha")
        and job.get("profile") == expected_profile
    )
    job_tree = (job.get("summary") or {}).get("git_tree_sha") if isinstance(job.get("summary"), dict) else None
    if not identity_matches or (job_tree and job_tree != session.get("tree_sha")):
        recovery = _transient_recovery_failure(
            session,
            "validation_job_identity_mismatch",
            job_id=job_id,
            expected_profile=expected_profile,
        )
        return session, recovery, "DEVELOPMENT_SESSION_RECOVERY_REQUIRED"

    status = str(job.get("status") or "")
    if status in VALIDATION_IN_PROGRESS_STATUSES:
        return session, {
            "transient_validation": True,
            "reconciled": False,
            "validation_in_progress": True,
            "mode": mode,
            "job": _ci_summary(job),
        }, "DEVELOPMENT_SESSION_VALIDATION_IN_PROGRESS"
    if status not in dx.VALIDATION_TERMINAL_STATUSES or not job_tree:
        # Older fast jobs did not persist git_tree_sha. An exact Git commit is
        # immutable, and resume_task already proved that branch, Workspace and
        # Session all resolve that commit to the Session tree.
        commit_proves_tree = (
            not job_tree
            and job.get("commit_sha") == session.get("head_commit_sha")
            and correlation.get("tree_sha") in {"", session.get("tree_sha")}
        )
    else:
        commit_proves_tree = False
    if status not in dx.VALIDATION_TERMINAL_STATUSES or (not job_tree and not commit_proves_tree):
        recovery = _transient_recovery_failure(
            session,
            "validation_job_terminal_evidence_incomplete",
            job_id=job_id,
            job_status=status,
        )
        return session, recovery, "DEVELOPMENT_SESSION_RECOVERY_REQUIRED"

    evidence = correlation.get("evidence") if isinstance(correlation.get("evidence"), dict) else {}
    selection = evidence.get("selection") if isinstance(evidence.get("selection"), dict) else {}
    result = dx.validation_result(
        str(session["session_id"]), int(session["session_revision"]), mode, job, selection, True
    )
    fields = {"last_fast_ci_job_id" if mode == "fast" else "last_full_ci_job_id": job_id}
    attestation = result.get("attestation")
    if isinstance(attestation, dict) and attestation.get("attestation_id"):
        fields["last_attestation_id"] = attestation["attestation_id"]
    failure = result.get("failure_pack")
    if isinstance(failure, dict) and failure.get("resource_uri"):
        fields["last_failure_resource_uri"] = failure["resource_uri"]
    next_status = "pr_ready" if result.get("merge_eligible") else "active"
    recovered_session = sessions.transition(
        str(session["session_id"]),
        int(session["session_revision"]),
        next_status,
        event_type="validation_reconciled",
        allowed_from={str(session["status"])},
        fields=fields,
    )
    return recovered_session, {
        "transient_validation": True,
        "reconciled": True,
        "mode": mode,
        "correlation_source": correlation_source,
        "tree_evidence": "ci_job_summary" if job_tree else "exact_immutable_commit_identity",
        "job": _ci_summary(job),
        "validation_result": result,
    }, None


def _next_actions(blockers: list[str], workspace: dict[str, Any] | None, session: dict[str, Any] | None, index: dict[str, Any] | None, pr: dict[str, Any] | None, policy: dict[str, Any]) -> list[str]:
    actions: list[str] = []
    if not workspace:
        return ["prepare_development_task"]
    if workspace.get("status") == "expired":
        return ["resume_development_workspace", "recovery_required"]
    if workspace.get("status") == "drifted":
        return ["recover_drifted_development_task", "recovery_required"]
    if not session:
        return ["recovery_required", "prepare_development_task"]
    if "DEVELOPMENT_SESSION_VALIDATION_IN_PROGRESS" in blockers:
        return ["wait_private_ci_job", "resume_development_task"]
    if session.get("status") in BLOCKED_SESSION_STATUSES:
        return ["recovery_required"]
    if index and index.get("status") != "ready":
        actions.append("request_index")
    if not blockers and workspace.get("lease_valid") and session.get("status") in ACTIVE_SESSION_STATUSES and index and index.get("status") == "ready":
        actions.append("continue_write")
        if bool((policy.get("policy") or {}).get("private_ci")):
            actions.extend(["run_fast_ci", "run_full_ci"])
        actions.append("prepare_pr")
        if pr or session.get("pull_number"):
            actions.append("readiness")
    if not actions:
        actions.append("recovery_required")
    # Stable ordering without duplicates.
    return list(dict.fromkeys(actions))


def resume_task(
    service: Any,
    repository: str,
    branch: str = "",
    pull_number: int = 0,
    recover_stale_session: bool = True,
    renew_lease: bool = False,
    expected_workspace_revision: int = 0,
    expected_session_revision: int = 0,
    lease_seconds: int = mygithub12.DEFAULT_LEASE_SECONDS,
    idempotency_key: str = "",
) -> dict[str, Any]:
    """Resume branch/PR development context without changing GitHub refs."""
    if not repository or "/" not in repository:
        raise MyGithub12Error("SEARCH_QUERY_INVALID", "repository must be owner/name")
    if not branch and int(pull_number or 0) <= 0:
        raise MyGithub12Error("SEARCH_QUERY_INVALID", "branch or pull_number is required")

    # Policy is returned as evidence from the same allowlists used by the public policy tool.
    policy = _repository_policy(repository)
    if not bool((policy.get("policy") or {}).get("github")):
        raise MyGithub12Error("REPOSITORY_NOT_ALLOWED", "repository is not allowed for GitHub operations", {"repository": repository})
    pr = _resolve_pr(repository, int(pull_number or 0), branch)
    current_main = _current_main(service, repository)
    effective_branch = branch or (str(pr.get("head_branch")) if pr else "")
    if pr is None and branch:
        pr = _discover_pr_by_branch(repository, effective_branch, current_main["branch"])
    branch_state = _resolve_branch(service, repository, effective_branch, current_main["branch"])
    branch_head = str(branch_state["commit_sha"])
    branch_tree = str(branch_state["tree_sha"])
    if pr and pr.get("head_sha") != branch_head:
        raise MyGithub12Error(
            "DEVELOPMENT_RESUME_INPUT_MISMATCH",
            "pull request head SHA does not match branch head",
            {"pull_number": pr.get("pull_number"), "pull_head_sha": pr.get("head_sha"), "branch_head_sha": branch_head},
        )

    workspace, workspace_candidates = _select_workspace(service, repository, effective_branch)
    recovery: dict[str, Any] | None = None
    blockers: list[str] = []
    degraded: list[str] = []
    session: dict[str, Any] | None = None
    session_candidates: list[dict[str, Any]] = []

    if workspace:
        if workspace.get("status") == "expired" and renew_lease:
            if not expected_workspace_revision:
                raise MyGithub12Error(
                    "WORKSPACE_REVISION_MISMATCH",
                    "expected_workspace_revision is required to resume an expired Workspace",
                    {"workspace_id": workspace.get("workspace_id"), "actual": workspace.get("revision")},
                )
            workspace = mygithub12.resume_workspace(service, workspace["workspace_id"], expected_workspace_revision, lease_seconds)
            recovery = {"workspace_resumed": True, "resume_evidence": workspace.get("resume_evidence")}
        session_candidates = find_sessions_for_workspace(str(workspace["workspace_id"]), include_terminal=False, limit=20)
        session = session_candidates[0] if session_candidates else None
        if session and expected_session_revision and int(session["session_revision"]) != int(expected_session_revision):
            raise MyGithub12Error(
                "DEVELOPMENT_SESSION_REVISION_MISMATCH",
                "development session revision changed",
                {"expected": int(expected_session_revision), "actual": int(session["session_revision"]), "development_session_id": session["session_id"]},
            )
        stale = bool(session) and (
            int(session.get("workspace_revision") or 0) != int(workspace.get("revision") or 0)
            or session.get("head_commit_sha") != workspace.get("head_sha")
            or session.get("tree_sha") != workspace.get("tree_sha")
            or abs(float(session.get("lease_expires_at") or 0) - float(workspace.get("lease_expires_at") or 0)) > 0.001
        )
        branch_drift = bool(workspace) and (
            workspace.get("head_sha") != branch_head or workspace.get("tree_sha") != branch_tree
        )
        if session and stale and not branch_drift:
            if recover_stale_session and workspace.get("status") == "active":
                recovery_result = dx.recover_stale_session(
                    service,
                    str(session["session_id"]),
                    int(session["session_revision"]),
                    int(workspace["revision"]),
                    str(workspace["head_sha"]),
                    idempotency_key or f"resume:{session['session_id']}:{workspace['revision']}",
                )
                recovery = {**(recovery or {}), "session": recovery_result}
                session = recovery_result["session"]
                workspace = recovery_result["workspace"]
            else:
                blockers.append("DEVELOPMENT_SESSION_RECOVERY_REQUIRED")
        if branch_drift:
            blockers.append("WORKSPACE_BRANCH_DRIFTED")
        if (
            session
            and not stale
            and not branch_drift
            and workspace.get("status") == "active"
            and session.get("status") in TRANSIENT_VALIDATION_STATUSES
            and recover_stale_session
        ):
            session, transient_recovery, transient_blocker = _reconcile_transient_validation(session)
            recovery = {**(recovery or {}), "transient": transient_recovery}
            if transient_blocker:
                blockers.append(transient_blocker)
        if workspace.get("status") in {"expired", "drifted", "closed"}:
            blockers.append("WORKSPACE_" + str(workspace.get("status", "unknown")).upper())
        if (
            session
            and session.get("status") in BLOCKED_SESSION_STATUSES
            and "DEVELOPMENT_SESSION_VALIDATION_IN_PROGRESS" not in blockers
        ):
            blockers.append("DEVELOPMENT_SESSION_" + str(session.get("status", "unknown")).upper())
        if not session:
            blockers.append("DEVELOPMENT_SESSION_NOT_FOUND")
    else:
        blockers.append("WORKSPACE_NOT_FOUND")

    index = mygithub12.get_index_status(service, repository, commit_sha=branch_head)
    if index.get("status") != "ready":
        blockers.append("INDEX_NOT_READY")
    overlap = None
    if workspace:
        try:
            overlap = mygithub12.workspace_overlap(service, str(workspace["workspace_id"]))
        except Exception as exc:
            degraded.append("OVERLAP_UNAVAILABLE")
            overlap = {"ok": False, "error_type": type(exc).__name__}

    readiness = None
    pr_number = int((pr or {}).get("pull_number") or (session or {}).get("pull_number") or 0)
    if pr_number:
        try:
            readiness = github_utils.get_github_pull_request_merge_readiness(repository, pr_number, branch_head, "", current_main["branch"])
        except Exception as exc:
            degraded.append("PR_READINESS_UNAVAILABLE")
            readiness = {"ok": False, "error_type": type(exc).__name__}

    ci = _recent_ci(repository, effective_branch, branch_head)
    session_evidence = _session_evidence(session, branch_head)
    blockers = list(dict.fromkeys(blockers))
    response = {
        "ok": True,
        "repository": repository,
        "input": {"branch": branch or "", "pull_number": int(pull_number or 0)},
        "policy": policy,
        "current_main": current_main,
        "branch": branch_state,
        "pull_request": pr,
        "workspace": workspace,
        "workspace_candidates": workspace_candidates,
        "development_session": session,
        "session_candidates": session_candidates,
        "index": index,
        "private_ci": {"current_head": ci, "session_evidence": session_evidence},
        "pull_request_readiness": readiness,
        "overlap": overlap,
        "recovery": recovery or _workspace_recovery_plan(workspace),
        "blockers": blockers,
        "degraded": degraded,
    }
    next_actions = _next_actions(blockers, workspace, session, index, pr, policy)
    response["live_facts"] = {
        "policy": policy, "current_main": current_main, "branch": branch_state, "pull_request": pr,
        "workspace": workspace, "development_session": session, "index": index,
        "private_ci_current_head": ci, "current_attestation": (session_evidence.get("current_head") or {}).get("validated_attestation"),
        "pull_request_readiness": readiness, "overlap": overlap,
    }
    response["historical_evidence"] = {"session": session_evidence.get("historical")}
    response["candidate_next_actions"] = next_actions
    response["next_allowed_actions"] = next_actions
    return response
