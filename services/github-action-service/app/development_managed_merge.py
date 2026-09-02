"""Managed PR merge lifecycle finalization and reconciliation.

This module deliberately does not perform the GitHub merge.  It verifies the
merged PR evidence and then finalizes the MyGithut12 control-plane lifecycle for
exactly one managed Development Session + Workspace pair.  Unmanaged PRs remain
compatible: no managed context is not an error for the public GitHub merge path.
"""
from __future__ import annotations

import re
from typing import Any

from app import development_session_store as sessions
from app import github_utils, mygithub12

MyGithub12Error = mygithub12.MyGithub12Error
_SHA_RE = re.compile(r"[0-9a-f]{40}")
_RECOVERABLE_PRE_MERGE_SESSION_STATES = {"active", "pr_ready"}


def _is_sha(value: Any) -> bool:
    return bool(_SHA_RE.fullmatch(str(value or "")))


def _error(code: str, message: str, details: dict[str, Any] | None = None) -> MyGithub12Error:
    return MyGithub12Error(code, message, details or {})


def _raise_result_error(result: dict[str, Any], default_code: str, default_message: str) -> None:
    if isinstance(result, dict) and result.get("ok") is False:
        err = result.get("error") if isinstance(result.get("error"), dict) else {}
        raise _error(
            str(err.get("code") or default_code),
            str(err.get("message") or default_message),
            dict(err.get("details") or {}),
        )


def _public_session_row(row: Any) -> dict[str, Any]:
    return sessions.get_session(str(row["session_id"]))


def _sessions_for_pr(
    repository: str,
    pull_number: int,
    head_branch: str,
    head_sha: str,
    base_branch: str,
) -> list[dict[str, Any]]:
    sessions.init_session_db()
    with sessions._db() as db:  # The session store owns this SQLite API.
        rows = db.execute(
            """SELECT * FROM development_sessions
               WHERE repository=? AND branch=? AND pull_number=?
                 AND head_commit_sha=? AND base_branch=?
               ORDER BY updated_at DESC LIMIT 20""",
            (repository, head_branch, int(pull_number), head_sha, base_branch),
        ).fetchall()
    return [_public_session_row(row) for row in rows]


def _workspace_candidates(
    service: Any,
    repository: str,
    pull_number: int,
    head_branch: str,
    head_sha: str,
    base_branch: str,
) -> list[dict[str, Any]]:
    listing = mygithub12.list_workspaces(
        service,
        repository=repository,
        branch=head_branch,
        limit=100,
    )
    candidates = []
    for workspace in listing.get("items", []):
        if (
            workspace.get("repository") == repository
            and workspace.get("branch") == head_branch
            and workspace.get("base_branch") == base_branch
            and workspace.get("head_sha") == head_sha
            and int(workspace.get("pr_number") or 0) == int(pull_number)
        ):
            candidates.append(workspace)
    return candidates


def _canonical_context(
    service: Any,
    repository: str,
    pull_number: int,
    head_branch: str,
    head_sha: str,
    base_branch: str,
    *,
    expected_workspace_id: str = "",
    expected_session_id: str = "",
    allow_legacy_exact_fallback: bool = False,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    workspaces = _workspace_candidates(
        service, repository, pull_number, head_branch, head_sha, base_branch
    )
    sessions_for_pr = _sessions_for_pr(
        repository, pull_number, head_branch, head_sha, base_branch
    )
    if expected_workspace_id:
        workspaces = [item for item in workspaces if item.get("workspace_id") == expected_workspace_id]
    if expected_session_id:
        sessions_for_pr = [item for item in sessions_for_pr if item.get("session_id") == expected_session_id]

    if (
        allow_legacy_exact_fallback
        and expected_workspace_id
        and expected_session_id
        and (not workspaces or not sessions_for_pr)
    ):
        try:
            workspace = mygithub12.get_workspace(service, expected_workspace_id)
            session = sessions.get_session(expected_session_id)
        except MyGithub12Error as exc:
            if exc.code in {"WORKSPACE_NOT_FOUND", "DEVELOPMENT_SESSION_NOT_FOUND"}:
                return None, None
            raise

        session_pull = session.get("pull_number")
        workspace_pull = workspace.get("pr_number")
        checks = {
            "workspace_id": workspace.get("workspace_id") == expected_workspace_id,
            "session_id": session.get("session_id") == expected_session_id,
            "session_workspace_id": session.get("workspace_id") == expected_workspace_id,
            "repository": session.get("repository") == workspace.get("repository") == repository,
            "branch": session.get("branch") == workspace.get("branch") == head_branch,
            "base_branch": session.get("base_branch") == workspace.get("base_branch") == base_branch,
            "head_sha": session.get("head_commit_sha") == workspace.get("head_sha") == head_sha,
            "tree_sha": session.get("tree_sha") == workspace.get("tree_sha"),
            "workspace_revision": int(session.get("workspace_revision") or 0) == int(workspace.get("revision") or 0),
            "session_pull_number": session_pull is None or int(session_pull) == int(pull_number),
            "workspace_pull_number": workspace_pull is None or int(workspace_pull) == int(pull_number),
        }
        mismatches = [key for key, ok in checks.items() if not ok]
        if mismatches:
            raise _error(
                "MANAGED_PR_IDENTITY_MISMATCH",
                "legacy managed PR Workspace and Development Session identities differ",
                {
                    "repository": repository,
                    "pull_number": int(pull_number),
                    "workspace_id": workspace.get("workspace_id"),
                    "session_id": session.get("session_id"),
                    "mismatches": mismatches,
                },
            )
        return workspace, session

    if not workspaces and not sessions_for_pr:
        return None, None
    if len(workspaces) != 1:
        raise _error(
            "MANAGED_PR_CONTEXT_AMBIGUOUS",
            "managed PR Workspace context is not unique",
            {
                "repository": repository,
                "pull_number": int(pull_number),
                "workspace_count": len(workspaces),
                "workspace_ids": [item.get("workspace_id") for item in workspaces],
            },
        )
    workspace = workspaces[0]
    sessions_for_pr = [
        item for item in sessions_for_pr
        if item.get("workspace_id") == workspace.get("workspace_id")
    ]
    if len(sessions_for_pr) != 1:
        raise _error(
            "MANAGED_PR_CONTEXT_AMBIGUOUS",
            "managed PR Development Session context is not unique",
            {
                "repository": repository,
                "pull_number": int(pull_number),
                "session_count": len(sessions_for_pr),
                "session_ids": [item.get("session_id") for item in sessions_for_pr],
                "workspace_id": workspace.get("workspace_id"),
            },
        )
    session = sessions_for_pr[0]
    mismatches = []
    checks = {
        "workspace_id": session.get("workspace_id") == workspace.get("workspace_id"),
        "repository": session.get("repository") == workspace.get("repository") == repository,
        "branch": session.get("branch") == workspace.get("branch") == head_branch,
        "base_branch": session.get("base_branch") == workspace.get("base_branch") == base_branch,
        "head_sha": session.get("head_commit_sha") == workspace.get("head_sha") == head_sha,
        "tree_sha": session.get("tree_sha") == workspace.get("tree_sha"),
        "pull_number": int(session.get("pull_number") or 0) == int(workspace.get("pr_number") or 0) == int(pull_number),
    }
    for key, ok in checks.items():
        if not ok:
            mismatches.append(key)
    if mismatches:
        raise _error(
            "MANAGED_PR_IDENTITY_MISMATCH",
            "managed PR Workspace and Development Session identities differ",
            {
                "repository": repository,
                "pull_number": int(pull_number),
                "workspace_id": workspace.get("workspace_id"),
                "session_id": session.get("session_id"),
                "mismatches": mismatches,
            },
        )
    return workspace, session


def _resolve_pr(repository: str, pull_number: int, pull_request: dict[str, Any] | None) -> dict[str, Any]:
    pr = dict(pull_request or {})
    if not pr:
        pr = github_utils.get_github_pull_request(repository, int(pull_number))
    _raise_result_error(pr, "PULL_REQUEST_NOT_FOUND", "pull request was not found")
    if pr.get("pull_number") and int(pr.get("pull_number") or 0) != int(pull_number):
        raise _error(
            "MANAGED_PR_IDENTITY_MISMATCH",
            "pull request evidence number differs from requested pull_number",
            {"pull_number": int(pull_number), "evidence_pull_number": pr.get("pull_number")},
        )
    return pr


def _current_base_sha(service: Any, repository: str, base_branch: str, merge_result: dict[str, Any]) -> str:
    candidate = str(merge_result.get("base_head_after") or merge_result.get("base_sha") or "")
    if _is_sha(candidate):
        return candidate
    branch = service.client.get_branch(repository, base_branch)
    sha = str(getattr(getattr(branch, "commit", None), "sha", "") or "") if branch else ""
    if not _is_sha(sha):
        raise _error(
            "MERGE_EVIDENCE_INCOMPLETE",
            "current base SHA could not be resolved for managed merge finalization",
            {"repository": repository, "base_branch": base_branch},
        )
    return sha


def _verify_merge_evidence(
    service: Any,
    repository: str,
    pull_number: int,
    expected_head_sha: str,
    expected_base_branch: str,
    pr: dict[str, Any],
    merge_result: dict[str, Any],
) -> dict[str, Any]:
    head_sha = str(pr.get("head_sha") or merge_result.get("head_sha") or expected_head_sha or "")
    head_branch = str(pr.get("head_branch") or merge_result.get("head_branch") or "")
    base_branch = str(pr.get("base_branch") or merge_result.get("base_branch") or expected_base_branch or "")
    merge_commit_sha = str(pr.get("merge_commit_sha") or merge_result.get("merge_commit_sha") or "")
    if not _is_sha(expected_head_sha) or head_sha != expected_head_sha:
        raise _error(
            "MANAGED_PR_IDENTITY_MISMATCH",
            "merged PR head SHA does not match the managed Session head",
            {"expected_head_sha": expected_head_sha, "pull_request_head_sha": head_sha},
        )
    if base_branch != expected_base_branch:
        raise _error(
            "MANAGED_PR_IDENTITY_MISMATCH",
            "merged PR base branch does not match the managed Session base branch",
            {"expected_base_branch": expected_base_branch, "pull_request_base_branch": base_branch},
        )
    if pr.get("merged") is not True and merge_result.get("merged") is not True:
        raise _error(
            "PULL_REQUEST_NOT_MERGED",
            "managed merge finalization requires merged PR evidence",
            {"repository": repository, "pull_number": int(pull_number)},
        )
    if not _is_sha(merge_commit_sha):
        raise _error(
            "MERGE_EVIDENCE_INCOMPLETE",
            "merged PR evidence does not include a full merge commit SHA",
            {"repository": repository, "pull_number": int(pull_number)},
        )
    current_base = _current_base_sha(service, repository, expected_base_branch, {**pr, **merge_result})
    ancestry = {
        "verified": False,
        "method": "merge_commit_equals_current_base" if merge_commit_sha == current_base else "github_compare",
        "merge_commit_sha": merge_commit_sha,
        "current_base_sha": current_base,
    }
    if merge_commit_sha == current_base:
        ancestry["verified"] = True
    else:
        try:
            repo = mygithub12._service_repo(service, repository)
            comparison = repo.compare(merge_commit_sha, current_base)
            merge_base = str(comparison.merge_base_commit.sha) if getattr(comparison, "merge_base_commit", None) else ""
            ahead_by = int(getattr(comparison, "ahead_by", 0) or 0)
            behind_by = int(getattr(comparison, "behind_by", 0) or 0)
            ancestry.update({
                "merge_base": merge_base,
                "ahead_by": ahead_by,
                "behind_by": behind_by,
                "verified": merge_base == merge_commit_sha and behind_by == 0,
            })
        except Exception as exc:
            ancestry.update({"error_type": type(exc).__name__})
    if ancestry.get("verified") is not True:
        raise _error(
            "MERGE_EVIDENCE_UNVERIFIED",
            "merged PR commit is not proven to be in the current base branch",
            {
                "repository": repository,
                "pull_number": int(pull_number),
                "merge_commit_sha": merge_commit_sha,
                "current_base_sha": current_base,
                "ancestry": ancestry,
            },
        )
    return {
        "repository": repository,
        "pull_number": int(pull_number),
        "head_branch": head_branch,
        "head_sha": head_sha,
        "base_branch": base_branch,
        "merge_commit_sha": merge_commit_sha,
        "current_base_sha": current_base,
        "merged_at": pr.get("merged_at") or merge_result.get("merged_at"),
        "ancestry": ancestry,
    }


def preflight_managed_pr_context(
    service: Any,
    repository: str,
    pull_number: int,
    expected_head_sha: str,
    expected_base_branch: str = "main",
    *,
    pull_request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Read-only exact managed-context preflight for a PR merge."""
    pr = _resolve_pr(repository, int(pull_number), pull_request)
    head_sha = str(pr.get("head_sha") or expected_head_sha or "")
    head_branch = str(pr.get("head_branch") or "")
    base_branch = str(pr.get("base_branch") or expected_base_branch or "")
    if expected_head_sha and head_sha != expected_head_sha:
        raise _error(
            "MANAGED_PR_IDENTITY_MISMATCH",
            "pull request head SHA does not match expected_head_sha",
            {"expected_head_sha": expected_head_sha, "pull_request_head_sha": head_sha},
        )
    if base_branch != expected_base_branch:
        raise _error(
            "MANAGED_PR_IDENTITY_MISMATCH",
            "pull request base branch does not match expected_base_branch",
            {"expected_base_branch": expected_base_branch, "pull_request_base_branch": base_branch},
        )
    workspace, session = _canonical_context(
        service, repository, int(pull_number), head_branch, head_sha, base_branch
    )
    if workspace is None and session is None:
        return {
            "ok": True,
            "status": "not_applicable",
            "managed": False,
            "reason": "NO_MANAGED_CONTEXT",
            "repository": repository,
            "pull_number": int(pull_number),
        }
    return {
        "ok": True,
        "status": "applicable",
        "managed": True,
        "repository": repository,
        "pull_number": int(pull_number),
        "workspace": workspace,
        "development_session": session,
    }


def finalize_managed_pr_merge(
    service: Any,
    repository: str,
    pull_number: int,
    expected_head_sha: str,
    expected_base_branch: str = "main",
    merge_result: dict[str, Any] | None = None,
    *,
    pull_request: dict[str, Any] | None = None,
    expected_workspace_id: str = "",
    expected_session_id: str = "",
    expected_workspace_revision: int = 0,
    expected_session_revision: int = 0,
    allow_no_context: bool = True,
) -> dict[str, Any]:
    """Finalize exactly one managed PR lifecycle after GitHub merge succeeded."""
    merge_result = dict(merge_result or {})
    pr = _resolve_pr(repository, int(pull_number), pull_request)
    evidence = _verify_merge_evidence(
        service,
        repository,
        int(pull_number),
        expected_head_sha,
        expected_base_branch,
        pr,
        merge_result,
    )
    workspace, session = _canonical_context(
        service,
        repository,
        int(pull_number),
        str(evidence["head_branch"]),
        str(evidence["head_sha"]),
        str(evidence["base_branch"]),
        expected_workspace_id=expected_workspace_id,
        expected_session_id=expected_session_id,
        allow_legacy_exact_fallback=True,
    )
    if workspace is None and session is None:
        if allow_no_context:
            return {
                "ok": True,
                "status": "not_applicable",
                "managed": False,
                "reason": "NO_MANAGED_CONTEXT",
                "repository": repository,
                "pull_number": int(pull_number),
                "evidence": evidence,
            }
        raise _error(
            "MANAGED_PR_CONTEXT_NOT_FOUND",
            "managed PR finalization requires an exact Workspace and Development Session",
            {"repository": repository, "pull_number": int(pull_number)},
        )
    assert workspace is not None and session is not None
    if expected_workspace_revision and int(workspace.get("revision") or 0) != int(expected_workspace_revision):
        raise _error(
            "WORKSPACE_REVISION_MISMATCH",
            "workspace revision changed before managed merge finalization",
            {"expected": int(expected_workspace_revision), "actual": workspace.get("revision")},
        )
    if expected_session_revision and int(session.get("session_revision") or 0) != int(expected_session_revision):
        raise _error(
            "DEVELOPMENT_SESSION_REVISION_MISMATCH",
            "development session revision changed before managed merge finalization",
            {"expected": int(expected_session_revision), "actual": session.get("session_revision")},
        )
    workspace_status = str(workspace.get("persisted_status") or workspace.get("status") or "")
    if session.get("status") == "merged" and workspace_status == "closed":
        return {
            "ok": True,
            "status": "already_finalized",
            "managed": True,
            "repository": repository,
            "pull_number": int(pull_number),
            "development_session": session,
            "workspace": workspace,
            "evidence": evidence,
            "idempotent": True,
        }
    if session.get("status") not in _RECOVERABLE_PRE_MERGE_SESSION_STATES:
        raise _error(
            "DEVELOPMENT_SESSION_STATE_INVALID",
            "development session state does not allow managed merge finalization",
            {"status": session.get("status"), "allowed": sorted(_RECOVERABLE_PRE_MERGE_SESSION_STATES)},
        )
    if workspace_status != "active":
        raise _error(
            "WORKSPACE_CLOSED",
            "managed merge finalization requires the active owning Workspace record",
            {"workspace_id": workspace.get("workspace_id"), "status": workspace.get("status"), "persisted_status": workspace_status},
        )
    finalized = sessions.finalize_merged_session_workspace(
        str(session["session_id"]),
        int(session["session_revision"]),
        str(workspace["workspace_id"]),
        int(workspace["revision"]),
        merge_evidence=evidence,
    )
    return {
        "ok": True,
        "status": "finalized",
        "managed": True,
        "repository": repository,
        "pull_number": int(pull_number),
        "development_session": finalized["session"],
        "workspace": finalized["workspace"],
        "audit": finalized.get("audit"),
        "evidence": evidence,
        "idempotent": False,
    }
