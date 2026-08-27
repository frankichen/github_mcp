"""DX2 resume helpers for recovering a branch or PR development context.

This module is intentionally a high-level read/recovery aggregator.  It does
not move branches, create PRs, merge, close, delete branches, or run CI.  The
only mutating behavior is guarded Session/Workspace recovery through existing
Workspace/Session CAS paths when the caller explicitly requests it and the
already-implemented DX2-SESSION safety checks prove the state is stale rather
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
from app.ci_database import list_jobs as db_list_jobs
from app.ci_mcp import build_private_ci_job_list_item

MyGithub12Error = mygithub12.MyGithub12Error
ACTIVE_SESSION_STATUSES = {"active", "pr_ready"}
BLOCKED_SESSION_STATUSES = {"blocked", "drifted", "closing", "validating_fast", "validating_full"}


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
            "action": "manual_branch_recovery",
            "workspace_id": workspace.get("workspace_id"),
            "drift_reason": workspace.get("drift_reason"),
        }
    return None


def _next_actions(blockers: list[str], workspace: dict[str, Any] | None, session: dict[str, Any] | None, index: dict[str, Any] | None, pr: dict[str, Any] | None) -> list[str]:
    actions: list[str] = []
    if not workspace:
        return ["prepare_development_task"]
    if workspace.get("status") == "expired":
        return ["resume_development_workspace", "recovery_required"]
    if workspace.get("status") == "drifted":
        return ["recovery_required"]
    if not session:
        return ["recovery_required", "prepare_development_task"]
    if session.get("status") in BLOCKED_SESSION_STATUSES:
        return ["recovery_required"]
    if index and index.get("status") != "ready":
        actions.append("request_index")
    if not blockers and workspace.get("lease_valid") and session.get("status") in ACTIVE_SESSION_STATUSES and index and index.get("status") == "ready":
        actions.extend(["continue_write", "run_fast_ci", "run_full_ci", "prepare_pr"])
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
        if session and stale:
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
        if workspace.get("status") in {"expired", "drifted", "closed"}:
            blockers.append("WORKSPACE_" + str(workspace.get("status", "unknown")).upper())
        if session and session.get("status") in BLOCKED_SESSION_STATUSES:
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
    next_actions = _next_actions(blockers, workspace, session, index, pr)
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
