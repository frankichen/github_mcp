"""Fail-stop recovery for a canonical development Writer whose stacked PR was retargeted.

This recovery is intentionally separate from same-base drift/base-sync recovery. It
adopts an already-existing GitHub branch only after proving the exact current task
PR, the exact historical stacked base, the upstream merged-PR evidence that carried
that stacked base into the new base (including squash merges), the current new base,
Workspace/Session CAS, task/base path deltas, scope and Writer ownership. It never
moves Git refs and never writes repository files.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from app import development_base_sync_recovery as base_sync
from app import development_drift_recovery as same_base
from app import development_managed_merge as managed_merge
from app import development_session_store as sessions
from app import github_utils, mygithub12

MyGithub12Error = mygithub12.MyGithub12Error
PINNED_RETARGET_HISTORICAL_OLD = "historical_old_base"
PINNED_RETARGET_ALREADY_NEW = "already_pinned_new_base"


def _raise_result_error(result: dict[str, Any], default_code: str, default_message: str) -> None:
    if isinstance(result, dict) and result.get("ok") is False:
        err = result.get("error") if isinstance(result.get("error"), dict) else {}
        raise MyGithub12Error(
            str(err.get("code") or default_code),
            str(err.get("message") or default_message),
            dict(err.get("details") or {}),
        )


def _classify_pinned_retarget_state(
    workspace_base_branch: str,
    workspace_base_sha: str,
    session_base_branch: str,
    session_base_sha: str,
    expected_old_base_branch: str,
    expected_old_base_sha: str,
    expected_new_base_branch: str,
    expected_new_base_sha: str,
) -> str:
    old_exact = (
        workspace_base_branch == expected_old_base_branch
        and session_base_branch == expected_old_base_branch
        and workspace_base_sha == expected_old_base_sha
        and session_base_sha == expected_old_base_sha
    )
    new_exact = (
        workspace_base_branch == expected_new_base_branch
        and session_base_branch == expected_new_base_branch
        and workspace_base_sha == expected_new_base_sha
        and session_base_sha == expected_new_base_sha
    )
    if old_exact:
        return PINNED_RETARGET_HISTORICAL_OLD
    if new_exact:
        return PINNED_RETARGET_ALREADY_NEW
    raise MyGithub12Error(
        "RECOVERY_BASE_CHANGED",
        "Workspace/Development Session bases do not match an allowed retarget recovery state",
        {
            "workspace_base_branch": workspace_base_branch,
            "workspace_base_sha": workspace_base_sha,
            "session_base_branch": session_base_branch,
            "session_base_sha": session_base_sha,
            "expected_old_base_branch": expected_old_base_branch,
            "expected_old_base_sha": expected_old_base_sha,
            "expected_new_base_branch": expected_new_base_branch,
            "expected_new_base_sha": expected_new_base_sha,
            "allowed_states": [PINNED_RETARGET_HISTORICAL_OLD, PINNED_RETARGET_ALREADY_NEW],
        },
    )


def _retarget_request_identity(
    repository: str,
    branch: str,
    pull_number: int,
    upstream_pull_number: int,
    workspace_id: str,
    development_session_id: str,
    expected_workspace_revision: int,
    expected_session_revision: int,
    expected_old_base_branch: str,
    expected_old_base_sha: str,
    expected_new_base_branch: str,
    expected_new_base_sha: str,
    expected_old_session_head_sha: str,
    expected_current_head_sha: str,
    expected_current_tree_sha: str,
    reviewed_overlap_paths: list[str],
    lease_seconds: int,
) -> dict[str, Any]:
    request = {
        "repository": repository,
        "branch": branch,
        "pull_number": int(pull_number),
        "upstream_pull_number": int(upstream_pull_number),
        "workspace_id": workspace_id,
        "development_session_id": development_session_id,
        "expected_workspace_revision": int(expected_workspace_revision),
        "expected_session_revision": int(expected_session_revision),
        "expected_old_base_branch": expected_old_base_branch,
        "expected_old_base_sha": expected_old_base_sha,
        "expected_new_base_branch": expected_new_base_branch,
        "expected_new_base_sha": expected_new_base_sha,
        "expected_old_session_head_sha": expected_old_session_head_sha,
        "expected_current_head_sha": expected_current_head_sha,
        "expected_current_tree_sha": expected_current_tree_sha,
        "lease_seconds": int(lease_seconds),
    }
    if reviewed_overlap_paths:
        request["reviewed_overlap_paths"] = list(reviewed_overlap_paths)
    return request


def _compare_retarget_delta(repo: Any, base_sha: str, head_sha: str, *, label: str) -> tuple[dict[str, Any], list[str]]:
    """Return rename-aware changed paths without requiring old stacked HEAD ancestry.

    A squash merge deliberately breaks old-base-HEAD -> new-base ancestry. The merge
    proof is validated separately from the merged PR's merge_commit_sha. This compare
    is therefore used only for exact path classification, never as merge proof.
    """
    try:
        comparison = repo.compare(base_sha, head_sha)
        merge_base = str(comparison.merge_base_commit.sha) if getattr(comparison, "merge_base_commit", None) else ""
        ahead_by = int(getattr(comparison, "ahead_by", 0) or 0)
        behind_by = int(getattr(comparison, "behind_by", 0) or 0)
        files = list(getattr(comparison, "files", []) or [])
    except Exception as exc:
        raise MyGithub12Error(
            "RECOVERY_BASE_RETARGET_DELTA_UNAVAILABLE",
            f"{label} changed-path comparison could not be verified",
            {"base_sha": base_sha, "head_sha": head_sha, "cause_type": type(exc).__name__},
        ) from exc
    if len(files) >= same_base._COMPARE_FILE_LIMIT:
        raise MyGithub12Error(
            "RECOVERY_SCOPE_INCOMPLETE",
            f"{label} changed-path comparison reached the bounded recovery limit",
            {"file_count": len(files), "limit": same_base._COMPARE_FILE_LIMIT, "label": label},
        )
    changed_paths = sorted({
        str(path)
        for item in files
        for path in (getattr(item, "filename", None), getattr(item, "previous_filename", None))
        if path
    })
    return {
        "verified": True,
        "label": label,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "merge_base": merge_base,
        "ahead_by": ahead_by,
        "behind_by": behind_by,
        "ancestry_required": False,
    }, changed_paths


def _verify_task_pull_request(
    repository: str,
    pull_number: int,
    branch: str,
    current_head_sha: str,
    new_base_branch: str,
    new_base_sha: str,
) -> dict[str, Any]:
    pr = github_utils.get_github_pull_request(repository, int(pull_number))
    _raise_result_error(pr, "PULL_REQUEST_NOT_FOUND", "task pull request was not found")
    exact = (
        int(pr.get("pull_number") or 0) == int(pull_number)
        and pr.get("state") == "open"
        and pr.get("merged") is not True
        and pr.get("head_branch") == branch
        and pr.get("head_sha") == current_head_sha
        and pr.get("base_branch") == new_base_branch
        and pr.get("base_sha") == new_base_sha
    )
    if not exact:
        raise MyGithub12Error(
            "RECOVERY_PR_IDENTITY_MISMATCH",
            "current task Pull Request no longer matches the exact retarget recovery identity",
            {
                "pull_number": int(pull_number),
                "expected_head_branch": branch,
                "expected_head_sha": current_head_sha,
                "expected_base_branch": new_base_branch,
                "expected_base_sha": new_base_sha,
                "actual_state": pr.get("state"),
                "actual_merged": pr.get("merged"),
                "actual_head_branch": pr.get("head_branch"),
                "actual_head_sha": pr.get("head_sha"),
                "actual_base_branch": pr.get("base_branch"),
                "actual_base_sha": pr.get("base_sha"),
            },
        )
    return {
        "pull_number": int(pull_number),
        "state": pr.get("state"),
        "draft": pr.get("draft"),
        "head_branch": pr.get("head_branch"),
        "head_sha": pr.get("head_sha"),
        "base_branch": pr.get("base_branch"),
        "base_sha": pr.get("base_sha"),
    }


def _verify_upstream_merge(
    service: Any,
    repository: str,
    upstream_pull_number: int,
    old_base_branch: str,
    old_base_sha: str,
    new_base_branch: str,
    new_base_sha: str,
) -> dict[str, Any]:
    upstream = github_utils.get_github_pull_request(repository, int(upstream_pull_number))
    _raise_result_error(upstream, "PULL_REQUEST_NOT_FOUND", "upstream pull request was not found")
    if (
        upstream.get("merged") is not True
        or upstream.get("head_branch") != old_base_branch
        or upstream.get("head_sha") != old_base_sha
        or upstream.get("base_branch") != new_base_branch
    ):
        raise MyGithub12Error(
            "RECOVERY_UPSTREAM_MERGE_PROOF_MISMATCH",
            "upstream Pull Request does not prove that the exact stacked base was merged into the new base branch",
            {
                "upstream_pull_number": int(upstream_pull_number),
                "expected_head_branch": old_base_branch,
                "expected_head_sha": old_base_sha,
                "expected_base_branch": new_base_branch,
                "actual_merged": upstream.get("merged"),
                "actual_head_branch": upstream.get("head_branch"),
                "actual_head_sha": upstream.get("head_sha"),
                "actual_base_branch": upstream.get("base_branch"),
            },
        )
    evidence = managed_merge._verify_merge_evidence(
        service,
        repository,
        int(upstream_pull_number),
        old_base_sha,
        new_base_branch,
        upstream,
        {
            "merged": True,
            "head_branch": old_base_branch,
            "head_sha": old_base_sha,
            "base_branch": new_base_branch,
            "merge_commit_sha": upstream.get("merge_commit_sha"),
            "base_head_after": new_base_sha,
        },
    )
    return {**evidence, "upstream_pull_number": int(upstream_pull_number)}


def _find_upstream_pull_number(
    repository: str,
    old_base_branch: str,
    old_base_sha: str,
    new_base_branch: str,
) -> int:
    listing = github_utils.list_github_pull_requests(
        repository,
        state="closed",
        head_branch=old_base_branch,
        base_branch=new_base_branch,
        sort="updated",
        direction="desc",
        limit=100,
        page=1,
    )
    _raise_result_error(listing, "PULL_REQUEST_LOOKUP_FAILED", "upstream pull request lookup failed")
    matching_numbers = [
        int(item.get("pull_number") or 0)
        for item in (listing.get("pull_requests") or [])
        if item.get("head_branch") == old_base_branch and item.get("head_sha") == old_base_sha
    ]
    merged_numbers: list[int] = []
    for number in matching_numbers:
        if number <= 0:
            continue
        pr = github_utils.get_github_pull_request(repository, number)
        _raise_result_error(pr, "PULL_REQUEST_NOT_FOUND", "upstream pull request was not found")
        if (
            pr.get("merged") is True
            and pr.get("head_branch") == old_base_branch
            and pr.get("head_sha") == old_base_sha
            and pr.get("base_branch") == new_base_branch
        ):
            merged_numbers.append(number)
    merged_numbers = sorted(set(merged_numbers))
    if len(merged_numbers) != 1:
        raise MyGithub12Error(
            "RECOVERY_UPSTREAM_MERGE_PROOF_REQUIRED",
            "retarget recovery requires exactly one merged upstream PR for the exact historical stacked base",
            {
                "old_base_branch": old_base_branch,
                "old_base_sha": old_base_sha,
                "new_base_branch": new_base_branch,
                "matching_pull_numbers": matching_numbers,
                "merged_pull_numbers": merged_numbers,
                "listing_has_more": bool(listing.get("has_more")),
            },
        )
    return merged_numbers[0]


def _fresh_retarget_github_identity(
    service: Any,
    repository: str,
    branch: str,
    pull_number: int,
    upstream_pull_number: int,
    expected_old_base_branch: str,
    expected_old_base_sha: str,
    expected_new_base_branch: str,
    expected_new_base_sha: str,
    expected_current_head_sha: str,
    expected_current_tree_sha: str,
) -> tuple[Any, dict[str, Any]]:
    repo, current = same_base._fresh_github_identity(
        service,
        repository,
        branch,
        expected_current_head_sha,
        expected_current_tree_sha,
        expected_new_base_branch,
        expected_new_base_sha,
    )
    old_base_state = service.client.get_branch(repository, expected_old_base_branch)
    if not old_base_state:
        raise MyGithub12Error(
            "RECOVERY_BASE_CHANGED",
            "historical stacked base branch no longer exists",
            {"base_branch": expected_old_base_branch},
        )
    actual_old_base_sha = str(old_base_state.commit.sha)
    if actual_old_base_sha != expected_old_base_sha:
        raise MyGithub12Error(
            "RECOVERY_BASE_CHANGED",
            "historical stacked base branch HEAD changed",
            {
                "base_branch": expected_old_base_branch,
                "expected": expected_old_base_sha,
                "actual": actual_old_base_sha,
            },
        )
    task_pr = _verify_task_pull_request(
        repository,
        int(pull_number),
        branch,
        expected_current_head_sha,
        expected_new_base_branch,
        expected_new_base_sha,
    )
    upstream_merge = _verify_upstream_merge(
        service,
        repository,
        int(upstream_pull_number),
        expected_old_base_branch,
        expected_old_base_sha,
        expected_new_base_branch,
        expected_new_base_sha,
    )
    return repo, {
        **current,
        "old_base_branch": expected_old_base_branch,
        "old_base_sha": expected_old_base_sha,
        "task_pull_request": task_pr,
        "upstream_merge": upstream_merge,
    }


def _verify_retarget_deltas(
    repo: Any,
    session: dict[str, Any],
    old_base_sha: str,
    new_base_sha: str,
    old_session_head_sha: str,
    current_head_sha: str,
    reviewed_overlap_paths: list[str],
    *,
    enforce_reviewed_overlap: bool,
) -> dict[str, Any]:
    try:
        old_session_tree = mygithub12._tree_sha(repo.get_commit(old_session_head_sha))
    except Exception as exc:
        raise MyGithub12Error(
            "RECOVERY_ANCESTRY_MISMATCH",
            "old Development Session HEAD could not be resolved",
            {"old_session_head": old_session_head_sha, "cause_type": type(exc).__name__},
        ) from exc
    if old_session_tree != session.get("tree_sha"):
        raise MyGithub12Error(
            "RECOVERY_ANCESTRY_MISMATCH",
            "old Development Session tree no longer matches its pinned HEAD",
            {
                "old_session_head": old_session_head_sha,
                "session_tree": session.get("tree_sha"),
                "actual_tree": old_session_tree,
            },
        )

    base_transition, base_transition_paths = _compare_retarget_delta(
        repo, old_base_sha, new_base_sha, label="old_stacked_base_to_new_base_path_delta",
    )
    old_task_ancestry, old_task_delta_paths = base_sync._compare_delta(
        repo, old_base_sha, old_session_head_sha, label="old_base_to_old_task_head",
    )
    task_ancestry, forward_task_delta_paths = base_sync._compare_delta(
        repo, old_session_head_sha, current_head_sha, label="old_task_head_to_current_head",
    )
    new_base_ancestry, new_task_delta_paths = base_sync._compare_delta(
        repo, new_base_sha, current_head_sha, label="new_base_to_current_head",
    )

    historical_base_overlap = sorted(set(old_task_delta_paths) & set(base_transition_paths))
    current_base_overlap = sorted(set(new_task_delta_paths) & set(base_transition_paths))
    overlap = sorted(set(historical_base_overlap) | set(current_base_overlap))
    reviewed_overlap = sorted(reviewed_overlap_paths)
    if enforce_reviewed_overlap and overlap != reviewed_overlap:
        raise MyGithub12Error(
            "RECOVERY_BASE_SYNC_OVERLAP",
            "retarget base overlap was not reviewed with an exact path-set match",
            {
                "actual_overlap_paths": overlap,
                "reviewed_overlap_paths": reviewed_overlap,
                "missing_reviewed_overlap_paths": sorted(set(overlap) - set(reviewed_overlap)),
                "unexpected_reviewed_overlap_paths": sorted(set(reviewed_overlap) - set(overlap)),
                "historical_base_overlap_paths": historical_base_overlap,
                "current_base_overlap_paths": current_base_overlap,
                "base_transition_paths": base_transition_paths,
                "old_task_delta_paths": old_task_delta_paths,
                "new_task_delta_paths": new_task_delta_paths,
            },
        )

    task_path_changes = sorted(set(old_task_delta_paths) ^ set(new_task_delta_paths))
    unexplained_task_path_changes = sorted(set(task_path_changes) - set(forward_task_delta_paths))
    if unexplained_task_path_changes:
        raise MyGithub12Error(
            "RECOVERY_TASK_DIFF_MISMATCH",
            "task path-set changes are not explained by the verified forward task advance",
            {
                "old_task_delta_paths": old_task_delta_paths,
                "new_task_delta_paths": new_task_delta_paths,
                "forward_task_delta_paths": forward_task_delta_paths,
                "unexplained_task_path_changes": unexplained_task_path_changes,
            },
        )

    forward_paths = set(forward_task_delta_paths)
    base_paths = set(base_transition_paths)
    overlap_paths = set(overlap)
    # Ambiguous base/task overlap is never silently classified as imported base.
    # It is caller-reviewed and remains task-owned for scope/ownership checks.
    excluded_imported_base_paths = sorted((forward_paths & base_paths) - overlap_paths)
    recovery_scope_delta_paths = sorted((forward_paths - base_paths) | (forward_paths & overlap_paths))
    excluded_unchanged_historical_cumulative_paths = sorted(set(old_task_delta_paths) - forward_paths)

    return {
        "base_transition": base_transition,
        "old_task_ancestry": old_task_ancestry,
        "task_ancestry": task_ancestry,
        "new_base_ancestry": new_base_ancestry,
        "base_transition_paths": base_transition_paths,
        "base_delta_paths": base_transition_paths,
        "old_task_delta_paths": old_task_delta_paths,
        "new_task_delta_paths": new_task_delta_paths,
        "forward_task_delta_paths": forward_task_delta_paths,
        "historical_cumulative_task_delta_paths": old_task_delta_paths,
        "external_forward_delta_paths": forward_task_delta_paths,
        "recovery_scope_delta_paths": recovery_scope_delta_paths,
        "excluded_imported_base_paths": excluded_imported_base_paths,
        "excluded_unchanged_historical_cumulative_paths": excluded_unchanged_historical_cumulative_paths,
        "task_path_changes": task_path_changes,
        "unexplained_task_path_changes": unexplained_task_path_changes,
        "historical_base_overlap_paths": historical_base_overlap,
        "current_base_overlap_paths": current_base_overlap,
        "base_task_overlap_paths": overlap,
        "actual_overlap_paths": overlap,
        "reviewed_overlap_paths": reviewed_overlap,
    }


def _replay_retarget_result(
    session: dict[str, Any],
    workspace: dict[str, Any],
    request: dict[str, Any],
    idempotency_key: str,
) -> dict[str, Any] | None:
    metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    record = metadata.get("last_retarget_recovery") if isinstance(metadata, dict) else None
    if not isinstance(record, dict) or record.get("idempotency_key") != idempotency_key:
        return None
    if record.get("request") != request:
        raise MyGithub12Error(
            "IDEMPOTENCY_CONFLICT",
            "retarget recovery idempotency key was reused with a different payload",
            {"workspace_id": workspace.get("workspace_id"), "development_session_id": session.get("session_id")},
        )
    after = record.get("after") if isinstance(record.get("after"), dict) else {}
    matches = (
        workspace.get("status") == "active"
        and not workspace.get("drift_reason")
        and session.get("status") == "active"
        and int(workspace.get("revision") or 0) == int(after.get("workspace_revision") or -1)
        and int(session.get("workspace_revision") or 0) == int(after.get("workspace_revision") or -1)
        and int(session.get("session_revision") or 0) == int(after.get("session_revision") or -1)
        and workspace.get("base_branch") == after.get("base_branch")
        and session.get("base_branch") == after.get("base_branch")
        and workspace.get("base_commit_sha") == after.get("base_sha")
        and session.get("base_commit_sha") == after.get("base_sha")
        and workspace.get("head_sha") == after.get("head_sha")
        and workspace.get("tree_sha") == after.get("tree_sha")
        and session.get("head_commit_sha") == after.get("head_sha")
        and session.get("tree_sha") == after.get("tree_sha")
        and int(workspace.get("pr_number") or 0) == int(request["pull_number"])
        and int(session.get("pull_number") or 0) == int(request["pull_number"])
    )
    if not matches:
        raise MyGithub12Error(
            "IDEMPOTENCY_CONFLICT",
            "recorded retarget recovery result no longer matches current control-plane state",
            {"workspace_id": workspace.get("workspace_id"), "development_session_id": session.get("session_id")},
        )
    return record


def _atomic_recover_retarget(
    service: Any,
    *,
    request: dict[str, Any],
    idempotency_key: str,
    verification: dict[str, Any],
) -> dict[str, Any]:
    sessions.init_session_db()
    repository = request["repository"]
    branch = request["branch"]
    pull_number = int(request["pull_number"])
    workspace_id = request["workspace_id"]
    session_id = request["development_session_id"]
    expected_workspace_revision = int(request["expected_workspace_revision"])
    expected_session_revision = int(request["expected_session_revision"])
    old_base_branch = request["expected_old_base_branch"]
    old_base_sha = request["expected_old_base_sha"]
    new_base_branch = request["expected_new_base_branch"]
    new_base_sha = request["expected_new_base_sha"]
    old_session_head = request["expected_old_session_head_sha"]
    current_head = request["expected_current_head_sha"]
    current_tree = request["expected_current_tree_sha"]
    now = sessions._now()
    lease_expires_at = now + max(60, min(int(request["lease_seconds"]), mygithub12.MAX_LEASE_SECONDS))
    try:
        with sessions._LOCK, sessions._db() as db:
            workspace_row = db.execute("SELECT * FROM workspaces WHERE workspace_id=?", (workspace_id,)).fetchone()
            session_row = db.execute("SELECT * FROM development_sessions WHERE session_id=?", (session_id,)).fetchone()
            if not workspace_row:
                raise MyGithub12Error("WORKSPACE_NOT_FOUND", "workspace was not found", {"workspace_id": workspace_id})
            if not session_row:
                raise MyGithub12Error("DEVELOPMENT_SESSION_NOT_FOUND", "development session was not found", {"development_session_id": session_id})

            metadata = json.loads(session_row["metadata_json"] or "{}")
            replay = metadata.get("last_retarget_recovery") if isinstance(metadata, dict) else None
            if isinstance(replay, dict) and replay.get("idempotency_key") == idempotency_key:
                if replay.get("request") != request:
                    raise MyGithub12Error("IDEMPOTENCY_CONFLICT", "retarget recovery idempotency key payload changed")
                after = replay.get("after") if isinstance(replay.get("after"), dict) else {}
                state_matches = (
                    workspace_row["status"] == "active"
                    and not workspace_row["drift_reason"]
                    and session_row["status"] == "active"
                    and int(workspace_row["revision"]) == int(after.get("workspace_revision") or -1)
                    and int(session_row["workspace_revision"]) == int(after.get("workspace_revision") or -1)
                    and int(session_row["session_revision"]) == int(after.get("session_revision") or -1)
                    and workspace_row["base_branch"] == after.get("base_branch")
                    and session_row["base_branch"] == after.get("base_branch")
                    and workspace_row["base_commit_sha"] == after.get("base_sha")
                    and session_row["base_commit_sha"] == after.get("base_sha")
                    and workspace_row["head_sha"] == after.get("head_sha")
                    and workspace_row["tree_sha"] == after.get("tree_sha")
                    and session_row["head_commit_sha"] == after.get("head_sha")
                    and session_row["tree_sha"] == after.get("tree_sha")
                    and int(workspace_row["pr_number"] or 0) == pull_number
                    and int(session_row["pull_number"] or 0) == pull_number
                )
                if not state_matches:
                    raise MyGithub12Error(
                        "IDEMPOTENCY_CONFLICT",
                        "recorded retarget recovery result no longer matches current control-plane state",
                    )
                _fresh_retarget_github_identity(
                    service,
                    repository,
                    branch,
                    pull_number,
                    int(request["upstream_pull_number"]),
                    old_base_branch,
                    old_base_sha,
                    new_base_branch,
                    new_base_sha,
                    current_head,
                    current_tree,
                )
                return {"replayed": True, "before": replay["before"], "after": replay["after"], "audit": replay["audit"]}

            if int(workspace_row["revision"]) != expected_workspace_revision:
                raise MyGithub12Error("WORKSPACE_REVISION_MISMATCH", "workspace revision changed before retarget recovery")
            if int(session_row["session_revision"]) != expected_session_revision:
                raise MyGithub12Error("DEVELOPMENT_SESSION_REVISION_MISMATCH", "development session revision changed before retarget recovery")
            if workspace_row["status"] == "closed":
                raise MyGithub12Error("WORKSPACE_CLOSED", "closed Workspace cannot be recovered")
            if workspace_row["status"] != "drifted" or workspace_row["drift_reason"] != "branch_moved_externally":
                raise MyGithub12Error(
                    "RECOVERY_DRIFT_REASON_UNSUPPORTED",
                    "only branch_moved_externally drift can use retarget recovery",
                    {"status": workspace_row["status"], "drift_reason": workspace_row["drift_reason"]},
                )
            if session_row["status"] not in same_base._ALLOWED_SESSION_STATES:
                raise MyGithub12Error(
                    "DEVELOPMENT_SESSION_STATE_INVALID",
                    "development session state does not permit retarget recovery",
                    {"status": session_row["status"]},
                )
            if workspace_row["repository"] != repository or workspace_row["branch"] != branch:
                raise MyGithub12Error("RECOVERY_IDENTITY_MISMATCH", "Workspace repository/branch changed before retarget recovery")
            if session_row["workspace_id"] != workspace_id or session_row["repository"] != repository or session_row["branch"] != branch:
                raise MyGithub12Error("RECOVERY_IDENTITY_MISMATCH", "Development Session identity changed before retarget recovery")
            if workspace_row["head_sha"] != current_head or workspace_row["tree_sha"] != current_tree:
                raise MyGithub12Error("RECOVERY_IDENTITY_MISMATCH", "Workspace HEAD/Tree changed before retarget recovery")
            if session_row["head_commit_sha"] != old_session_head:
                raise MyGithub12Error(
                    "RECOVERY_ANCESTRY_MISMATCH",
                    "Development Session HEAD changed before retarget recovery",
                    {"expected_old_session_head": old_session_head, "actual": session_row["head_commit_sha"]},
                )
            if workspace_row["pr_number"] not in (None, pull_number) or session_row["pull_number"] not in (None, pull_number):
                raise MyGithub12Error(
                    "RECOVERY_PR_IDENTITY_MISMATCH",
                    "Workspace/Development Session are bound to another Pull Request",
                    {"workspace_pr_number": workspace_row["pr_number"], "session_pull_number": session_row["pull_number"], "pull_number": pull_number},
                )

            pinned_state = _classify_pinned_retarget_state(
                str(workspace_row["base_branch"]),
                str(workspace_row["base_commit_sha"]),
                str(session_row["base_branch"]),
                str(session_row["base_commit_sha"]),
                old_base_branch,
                old_base_sha,
                new_base_branch,
                new_base_sha,
            )
            if pinned_state != verification.get("pinned_base_state"):
                raise MyGithub12Error(
                    "RECOVERY_BASE_CHANGED",
                    "pinned retarget recovery state changed before atomic recovery",
                    {"expected_state": verification.get("pinned_base_state"), "actual_state": pinned_state},
                )
            pinned_before_branch = new_base_branch if pinned_state == PINNED_RETARGET_ALREADY_NEW else old_base_branch
            pinned_before_sha = new_base_sha if pinned_state == PINNED_RETARGET_ALREADY_NEW else old_base_sha

            other = db.execute(
                """SELECT workspace_id FROM workspaces WHERE repository=? AND branch=? AND workspace_id<>?
                AND (status='drifted' OR (status='active' AND lease_expires_at>?)) LIMIT 1""",
                (repository, branch, workspace_id, now),
            ).fetchone()
            if other:
                raise MyGithub12Error(
                    "RECOVERY_BRANCH_OWNERSHIP_CONFLICT",
                    "another active or drifted Workspace claims the recovery branch",
                    {"conflicting_workspace_id": other["workspace_id"]},
                )

            _fresh_retarget_github_identity(
                service,
                repository,
                branch,
                pull_number,
                int(request["upstream_pull_number"]),
                old_base_branch,
                old_base_sha,
                new_base_branch,
                new_base_sha,
                current_head,
                current_tree,
            )

            before = {
                "workspace_revision": int(workspace_row["revision"]),
                "session_revision": int(session_row["session_revision"]),
                "pinned_base_state": pinned_state,
                "workspace_base_branch": workspace_row["base_branch"],
                "workspace_base_sha": workspace_row["base_commit_sha"],
                "session_base_branch": session_row["base_branch"],
                "session_base_sha": session_row["base_commit_sha"],
                "workspace_head_sha": workspace_row["head_sha"],
                "workspace_tree_sha": workspace_row["tree_sha"],
                "session_head_sha": session_row["head_commit_sha"],
                "session_tree_sha": session_row["tree_sha"],
                "workspace_status": workspace_row["status"],
                "session_status": session_row["status"],
                "drift_reason": workspace_row["drift_reason"],
                "workspace_pr_number": workspace_row["pr_number"],
                "session_pull_number": session_row["pull_number"],
                "last_fast_ci_job_id": session_row["last_fast_ci_job_id"],
                "last_full_ci_job_id": session_row["last_full_ci_job_id"],
                "last_attestation_id": session_row["last_attestation_id"],
                "last_failure_resource_uri": session_row["last_failure_resource_uri"],
            }
            after = {
                "workspace_revision": int(workspace_row["revision"]) + 1,
                "session_revision": int(session_row["session_revision"]) + 1,
                "base_branch": new_base_branch,
                "base_sha": new_base_sha,
                "head_sha": current_head,
                "tree_sha": current_tree,
                "status": "active",
                "pull_number": pull_number,
                "lease_expires_at": lease_expires_at,
            }
            stale_ci_cleared = bool(
                session_row["last_fast_ci_job_id"] or session_row["last_full_ci_job_id"] or session_row["last_failure_resource_uri"]
            )
            stale_attestation_cleared = bool(session_row["last_attestation_id"])
            audit = {
                "repository": repository,
                "branch": branch,
                "pull_number": pull_number,
                "upstream_pull_number": int(request["upstream_pull_number"]),
                "workspace_id": workspace_id,
                "development_session_id": session_id,
                "old_base_branch": old_base_branch,
                "old_base_sha": old_base_sha,
                "new_base_branch": new_base_branch,
                "new_base_sha": new_base_sha,
                "pinned_base_state": pinned_state,
                "old_session_head": old_session_head,
                "current_head": current_head,
                "current_tree": current_tree,
                "task_pull_request": verification["github"]["task_pull_request"],
                "upstream_merge": verification["github"]["upstream_merge"],
                "base_transition": verification["deltas"]["base_transition"],
                "base_transition_paths": verification["deltas"]["base_transition_paths"],
                "actual_overlap_paths": verification["deltas"]["actual_overlap_paths"],
                "reviewed_overlap_paths": verification["deltas"]["reviewed_overlap_paths"],
                "old_task_ancestry": verification["deltas"]["old_task_ancestry"],
                "task_ancestry": verification["deltas"]["task_ancestry"],
                "new_base_ancestry": verification["deltas"]["new_base_ancestry"],
                "old_task_delta_paths": verification["deltas"]["old_task_delta_paths"],
                "new_task_delta_paths": verification["deltas"]["new_task_delta_paths"],
                "forward_task_delta_paths": verification["deltas"]["forward_task_delta_paths"],
                "historical_cumulative_task_delta_paths": verification["deltas"]["historical_cumulative_task_delta_paths"],
                "external_forward_delta_paths": verification["deltas"]["external_forward_delta_paths"],
                "recovery_scope_delta_paths": verification["deltas"]["recovery_scope_delta_paths"],
                "excluded_imported_base_paths": verification["deltas"]["excluded_imported_base_paths"],
                "excluded_unchanged_historical_cumulative_paths": verification["deltas"]["excluded_unchanged_historical_cumulative_paths"],
                "task_path_changes": verification["deltas"]["task_path_changes"],
                "unexplained_task_path_changes": verification["deltas"]["unexplained_task_path_changes"],
                "outside_scope_paths": verification["scope"].get("outside_scope_paths", []),
                "overlap_result": {
                    "historical_base_overlap_paths": verification["deltas"]["historical_base_overlap_paths"],
                    "current_base_overlap_paths": verification["deltas"]["current_base_overlap_paths"],
                    "base_task_overlap_paths": verification["deltas"]["base_task_overlap_paths"],
                    "workspace": verification["ownership"],
                },
                "scope": verification["scope"],
                "old_workspace_revision": before["workspace_revision"],
                "new_workspace_revision": after["workspace_revision"],
                "old_session_revision": before["session_revision"],
                "new_session_revision": after["session_revision"],
                "stale_ci_evidence_cleared": stale_ci_cleared,
                "stale_attestation_evidence_cleared": stale_attestation_cleared,
                "idempotency_identity": hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest(),
            }
            metadata["last_retarget_recovery"] = {
                "idempotency_key": idempotency_key,
                "request": request,
                "before": before,
                "after": after,
                "audit": audit,
            }

            ws_update = db.execute(
                """UPDATE workspaces SET base_branch=?,base_commit_sha=?,head_sha=?,tree_sha=?,status='active',
                drift_reason=NULL,lease_expires_at=?,index_commit_sha=NULL,pr_number=?,revision=revision+1,updated_at=?
                WHERE workspace_id=? AND revision=? AND status='drifted' AND drift_reason='branch_moved_externally'
                AND base_branch=? AND base_commit_sha=? AND (pr_number IS NULL OR pr_number=?)""",
                (
                    new_base_branch,
                    new_base_sha,
                    current_head,
                    current_tree,
                    lease_expires_at,
                    pull_number,
                    now,
                    workspace_id,
                    expected_workspace_revision,
                    pinned_before_branch,
                    pinned_before_sha,
                    pull_number,
                ),
            )
            if ws_update.rowcount != 1:
                raise MyGithub12Error("WORKSPACE_REVISION_MISMATCH", "Workspace changed while applying retarget recovery")

            session_update = db.execute(
                """UPDATE development_sessions SET status='active',base_branch=?,base_commit_sha=?,head_commit_sha=?,tree_sha=?,
                workspace_revision=?,lease_expires_at=?,index_commit_sha=NULL,pull_number=?,last_fast_ci_job_id=NULL,
                last_full_ci_job_id=NULL,last_attestation_id=NULL,last_failure_resource_uri=NULL,metadata_json=?,
                session_revision=session_revision+1,updated_at=?
                WHERE session_id=? AND session_revision=? AND base_branch=? AND base_commit_sha=? AND head_commit_sha=?
                AND (pull_number IS NULL OR pull_number=?)""",
                (
                    new_base_branch,
                    new_base_sha,
                    current_head,
                    current_tree,
                    after["workspace_revision"],
                    lease_expires_at,
                    pull_number,
                    json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
                    now,
                    session_id,
                    expected_session_revision,
                    pinned_before_branch,
                    pinned_before_sha,
                    old_session_head,
                    pull_number,
                ),
            )
            if session_update.rowcount != 1:
                raise MyGithub12Error(
                    "DEVELOPMENT_SESSION_REVISION_MISMATCH",
                    "Development Session changed while applying retarget recovery",
                )

            updated_row = db.execute("SELECT * FROM development_sessions WHERE session_id=?", (session_id,)).fetchone()
            sessions._append_event(
                db,
                updated_row,
                "retarget_recovery",
                session_row["status"],
                "active",
                after["session_revision"],
                audit,
            )
            _fresh_retarget_github_identity(
                service,
                repository,
                branch,
                pull_number,
                int(request["upstream_pull_number"]),
                old_base_branch,
                old_base_sha,
                new_base_branch,
                new_base_sha,
                current_head,
                current_tree,
            )
        return {"replayed": False, "before": before, "after": after, "audit": audit}
    except sqlite3.IntegrityError as exc:
        raise MyGithub12Error(
            "RECOVERY_BRANCH_OWNERSHIP_CONFLICT",
            "Workspace ownership changed while activating retargeted state",
            {"workspace_id": workspace_id, "repository": repository, "branch": branch},
        ) from exc


def plan_retargeted_task(
    service: Any,
    workspace: dict[str, Any],
    session: dict[str, Any],
    task_pull_request: dict[str, Any],
    current_base: dict[str, Any],
    branch_state: dict[str, Any],
) -> dict[str, Any]:
    repository = str(workspace.get("repository") or "")
    branch = str(workspace.get("branch") or "")
    old_base_branch = str(workspace.get("base_branch") or "")
    old_base_sha = str(workspace.get("base_commit_sha") or "")
    new_base_branch = str(current_base.get("branch") or "")
    new_base_sha = str(current_base.get("commit_sha") or "")
    old_session_head = str(session.get("head_commit_sha") or "")
    current_head = str(branch_state.get("commit_sha") or "")
    current_tree = str(branch_state.get("tree_sha") or "")
    pull_number = int(task_pull_request.get("pull_number") or 0)

    structural_exact = (
        workspace.get("status") == "drifted"
        and workspace.get("drift_reason") == "branch_moved_externally"
        and session.get("workspace_id") == workspace.get("workspace_id")
        and session.get("repository") == repository
        and session.get("branch") == branch
        and session.get("base_branch") == old_base_branch
        and session.get("base_commit_sha") == old_base_sha
        and old_base_branch
        and new_base_branch
        and old_base_branch != new_base_branch
        and old_base_sha
        and new_base_sha
        and task_pull_request.get("head_branch") == branch
        and task_pull_request.get("head_sha") == current_head
        and task_pull_request.get("base_branch") == new_base_branch
        and task_pull_request.get("base_sha") == new_base_sha
        and current_tree
        and pull_number > 0
    )
    if not structural_exact:
        raise MyGithub12Error(
            "RECOVERY_IDENTITY_MISMATCH",
            "retarget recovery planner requires exact drifted Workspace/Session/current PR identities",
        )

    upstream_pull_number = _find_upstream_pull_number(
        repository, old_base_branch, old_base_sha, new_base_branch,
    )
    repo, github_identity = _fresh_retarget_github_identity(
        service,
        repository,
        branch,
        pull_number,
        upstream_pull_number,
        old_base_branch,
        old_base_sha,
        new_base_branch,
        new_base_sha,
        current_head,
        current_tree,
    )
    deltas = _verify_retarget_deltas(
        repo,
        session,
        old_base_sha,
        new_base_sha,
        old_session_head,
        current_head,
        [],
        enforce_reviewed_overlap=False,
    )
    return {
        "reason": "WORKSPACE_BASE_RETARGETED_EXTERNALLY",
        "action": "recover_retargeted_development_task",
        "recovery_tool": "recover_retargeted_development_task",
        "manual_recovery_required": True,
        "repository": repository,
        "branch": branch,
        "pull_number": pull_number,
        "upstream_pull_number": upstream_pull_number,
        "workspace_id": workspace.get("workspace_id"),
        "development_session_id": session.get("session_id"),
        "expected_workspace_revision": workspace.get("revision"),
        "expected_session_revision": session.get("session_revision"),
        "expected_old_base_branch": old_base_branch,
        "expected_old_base_sha": old_base_sha,
        "expected_new_base_branch": new_base_branch,
        "expected_new_base_sha": new_base_sha,
        "expected_old_session_head_sha": old_session_head,
        "expected_current_head_sha": current_head,
        "expected_current_tree_sha": current_tree,
        "actual_overlap_paths": deltas["actual_overlap_paths"],
        "reviewed_overlap_required": bool(deltas["actual_overlap_paths"]),
        "preflight": {"github": github_identity, "deltas": deltas},
    }


def recover_retargeted_task(
    service: Any,
    repository: str,
    branch: str,
    pull_number: int,
    upstream_pull_number: int,
    workspace_id: str,
    development_session_id: str,
    expected_workspace_revision: int,
    expected_session_revision: int,
    expected_old_base_branch: str,
    expected_old_base_sha: str,
    expected_new_base_branch: str,
    expected_new_base_sha: str,
    expected_old_session_head_sha: str,
    expected_current_head_sha: str,
    expected_current_tree_sha: str,
    idempotency_key: str,
    lease_seconds: int = mygithub12.DEFAULT_LEASE_SECONDS,
    reviewed_overlap_paths_json: str = "[]",
) -> dict[str, Any]:
    if not repository or "/" not in repository or not branch:
        raise MyGithub12Error("SEARCH_QUERY_INVALID", "repository and branch are required")
    if int(pull_number) <= 0 or int(upstream_pull_number) <= 0:
        raise MyGithub12Error("SEARCH_QUERY_INVALID", "positive task/upstream pull numbers are required")
    if not workspace_id or not development_session_id or not idempotency_key:
        raise MyGithub12Error("SEARCH_QUERY_INVALID", "workspace_id, development_session_id and idempotency_key are required")
    if int(expected_workspace_revision) <= 0 or int(expected_session_revision) <= 0:
        raise MyGithub12Error("SEARCH_QUERY_INVALID", "positive expected Workspace/Session revisions are required")
    if not all((
        expected_old_base_branch,
        expected_old_base_sha,
        expected_new_base_branch,
        expected_new_base_sha,
        expected_old_session_head_sha,
        expected_current_head_sha,
        expected_current_tree_sha,
    )):
        raise MyGithub12Error("SEARCH_QUERY_INVALID", "exact old/new base and old/current task identities are required")
    if expected_old_base_branch == expected_new_base_branch:
        raise MyGithub12Error(
            "RECOVERY_BASE_CHANGED",
            "retarget recovery requires distinct old and new base branches",
            {"base_branch": expected_old_base_branch},
        )
    reviewed_overlap_paths = base_sync._parse_reviewed_overlap_paths(reviewed_overlap_paths_json)
    workspace = mygithub12.get_workspace(service, workspace_id)
    session = sessions.get_session(development_session_id)
    request = _retarget_request_identity(
        repository,
        branch,
        int(pull_number),
        int(upstream_pull_number),
        workspace_id,
        development_session_id,
        int(expected_workspace_revision),
        int(expected_session_revision),
        expected_old_base_branch,
        expected_old_base_sha,
        expected_new_base_branch,
        expected_new_base_sha,
        expected_old_session_head_sha,
        expected_current_head_sha,
        expected_current_tree_sha,
        reviewed_overlap_paths,
        int(lease_seconds),
    )

    if (
        workspace.get("repository") != repository
        or workspace.get("branch") != branch
        or session.get("workspace_id") != workspace_id
        or session.get("repository") != repository
        or session.get("branch") != branch
    ):
        raise MyGithub12Error(
            "RECOVERY_IDENTITY_MISMATCH",
            "Workspace/Development Session do not match requested repository/branch identities",
        )

    replay = _replay_retarget_result(session, workspace, request, idempotency_key)
    if replay:
        repo, github_identity = _fresh_retarget_github_identity(
            service,
            repository,
            branch,
            int(pull_number),
            int(upstream_pull_number),
            expected_old_base_branch,
            expected_old_base_sha,
            expected_new_base_branch,
            expected_new_base_sha,
            expected_current_head_sha,
            expected_current_tree_sha,
        )
        replay_audit = replay.get("audit") if isinstance(replay.get("audit"), dict) else {}
        ownership = base_sync._verify_base_sync_ownership(
            service,
            repo,
            workspace,
            expected_new_base_sha,
            list(replay_audit.get("recovery_scope_delta_paths") or []),
        )
        index = base_sync._base_sync_index_state(
            service,
            repository,
            expected_current_head_sha,
            expected_current_tree_sha,
            expected_new_base_sha,
            development_session_id,
        )
        scope_required = bool((replay_audit.get("scope") or {}).get("declaration_required"))
        return {
            "ok": True,
            "control_plane_recovery": "CONTROL_PLANE_RETARGET_RECOVERY_SUCCESS",
            "replayed": True,
            "workspace": workspace,
            "development_session": session,
            "before": replay.get("before"),
            "after": replay.get("after"),
            "audit": replay.get("audit"),
            "verification": {"github": github_identity, "ownership": ownership},
            "index": index,
            "index_required": index["index_required"],
            "scope_declaration_required": scope_required,
            "writer_ready": index["ready"] and not scope_required,
        }

    if workspace.get("status") == "closed":
        raise MyGithub12Error("WORKSPACE_CLOSED", "closed Workspace cannot be recovered")
    if workspace.get("status") != "drifted" or workspace.get("drift_reason") != "branch_moved_externally":
        raise MyGithub12Error(
            "RECOVERY_DRIFT_REASON_UNSUPPORTED",
            "only a drifted Workspace with branch_moved_externally may use retarget recovery",
            {"status": workspace.get("status"), "drift_reason": workspace.get("drift_reason")},
        )
    if int(workspace.get("revision") or 0) != int(expected_workspace_revision):
        raise MyGithub12Error("WORKSPACE_REVISION_MISMATCH", "workspace revision changed before retarget recovery")
    if int(session.get("session_revision") or 0) != int(expected_session_revision):
        raise MyGithub12Error("DEVELOPMENT_SESSION_REVISION_MISMATCH", "development session revision changed before retarget recovery")

    pinned_state = _classify_pinned_retarget_state(
        str(workspace.get("base_branch") or ""),
        str(workspace.get("base_commit_sha") or ""),
        str(session.get("base_branch") or ""),
        str(session.get("base_commit_sha") or ""),
        expected_old_base_branch,
        expected_old_base_sha,
        expected_new_base_branch,
        expected_new_base_sha,
    )
    if session.get("head_commit_sha") != expected_old_session_head_sha:
        raise MyGithub12Error(
            "RECOVERY_ANCESTRY_MISMATCH",
            "old Development Session HEAD differs from explicit retarget recovery identity",
            {"expected": expected_old_session_head_sha, "actual": session.get("head_commit_sha")},
        )
    if workspace.get("head_sha") != expected_current_head_sha or workspace.get("tree_sha") != expected_current_tree_sha:
        raise MyGithub12Error(
            "RECOVERY_IDENTITY_MISMATCH",
            "drifted Workspace does not carry expected current branch identity",
            {"workspace_head": workspace.get("head_sha"), "workspace_tree": workspace.get("tree_sha")},
        )

    repo, github_identity = _fresh_retarget_github_identity(
        service,
        repository,
        branch,
        int(pull_number),
        int(upstream_pull_number),
        expected_old_base_branch,
        expected_old_base_sha,
        expected_new_base_branch,
        expected_new_base_sha,
        expected_current_head_sha,
        expected_current_tree_sha,
    )
    deltas = _verify_retarget_deltas(
        repo,
        session,
        expected_old_base_sha,
        expected_new_base_sha,
        expected_old_session_head_sha,
        expected_current_head_sha,
        reviewed_overlap_paths,
        enforce_reviewed_overlap=True,
    )
    scope = base_sync._verify_base_sync_scope(workspace, deltas["recovery_scope_delta_paths"])
    ownership = base_sync._verify_base_sync_ownership(
        service,
        repo,
        workspace,
        expected_new_base_sha,
        deltas["recovery_scope_delta_paths"],
    )
    verification = {
        "github": github_identity,
        "pinned_base_state": pinned_state,
        "deltas": deltas,
        "scope": scope,
        "ownership": ownership,
    }
    recovered = _atomic_recover_retarget(
        service,
        request=request,
        idempotency_key=idempotency_key,
        verification=verification,
    )
    recovered_workspace = mygithub12.get_workspace(service, workspace_id)
    recovered_session = sessions.get_session(development_session_id)
    index = base_sync._base_sync_index_state(
        service,
        repository,
        expected_current_head_sha,
        expected_current_tree_sha,
        expected_new_base_sha,
        development_session_id,
    )
    return {
        "ok": True,
        "control_plane_recovery": "CONTROL_PLANE_RETARGET_RECOVERY_SUCCESS",
        "replayed": bool(recovered.get("replayed")),
        "workspace": recovered_workspace,
        "development_session": recovered_session,
        "before": recovered.get("before"),
        "after": recovered.get("after"),
        "audit": recovered.get("audit"),
        "verification": verification,
        "index": index,
        "index_required": index["index_required"],
        "scope_declaration_required": scope["declaration_required"],
        "writer_ready": index["ready"] and not scope["declaration_required"],
    }
