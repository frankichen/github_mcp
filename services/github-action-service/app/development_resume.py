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
from app import github_utils, mygithub12
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
    }
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
# DX2_RESUME_CHUNK_03
# DX2_RESUME_CHUNK_04
