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
from app import attestation_registry, ci_request_store, github_utils, mygithub12
from app import development_convergence_store as convergence_store
from app import development_managed_merge as managed_merge
from app.github_policy import repository_is_allowed
from app.ci_repository_config import is_private_ci_enabled, is_test_deploy_enabled, is_self_deploy_enabled
from app.ci_database import get_job as db_get_job, list_jobs as db_list_jobs
from app.ci_mcp import build_private_ci_job_list_item
from app.ci_models import REQUEST_ONLY_TERMINAL_STATUSES

MyGithub12Error = mygithub12.MyGithub12Error
ACTIVE_SESSION_STATUSES = {"active", "pr_ready"}
BLOCKED_SESSION_STATUSES = {"blocked", "drifted", "closing", "validating_fast", "validating_full"}
TRANSIENT_VALIDATION_STATUSES = {"validating_fast": "fast", "validating_full": "full"}
VALIDATION_IN_PROGRESS_STATUSES = {"queued", "leased", "downloading", "preparing", "running", "cancel_requested"}
CONVERGENCE_RESUME_MODES = ("full", "fast")
CONVERGENCE_TERMINAL_CI_STATUSES = {
    "passed", "failed", "timed_out", "cancelled", "superseded", "worker_lost",
    "internal_error", "preflight_failed",
}


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


def _resolve_recovery_base(
    service: Any,
    repository: str,
    workspace: dict[str, Any] | None,
    session: dict[str, Any] | None,
    pr: dict[str, Any] | None,
    current_main: dict[str, Any],
) -> dict[str, Any]:
    """Resolve the exact live base branch for drift recovery, not just repository main."""
    candidates = {
        str(value)
        for value in (
            (workspace or {}).get("base_branch"),
            (session or {}).get("base_branch"),
            (pr or {}).get("base_branch"),
        )
        if value
    }
    if len(candidates) > 1:
        raise MyGithub12Error(
            "RECOVERY_IDENTITY_MISMATCH",
            "Workspace, Development Session and Pull Request disagree on the recovery base branch",
            {"base_branches": sorted(candidates)},
        )
    base_branch = next(iter(candidates), str(current_main.get("branch") or ""))
    if not base_branch:
        raise MyGithub12Error("RECOVERY_BASE_CHANGED", "recovery base branch is unavailable")
    if base_branch == current_main.get("branch"):
        return dict(current_main)
    try:
        identity = mygithub12.resolve_identity(service, repository, ref=base_branch)
    except Exception as exc:
        raise MyGithub12Error(
            "RECOVERY_BASE_CHANGED",
            "recovery base branch could not be resolved",
            {"base_branch": base_branch, "cause_type": type(exc).__name__},
        ) from exc
    if not identity.get("commit_sha") or not identity.get("tree_sha"):
        raise MyGithub12Error(
            "RECOVERY_BASE_CHANGED",
            "recovery base branch did not resolve to an exact HEAD/Tree",
            {"base_branch": base_branch},
        )
    return {**identity, "branch": base_branch}


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


def _resume_convergence_identity(
    repository: str,
    branch: str,
    branch_head: str,
    branch_tree: str,
    workspace: dict[str, Any] | None,
    session: dict[str, Any] | None,
    current_main: dict[str, Any],
) -> dict[str, Any] | None:
    """Build the Store lookup identity from the current recovery evidence."""
    if not session:
        return None
    session_id = str(session.get("session_id") or "")
    workspace_id = str(session.get("workspace_id") or (workspace or {}).get("workspace_id") or "")
    base_branch = str(
        session.get("base_branch")
        or (workspace or {}).get("base_branch")
        or current_main.get("branch")
        or ""
    )
    base_sha = str(
        session.get("base_commit_sha")
        or (workspace or {}).get("base_commit_sha")
        or current_main.get("commit_sha")
        or ""
    )
    if not session_id or not workspace_id or not base_branch or not base_sha:
        return None
    return {
        "repository": repository,
        "branch": branch,
        "development_session_id": session_id,
        "workspace_id": workspace_id,
        "head_sha": str(branch_head),
        "tree_sha": str(branch_tree),
        "base_branch": base_branch,
        "base_sha": base_sha,
    }


def _resume_convergence_is_current(
    snapshot: dict[str, Any],
    identity: dict[str, Any],
    session: dict[str, Any] | None,
    workspace: dict[str, Any] | None,
) -> bool:
    """Require the persisted run and the fresh branch/session evidence to agree."""
    for key in (
        "repository", "branch", "development_session_id", "head_sha", "tree_sha",
        "base_branch", "base_sha",
    ):
        if snapshot.get(key) != identity.get(key):
            return False
    if snapshot.get("workspace_id") not in {None, identity.get("workspace_id")}:
        return False
    if session:
        if session.get("head_commit_sha") and session.get("head_commit_sha") != identity["head_sha"]:
            return False
        if session.get("tree_sha") and session.get("tree_sha") != identity["tree_sha"]:
            return False
    if workspace:
        if workspace.get("head_sha") and workspace.get("head_sha") != identity["head_sha"]:
            return False
        if workspace.get("tree_sha") and workspace.get("tree_sha") != identity["tree_sha"]:
            return False
    return True


def _read_historical_convergences(identity: dict[str, Any]) -> list[dict[str, Any]]:
    """Read every same-session/base run without creating or advancing one."""
    return convergence_store.list_convergences_for_session(
        repository=identity["repository"],
        branch=identity["branch"],
        development_session_id=identity["development_session_id"],
        base_branch=identity["base_branch"],
        base_sha=identity["base_sha"],
        modes=CONVERGENCE_RESUME_MODES,
        limit=50,
    )


def _convergence_request_snapshot(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    request_id = str(snapshot.get("ci_request_id") or "")
    if not request_id:
        return None
    try:
        request = ci_request_store.get_ci_request(request_id)
    except Exception as exc:
        return {
            "request_id": request_id,
            "status": "unavailable",
            "phase": "unknown",
            "revision": None,
            "terminal": False,
            "available": False,
            "error_type": type(exc).__name__,
        }
    if request is not None:
        return request
    return {
        "request_id": request_id,
        "status": "not_found",
        "phase": "unknown",
        "revision": None,
        "terminal": False,
        "available": False,
    }


def _convergence_worker_snapshot(
    snapshot: dict[str, Any], request: dict[str, Any] | None,
) -> dict[str, Any] | None:
    request_job_id = str((request or {}).get("worker_job_id") or "")
    job_id = str(snapshot.get("ci_job_id") or request_job_id or "")
    if not job_id:
        return None
    try:
        job = db_get_job(job_id)
    except Exception as exc:
        return {
            "job_id": job_id,
            "status": "unavailable",
            "terminal": False,
            "error_type": type(exc).__name__,
        }
    if job is None:
        return {"job_id": job_id, "status": "not_found", "terminal": False}
    result = _ci_summary(job) or {"job_id": job_id, "status": job.get("status")}
    result["terminal"] = str(job.get("status") or "") in CONVERGENCE_TERMINAL_CI_STATUSES
    return result


def _enrich_convergence_snapshot(
    snapshot: dict[str, Any],
    *,
    classification: str,
) -> dict[str, Any]:
    """Attach bounded request/Worker observations to one Store snapshot."""
    result = dict(snapshot)
    request = _convergence_request_snapshot(result)
    worker = _convergence_worker_snapshot(result, request)
    identity = {
        "convergence_id": result.get("convergence_id"),
        "repository": result.get("repository"),
        "branch": result.get("branch"),
        "development_session_id": result.get("development_session_id"),
        "session_id": result.get("development_session_id"),
        "workspace_id": result.get("workspace_id"),
        "head_sha": result.get("head_sha"),
        "commit_sha": result.get("head_sha"),
        "tree_sha": result.get("tree_sha"),
        "base_branch": result.get("base_branch"),
        "base_sha": result.get("base_sha"),
        "mode": result.get("mode"),
    }
    result["convergence_identity"] = identity
    result["resume_classification"] = classification
    result["ci_request"] = request
    result["ci_request_phase"] = (request or {}).get("phase")
    result["ci_request_status"] = (request or {}).get("status")
    result["ci_request_revision"] = (request or {}).get("revision")
    result["worker_job_id"] = str(result.get("ci_job_id") or (request or {}).get("worker_job_id") or "") or None
    result["worker"] = worker
    result["worker_job"] = worker
    result["ci_job"] = worker
    result["worker_status"] = (worker or {}).get("status")
    return result


def _resume_convergences(
    *,
    repository: str,
    branch: str,
    branch_head: str,
    branch_tree: str,
    workspace: dict[str, Any] | None,
    session: dict[str, Any] | None,
    current_main: dict[str, Any],
) -> dict[str, Any]:
    """Return current exact, pending and historical convergence evidence."""
    identity = _resume_convergence_identity(
        repository, branch, branch_head, branch_tree, workspace, session, current_main
    )
    empty = {
        "identity": identity,
        "live": {"identity": identity, "exact_head": False, "convergence": None},
        "current_exact": [],
        "pending": [],
        "historical": [],
        "primary": None,
        "errors": [],
    }
    if not identity or not session:
        return empty

    session_exact = _resume_convergence_is_current(
        {
            "repository": repository,
            "branch": branch,
            "development_session_id": session.get("session_id"),
            "head_sha": session.get("head_commit_sha"),
            "tree_sha": session.get("tree_sha"),
            "base_branch": identity["base_branch"],
            "base_sha": identity["base_sha"],
            "workspace_id": identity["workspace_id"],
        },
        identity,
        session,
        workspace,
    )
    snapshots: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    if session_exact:
        for mode in CONVERGENCE_RESUME_MODES:
            try:
                active = convergence_store.find_active_convergence(
                    repository=identity["repository"],
                    development_session_id=identity["development_session_id"],
                    head_sha=identity["head_sha"],
                    tree_sha=identity["tree_sha"],
                    mode=mode,
                    base_branch=identity["base_branch"],
                    base_sha=identity["base_sha"],
                )
            except Exception as exc:
                errors.append({"operation": "find_active_convergence", "mode": mode, "error_type": type(exc).__name__})
                continue
            if active:
                snapshots.append(active)
    try:
        snapshots.extend(_read_historical_convergences(identity))
    except Exception as exc:
        errors.append({"operation": "read_convergence_history", "error_type": type(exc).__name__})

    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for snapshot in snapshots:
        convergence_id = str(snapshot.get("convergence_id") or "")
        if not convergence_id or convergence_id in seen:
            continue
        seen.add(convergence_id)
        unique.append(snapshot)

    current_raw = [
        snapshot for snapshot in unique
        if _resume_convergence_is_current(snapshot, identity, session, workspace)
    ]
    mode_order = {mode: index for index, mode in enumerate(CONVERGENCE_RESUME_MODES)}
    current_raw.sort(key=lambda item: (mode_order.get(str(item.get("mode") or ""), 99), -float(item.get("updated_at") or 0)))
    current_exact = [
        _enrich_convergence_snapshot(
            snapshot,
            classification="pending" if not bool(snapshot.get("terminal")) else "historical",
        )
        for snapshot in current_raw
    ]
    pending = [item for item in current_exact if not bool(item.get("terminal"))]
    pending_ids = {str(item.get("convergence_id")) for item in pending}
    historical = [
        _enrich_convergence_snapshot(
            snapshot,
            classification="historical",
        )
        for snapshot in unique
        if str(snapshot.get("convergence_id")) not in pending_ids
    ]
    historical.sort(key=lambda item: -float(item.get("updated_at") or 0))
    primary = pending[0] if pending else None
    empty.update(
        {
            "live": {
                "identity": identity,
                "exact_head": bool(pending),
                "convergence": primary,
                "convergences": pending,
            },
            "current_exact": current_exact,
            "pending": pending,
            "historical": historical,
            "primary": primary,
            "errors": errors,
        }
    )
    return empty


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


def _resume_ancestry_evidence(service: Any, repository: str, ancestor: str, descendant: str) -> dict[str, Any]:
    try:
        repo = mygithub12._service_repo(service, repository)
        comparison = repo.compare(ancestor, descendant)
        merge_base = str(comparison.merge_base_commit.sha) if getattr(comparison, "merge_base_commit", None) else ""
        ahead_by = int(getattr(comparison, "ahead_by", 0) or 0)
        behind_by = int(getattr(comparison, "behind_by", 0) or 0)
        return {
            "verified": merge_base == ancestor and behind_by == 0,
            "ancestor": ancestor,
            "descendant": descendant,
            "merge_base": merge_base,
            "ahead_by": ahead_by,
            "behind_by": behind_by,
        }
    except Exception as exc:
        return {
            "verified": False,
            "ancestor": ancestor,
            "descendant": descendant,
            "error_type": type(exc).__name__,
        }


def _resume_exact_commit_sha(value: Any) -> str:
    sha = str(value or "").strip()
    if len(sha) != 40:
        return ""
    try:
        int(sha, 16)
    except ValueError:
        return ""
    return sha


def _resume_merge_base_evidence(
    service: Any, repository: str, left: str, right: str,
) -> dict[str, Any]:
    try:
        repo = mygithub12._service_repo(service, repository)
        comparison = repo.compare(left, right)
        merge_base = _resume_exact_commit_sha(
            str(getattr(getattr(comparison, "merge_base_commit", None), "sha", "") or "")
        )
        if not merge_base:
            return {
                "verified": False, "left": left, "right": right,
                "reason": "merge_base_unavailable",
            }
        return {
            "verified": True, "left": left, "right": right,
            "merge_base_sha": merge_base,
            "ahead_by": int(getattr(comparison, "ahead_by", 0) or 0),
            "behind_by": int(getattr(comparison, "behind_by", 0) or 0),
        }
    except Exception as exc:
        return {
            "verified": False, "left": left, "right": right,
            "reason": "merge_base_unavailable", "cause_type": type(exc).__name__,
        }


def _resume_historical_old_base_evidence(
    service: Any, session: dict[str, Any], live_new_base: str, old_session_head: str,
) -> dict[str, Any]:
    session_id = str(session.get("session_id") or "")
    repository = str(session.get("repository") or "")
    branch = str(session.get("branch") or "")
    metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    sources: list[dict[str, Any]] = []

    prepared_record = metadata.get("prepared_base_identity")
    prepared_sha = ""
    if prepared_record is not None:
        if not isinstance(prepared_record, dict):
            return {
                "verified": False, "reason": "RECOVERY_HISTORICAL_OLD_BASE_AMBIGUOUS",
                "sources": sources, "detail": "prepared_base_identity_malformed",
            }
        prepared_sha = _resume_exact_commit_sha(prepared_record.get("commit_sha"))
        prepared_repository = str(prepared_record.get("repository") or repository)
        if not prepared_sha or prepared_repository != repository:
            return {
                "verified": False, "reason": "RECOVERY_HISTORICAL_OLD_BASE_AMBIGUOUS",
                "sources": sources, "detail": "prepared_base_identity_invalid",
            }
        sources.append({"kind": "prepared_base_identity", "commit_sha": prepared_sha})

    try:
        events = sessions.list_events(session_id, limit=500)
    except Exception as exc:
        return {
            "verified": False, "reason": "RECOVERY_HISTORICAL_OLD_BASE_UNAVAILABLE",
            "sources": sources, "detail": "development_session_events_unavailable",
            "cause_type": type(exc).__name__,
        }

    transitions: list[dict[str, Any]] = []
    for event in events:
        if event.get("event_type") != "base_sync_recovery":
            continue
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        old_sha = _resume_exact_commit_sha(data.get("old_base_sha"))
        new_sha = _resume_exact_commit_sha(data.get("new_base_sha"))
        event_repository = str(data.get("repository") or repository)
        event_branch = str(data.get("branch") or branch)
        if (
            not old_sha or not new_sha or old_sha == new_sha
            or event_repository != repository or event_branch != branch
        ):
            return {
                "verified": False, "reason": "RECOVERY_HISTORICAL_OLD_BASE_AMBIGUOUS",
                "sources": sources, "detail": "base_sync_recovery_event_invalid",
                "event_id": event.get("id"),
            }
        transitions.append({
            "old_base_sha": old_sha, "new_base_sha": new_sha,
            "event_id": event.get("id"), "session_revision": event.get("session_revision"),
        })

    last_record = metadata.get("last_base_sync_recovery")
    last_pair: tuple[str, str] | None = None
    if last_record is not None:
        audit = last_record.get("audit") if isinstance(last_record, dict) else None
        if not isinstance(audit, dict):
            return {
                "verified": False, "reason": "RECOVERY_HISTORICAL_OLD_BASE_AMBIGUOUS",
                "sources": sources, "detail": "last_base_sync_recovery_malformed",
            }
        audit_old = _resume_exact_commit_sha(audit.get("old_base_sha"))
        audit_new = _resume_exact_commit_sha(audit.get("new_base_sha"))
        audit_repository = str(audit.get("repository") or repository)
        audit_branch = str(audit.get("branch") or branch)
        if (
            not audit_old or not audit_new or audit_old == audit_new
            or audit_repository != repository or audit_branch != branch
        ):
            return {
                "verified": False, "reason": "RECOVERY_HISTORICAL_OLD_BASE_AMBIGUOUS",
                "sources": sources, "detail": "last_base_sync_recovery_invalid",
            }
        last_pair = (audit_old, audit_new)

    persisted_candidate = prepared_sha
    if transitions:
        tip = persisted_candidate or transitions[0]["old_base_sha"]
        for transition in transitions:
            if transition["old_base_sha"] != tip:
                return {
                    "verified": False, "reason": "RECOVERY_HISTORICAL_OLD_BASE_AMBIGUOUS",
                    "sources": sources, "detail": "base_sync_recovery_history_not_linear",
                    "expected_old_base_sha": tip,
                    "observed_old_base_sha": transition["old_base_sha"],
                }
            tip = transition["new_base_sha"]
        persisted_candidate = tip
        sources.append({
            "kind": "base_sync_recovery_events", "count": len(transitions),
            "latest_new_base_sha": persisted_candidate,
        })

    if last_pair is not None:
        if transitions:
            event_pair = (transitions[-1]["old_base_sha"], transitions[-1]["new_base_sha"])
            if event_pair != last_pair:
                return {
                    "verified": False, "reason": "RECOVERY_HISTORICAL_OLD_BASE_AMBIGUOUS",
                    "sources": sources, "detail": "base_sync_event_metadata_disagree",
                }
        else:
            if persisted_candidate and last_pair[0] != persisted_candidate:
                return {
                    "verified": False, "reason": "RECOVERY_HISTORICAL_OLD_BASE_AMBIGUOUS",
                    "sources": sources, "detail": "base_sync_metadata_history_not_linear",
                }
            persisted_candidate = last_pair[1]
        sources.append({
            "kind": "last_base_sync_recovery",
            "old_base_sha": last_pair[0], "new_base_sha": last_pair[1],
        })

    if persisted_candidate:
        return {
            "verified": True, "historical_old_base_sha": persisted_candidate,
            "source": "persisted_control_plane_history", "sources": sources,
        }

    merge_base = _resume_merge_base_evidence(
        service, repository, live_new_base, old_session_head,
    )
    if merge_base.get("verified") and merge_base.get("merge_base_sha"):
        return {
            "verified": True,
            "historical_old_base_sha": str(merge_base["merge_base_sha"]),
            "source": "strict_git_merge_base", "sources": sources,
            "merge_base_evidence": merge_base,
        }
    return {
        "verified": False, "reason": "RECOVERY_HISTORICAL_OLD_BASE_UNAVAILABLE",
        "sources": sources, "detail": "no_unique_persisted_or_git_evidence",
        "merge_base_evidence": merge_base,
    }


def _workspace_recovery_plan(
    workspace: dict[str, Any] | None,
    *,
    service: Any | None = None,
    session: dict[str, Any] | None = None,
    current_main: dict[str, Any] | None = None,
    current_base: dict[str, Any] | None = None,
    branch_state: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
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
        old_base = str(workspace.get("base_commit_sha") or "")
        session_base = str((session or {}).get("base_commit_sha") or "")
        live_base = current_base or current_main or {}
        base_branch = str(workspace.get("base_branch") or "")
        live_base_branch = str(live_base.get("branch") or "")
        new_base = str(live_base.get("commit_sha") or "")
        old_head = str((session or {}).get("head_commit_sha") or "")
        current_head = str((branch_state or {}).get("commit_sha") or "")
        repository = str(workspace.get("repository") or "")
        base_sync_shape = bool(
            service
            and session
            and live_base
            and branch_state
            and workspace.get("drift_reason") == "branch_moved_externally"
            and base_branch
            and live_base_branch == base_branch
            and old_base
            and session_base == old_base
            and new_base
            and old_head
            and current_head
        )
        recovery_old_base = old_base
        historical_evidence: dict[str, Any] | None = None
        base_sync = bool(base_sync_shape and new_base != old_base)
        if base_sync:
            historical_evidence = {
                "verified": True, "historical_old_base_sha": old_base,
                "source": "currently_pinned_historical_base",
                "sources": [{"kind": "workspace_session_base", "commit_sha": old_base}],
            }
        elif base_sync_shape and new_base == old_base and old_head != current_head:
            historical_evidence = _resume_historical_old_base_evidence(
                service, session, new_base, old_head,
            )
            if not historical_evidence.get("verified"):
                return {
                    "reason": historical_evidence.get("reason") or "RECOVERY_HISTORICAL_OLD_BASE_UNAVAILABLE",
                    "action": "recovery_required",
                    "manual_recovery_required": True,
                    "candidate_recovery_tool": "recover_base_synced_development_task",
                    "workspace_id": workspace.get("workspace_id"),
                    "development_session_id": session.get("session_id"),
                    "drift_reason": workspace.get("drift_reason"),
                    "expected_new_base_sha": new_base,
                    "expected_base_branch": base_branch,
                    "expected_old_session_head_sha": old_head,
                    "expected_current_head_sha": current_head,
                    "historical_old_base_evidence": historical_evidence,
                }
            recovery_old_base = str(historical_evidence.get("historical_old_base_sha") or "")
            base_sync = bool(recovery_old_base and recovery_old_base != new_base)
        if base_sync:
            preflight = {
                "base_ancestry": _resume_ancestry_evidence(service, repository, recovery_old_base, new_base),
                "old_task_base_ancestry": _resume_ancestry_evidence(service, repository, recovery_old_base, old_head),
                "task_ancestry": _resume_ancestry_evidence(service, repository, old_head, current_head),
                "new_base_ancestry": _resume_ancestry_evidence(service, repository, new_base, current_head),
            }
            preflight["verified"] = all(
                item.get("verified") for item in preflight.values() if isinstance(item, dict)
            )
            preflight["historical_old_base_evidence"] = historical_evidence
            if not preflight["verified"]:
                return {
                    "reason": "RECOVERY_ANCESTRY_MISMATCH",
                    "action": "recovery_required",
                    "manual_recovery_required": True,
                    "candidate_recovery_tool": "recover_base_synced_development_task",
                    "workspace_id": workspace.get("workspace_id"),
                    "development_session_id": session.get("session_id"),
                    "drift_reason": workspace.get("drift_reason"),
                    "expected_old_base_sha": recovery_old_base,
                    "expected_new_base_sha": new_base,
                    "expected_base_branch": base_branch,
                    "expected_old_session_head_sha": old_head,
                    "expected_current_head_sha": current_head,
                    "expected_current_tree_sha": (branch_state or {}).get("tree_sha"),
                    "historical_old_base_evidence": historical_evidence,
                    "preflight": preflight,
                }
            workspace_has_current_identity = (
                workspace.get("head_sha") == current_head
                and workspace.get("tree_sha") == branch_state.get("tree_sha")
            )
            if not workspace_has_current_identity:
                return {
                    "reason": "WORKSPACE_REFRESH_REQUIRED_BEFORE_BASE_SYNC_RECOVERY",
                    "action": "refresh_development_workspace",
                    "manual_recovery_required": True,
                    "workspace_id": workspace.get("workspace_id"),
                    "development_session_id": session.get("session_id"),
                    "expected_workspace_revision": workspace.get("revision"),
                    "expected_base_branch": base_branch,
                    "expected_current_head_sha": current_head,
                    "expected_current_tree_sha": branch_state.get("tree_sha"),
                    "next_action": "recover_base_synced_development_task",
                    "recovery_sequence": [
                        "refresh_development_workspace",
                        "recover_base_synced_development_task",
                    ],
                    "preflight": preflight,
                }
            return {
                "reason": "WORKSPACE_BASE_SYNCED_EXTERNALLY",
                "action": "recover_base_synced_development_task",
                "recovery_tool": "recover_base_synced_development_task",
                "manual_recovery_required": True,
                "workspace_id": workspace.get("workspace_id"),
                "development_session_id": session.get("session_id"),
                "drift_reason": workspace.get("drift_reason"),
                "repository": repository,
                "branch": workspace.get("branch"),
                "expected_workspace_revision": workspace.get("revision"),
                "expected_session_revision": session.get("session_revision"),
                "expected_old_base_sha": recovery_old_base,
                "expected_new_base_sha": new_base,
                "expected_base_branch": base_branch,
                "expected_old_session_head_sha": old_head,
                "expected_current_head_sha": current_head,
                "expected_current_tree_sha": branch_state.get("tree_sha"),
                "preflight": preflight,
            }
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


def _session_workspace_identity_exact(
    session: dict[str, Any], workspace: dict[str, Any],
) -> bool:
    """Return whether a revision/lease-only recovery is safe to attempt."""
    return all(
        session.get(session_key) not in (None, "")
        and workspace.get(workspace_key) not in (None, "")
        and session.get(session_key) == workspace.get(workspace_key)
        for session_key, workspace_key in (
            ("workspace_id", "workspace_id"),
            ("repository", "repository"),
            ("branch", "branch"),
            ("base_branch", "base_branch"),
            ("base_commit_sha", "base_commit_sha"),
            ("head_commit_sha", "head_sha"),
            ("tree_sha", "tree_sha"),
        )
    )


def _validation_request_summary(request: dict[str, Any], worker_job_id: str = "") -> dict[str, Any]:
    """Expose the durable Request identity without making its phase authoritative."""
    return {
        "request_id": request.get("request_id"),
        "repository": request.get("repository"),
        "branch": request.get("branch"),
        "commit_sha": request.get("commit_sha"),
        "tree_sha": request.get("tree_sha"),
        "profile": request.get("profile"),
        "phase": request.get("phase"),
        "status": request.get("status"),
        "revision": request.get("revision"),
        "worker_job_id": worker_job_id or request.get("worker_job_id"),
    }


def _reconcile_terminal_validation_set(
    session: dict[str, Any],
    workspace: dict[str, Any],
    correlations: list[dict[str, Any]],
    *,
    mode: str,
    expected_profile: str,
    expected_base: str,
    workspace_revision: int,
    validation_generation: dict[str, Any],
    drift_reconciliation: bool,
) -> tuple[dict[str, Any], dict[str, Any], str | None]:
    """Reconcile an exact set of terminal Request -> Worker correlations.

    No member is selected as authoritative merge evidence.  Every persisted
    correlation must belong to this exact validation operation and prove a
    terminal durable Worker before one atomic Session transition is allowed.
    """
    session_id = str(session["session_id"])
    session_revision = int(session["session_revision"])
    generation_revision = int(validation_generation["generation_revision"])
    generation_workspace_revision = int(validation_generation["generation_workspace_revision"])
    request_ids = sorted({str(item.get("request_id") or "") for item in correlations if item.get("request_id")})
    if len(request_ids) < 2:
        recovery = _transient_recovery_failure(
            session, "validation_terminal_correlation_set_incomplete",
            correlation_count=len(request_ids), request_ids=request_ids,
        )
        return session, recovery, "DEVELOPMENT_SESSION_RECOVERY_REQUIRED"
    if any(int(item.get("session_revision") or -1) != generation_revision for item in correlations):
        recovery = _transient_recovery_failure(
            session, "validation_terminal_correlation_set_revision_mismatch",
            request_ids=request_ids,
            expected_generation_revision=generation_revision,
            current_session_revision=session_revision,
        )
        return session, recovery, "DEVELOPMENT_SESSION_RECOVERY_REQUIRED"
    if any(not item.get("request_id") for item in correlations):
        recovery = _transient_recovery_failure(
            session, "validation_terminal_correlation_set_unowned",
            request_ids=request_ids,
        )
        return session, recovery, "DEVELOPMENT_SESSION_RECOVERY_REQUIRED"

    members: list[dict[str, Any]] = []
    pair_bindings: list[dict[str, str]] = []
    for request_id in request_ids:
        owned = [item for item in correlations if str(item.get("request_id") or "") == request_id]
        persisted_job_ids = sorted({str(item.get("job_id") or "") for item in owned if item.get("job_id")})
        if len(persisted_job_ids) > 1:
            recovery = _transient_recovery_failure(
                session, "validation_worker_correlation_not_unique",
                request_id=request_id, job_ids=persisted_job_ids,
            )
            return session, recovery, "DEVELOPMENT_SESSION_RECOVERY_REQUIRED"

        request = ci_request_store.get_ci_request(request_id)
        if not request:
            recovery = _transient_recovery_failure(
                session, "validation_request_not_found", request_id=request_id,
            )
            return session, recovery, "DEVELOPMENT_SESSION_RECOVERY_REQUIRED"
        payload = ci_request_store.get_ci_request_payload(request_id)
        request_tree = str(request.get("tree_sha") or "")
        request_identity_matches = (
            request.get("repository") == session.get("repository")
            and request.get("branch") == session.get("branch")
            and request.get("commit_sha") == session.get("head_commit_sha")
            and request.get("profile") == expected_profile
            and request_tree == session.get("tree_sha")
            and payload.get("repository") == session.get("repository")
            and payload.get("branch") == session.get("branch")
            and payload.get("commit_sha") == session.get("head_commit_sha")
            and payload.get("tree_sha") == session.get("tree_sha")
            and payload.get("profile") == expected_profile
            and payload.get("mode") == mode
            and payload.get("base_sha") == expected_base
            and payload.get("base_branch") in (None, "", session.get("base_branch"))
            and payload.get("development_session_id") == session_id
            and payload.get("workspace_id") == workspace.get("workspace_id")
            and int(payload.get("expected_session_revision") or -1) == generation_revision
            and int(payload.get("workspace_revision") or -1) == generation_workspace_revision
        )
        if not request_identity_matches:
            recovery = _transient_recovery_failure(
                session, "validation_request_identity_mismatch",
                request_id=request_id, expected_profile=expected_profile,
            )
            return session, recovery, "DEVELOPMENT_SESSION_RECOVERY_REQUIRED"

        worker_job_id = str(request.get("worker_job_id") or "")
        if not worker_job_id:
            recovery = _transient_recovery_failure(
                session, "validation_request_worker_missing_in_set",
                request_id=request_id,
            )
            return session, recovery, "DEVELOPMENT_SESSION_RECOVERY_REQUIRED"
        request_phase = str(request.get("phase") or request.get("status") or "")
        if request_phase not in {"queued", "running", "terminal"}:
            recovery = _transient_recovery_failure(
                session, "validation_request_worker_pair_mismatch",
                request_id=request_id, request_phase=request_phase, worker_job_id=worker_job_id,
            )
            return session, recovery, "DEVELOPMENT_SESSION_RECOVERY_REQUIRED"
        if persisted_job_ids and persisted_job_ids != [worker_job_id]:
            recovery = _transient_recovery_failure(
                session, "validation_request_worker_pair_mismatch",
                request_id=request_id, worker_job_id=worker_job_id,
                persisted_job_ids=persisted_job_ids,
            )
            return session, recovery, "DEVELOPMENT_SESSION_RECOVERY_REQUIRED"

        job = db_get_job(worker_job_id)
        if not job:
            recovery = _transient_recovery_failure(
                session, "validation_job_not_found", request_id=request_id, job_id=worker_job_id,
            )
            return session, recovery, "DEVELOPMENT_SESSION_RECOVERY_REQUIRED"
        job_tree = (job.get("summary") or {}).get("git_tree_sha") if isinstance(job.get("summary"), dict) else None
        worker_identity_matches = (
            job.get("job_id") == worker_job_id
            and job.get("repository") == session.get("repository")
            and job.get("branch") == session.get("branch")
            and job.get("commit_sha") == session.get("head_commit_sha")
            and job.get("profile") == expected_profile
            and job.get("base_sha") == expected_base
        )
        tree_proven = bool(job_tree == session.get("tree_sha")) if job_tree else bool(
            request_tree == session.get("tree_sha")
            and job.get("commit_sha") == request.get("commit_sha")
            and request.get("worker_job_id") == worker_job_id
        )
        if (
            not worker_identity_matches
            or not tree_proven
            or job.get("superseded_by_job_id")
            or str(job.get("status") or "") == "superseded"
        ):
            recovery = _transient_recovery_failure(
                session, "validation_job_identity_mismatch",
                request_id=request_id, job_id=worker_job_id, expected_profile=expected_profile,
            )
            return session, recovery, "DEVELOPMENT_SESSION_RECOVERY_REQUIRED"

        status = str(job.get("status") or "")
        if status in VALIDATION_IN_PROGRESS_STATUSES:
            return session, {
                "transient_validation": True,
                "reconciled": False,
                "validation_in_progress": True,
                "mode": mode,
                "correlation_source": "persisted_terminal_set",
                "correlation_set": {
                    "request_ids": request_ids,
                    "members": members + [{
                        "request": _validation_request_summary(request, worker_job_id),
                        "job": _ci_summary(job),
                    }],
                },
            }, "DEVELOPMENT_SESSION_VALIDATION_IN_PROGRESS"
        if status not in dx.VALIDATION_TERMINAL_STATUSES or status == "superseded":
            recovery = _transient_recovery_failure(
                session, "validation_job_terminal_evidence_incomplete",
                request_id=request_id, job_id=worker_job_id, job_status=status,
            )
            return session, recovery, "DEVELOPMENT_SESSION_RECOVERY_REQUIRED"
        if status == "passed" and job.get("exit_code") != 0:
            recovery = _transient_recovery_failure(
                session, "validation_pass_exit_code_invalid",
                request_id=request_id, job_id=worker_job_id,
            )
            return session, recovery, "DEVELOPMENT_SESSION_RECOVERY_REQUIRED"

        members.append({
            "request": _validation_request_summary(request, worker_job_id),
            "job": _ci_summary(job),
            "tree_evidence": "ci_job_summary" if job_tree else "request_tree_worker_pair",
        })
        pair_bindings.append({"request_id": request_id, "job_id": worker_job_id, "status": status})

    try:
        settlement = sessions.reconcile_terminal_validation_set(
            session_id,
            session_revision,
            workspace_revision,
            mode,
            str(session["head_commit_sha"]),
            str(session["tree_sha"]),
            pair_bindings,
            validation_generation_revision=generation_revision,
            validation_generation_workspace_revision=generation_workspace_revision,
            allow_branch_drift=drift_reconciliation,
        )
    except MyGithub12Error as exc:
        recovery = _transient_recovery_failure(
            session, "validation_terminal_correlation_set_bind_failed",
            request_ids=request_ids, error_code=exc.code,
        )
        return session, recovery, "DEVELOPMENT_SESSION_RECOVERY_REQUIRED"

    recovered_session = settlement["session"]
    audit = settlement["audit"]
    return recovered_session, {
        "transient_validation": True,
        "reconciled": True,
        "mode": mode,
        "correlation_source": "persisted_terminal_set",
        "correlation_set": {
            "request_ids": audit.get("request_ids", request_ids),
            "job_ids": audit.get("job_ids", []),
            "members": members,
        },
        "correlation_set_audit": audit,
        "validation_result": {
            "terminal": True,
            "merge_eligible": False,
            "attestation": None,
            "failure_pack": None,
            "correlation_set": True,
        },
        "workspace_drift_pending_recovery": drift_reconciliation,
    }, None


def _reconcile_transient_validation(
    session: dict[str, Any], workspace: dict[str, Any], *, allow_branch_drift: bool = False,
) -> tuple[dict[str, Any], dict[str, Any], str | None]:
    """Reconcile exactly one persisted Request -> Worker pair without starting CI."""
    mode = TRANSIENT_VALIDATION_STATUSES[str(session["status"])]
    expected_profile = "repo-fast-check" if mode == "fast" else "repo-auto-check"
    expected_base = str(session.get("base_commit_sha") or workspace.get("base_commit_sha") or "")
    session_id = str(session["session_id"])
    session_revision = int(session["session_revision"])
    workspace_revision = int(workspace.get("revision") or 0)
    session_workspace_revision = int(session.get("workspace_revision") or 0)
    static_identity_matches = all(
        session.get(session_key) not in (None, "")
        and workspace.get(workspace_key) not in (None, "")
        and session.get(session_key) == workspace.get(workspace_key)
        for session_key, workspace_key in (
            ("workspace_id", "workspace_id"),
            ("repository", "repository"),
            ("branch", "branch"),
            ("base_branch", "base_branch"),
            ("base_commit_sha", "base_commit_sha"),
        )
    )
    drift_reconciliation = bool(
        allow_branch_drift
        and workspace.get("status") == "drifted"
        and workspace.get("drift_reason") == "branch_moved_externally"
        and static_identity_matches
        and session_workspace_revision < workspace_revision
        and (
            session.get("head_commit_sha") != workspace.get("head_sha")
            or session.get("tree_sha") != workspace.get("tree_sha")
        )
    )
    active_exact_reconciliation = not (
        int(session.get("workspace_revision") or 0) != workspace_revision
        or not _session_workspace_identity_exact(session, workspace)
        or expected_base != str(workspace.get("base_commit_sha") or "")
        or workspace.get("status") != "active"
        or workspace.get("drift_reason")
    )
    if not active_exact_reconciliation and not drift_reconciliation:
        recovery = _transient_recovery_failure(session, "validation_workspace_session_cas_mismatch")
        return session, recovery, "DEVELOPMENT_SESSION_RECOVERY_REQUIRED"

    try:
        validation_generation = sessions.validation_generation_context(
            session_id, session_revision, mode,
            str(session["head_commit_sha"]), str(session["tree_sha"]),
        )
    except MyGithub12Error as exc:
        recovery = _transient_recovery_failure(
            session, "validation_generation_resolution_failed", error_code=exc.code,
        )
        return session, recovery, "DEVELOPMENT_SESSION_RECOVERY_REQUIRED"
    generation_revision = int(validation_generation["generation_revision"])
    generation_workspace_revision = int(validation_generation["generation_workspace_revision"])
    correlations = sessions.validation_correlations(
        session_id, generation_revision, mode,
        str(session["head_commit_sha"]), str(session["tree_sha"]), exact_revision=True,
    )
    request_ids = sorted({str(item.get("request_id")) for item in correlations if item.get("request_id")})
    job_ids = sorted({str(item.get("job_id")) for item in correlations if item.get("job_id")})
    if len(request_ids) > 1:
        return _reconcile_terminal_validation_set(
            session,
            workspace,
            correlations,
            mode=mode,
            expected_profile=expected_profile,
            expected_base=expected_base,
            workspace_revision=workspace_revision,
            validation_generation=validation_generation,
            drift_reconciliation=drift_reconciliation,
        )
    if len(job_ids) > 1:
        recovery = _transient_recovery_failure(
            session, "validation_worker_correlation_not_unique",
            correlation_count=len(job_ids), job_ids=job_ids,
        )
        return session, recovery, "DEVELOPMENT_SESSION_RECOVERY_REQUIRED"

    correlation_source = "persisted_request_id"
    if request_ids:
        request_id = request_ids[0]
        request = ci_request_store.get_ci_request(request_id)
    else:
        if len(job_ids) != 1:
            recovery = _transient_recovery_failure(
                session, "validation_request_correlation_missing",
                strict_job_correlation_count=len(job_ids),
            )
            return session, recovery, "DEVELOPMENT_SESSION_RECOVERY_REQUIRED"
        request = ci_request_store.get_ci_request_by_worker_job_id(job_ids[0])
        request_id = str((request or {}).get("request_id") or "")
        correlation_source = "legacy_exact_job_to_request"
    if not request or not request_id:
        recovery = _transient_recovery_failure(
            session, "validation_request_not_found", request_id=request_id or None,
        )
        return session, recovery, "DEVELOPMENT_SESSION_RECOVERY_REQUIRED"

    payload = ci_request_store.get_ci_request_payload(request_id)
    request_tree = str(request.get("tree_sha") or "")
    request_identity_matches = (
        request.get("repository") == session.get("repository")
        and request.get("branch") == session.get("branch")
        and request.get("commit_sha") == session.get("head_commit_sha")
        and request.get("profile") == expected_profile
        and request_tree == session.get("tree_sha")
        and payload.get("repository") == session.get("repository")
        and payload.get("branch") == session.get("branch")
        and payload.get("commit_sha") == session.get("head_commit_sha")
        and payload.get("tree_sha") == session.get("tree_sha")
        and payload.get("profile") == expected_profile
        and payload.get("mode") == mode
        and payload.get("base_sha") == expected_base
        and payload.get("base_branch") in (None, "", session.get("base_branch"))
        and payload.get("development_session_id") in (None, "", session_id)
        and payload.get("workspace_id") in (None, "", workspace.get("workspace_id"))
    )
    if not request_identity_matches:
        recovery = _transient_recovery_failure(
            session, "validation_request_identity_mismatch",
            request_id=request_id, expected_profile=expected_profile,
        )
        return session, recovery, "DEVELOPMENT_SESSION_RECOVERY_REQUIRED"

    worker_job_id = str(request.get("worker_job_id") or "")
    if not worker_job_id:
        if job_ids:
            recovery = _transient_recovery_failure(
                session, "validation_request_worker_pair_mismatch",
                request_id=request_id, persisted_job_ids=job_ids,
            )
            return session, recovery, "DEVELOPMENT_SESSION_RECOVERY_REQUIRED"
        request_phase = str(request.get("phase") or request.get("status") or "")
        request_status = str(request.get("status") or "")
        if request_phase in {"accepted", "preparing", "queued"}:
            return session, {
                "transient_validation": True,
                "reconciled": False,
                "validation_in_progress": True,
                "mode": mode,
                "correlation_source": correlation_source,
                "request": _validation_request_summary(request),
            }, "DEVELOPMENT_SESSION_VALIDATION_IN_PROGRESS"
        request_only_terminal = bool(
            request_phase == "terminal"
            and request_status in REQUEST_ONLY_TERMINAL_STATUSES
        )
        if not request_only_terminal:
            recovery = _transient_recovery_failure(
                session, "validation_request_worker_missing_terminal",
                request_id=request_id, request_phase=request_phase, request_status=request_status,
            )
            return session, recovery, "DEVELOPMENT_SESSION_RECOVERY_REQUIRED"
        try:
            binding = sessions.bind_validation_request_worker(
                session_id, session_revision, workspace_revision, mode,
                str(session["head_commit_sha"]), str(session["tree_sha"]), request_id, "",
                allow_branch_drift=drift_reconciliation,
                request_terminal_status=request_status,
            )
        except MyGithub12Error as exc:
            recovery = _transient_recovery_failure(
                session, "validation_request_terminal_bind_failed",
                request_id=request_id, request_status=request_status, error_code=exc.code,
            )
            return session, recovery, "DEVELOPMENT_SESSION_RECOVERY_REQUIRED"
        result = {
            "terminal": True,
            "merge_eligible": False,
            "attestation": None,
            "failure_pack": None,
            "request_terminal": True,
            "status": request_status,
            "preflight_error_code": request.get("preflight_error_code"),
        }
        recovered_session = sessions.transition(
            session_id, session_revision, "active",
            event_type="validation_request_terminal_reconciled",
            allowed_from={str(session["status"])},
        )
        return recovered_session, {
            "transient_validation": True,
            "reconciled": True,
            "mode": mode,
            "correlation_source": correlation_source,
            "correlation_backfill": binding,
            "request": _validation_request_summary(request),
            "job": None,
            "tree_evidence": "request_payload",
            "validation_result": result,
            "workspace_drift_pending_recovery": drift_reconciliation,
        }, None

    if job_ids and any(job_id != worker_job_id for job_id in job_ids):
        recovery = _transient_recovery_failure(
            session, "validation_request_worker_pair_mismatch",
            request_id=request_id, worker_job_id=worker_job_id, persisted_job_ids=job_ids,
        )
        return session, recovery, "DEVELOPMENT_SESSION_RECOVERY_REQUIRED"
    job = db_get_job(worker_job_id)
    if not job:
        recovery = _transient_recovery_failure(
            session, "validation_job_not_found", request_id=request_id, job_id=worker_job_id,
        )
        return session, recovery, "DEVELOPMENT_SESSION_RECOVERY_REQUIRED"
    worker_identity_matches = (
        job.get("job_id") == worker_job_id
        and job.get("repository") == session.get("repository")
        and job.get("branch") == session.get("branch")
        and job.get("commit_sha") == session.get("head_commit_sha")
        and job.get("profile") == expected_profile
        and job.get("base_sha") == expected_base
    )
    job_tree = (job.get("summary") or {}).get("git_tree_sha") if isinstance(job.get("summary"), dict) else None
    tree_proven = bool(job_tree == session.get("tree_sha")) if job_tree else bool(
        request_tree == session.get("tree_sha")
        and job.get("commit_sha") == request.get("commit_sha")
        and request.get("worker_job_id") == worker_job_id
    )
    if (
        not worker_identity_matches
        or not tree_proven
        or job.get("superseded_by_job_id")
        or str(job.get("status") or "") == "superseded"
    ):
        recovery = _transient_recovery_failure(
            session, "validation_job_identity_mismatch",
            request_id=request_id, job_id=worker_job_id, expected_profile=expected_profile,
        )
        return session, recovery, "DEVELOPMENT_SESSION_RECOVERY_REQUIRED"

    status = str(job.get("status") or "")
    if status == "passed" and job.get("exit_code") not in {None, 0}:
        recovery = _transient_recovery_failure(
            session, "validation_pass_exit_code_invalid", job_id=worker_job_id,
        )
        return session, recovery, "DEVELOPMENT_SESSION_RECOVERY_REQUIRED"
    reusable_attestation = None
    if status == "passed" and mode == "full":
        if job.get("exit_code") != 0:
            recovery = _transient_recovery_failure(
                session, "validation_full_pass_exit_code_invalid", job_id=worker_job_id,
            )
            return session, recovery, "DEVELOPMENT_SESSION_RECOVERY_REQUIRED"
        attestation_validation = attestation_registry.find_reusable_attestation_for_job(worker_job_id)
        reusable_attestation = (
            attestation_validation.get("attestation")
            if isinstance(attestation_validation, dict) else None
        )
        attestation_matches = bool(
            attestation_validation.get("ok") is True
            and attestation_validation.get("reusable") is True
            and isinstance(reusable_attestation, dict)
            and reusable_attestation.get("repository") == session.get("repository")
            and reusable_attestation.get("tested_commit_sha") == session.get("head_commit_sha")
            and reusable_attestation.get("tested_tree_sha") == session.get("tree_sha")
            and reusable_attestation.get("base_sha") == expected_base
            and reusable_attestation.get("private_ci_job_id") == worker_job_id
            and reusable_attestation.get("profile") == expected_profile
        )
        if not attestation_matches:
            recovery = _transient_recovery_failure(
                session, "validation_full_attestation_not_reusable", job_id=worker_job_id,
            )
            return session, recovery, "DEVELOPMENT_SESSION_RECOVERY_REQUIRED"

    try:
        binding = sessions.bind_validation_request_worker(
            session_id, session_revision, workspace_revision, mode,
            str(session["head_commit_sha"]), str(session["tree_sha"]), request_id, worker_job_id,
            allow_branch_drift=drift_reconciliation,
        )
    except MyGithub12Error as exc:
        recovery = _transient_recovery_failure(
            session, "validation_correlation_backfill_failed",
            request_id=request_id, job_id=worker_job_id, error_code=exc.code,
        )
        return session, recovery, "DEVELOPMENT_SESSION_RECOVERY_REQUIRED"

    if status in VALIDATION_IN_PROGRESS_STATUSES:
        return session, {
            "transient_validation": True,
            "reconciled": False,
            "validation_in_progress": True,
            "mode": mode,
            "correlation_source": correlation_source,
            "correlation_backfill": binding,
            "request": _validation_request_summary(request, worker_job_id),
            "tree_evidence": "ci_job_summary" if job_tree else "request_tree_worker_pair",
            "job": _ci_summary(job),
        }, "DEVELOPMENT_SESSION_VALIDATION_IN_PROGRESS"
    if status not in dx.VALIDATION_TERMINAL_STATUSES:
        recovery = _transient_recovery_failure(
            session, "validation_job_terminal_evidence_incomplete",
            request_id=request_id, job_id=worker_job_id, job_status=status,
        )
        return session, recovery, "DEVELOPMENT_SESSION_RECOVERY_REQUIRED"

    correlation = next(
        (
            item for item in correlations
            if str(item.get("request_id") or "") == request_id
            and isinstance(item.get("evidence"), dict)
            and isinstance(item["evidence"].get("selection"), dict)
        ),
        correlations[0] if correlations else {},
    )
    evidence = correlation.get("evidence") if isinstance(correlation.get("evidence"), dict) else {}
    selection = evidence.get("selection") if isinstance(evidence.get("selection"), dict) else {}
    result = dx.validation_result(
        session_id, session_revision, mode, job, selection, True,
        request_id=request_id, reusable_attestation=reusable_attestation,
        allow_attestation_creation=False,
    )
    if status == "passed" and mode == "full" and not result.get("merge_eligible"):
        recovery = _transient_recovery_failure(
            session, "validation_full_pass_not_merge_eligible", job_id=worker_job_id,
        )
        return session, recovery, "DEVELOPMENT_SESSION_RECOVERY_REQUIRED"
    fields = {"last_fast_ci_job_id" if mode == "fast" else "last_full_ci_job_id": worker_job_id}
    attestation = result.get("attestation")
    if isinstance(attestation, dict) and attestation.get("attestation_id"):
        fields["last_attestation_id"] = attestation["attestation_id"]
    failure = result.get("failure_pack")
    if isinstance(failure, dict) and failure.get("resource_uri"):
        fields["last_failure_resource_uri"] = failure["resource_uri"]
    # A Full PASS belongs to the old Session HEAD while branch drift is pending.
    # Keep it as historical evidence only; formal drift/base-sync recovery must
    # adopt the new HEAD and invalidate the old merge evidence.
    next_status = (
        "pr_ready" if not drift_reconciliation and mode == "full" and result.get("merge_eligible") else "active"
    )
    recovered_session = sessions.transition(
        session_id, session_revision, next_status,
        event_type="validation_reconciled", allowed_from={str(session["status"])}, fields=fields,
    )
    return recovered_session, {
        "transient_validation": True,
        "reconciled": True,
        "mode": mode,
        "correlation_source": correlation_source,
        "correlation_backfill": binding,
        "request": _validation_request_summary(request, worker_job_id),
        "tree_evidence": "ci_job_summary" if job_tree else "request_tree_worker_pair",
        "job": _ci_summary(job),
        "validation_result": result,
        "workspace_drift_pending_recovery": drift_reconciliation,
    }, None


def _next_actions(
    blockers: list[str],
    workspace: dict[str, Any] | None,
    session: dict[str, Any] | None,
    index: dict[str, Any] | None,
    pr: dict[str, Any] | None,
    policy: dict[str, Any],
    recovery_plan: dict[str, Any] | None = None,
    pending_convergences: list[dict[str, Any]] | None = None,
) -> list[str]:
    if pr and pr.get("merged") is True and session and session.get("status") == "merged" and workspace and str(workspace.get("status")) == "closed":
        return ["managed_merge_finalized"]
    actions: list[str] = []
    if not workspace:
        return ["prepare_development_task"]
    if (
        pr
        and pr.get("merged") is True
        and session
        and session.get("status") in ACTIVE_SESSION_STATUSES
        and "MANAGED_MERGE_RECONCILIATION_REQUIRED" in blockers
    ):
        return ["resume_development_task"]
    if "DEVELOPMENT_SESSION_VALIDATION_IN_PROGRESS" in blockers:
        return ["get_private_ci_job", "resume_development_task"]
    if workspace.get("status") == "expired":
        return ["resume_development_workspace", "recovery_required"]
    if workspace.get("status") == "drifted":
        action = str((recovery_plan or {}).get("action") or "recover_drifted_development_task")
        return [action, "recovery_required"]
    if not session:
        return ["recovery_required", "prepare_development_task"]
    if session.get("status") in BLOCKED_SESSION_STATUSES:
        return ["recovery_required"]
    if index and index.get("status") != "ready":
        actions.append("request_index")
    if not blockers and workspace.get("lease_valid") and session.get("status") in ACTIVE_SESSION_STATUSES and index and index.get("status") == "ready":
        actions.append("continue_write")
        if bool((policy.get("policy") or {}).get("private_ci")):
            pending_modes = {
                str(item.get("mode") or "") for item in (pending_convergences or [])
            }
            if "fast" not in pending_modes:
                actions.append("run_fast_ci")
            if "full" not in pending_modes:
                actions.append("run_full_ci")
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
        session_candidates = find_sessions_for_workspace(
            str(workspace["workspace_id"]),
            include_terminal=bool(pr and pr.get("merged") is True),
            limit=20,
        )
        session = session_candidates[0] if session_candidates else None
        if session and expected_session_revision and int(session["session_revision"]) != int(expected_session_revision):
            raise MyGithub12Error(
                "DEVELOPMENT_SESSION_REVISION_MISMATCH",
                "development session revision changed",
                {"expected": int(expected_session_revision), "actual": int(session["session_revision"]), "development_session_id": session["session_id"]},
            )
        if pr and pr.get("merged") is True and workspace and session:
            if recover_stale_session:
                merge_reconciliation = managed_merge.finalize_managed_pr_merge(
                    service,
                    repository,
                    int(pr.get("pull_number") or pull_number or 0),
                    branch_head,
                    current_main["branch"],
                    {"base_head_after": current_main["commit_sha"], "merge_commit_sha": pr.get("merge_commit_sha")},
                    pull_request=pr,
                    expected_workspace_id=str(workspace["workspace_id"]),
                    expected_session_id=str(session["session_id"]),
                    expected_workspace_revision=int(workspace["revision"]),
                    expected_session_revision=int(session["session_revision"]),
                    allow_no_context=False,
                )
                recovery = {**(recovery or {}), "managed_merge_reconciliation": merge_reconciliation}
                if merge_reconciliation.get("managed"):
                    session = merge_reconciliation.get("development_session") or session
                    workspace = merge_reconciliation.get("workspace") or workspace
                    session_candidates = [session]
                    workspace_candidates = [workspace]
            else:
                recovery = {**(recovery or {}), "managed_merge_reconciliation": {
                    "action": "resume_development_task",
                    "manual_recovery_required": True,
                    "reason": "MANAGED_MERGE_RECONCILIATION_REQUIRED",
                    "development_session_id": session.get("session_id"),
                    "workspace_id": workspace.get("workspace_id"),
                }}
                blockers.append("MANAGED_MERGE_RECONCILIATION_REQUIRED")
        stale = bool(session) and (
            int(session.get("workspace_revision") or 0) != int(workspace.get("revision") or 0)
            or session.get("head_commit_sha") != workspace.get("head_sha")
            or session.get("tree_sha") != workspace.get("tree_sha")
            or abs(float(session.get("lease_expires_at") or 0) - float(workspace.get("lease_expires_at") or 0)) > 0.001
        )
        branch_drift = bool(workspace) and (
            workspace.get("head_sha") != branch_head or workspace.get("tree_sha") != branch_tree
        )
        persisted_branch_drift = bool(
            not branch_drift
            and workspace.get("status") == "drifted"
            and workspace.get("drift_reason") == "branch_moved_externally"
            and workspace.get("head_sha") == branch_head
            and workspace.get("tree_sha") == branch_tree
        )
        drifted_validation_candidate = bool(
            session and stale and persisted_branch_drift
            and session.get("status") in TRANSIENT_VALIDATION_STATUSES
        )
        if session and stale and not branch_drift:
            if recover_stale_session and drifted_validation_candidate:
                pass
            elif recover_stale_session and workspace.get("status") == "active":
                transient_state = session.get("status") in TRANSIENT_VALIDATION_STATUSES
                if transient_state and not _session_workspace_identity_exact(session, workspace):
                    # A transient validation may only cross a Workspace revision/
                    # lease boundary when its code/base identity is unchanged.
                    # A changed identity must remain fail-closed; the generic
                    # stale-session forward recovery is for non-validation work.
                    blockers.append("DEVELOPMENT_SESSION_RECOVERY_REQUIRED")
                else:
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
        # recover_stale_session() may have advanced only the Session/Workspace
        # revision and lease. Recompute stale before attempting to reconcile the
        # durable Request -> Worker pair; the old boolean would skip recovery.
        stale = bool(session) and (
            int(session.get("workspace_revision") or 0) != int(workspace.get("revision") or 0)
            or session.get("head_commit_sha") != workspace.get("head_sha")
            or session.get("tree_sha") != workspace.get("tree_sha")
            or abs(float(session.get("lease_expires_at") or 0) - float(workspace.get("lease_expires_at") or 0)) > 0.001
        )
        active_validation_reconciliation = bool(
            session
            and not stale
            and not branch_drift
            and workspace.get("status") == "active"
        )
        drifted_validation_reconciliation = bool(
            session and stale and persisted_branch_drift
        )
        if (
            session
            and session.get("status") in TRANSIENT_VALIDATION_STATUSES
            and recover_stale_session
            and (active_validation_reconciliation or drifted_validation_reconciliation)
        ):
            session, transient_recovery, transient_blocker = _reconcile_transient_validation(
                session, workspace, allow_branch_drift=drifted_validation_reconciliation,
            )
            recovery = {**(recovery or {}), "transient": transient_recovery}
            if transient_blocker:
                blockers.append(transient_blocker)
        settled_managed_merge = bool(
            pr and pr.get("merged") is True
            and session
            and session.get("status") == "merged"
            and workspace
            and workspace.get("status") == "closed"
        )
        if workspace.get("status") in {"expired", "drifted", "closed"} and not settled_managed_merge:
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
    convergence_evidence = _resume_convergences(
        repository=repository,
        branch=effective_branch,
        branch_head=branch_head,
        branch_tree=branch_tree,
        workspace=workspace,
        session=session,
        current_main=current_main,
    )
    if convergence_evidence["errors"]:
        degraded.append("CONVERGENCE_STATE_UNAVAILABLE")
    blockers = list(dict.fromkeys(blockers))
    recovery_base = current_main
    if workspace and workspace.get("status") == "drifted":
        recovery_base = _resolve_recovery_base(
            service, repository, workspace, session, pr, current_main,
        )
    workspace_recovery = recovery or _workspace_recovery_plan(
        workspace,
        service=service,
        session=session,
        current_main=current_main,
        current_base=recovery_base,
        branch_state=branch_state,
    )
    response = {
        "ok": True,
        "repository": repository,
        "input": {"branch": branch or "", "pull_number": int(pull_number or 0)},
        "policy": policy,
        "current_main": current_main,
        "recovery_base": recovery_base,
        "branch": branch_state,
        "pull_request": pr,
        "workspace": workspace,
        "workspace_candidates": workspace_candidates,
        "development_session": session,
        "session_candidates": session_candidates,
        "index": index,
        "private_ci": {"current_head": ci, "session_evidence": session_evidence},
        "convergence": convergence_evidence["primary"],
        "convergence_identity": (
            convergence_evidence["primary"].get("convergence_identity")
            if convergence_evidence["primary"]
            else None
        ),
        "convergence_phase": (
            convergence_evidence["primary"].get("phase")
            if convergence_evidence["primary"]
            else None
        ),
        "convergence_status": (
            convergence_evidence["primary"].get("status")
            if convergence_evidence["primary"]
            else None
        ),
        "convergence_revision": (
            convergence_evidence["primary"].get("revision")
            if convergence_evidence["primary"]
            else None
        ),
        "current_exact_convergence": convergence_evidence["primary"],
        "pending_convergence": (
            convergence_evidence["pending"][0]
            if convergence_evidence["pending"]
            else None
        ),
        "pending_convergences": convergence_evidence["pending"],
        "historical_convergences": convergence_evidence["historical"],
        "convergence_evidence": {
            "live": convergence_evidence["live"],
            "pending": convergence_evidence["pending"],
            "historical": convergence_evidence["historical"],
        },
        "pending_work": {
            "convergences": convergence_evidence["pending"],
            "ci_requests": [
                item["ci_request"]
                for item in convergence_evidence["pending"]
                if item.get("ci_request")
            ],
            "worker_jobs": [
                item["worker"]
                for item in convergence_evidence["pending"]
                if item.get("worker")
            ],
        },
        "pull_request_readiness": readiness,
        "overlap": overlap,
        "recovery": workspace_recovery,
        "blockers": blockers,
        "degraded": degraded,
    }
    next_actions = _next_actions(
        blockers,
        workspace,
        session,
        index,
        pr,
        policy,
        workspace_recovery,
        convergence_evidence["pending"],
    )
    response["live_facts"] = {
        "policy": policy, "current_main": current_main, "recovery_base": recovery_base, "branch": branch_state, "pull_request": pr,
        "workspace": workspace, "development_session": session, "index": index,
        "private_ci_current_head": ci, "current_attestation": (session_evidence.get("current_head") or {}).get("validated_attestation"),
        "convergence": convergence_evidence["live"],
        "pull_request_readiness": readiness, "overlap": overlap,
    }
    response["historical_evidence"] = {"session": session_evidence.get("historical")}
    if convergence_evidence["historical"]:
        response["historical_evidence"]["convergences"] = convergence_evidence["historical"]
    response["candidate_next_actions"] = next_actions
    response["next_allowed_actions"] = next_actions
    return response
