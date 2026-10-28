"""Explicit fail-stop recovery for externally advanced development branches.

This module only adopts an already-existing, freshly verified GitHub branch
identity into Workspace/Development Session control-plane state. It never
moves a Git ref and never writes repository files.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from typing import Any

from app import development_session_store as sessions
from app import mygithub12
from app import mygithub12_workspace as workspace_scope

MyGithub12Error = mygithub12.MyGithub12Error
_ALLOWED_SESSION_STATES = frozenset({"active", "blocked", "drifted", "pr_ready"})
_ACTIVE_STALE_SESSION_STATES = frozenset(sessions.ACTIVE_STATES | {"validation_failed"})
_TERMINAL_SESSION_STATES = frozenset(sessions.TERMINAL_STATES)
_COMPARE_FILE_LIMIT = 300
_EXACT_COMMIT_SHA = re.compile(r"[0-9a-fA-F]{40}\Z")


def reconcile_drifted_validation(session: dict[str, Any], workspace: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Settle old-head CI evidence only; never adopt a HEAD or renew a lease."""
    if session.get("status") not in {"validating_fast", "validating_full"}:
        return session, None
    from app import development_resume as resume
    if workspace.get("owner") != session.get("owner"):
        raise MyGithub12Error("RECOVERY_OWNER_MISMATCH", "validation recovery Writer ownership differs")
    recovered, evidence, blocker = resume._reconcile_transient_validation(
        session, workspace, allow_branch_drift=True,
    )
    if blocker:
        raise MyGithub12Error(blocker, "old-head validation must be resolved before branch recovery", {
            "validation_recovery": evidence,
            "expected_workspace_revision": workspace["revision"],
            "expected_session_revision": recovered["session_revision"],
            "next_allowed_actions": ["get_private_ci_job", "resume_development_task"],
        })
    if not evidence.get("reconciled") or recovered.get("status") != "active":
        raise MyGithub12Error("DEVELOPMENT_SESSION_RECOVERY_REQUIRED", "validation did not settle to an active historical Session")
    return recovered, {**evidence, "session_revision_before": session["session_revision"],
                       "session_revision_after": recovered["session_revision"]}


def recovery_session_revision(request: dict[str, Any], verification: dict[str, Any]) -> int:
    evidence = verification.get("validation_reconciliation")
    if not evidence:
        return int(request["expected_session_revision"])
    if (not evidence.get("reconciled")
            or int(evidence["session_revision_before"]) != int(request["expected_session_revision"])
            or int(evidence["session_revision_after"]) != int(request["expected_session_revision"]) + 1):
        raise MyGithub12Error("DEVELOPMENT_SESSION_REVISION_MISMATCH", "validation settlement CAS proof changed")
    return int(evidence["session_revision_after"])


def _recovery_record_key(recovery_kind: str) -> str:
    if recovery_kind == "active_workspace_stale_session":
        return "last_active_workspace_stale_session_recovery"
    return "last_manual_branch_recovery"


def _base_identity_mode(
    workspace: dict[str, Any], session: dict[str, Any], expected_base_branch: str, expected_base_sha: str,
) -> str:
    """Return exact or guarded legacy-normalization identity mode."""
    workspace_base = str(workspace.get("base_branch") or "")
    session_base = str(session.get("base_branch") or "")
    if workspace_base == expected_base_branch and session_base == expected_base_branch:
        return "exact"
    malformed = (
        workspace_base == session_base
        and bool(_EXACT_COMMIT_SHA.fullmatch(workspace_base))
        and workspace_base == expected_base_sha
        and workspace.get("base_commit_sha") == expected_base_sha
        and session.get("base_commit_sha") == expected_base_sha
        and not _EXACT_COMMIT_SHA.fullmatch(str(expected_base_branch or ""))
    )
    if malformed:
        if workspace.get("owner") != session.get("owner"):
            raise MyGithub12Error(
                "RECOVERY_OWNER_MISMATCH",
                "Workspace and Development Session owners differ during base identity normalization",
            )
        return "normalize_legacy_commit_base"
    raise MyGithub12Error(
        "RECOVERY_IDENTITY_MISMATCH",
        "Workspace/Development Session base identity does not match the requested branch",
        {
            "workspace_base_branch": workspace_base,
            "session_base_branch": session_base,
            "expected_base_branch": expected_base_branch,
        },
    )


def _request_identity(
    repository: str,
    branch: str,
    workspace_id: str,
    development_session_id: str,
    expected_workspace_revision: int,
    expected_session_revision: int,
    expected_current_head_sha: str,
    expected_current_tree_sha: str,
    expected_base_branch: str,
    expected_base_sha: str,
    lease_seconds: int,
    reviewed_scope_expansion_paths: list[str] | None = None,
    recovery_kind: str = "manual_branch_recovery",
    reviewed_active_overlap_paths: list[str] | None = None,
) -> dict[str, Any]:
    request = {
        "repository": repository,
        "branch": branch,
        "workspace_id": workspace_id,
        "development_session_id": development_session_id,
        "expected_workspace_revision": int(expected_workspace_revision),
        "expected_session_revision": int(expected_session_revision),
        "expected_current_head_sha": expected_current_head_sha,
        "expected_current_tree_sha": expected_current_tree_sha,
        "expected_base_branch": expected_base_branch,
        "expected_base_sha": expected_base_sha,
        "lease_seconds": int(lease_seconds),
        "reviewed_scope_expansion_paths": sorted(reviewed_scope_expansion_paths or []),
    }
    # Keep historical drift idempotency payloads compatible across upgrades.
    if recovery_kind != "manual_branch_recovery":
        request["recovery_kind"] = recovery_kind
    if recovery_kind == "active_workspace_stale_session" and reviewed_active_overlap_paths:
        request["reviewed_active_overlap_paths"] = sorted(reviewed_active_overlap_paths)
    return request


def _replay_result(
    session: dict[str, Any],
    workspace: dict[str, Any],
    request: dict[str, Any],
    idempotency_key: str,
) -> dict[str, Any] | None:
    metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    recovery_kind = str(request.get("recovery_kind") or "manual_branch_recovery")
    record = metadata.get(_recovery_record_key(recovery_kind)) if isinstance(metadata, dict) else None
    if not isinstance(record, dict) or record.get("idempotency_key") != idempotency_key:
        return None
    if record.get("request") != request:
        raise MyGithub12Error(
            "IDEMPOTENCY_CONFLICT",
            "manual branch recovery idempotency key was reused with a different payload",
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
        and workspace.get("head_sha") == after.get("head_sha")
        and workspace.get("tree_sha") == after.get("tree_sha")
        and session.get("head_commit_sha") == after.get("head_sha")
        and session.get("tree_sha") == after.get("tree_sha")
        and workspace.get("base_branch") == after.get("base_branch")
        and session.get("base_branch") == after.get("base_branch")
        and workspace.get("base_commit_sha") == after.get("base_commit_sha")
        and session.get("base_commit_sha") == after.get("base_commit_sha")
        and workspace.get("scope") == after.get("scope")
        and workspace.get("owner") == session.get("owner") == record.get("before", {}).get("workspace_owner")
        and float(workspace.get("lease_expires_at") or 0) > sessions._now()
        and float(session.get("lease_expires_at") or 0) == float(workspace.get("lease_expires_at") or 0)
    )
    if not matches:
        raise MyGithub12Error(
            "IDEMPOTENCY_CONFLICT",
            "recorded manual branch recovery result no longer matches current control-plane state",
            {"workspace_id": workspace.get("workspace_id"), "development_session_id": session.get("session_id")},
        )
    return record


def _verify_pinned_base_advance(
    repo: Any, workspace: dict[str, Any], session: dict[str, Any], live_base_sha: str,
) -> dict[str, Any]:
    """Prove the pinned base and task branch descend from a verified common commit.

    A Workspace can be pinned to a newer main commit than the feature branch's
    actual fork point. Session adoption does not merge either side, so this is
    safe when Git proves both lineages and the exact task/live deltas. Their
    overlapping paths are audit information for later integration, not a
    reason to block this control-plane-only recovery.
    """
    pinned = str(workspace.get("base_commit_sha") or "")
    if pinned != session.get("base_commit_sha"):
        raise MyGithub12Error("RECOVERY_BASE_CHANGED", "Workspace and Session pinned bases differ")
    evidence = {"pinned_base_sha": pinned, "live_base_sha": live_base_sha, "base_advanced": pinned != live_base_sha}
    if pinned == live_base_sha:
        return {
            **evidence,
            "base_ancestry": {
                "verified": True,
                "label": "resume_base_advance",
                "base_sha": pinned,
                "head_sha": live_base_sha,
                "merge_base": pinned,
                "ahead_by": 0,
                "behind_by": 0,
            },
            "overlapping_paths": [],
            "overlap_blocks_session_adoption": False,
        }
    from app.development_base_sync_recovery import _compare_delta

    def compare_delta(
        base_sha: str,
        head_sha: str,
        *,
        label: str,
        require_advance: bool = False,
        allow_non_ancestor_path_classification: bool = False,
    ) -> tuple[dict[str, Any], list[str]]:
        if base_sha == head_sha:
            if require_advance:
                raise MyGithub12Error(
                    "RECOVERY_ANCESTRY_MISMATCH",
                    f"{label} must be a strict forward advance",
                    {"base_sha": base_sha, "head_sha": head_sha},
                )
            return {
                "verified": True,
                "label": label,
                "base_sha": base_sha,
                "head_sha": head_sha,
                "merge_base": base_sha,
                "ahead_by": 0,
                "behind_by": 0,
            }, []
        return _compare_delta(
            repo,
            base_sha,
            head_sha,
            label=label,
            require_advance=require_advance,
            allow_non_ancestor_path_classification=allow_non_ancestor_path_classification,
        )

    # The historical pin must still be on the live base's forward lineage.
    # A branch rewrite or unreadable base delta remains a hard failure.
    base_ancestry, _ = compare_delta(pinned, live_base_sha, label="resume_base_advance", require_advance=True)

    # Do not assume the historical pin is an ancestor of the task branch. Ask
    # Git for their exact merge-base, then independently prove that it is an
    # ancestor of the pin, current task HEAD, and live base.
    task_ancestry, _ = compare_delta(
        pinned,
        str(workspace["head_sha"]),
        label="resume_pinned_task_merge_base",
        allow_non_ancestor_path_classification=True,
    )
    task_merge_base = str(task_ancestry.get("merge_base") or "")
    if not _EXACT_COMMIT_SHA.fullmatch(task_merge_base):
        raise MyGithub12Error(
            "RECOVERY_ANCESTRY_MISMATCH",
            "historical pinned base and task branch have no verifiable exact merge-base",
            {"pinned_base_sha": pinned, "task_head_sha": workspace.get("head_sha"), "ancestry": task_ancestry},
        )
    task_head = str(workspace["head_sha"])
    ahead_by = int(task_ancestry.get("ahead_by") or 0)
    behind_by = int(task_ancestry.get("behind_by") or 0)
    coherent_compare = (
        (pinned == task_head and task_merge_base == pinned and ahead_by == 0 and behind_by == 0)
        or (task_merge_base == pinned and ahead_by > 0 and behind_by == 0)
        or (task_merge_base == task_head and ahead_by == 0 and behind_by > 0)
        or (task_merge_base not in {pinned, task_head} and ahead_by > 0 and behind_by > 0)
    )
    if not coherent_compare:
        raise MyGithub12Error(
            "RECOVERY_ANCESTRY_MISMATCH",
            "historical base/task comparison did not provide a coherent Git ancestry result",
            {"pinned_base_sha": pinned, "task_head_sha": task_head, "ancestry": task_ancestry},
        )

    merge_base_to_pin, _ = compare_delta(
        task_merge_base, pinned, label="resume_task_merge_base_to_pinned", require_advance=task_merge_base != pinned,
    )
    merge_base_to_task, task_paths = compare_delta(
        task_merge_base,
        str(workspace["head_sha"]),
        label="resume_task_merge_base_to_head",
        require_advance=task_merge_base != str(workspace["head_sha"]),
    )
    merge_base_to_live, base_paths = compare_delta(
        task_merge_base,
        live_base_sha,
        label="resume_task_merge_base_to_live_base",
        require_advance=task_merge_base != live_base_sha,
    )
    overlap = sorted(set(base_paths) & set(task_paths))
    evidence.update({
        "base_ancestry": base_ancestry,
        "task_ancestry": task_ancestry,
        "task_merge_base_sha": task_merge_base,
        "merge_base_to_pinned": merge_base_to_pin,
        "merge_base_to_task_head": merge_base_to_task,
        "merge_base_to_live_base": merge_base_to_live,
        "base_changed_paths": base_paths,
        "task_changed_paths": task_paths,
        "overlapping_paths": overlap,
        "overlap_blocks_session_adoption": False,
    })
    return evidence


def _fresh_github_identity(
    service: Any,
    repository: str,
    branch: str,
    expected_head_sha: str,
    expected_tree_sha: str,
    base_branch: str,
    expected_base_sha: str,
) -> tuple[Any, dict[str, str]]:
    repo = mygithub12._service_repo(service, repository)
    branch_state = service.client.get_branch(repository, branch)
    if not branch_state:
        raise MyGithub12Error(
            "RECOVERY_BRANCH_DELETED",
            "recovery branch no longer exists",
            {"repository": repository, "branch": branch},
        )
    actual_head = str(branch_state.commit.sha)
    if actual_head != expected_head_sha:
        raise MyGithub12Error(
            "RECOVERY_HEAD_MISMATCH",
            "recovery branch HEAD changed",
            {"expected": expected_head_sha, "actual": actual_head},
        )
    actual_tree = mygithub12._tree_sha(repo.get_commit(actual_head))
    if actual_tree != expected_tree_sha:
        raise MyGithub12Error(
            "RECOVERY_TREE_MISMATCH",
            "recovery branch Tree changed",
            {"expected": expected_tree_sha, "actual": actual_tree},
        )
    base_state = service.client.get_branch(repository, base_branch)
    if not base_state:
        raise MyGithub12Error(
            "RECOVERY_BASE_CHANGED",
            "recovery base branch no longer exists",
            {"base_branch": base_branch},
        )
    actual_base = str(base_state.commit.sha)
    if actual_base != expected_base_sha:
        raise MyGithub12Error(
            "RECOVERY_BASE_CHANGED",
            "recovery base branch HEAD changed",
            {"base_branch": base_branch, "expected": expected_base_sha, "actual": actual_base},
        )
    return repo, {
        "head_sha": actual_head,
        "tree_sha": actual_tree,
        "base_branch": base_branch,
        "base_sha": actual_base,
    }


def _verify_forward_only(repo: Any, session: dict[str, Any], current_head: str) -> tuple[dict[str, Any], list[str]]:
    old_head = str(session["head_commit_sha"])
    if old_head == current_head:
        raise MyGithub12Error(
            "RECOVERY_ANCESTRY_MISMATCH",
            "manual drift recovery requires a forward-only branch advance",
            {"old_session_head": old_head, "current_head": current_head},
        )
    try:
        old_tree = mygithub12._tree_sha(repo.get_commit(old_head))
        comparison = repo.compare(old_head, current_head)
        merge_base = str(comparison.merge_base_commit.sha) if getattr(comparison, "merge_base_commit", None) else ""
        ahead_by = int(getattr(comparison, "ahead_by", 0) or 0)
        behind_by = int(getattr(comparison, "behind_by", 0) or 0)
        files = list(getattr(comparison, "files", []) or [])
    except Exception as exc:
        raise MyGithub12Error(
            "RECOVERY_ANCESTRY_MISMATCH",
            "recovery ancestry could not be verified",
            {"old_session_head": old_head, "current_head": current_head, "cause_type": type(exc).__name__},
        ) from exc
    evidence = {
        "verified": old_tree == session.get("tree_sha") and merge_base == old_head and ahead_by > 0 and behind_by == 0,
        "old_session_head": old_head,
        "old_session_tree": session.get("tree_sha"),
        "old_commit_tree": old_tree,
        "current_head": current_head,
        "merge_base": merge_base,
        "ahead_by": ahead_by,
        "behind_by": behind_by,
    }
    if not evidence["verified"]:
        raise MyGithub12Error(
            "RECOVERY_ANCESTRY_MISMATCH",
            "old Development Session HEAD is not a verified ancestor of current branch HEAD",
            {"ancestry": evidence},
        )
    if len(files) >= _COMPARE_FILE_LIMIT:
        raise MyGithub12Error(
            "RECOVERY_SCOPE_INCOMPLETE",
            "changed-path comparison reached the bounded recovery limit",
            {"file_count": len(files), "limit": _COMPARE_FILE_LIMIT},
        )
    changed_paths = sorted({
        str(path)
        for item in files
        for path in (getattr(item, "filename", None), getattr(item, "previous_filename", None))
        if path
    })
    return evidence, changed_paths


def _parse_reviewed_scope_expansion_paths(value: str) -> list[str]:
    try:
        paths = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError) as exc:
        raise MyGithub12Error(
            "SEARCH_QUERY_INVALID",
            "reviewed_scope_expansion_paths_json must be a JSON array of exact Git paths",
        ) from exc
    if not isinstance(paths, list):
        raise MyGithub12Error(
            "SEARCH_QUERY_INVALID",
            "reviewed_scope_expansion_paths_json must be a JSON array of exact Git paths",
        )
    reviewed: list[str] = []
    for path in paths:
        if not isinstance(path, str) or not path or path in reviewed:
            raise MyGithub12Error(
                "SEARCH_QUERY_INVALID",
                "reviewed_scope_expansion_paths_json must contain unique non-empty exact Git paths",
            )
        mygithub12._safe_path(path)
        reviewed.append(path)
    return sorted(reviewed)


def _verify_scope(
    workspace: dict[str, Any], changed_paths: list[str], reviewed_scope_expansion_paths: list[str] | None = None,
) -> dict[str, Any]:
    scope = workspace.get("scope") if isinstance(workspace.get("scope"), dict) else {}
    declared = [str(value).strip().strip("/") for value in (scope.get("paths") or []) if str(value).strip().strip("/")]
    outside = [
        path for path in changed_paths
        if not any(workspace_scope.scope_path_matches(path, declaration) for declaration in declared)
    ]
    reviewed = sorted(reviewed_scope_expansion_paths or [])
    if reviewed != outside:
        raise MyGithub12Error(
            "RECOVERY_SCOPE_VIOLATION",
            "reviewed recovery scope expansion must exactly equal changed paths outside the Workspace declared scope",
            {
                "workspace_id": workspace.get("workspace_id"),
                "outside_scope_paths": outside,
                "required_scope_expansion_paths": outside,
                "reviewed_scope_expansion_paths": reviewed,
                "missing_reviewed_scope_expansion_paths": sorted(set(outside) - set(reviewed)),
                "unexpected_reviewed_scope_expansion_paths": sorted(set(reviewed) - set(outside)),
            },
        )
    previous_scope = json.loads(json.dumps(scope, ensure_ascii=False))
    resulting_scope = json.loads(json.dumps(scope, ensure_ascii=False))
    existing_paths = list(resulting_scope.get("paths") or [])
    if reviewed:
        resulting_scope["paths"] = existing_paths + [path for path in reviewed if path not in existing_paths]
    return {
        "verified": True,
        "declared_paths": declared,
        "changed_paths": changed_paths,
        "outside_scope_paths": outside,
        "required_scope_expansion_paths": outside,
        "reviewed_scope_expansion_paths": reviewed,
        "previous_scope": previous_scope,
        "resulting_scope": resulting_scope,
        "enforcement": "exact_reviewed_scope_expansion",
    }


def _ownership_snapshot(db: sqlite3.Connection, repository: str) -> list[dict[str, Any]]:
    return [dict(row) for row in db.execute(
        "SELECT workspace_id,repository,branch,base_branch,base_commit_sha,head_sha,tree_sha,"
        "status,revision,owner,lease_expires_at,scope_json,drift_reason FROM workspaces "
        "WHERE repository=? AND status IN ('active','drifted') ORDER BY workspace_id", (repository,),
    ).fetchall()]


def _verify_ownership_snapshot(db: sqlite3.Connection, evidence: dict[str, Any]) -> None:
    if _ownership_snapshot(db, evidence["repository"]) != evidence.get("workspace_snapshot"):
        raise MyGithub12Error("WORKSPACE_REVISION_MISMATCH", "Workspace ownership/overlap inputs changed after verification")


def _parse_reviewed_active_overlap_paths(value: str) -> list[str]:
    """Use the exact/safe Git-path parser for an independent active-Writer review."""
    try:
        return _parse_reviewed_scope_expansion_paths(value)
    except MyGithub12Error as exc:
        raise MyGithub12Error(
            "SEARCH_QUERY_INVALID",
            "reviewed_active_overlap_paths_json must contain unique, safe Git paths",
        ) from exc


def _verify_ownership(
    service: Any,
    workspace: dict[str, Any],
    reviewed_active_overlap_paths: list[str] | None = None,
    *,
    allow_parallel_branch_overlap: bool = False,
) -> dict[str, Any]:
    repository = str(workspace["repository"])
    branch = str(workspace["branch"])
    workspace_id = str(workspace["workspace_id"])
    with sessions._db() as db:
        snapshot = _ownership_snapshot(db, repository)
    listing = mygithub12.list_workspaces(service, repository=repository, branch=branch, limit=100)
    candidates = [item for item in (listing.get("items") or []) if item.get("branch") == branch]
    if not any(item.get("workspace_id") == workspace_id for item in candidates):
        raise MyGithub12Error(
            "RECOVERY_BRANCH_OWNERSHIP_CONFLICT",
            "target Workspace is no longer the branch owner",
            {"workspace_id": workspace_id, "repository": repository, "branch": branch},
        )
    conflicts = [
        item for item in candidates
        if item.get("workspace_id") != workspace_id
        and (item.get("status") == "drifted" or (item.get("status") == "active" and item.get("lease_valid")))
    ]
    if conflicts:
        raise MyGithub12Error(
            "RECOVERY_BRANCH_OWNERSHIP_CONFLICT",
            "another active or drifted Workspace claims the recovery branch",
            {"workspace_id": workspace_id, "conflicting_workspace_ids": [item.get("workspace_id") for item in conflicts]},
        )
    try:
        overlap = mygithub12.workspace_overlap(service, workspace_id)
    except Exception as exc:
        if not allow_parallel_branch_overlap:
            raise MyGithub12Error(
                "RECOVERY_WORKSPACE_OVERLAP_UNAVAILABLE",
                "Workspace overlap could not be verified for control-plane recovery",
                {"workspace_id": workspace_id, "cause_type": type(exc).__name__},
            ) from exc
        overlap = None
    overlap_valid = (
        isinstance(overlap, dict)
        and overlap.get("ok") is not False
        and isinstance(overlap.get("items"), list)
    )
    if allow_parallel_branch_overlap and (
        not overlap_valid or overlap.get("comparison_verified") is not True
    ):
        # This recovery does not write Git refs or files, so unavailable
        # cross-branch overlap diagnostics are not an ownership conflict.
        # Preserve the fact that the diagnostic was unavailable in the audit.
        overlap = {
            "ok": False,
            "workspace_id": workspace_id,
            "comparison_verified": False,
            "items": [],
            "comparison_unavailable": True,
        }
        return {
            "verified": True,
            "repository": repository,
            "workspace_id": workspace_id,
            "overlap": overlap,
            "workspace_snapshot": snapshot,
            "reviewed_active_overlap_paths": sorted(reviewed_active_overlap_paths or []),
            "required_active_overlap_paths": [],
            "parallel_branch_overlap_non_blocking": True,
            "parallel_branch_overlap_evidence_available": False,
            "parallel_branch_overlap_count": None,
        }
    if not overlap_valid:
        raise MyGithub12Error(
            "RECOVERY_WORKSPACE_OVERLAP_UNAVAILABLE",
            "Workspace overlap could not be verified for control-plane recovery",
            {"workspace_id": workspace_id},
        )
    if overlap.get("comparison_verified") is False:
        raise MyGithub12Error(
            "RECOVERY_WORKSPACE_OVERLAP_UNAVAILABLE",
            "one or more active Workspace change comparisons could not be verified",
            {"workspace_id": workspace_id, "overlap": overlap},
        )
    high = [item for item in (overlap.get("items") or []) if item.get("level") == "high"]
    reviewed = sorted(reviewed_active_overlap_paths or [])
    if allow_parallel_branch_overlap:
        # Session adoption only updates control-plane identity; it does not
        # write Git refs or files. Cross-branch overlap is retained as audit
        # evidence for later integration, while same-branch Writer uniqueness
        # and the transactional Workspace snapshot CAS remain mandatory.
        return {
            "verified": True,
            "repository": repository,
            "workspace_id": workspace_id,
            "overlap": overlap,
            "workspace_snapshot": snapshot,
            "reviewed_active_overlap_paths": reviewed,
            "required_active_overlap_paths": [],
            "parallel_branch_overlap_non_blocking": True,
            "parallel_branch_overlap_evidence_available": True,
            "parallel_branch_overlap_count": len(high),
        }
    changed_paths: set[str] = set()
    unreviewable: list[str] = []
    for item in high:
        # File-level changed_paths are authoritative from both branches; scope
        # declarations alone are not proof of an actual concurrent write.
        items = item.get("evidence") if isinstance(item, dict) else None
        if not isinstance(items, list):
            unreviewable.append(str(item.get("workspace_id") if isinstance(item, dict) else "unknown"))
            continue
        kinds = {str(e.get("kind")) for e in items if isinstance(e, dict)}
        # API/DB/Migration, same-symbol, config, or uncertain comparisons require
        # a separate substantive resolution: this is not a universal waiver.
        if not kinds or "changed_paths" not in kinds or (kinds - {"changed_paths", "paths"}):
            unreviewable.append(str(item.get("workspace_id") or "unknown"))
            continue
        actual = [e.get("items") for e in items if isinstance(e, dict) and e.get("kind") == "changed_paths"]
        if not actual or any(not isinstance(paths, list) or not paths or
                             any(not isinstance(path, str) or not path for path in paths)
                             for paths in actual):
            unreviewable.append(str(item.get("workspace_id") or "unknown"))
            continue
        for paths in actual:
            changed_paths.update(paths)
    required = sorted(changed_paths)
    if unreviewable or reviewed != required:
        raise MyGithub12Error(
            "RECOVERY_WORKSPACE_OVERLAP",
            "active Writer shared-file paths require an exact independent review",
            {"workspace_id": workspace_id, "overlap": high,
             "required_reviewed_active_overlap_paths": required,
             "reviewed_active_overlap_paths": reviewed,
             "unreviewable_workspace_ids": sorted(unreviewable)},
        )
    evidence = {"verified": True, "repository": repository, "workspace_id": workspace_id,
                "overlap": overlap, "workspace_snapshot": snapshot,
                "reviewed_active_overlap_paths": reviewed,
                "required_active_overlap_paths": required}
    with sessions._db() as db:
        _verify_ownership_snapshot(db, evidence)
    return evidence


def _atomic_recover(
    service: Any,
    *,
    request: dict[str, Any],
    idempotency_key: str,
    verification: dict[str, Any],
) -> dict[str, Any]:
    sessions.init_session_db()
    repository = request["repository"]
    branch = request["branch"]
    workspace_id = request["workspace_id"]
    session_id = request["development_session_id"]
    expected_workspace_revision = request["expected_workspace_revision"]
    expected_session_revision = recovery_session_revision(request, verification)
    current_head = request["expected_current_head_sha"]
    current_tree = request["expected_current_tree_sha"]
    base_branch = request["expected_base_branch"]
    live_base_sha = request["expected_base_sha"]
    base_sha = request.get("pinned_base_sha", live_base_sha)
    recovery_kind = str(request.get("recovery_kind") or "manual_branch_recovery")
    active_workspace_stale_session = recovery_kind == "active_workspace_stale_session"
    record_key = _recovery_record_key(recovery_kind)
    now = sessions._now()
    lease_expires_at = now + max(60, min(int(request["lease_seconds"]), mygithub12.MAX_LEASE_SECONDS))
    try:
        with sessions._LOCK, sessions._db() as db:
            db.execute("BEGIN IMMEDIATE")
            workspace_row = db.execute("SELECT * FROM workspaces WHERE workspace_id=?", (workspace_id,)).fetchone()
            session_row = db.execute("SELECT * FROM development_sessions WHERE session_id=?", (session_id,)).fetchone()
            if not workspace_row:
                raise MyGithub12Error("WORKSPACE_NOT_FOUND", "workspace was not found", {"workspace_id": workspace_id})
            if not session_row:
                raise MyGithub12Error("DEVELOPMENT_SESSION_NOT_FOUND", "development session was not found", {"development_session_id": session_id})
            if workspace_row["base_commit_sha"] != base_sha or session_row["base_commit_sha"] != base_sha:
                raise MyGithub12Error(
                    "RECOVERY_BASE_CHANGED",
                    "recovery base SHA differs from the Workspace/Development Session pinned base",
                    {"workspace_base_sha": workspace_row["base_commit_sha"], "session_base_sha": session_row["base_commit_sha"], "expected_base_sha": base_sha},
                )
            metadata = json.loads(session_row["metadata_json"] or "{}")
            record = metadata.get(record_key) if isinstance(metadata, dict) else None
            if isinstance(record, dict) and record.get("idempotency_key") == idempotency_key:
                if record.get("request") != request:
                    raise MyGithub12Error("IDEMPOTENCY_CONFLICT", "manual branch recovery idempotency key payload changed")
                after = record.get("after") if isinstance(record.get("after"), dict) else {}
                replay_state_matches = (
                    workspace_row["status"] == "active"
                    and not workspace_row["drift_reason"]
                    and session_row["status"] == "active"
                    and int(workspace_row["revision"]) == int(after.get("workspace_revision") or -1)
                    and int(session_row["workspace_revision"]) == int(after.get("workspace_revision") or -1)
                    and int(session_row["session_revision"]) == int(after.get("session_revision") or -1)
                    and workspace_row["head_sha"] == after.get("head_sha")
                    and workspace_row["tree_sha"] == after.get("tree_sha")
                    and session_row["head_commit_sha"] == after.get("head_sha")
                    and session_row["tree_sha"] == after.get("tree_sha")
                    and workspace_row["base_branch"] == after.get("base_branch")
                    and session_row["base_branch"] == after.get("base_branch")
                    and workspace_row["base_commit_sha"] == after.get("base_commit_sha")
                    and session_row["base_commit_sha"] == after.get("base_commit_sha")
                    and json.loads(workspace_row["scope_json"] or "{}") == after.get("scope")
                    and workspace_row["owner"] == session_row["owner"] == record.get("before", {}).get("workspace_owner")
                    and float(workspace_row["lease_expires_at"] or 0) > sessions._now()
                    and float(session_row["lease_expires_at"] or 0) == float(workspace_row["lease_expires_at"] or 0)
                )
                if not replay_state_matches:
                    raise MyGithub12Error(
                        "IDEMPOTENCY_CONFLICT",
                        "recorded manual branch recovery result no longer matches current control-plane state",
                    )
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
                _base_identity_mode(dict(workspace_row), dict(session_row), base_branch, base_sha)
                _fresh_github_identity(service, repository, branch, current_head, current_tree, base_branch, live_base_sha)
                if active_workspace_stale_session:
                    _verify_ownership_snapshot(db, verification["ownership"])
                return {"replayed": True, "before": record["before"], "after": record["after"], "audit": record["audit"]}
            if int(workspace_row["revision"]) != int(expected_workspace_revision):
                raise MyGithub12Error("WORKSPACE_REVISION_MISMATCH", "workspace revision changed before drift recovery")
            if int(session_row["session_revision"]) != int(expected_session_revision):
                raise MyGithub12Error("DEVELOPMENT_SESSION_REVISION_MISMATCH", "development session revision changed before drift recovery")
            if workspace_row["status"] == "closed":
                raise MyGithub12Error("WORKSPACE_CLOSED", "closed Workspace cannot be recovered")
            if active_workspace_stale_session:
                if session_row["status"] in _TERMINAL_SESSION_STATES or session_row["closed_at"] is not None:
                    raise MyGithub12Error("DEVELOPMENT_SESSION_CLOSED", "closed Development Session cannot be recovered")
                if workspace_row["status"] != "active" or workspace_row["drift_reason"] is not None:
                    raise MyGithub12Error(
                        "ACTIVE_WORKSPACE_RECOVERY_STATE_INVALID",
                        "active Workspace stale-Session recovery requires active/no-drift Workspace state",
                        {"status": workspace_row["status"], "drift_reason": workspace_row["drift_reason"]},
                    )
                if session_row["status"] not in _ACTIVE_STALE_SESSION_STATES:
                    raise MyGithub12Error(
                        "DEVELOPMENT_SESSION_STATE_INVALID",
                        "Development Session state does not permit active Workspace stale-Session recovery",
                        {"status": session_row["status"]},
                    )
                if float(workspace_row["lease_expires_at"] or 0) <= now:
                    raise MyGithub12Error("WORKSPACE_LEASE_REQUIRED", "active Workspace lease is expired")
            else:
                if workspace_row["status"] != "drifted" or workspace_row["drift_reason"] != "branch_moved_externally":
                    raise MyGithub12Error(
                        "RECOVERY_DRIFT_REASON_UNSUPPORTED",
                        "only branch_moved_externally drift can be recovered",
                        {"status": workspace_row["status"], "drift_reason": workspace_row["drift_reason"]},
                    )
                if session_row["status"] not in _ALLOWED_SESSION_STATES:
                    raise MyGithub12Error(
                        "DEVELOPMENT_SESSION_STATE_INVALID",
                        "development session state does not permit manual drift recovery",
                        {"status": session_row["status"]},
                    )
            identity_ok = (
                workspace_row["repository"] == repository
                and workspace_row["branch"] == branch
                and workspace_row["head_sha"] == current_head
                and workspace_row["tree_sha"] == current_tree
                and session_row["workspace_id"] == workspace_id
                and session_row["repository"] == repository
                and session_row["branch"] == branch
                and workspace_row["owner"] == session_row["owner"]
            )
            if not identity_ok:
                raise MyGithub12Error("RECOVERY_IDENTITY_MISMATCH", "Workspace/Session identity changed before drift recovery")
            base_identity_mode = _base_identity_mode(dict(workspace_row), dict(session_row), base_branch, base_sha)
            if active_workspace_stale_session:
                stale_session_identity = (
                    int(session_row["workspace_revision"]) < int(workspace_row["revision"])
                    and session_row["head_commit_sha"] != current_head
                    and workspace_row["head_sha"] == current_head
                    and workspace_row["tree_sha"] == current_tree
                    and session_row["base_branch"] == base_branch
                    and workspace_row["base_branch"] == base_branch
                    and session_row["base_commit_sha"] == base_sha
                    and workspace_row["base_commit_sha"] == base_sha
                )
                if not stale_session_identity:
                    raise MyGithub12Error(
                        "RECOVERY_IDENTITY_MISMATCH",
                        "active Workspace and stale Development Session do not form the required forward-adoption identity",
                        {
                            "workspace_revision": int(workspace_row["revision"]),
                            "session_workspace_revision": int(session_row["workspace_revision"]),
                            "workspace_head": workspace_row["head_sha"],
                            "session_head": session_row["head_commit_sha"],
                            "current_head": current_head,
                        },
                    )
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
            _fresh_github_identity(service, repository, branch, current_head, current_tree, base_branch, live_base_sha)
            if active_workspace_stale_session:
                fresh_workspace = {**dict(workspace_row), "scope": json.loads(workspace_row["scope_json"] or "{}")}
                # Remote overlap was proved before taking the SQLite writer
                # lock. Every input row must still be identical under CAS.
                _verify_ownership_snapshot(db, verification["ownership"])
                verification["scope"] = _verify_scope(
                    fresh_workspace, verification["scope"]["changed_paths"],
                    request.get("reviewed_scope_expansion_paths"),
                )
                canonical = db.execute(
                    "SELECT session_id FROM development_sessions WHERE workspace_id=? AND closed_at IS NULL "
                    "AND status NOT IN (?,?,?,?) LIMIT 2",
                    (workspace_id, *sorted(_TERMINAL_SESSION_STATES)),
                ).fetchall()
                if len(canonical) != 1 or canonical[0]["session_id"] != session_id:
                    raise MyGithub12Error("DEVELOPMENT_SESSION_WORKSPACE_MISMATCH", "canonical Session changed during recovery")
            before = {
                "workspace_revision": int(workspace_row["revision"]),
                "session_revision": int(session_row["session_revision"]),
                "workspace_head_sha": workspace_row["head_sha"],
                "workspace_tree_sha": workspace_row["tree_sha"],
                "session_head_sha": session_row["head_commit_sha"],
                "session_tree_sha": session_row["tree_sha"],
                "workspace_status": workspace_row["status"],
                "session_status": session_row["status"],
                "drift_reason": workspace_row["drift_reason"],
                "workspace_base_branch": workspace_row["base_branch"],
                "session_base_branch": session_row["base_branch"],
                "session_workspace_revision": int(session_row["workspace_revision"]),
                "workspace_owner": workspace_row["owner"],
                "session_owner": session_row["owner"],
            }
            current_scope = json.loads(workspace_row["scope_json"] or "{}")
            scope_changed = verification["scope"]["resulting_scope"] != current_scope
            if active_workspace_stale_session:
                after_workspace_revision = int(workspace_row["revision"]) + int(scope_changed)
                lease_expires_at = float(workspace_row["lease_expires_at"])
            else:
                after_workspace_revision = int(workspace_row["revision"]) + 1
            after = {
                "workspace_revision": after_workspace_revision,
                "session_revision": int(session_row["session_revision"]) + 1,
                "head_sha": current_head,
                "tree_sha": current_tree,
                "status": "active",
                "lease_expires_at": lease_expires_at,
                "base_branch": base_branch,
                "base_commit_sha": base_sha,
                "scope": verification["scope"]["resulting_scope"],
            }
            audit = {
                "repository": repository,
                "branch": branch,
                "workspace_id": workspace_id,
                "development_session_id": session_id,
                "old_workspace_revision": before["workspace_revision"],
                "new_workspace_revision": after["workspace_revision"],
                "old_session_revision": before["session_revision"],
                "new_session_revision": after["session_revision"],
                "old_session_head": before["session_head_sha"],
                "adopted_head": current_head,
                "adopted_tree": current_tree,
                "base_sha": base_sha,
                "base_identity_normalization": {
                    "performed": base_identity_mode == "normalize_legacy_commit_base",
                    "mode": base_identity_mode,
                    "old_workspace_base_branch": before["workspace_base_branch"],
                    "old_session_base_branch": before["session_base_branch"],
                    "new_base_branch": base_branch,
                    "pinned_base_sha": base_sha,
                },
                "drift_reason": before["drift_reason"],
                "ancestry": verification["ancestry"],
                "scope": verification["scope"],
                "ownership": verification["ownership"],
                "idempotency_identity": hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest(),
            }
            if verification.get("validation_reconciliation"):
                audit["validation_reconciliation"] = verification["validation_reconciliation"]
            if active_workspace_stale_session:
                audit.update({
                    "recovery_kind": "active_workspace_stale_session",
                    "base_advance": verification.get("base_advance"),
                    "before": {
                        "session_revision": before["session_revision"],
                        "session_head": before["session_head_sha"],
                        "session_tree": before["session_tree_sha"],
                        "session_workspace_revision": before["session_workspace_revision"],
                        "session_status": before["session_status"],
                    },
                    "workspace": {
                        "workspace_revision": before["workspace_revision"],
                        "workspace_head": before["workspace_head_sha"],
                        "workspace_tree": before["workspace_tree_sha"],
                        "workspace_status": before["workspace_status"],
                        "drift_reason": before["drift_reason"],
                    },
                    "github": {
                        "repository": repository,
                        "branch": branch,
                        "current_head": current_head,
                        "current_tree": current_tree,
                        "base_branch": base_branch,
                        "base_sha": base_sha,
                    },
                    "scope": {
                        "declared_paths": verification["scope"]["declared_paths"],
                        "changed_paths": verification["scope"]["changed_paths"],
                        "outside_scope_paths": verification["scope"]["outside_scope_paths"],
                        "reviewed_scope_expansion_paths": verification["scope"]["reviewed_scope_expansion_paths"],
                        "reviewed_expansion_paths": verification["scope"]["reviewed_scope_expansion_paths"],
                        "verified": verification["scope"]["verified"],
                    },
                    "overlap": verification["ownership"].get("overlap"),
                    "after": {
                        "session_revision": after["session_revision"],
                        "session_head": current_head,
                        "session_tree": current_tree,
                        "session_workspace_revision": after["workspace_revision"],
                        "session_status": "active",
                        "workspace_revision": after["workspace_revision"],
                        "workspace_head": workspace_row["head_sha"],
                        "workspace_tree": workspace_row["tree_sha"],
                        "workspace_status": "active",
                        "drift_reason": None,
                    },
                    "stale_evidence_cleared": True,
                })
            metadata[record_key] = {
                "idempotency_key": idempotency_key,
                "request": request,
                "before": before,
                "after": after,
                "audit": audit,
            }
            if active_workspace_stale_session:
                ws_update = db.execute(
                    """UPDATE workspaces
                    SET scope_json=?,revision=revision+?,updated_at=CASE WHEN ?=1 THEN ? ELSE updated_at END
                    WHERE workspace_id=? AND repository=? AND branch=? AND revision=? AND status='active'
                      AND drift_reason IS NULL AND head_sha=? AND tree_sha=? AND lease_expires_at>?""",
                    (
                        json.dumps(after["scope"], ensure_ascii=False, separators=(",", ":")),
                        int(scope_changed),
                        int(scope_changed),
                        sessions._now(),
                        workspace_id,
                        repository,
                        branch,
                        expected_workspace_revision,
                        current_head,
                        current_tree,
                        now,
                    ),
                )
            else:
                ws_update = db.execute(
                    """UPDATE workspaces SET base_branch=?,head_sha=?,tree_sha=?,scope_json=?,status='active',drift_reason=NULL,lease_expires_at=?,
                    index_commit_sha=NULL,revision=revision+1,updated_at=?
                    WHERE workspace_id=? AND revision=? AND status='drifted' AND drift_reason='branch_moved_externally'""",
                    (
                        base_branch,
                        current_head,
                        current_tree,
                        json.dumps(after["scope"], ensure_ascii=False, separators=(",", ":")),
                        lease_expires_at,
                        now,
                        workspace_id,
                        expected_workspace_revision,
                    ),
                )
            if ws_update.rowcount != 1:
                raise MyGithub12Error("WORKSPACE_REVISION_MISMATCH", "Workspace changed while applying drift recovery")
            session_update = db.execute(
                """UPDATE development_sessions SET base_branch=?,status='active',head_commit_sha=?,tree_sha=?,workspace_revision=?,
                lease_expires_at=?,index_commit_sha=NULL,last_fast_ci_job_id=NULL,last_full_ci_job_id=NULL,
                last_attestation_id=NULL,last_failure_resource_uri=NULL,metadata_json=?,session_revision=session_revision+1,updated_at=?
                WHERE session_id=? AND session_revision=?
                  AND EXISTS (SELECT 1 FROM workspaces WHERE workspace_id=? AND revision=? AND status='active'
                    AND drift_reason IS NULL AND head_sha=? AND tree_sha=?)""",
                (
                    base_branch,
                    current_head,
                    current_tree,
                    after["workspace_revision"],
                    lease_expires_at,
                    json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
                    now,
                    session_id,
                    expected_session_revision,
                    workspace_id,
                    after["workspace_revision"],
                    current_head,
                    current_tree,
                ),
            )
            if session_update.rowcount != 1:
                raise MyGithub12Error("DEVELOPMENT_SESSION_REVISION_MISMATCH", "Development Session changed while applying drift recovery")
            updated_row = db.execute("SELECT * FROM development_sessions WHERE session_id=?", (session_id,)).fetchone()
            sessions._append_event(
                db,
                updated_row,
                "active_workspace_stale_session_recovery" if active_workspace_stale_session else "manual_branch_recovery",
                session_row["status"],
                "active",
                after["session_revision"],
                audit,
            )
            _fresh_github_identity(service, repository, branch, current_head, current_tree, base_branch, live_base_sha)
            if active_workspace_stale_session and lease_expires_at <= sessions._now():
                raise MyGithub12Error("WORKSPACE_LEASE_REQUIRED", "Workspace lease expired while verifying recovery")
        return {"replayed": False, "before": before, "after": after, "audit": audit}
    except sqlite3.IntegrityError as exc:
        raise MyGithub12Error(
            "RECOVERY_BRANCH_OWNERSHIP_CONFLICT",
            "Workspace ownership changed while activating recovered state",
            {"workspace_id": workspace_id, "repository": repository, "branch": branch},
        ) from exc


def _index_state(service: Any, repository: str, head_sha: str, tree_sha: str, old_head: str, session_id: str) -> dict[str, Any]:
    try:
        status = mygithub12.get_index_status(service, repository, commit_sha=head_sha)
        ready = status.get("status") == "ready" and status.get("commit_sha") == head_sha and status.get("tree_sha") == tree_sha
        request = None
        if not ready:
            request = mygithub12.request_index_build(
                service,
                repository,
                head_sha,
                "auto",
                old_head,
                "interactive",
                f"manual-recovery-index:{session_id}:{head_sha}",
                False,
            )
        return {"ready": ready, "index_required": not ready, "status": status, "request": request, "error": None}
    except Exception as exc:
        return {
            "ready": False,
            "index_required": True,
            "status": None,
            "request": None,
            "error": {"code": getattr(exc, "code", type(exc).__name__), "message": getattr(exc, "message", str(exc))},
        }


def recover_drifted_task(
    service: Any,
    repository: str,
    branch: str,
    workspace_id: str,
    development_session_id: str,
    expected_workspace_revision: int,
    expected_session_revision: int,
    expected_current_head_sha: str,
    expected_current_tree_sha: str,
    expected_base_branch: str,
    expected_base_sha: str,
    idempotency_key: str,
    lease_seconds: int = mygithub12.DEFAULT_LEASE_SECONDS,
    reviewed_scope_expansion_paths_json: str = "[]",
) -> dict[str, Any]:
    """Recover one drifted Workspace/Session pair without changing GitHub refs."""
    if not repository or "/" not in repository or not branch:
        raise MyGithub12Error("SEARCH_QUERY_INVALID", "repository and branch are required")
    if not workspace_id or not development_session_id or not idempotency_key:
        raise MyGithub12Error("SEARCH_QUERY_INVALID", "workspace_id, development_session_id and idempotency_key are required")
    if int(expected_workspace_revision) <= 0 or int(expected_session_revision) <= 0:
        raise MyGithub12Error("SEARCH_QUERY_INVALID", "positive expected Workspace/Session revisions are required")
    if not expected_current_head_sha or not expected_current_tree_sha or not expected_base_branch or not expected_base_sha:
        raise MyGithub12Error("SEARCH_QUERY_INVALID", "exact current HEAD/Tree and base identity are required")
    reviewed_scope_expansion_paths = _parse_reviewed_scope_expansion_paths(reviewed_scope_expansion_paths_json)
    workspace = mygithub12.get_workspace(service, workspace_id)
    session = sessions.get_session(development_session_id)
    request = _request_identity(
        repository,
        branch,
        workspace_id,
        development_session_id,
        expected_workspace_revision,
        expected_session_revision,
        expected_current_head_sha,
        expected_current_tree_sha,
        expected_base_branch,
        expected_base_sha,
        lease_seconds,
        reviewed_scope_expansion_paths,
    )
    identity_ok = (
        workspace.get("repository") == repository
        and workspace.get("branch") == branch
        and session.get("workspace_id") == workspace_id
        and session.get("repository") == repository
        and session.get("branch") == branch
    )
    if not identity_ok:
        raise MyGithub12Error(
            "RECOVERY_IDENTITY_MISMATCH",
            "Workspace/Development Session do not match the requested repository, branch, or base branch",
            {"workspace_id": workspace_id, "development_session_id": development_session_id},
        )
    if workspace.get("base_commit_sha") != expected_base_sha or session.get("base_commit_sha") != expected_base_sha:
        raise MyGithub12Error(
            "RECOVERY_BASE_CHANGED",
            "recovery base SHA differs from the Workspace/Development Session pinned base",
            {"workspace_base_sha": workspace.get("base_commit_sha"), "session_base_sha": session.get("base_commit_sha"), "expected_base_sha": expected_base_sha},
        )
    _base_identity_mode(workspace, session, expected_base_branch, expected_base_sha)
    replay = _replay_result(session, workspace, request, idempotency_key)
    if replay:
        _fresh_github_identity(
            service,
            repository,
            branch,
            expected_current_head_sha,
            expected_current_tree_sha,
            expected_base_branch,
            expected_base_sha,
        )
        _verify_ownership(service, workspace)
        index = _index_state(
            service,
            repository,
            expected_current_head_sha,
            expected_current_tree_sha,
            str((replay.get("before") or {}).get("session_head_sha") or expected_current_head_sha),
            development_session_id,
        )
        return {
            "ok": True,
            "control_plane_recovery": "CONTROL_PLANE_RECOVERY_SUCCESS",
            "replayed": True,
            "workspace": workspace,
            "development_session": session,
            "before": replay.get("before"),
            "after": replay.get("after"),
            "audit": replay.get("audit"),
            "index": index,
            "index_required": index["index_required"],
            "writer_ready": index["ready"],
        }
    if workspace.get("status") == "closed":
        raise MyGithub12Error("WORKSPACE_CLOSED", "closed Workspace cannot be recovered")
    if workspace.get("status") != "drifted" or workspace.get("drift_reason") != "branch_moved_externally":
        raise MyGithub12Error(
            "RECOVERY_DRIFT_REASON_UNSUPPORTED",
            "only a drifted Workspace with branch_moved_externally may use manual recovery",
            {"status": workspace.get("status"), "drift_reason": workspace.get("drift_reason")},
        )
    if int(workspace.get("revision") or 0) != int(expected_workspace_revision):
        raise MyGithub12Error("WORKSPACE_REVISION_MISMATCH", "workspace revision changed before drift recovery")
    if int(session.get("session_revision") or 0) != int(expected_session_revision):
        raise MyGithub12Error("DEVELOPMENT_SESSION_REVISION_MISMATCH", "development session revision changed before drift recovery")
    if session.get("status") not in _ALLOWED_SESSION_STATES | {"validating_fast", "validating_full"}:
        raise MyGithub12Error("DEVELOPMENT_SESSION_STATE_INVALID", "development session state does not permit manual drift recovery")
    if workspace.get("head_sha") != expected_current_head_sha or workspace.get("tree_sha") != expected_current_tree_sha:
        raise MyGithub12Error(
            "RECOVERY_IDENTITY_MISMATCH",
            "drifted Workspace does not carry the expected current branch identity",
            {"workspace_head": workspace.get("head_sha"), "workspace_tree": workspace.get("tree_sha")},
        )
    repo, github_identity = _fresh_github_identity(
        service,
        repository,
        branch,
        expected_current_head_sha,
        expected_current_tree_sha,
        expected_base_branch,
        expected_base_sha,
    )
    ancestry, changed_paths = _verify_forward_only(repo, session, expected_current_head_sha)
    scope = _verify_scope(workspace, changed_paths, reviewed_scope_expansion_paths)
    ownership = _verify_ownership(service, workspace)
    verification = {"github": github_identity, "ancestry": ancestry, "scope": scope, "ownership": ownership}
    session, settlement = reconcile_drifted_validation(session, workspace)
    if settlement:
        verification["validation_reconciliation"] = settlement
    recovered = _atomic_recover(service, request=request, idempotency_key=idempotency_key, verification=verification)
    recovered_workspace = mygithub12.get_workspace(service, workspace_id)
    recovered_session = sessions.get_session(development_session_id)
    index = _index_state(
        service,
        repository,
        expected_current_head_sha,
        expected_current_tree_sha,
        str((recovered.get("before") or {}).get("session_head_sha") or session["head_commit_sha"]),
        development_session_id,
    )
    return {
        "ok": True,
        "control_plane_recovery": "CONTROL_PLANE_RECOVERY_SUCCESS",
        "replayed": bool(recovered.get("replayed")),
        "workspace": recovered_workspace,
        "development_session": recovered_session,
        "before": recovered.get("before"),
        "after": recovered.get("after"),
        "audit": recovered.get("audit"),
        "verification": verification,
        "index": index,
        "index_required": index["index_required"],
        "writer_ready": index["ready"],
    }


def recover_active_workspace_stale_session(
    service: Any,
    repository: str,
    branch: str,
    workspace_id: str,
    development_session_id: str,
    expected_workspace_revision: int,
    expected_session_revision: int,
    expected_current_head_sha: str,
    expected_current_tree_sha: str,
    expected_base_branch: str,
    expected_base_sha: str,
    idempotency_key: str,
    lease_seconds: int = mygithub12.DEFAULT_LEASE_SECONDS,
    reviewed_scope_expansion_paths_json: str = "[]",
    reviewed_active_overlap_paths_json: str = "[]",
) -> dict[str, Any]:
    """Adopt a verified active Workspace identity into its stale Session."""
    if not repository or "/" not in repository or not branch:
        raise MyGithub12Error("SEARCH_QUERY_INVALID", "repository and branch are required")
    if not workspace_id or not development_session_id or not idempotency_key:
        raise MyGithub12Error("SEARCH_QUERY_INVALID", "workspace_id, development_session_id and idempotency_key are required")
    if int(expected_workspace_revision) <= 0 or int(expected_session_revision) <= 0:
        raise MyGithub12Error("SEARCH_QUERY_INVALID", "positive expected Workspace/Session revisions are required")
    if not expected_current_head_sha or not expected_current_tree_sha or not expected_base_branch or not expected_base_sha:
        raise MyGithub12Error("SEARCH_QUERY_INVALID", "exact current HEAD/Tree and base identity are required")
    reviewed_scope_expansion_paths = _parse_reviewed_scope_expansion_paths(reviewed_scope_expansion_paths_json)
    reviewed_active_overlap_paths = _parse_reviewed_active_overlap_paths(reviewed_active_overlap_paths_json)
    workspace = mygithub12.get_workspace(service, workspace_id)
    session = sessions.get_session(development_session_id)
    identity = (
        workspace.get("repository") == repository
        and workspace.get("branch") == branch
        and session.get("workspace_id") == workspace_id
        and session.get("repository") == repository
        and session.get("branch") == branch
        and workspace.get("owner") == session.get("owner")
    )
    if not identity:
        raise MyGithub12Error(
            "RECOVERY_IDENTITY_MISMATCH",
            "Workspace and Development Session are not the same canonical Writer",
            {"workspace_id": workspace_id, "development_session_id": development_session_id},
        )
    request = _request_identity(
        repository,
        branch,
        workspace_id,
        development_session_id,
        expected_workspace_revision,
        expected_session_revision,
        expected_current_head_sha,
        expected_current_tree_sha,
        expected_base_branch,
        expected_base_sha,
        lease_seconds,
        reviewed_scope_expansion_paths,
        recovery_kind="active_workspace_stale_session",
        reviewed_active_overlap_paths=reviewed_active_overlap_paths,
    )
    pinned_base_sha = str(workspace.get("base_commit_sha") or "")
    if pinned_base_sha != expected_base_sha:
        request["pinned_base_sha"] = pinned_base_sha
    replay = _replay_result(session, workspace, request, idempotency_key)
    if replay:
        _fresh_github_identity(
            service,
            repository,
            branch,
            expected_current_head_sha,
            expected_current_tree_sha,
            expected_base_branch,
            expected_base_sha,
        )
        _verify_ownership(
            service, workspace, reviewed_active_overlap_paths, allow_parallel_branch_overlap=True,
        )
        index = _index_state(
            service,
            repository,
            expected_current_head_sha,
            expected_current_tree_sha,
            str((replay.get("before") or {}).get("session_head_sha") or expected_current_head_sha),
            development_session_id,
        )
        return {
            "ok": True,
            "control_plane_recovery": "CONTROL_PLANE_RECOVERY_SUCCESS",
            "recovery_kind": "active_workspace_stale_session",
            "replayed": True,
            "workspace": workspace,
            "development_session": session,
            "before": replay.get("before"),
            "after": replay.get("after"),
            "audit": replay.get("audit"),
            "index": index,
            "index_required": index["index_required"],
            "writer_ready": index["ready"],
            "git_ref_changed": False,
        }
    # Another caller may have completed this exact CAS operation before our
    # initial read. Reconcile the winner's durable result without renewing it.
    recorded = (session.get("metadata") or {}).get("last_active_workspace_stale_session_recovery") or {}
    if recorded.get("request") == request:
        reconciled = reconcile_active_recovery(service, request, idempotency_key)
        if reconciled is not None:
            return reconciled
    if workspace.get("status") == "closed":
        raise MyGithub12Error("WORKSPACE_CLOSED", "closed Workspace cannot be recovered")
    if workspace.get("status") != "active" or workspace.get("drift_reason") is not None:
        raise MyGithub12Error(
            "ACTIVE_WORKSPACE_RECOVERY_STATE_INVALID",
            "active Workspace stale-Session recovery requires active/no-drift Workspace state",
            {"status": workspace.get("status"), "drift_reason": workspace.get("drift_reason")},
        )
    if session.get("status") in _TERMINAL_SESSION_STATES or session.get("closed_at") is not None:
        raise MyGithub12Error("DEVELOPMENT_SESSION_CLOSED", "closed Development Session cannot be recovered")
    if session.get("status") not in _ACTIVE_STALE_SESSION_STATES:
        raise MyGithub12Error(
            "DEVELOPMENT_SESSION_STATE_INVALID",
            "Development Session state does not permit active Workspace stale-Session recovery",
            {"status": session.get("status")},
        )
    if int(workspace.get("revision") or 0) != int(expected_workspace_revision):
        raise MyGithub12Error(
            "WORKSPACE_REVISION_MISMATCH",
            "Workspace revision changed before active Workspace stale-Session recovery",
            {"expected": int(expected_workspace_revision), "actual": int(workspace.get("revision") or 0)},
        )
    if int(session.get("session_revision") or 0) != int(expected_session_revision):
        raise MyGithub12Error(
            "DEVELOPMENT_SESSION_REVISION_MISMATCH",
            "Development Session revision changed before active Workspace stale-Session recovery",
            {"expected": int(expected_session_revision), "actual": int(session.get("session_revision") or 0)},
        )
    if not workspace.get("lease_valid"):
        raise MyGithub12Error("WORKSPACE_LEASE_REQUIRED", "active Workspace lease is expired")
    if (
        workspace.get("head_sha") != expected_current_head_sha
        or workspace.get("tree_sha") != expected_current_tree_sha
    ):
        raise MyGithub12Error(
            "RECOVERY_IDENTITY_MISMATCH",
            "active Workspace does not carry the expected current branch HEAD/Tree",
            {"workspace_head": workspace.get("head_sha"), "workspace_tree": workspace.get("tree_sha")},
        )
    if (
        workspace.get("base_branch") != expected_base_branch
        or session.get("base_branch") != expected_base_branch
        or not pinned_base_sha
        or session.get("base_commit_sha") != pinned_base_sha
    ):
        raise MyGithub12Error(
            "RECOVERY_BASE_CHANGED",
            "active Workspace stale-Session recovery requires the exact pinned base branch and SHA",
            {
                "workspace_base_branch": workspace.get("base_branch"),
                "session_base_branch": session.get("base_branch"),
                "workspace_base_sha": workspace.get("base_commit_sha"),
                "session_base_sha": session.get("base_commit_sha"),
                "expected_base_branch": expected_base_branch,
                "expected_base_sha": expected_base_sha,
            },
        )
    if (
        int(session.get("workspace_revision") or 0) >= int(workspace.get("revision") or 0)
        or session.get("head_commit_sha") == expected_current_head_sha
    ):
        raise MyGithub12Error(
            "RECOVERY_IDENTITY_MISMATCH",
            "Development Session is not stale behind the active Workspace identity",
            {
                "workspace_revision": workspace.get("revision"),
                "session_workspace_revision": session.get("workspace_revision"),
                "workspace_head": workspace.get("head_sha"),
                "session_head": session.get("head_commit_sha"),
            },
        )
    with sessions._db() as db:
        nonterminal = db.execute(
            """SELECT session_id FROM development_sessions WHERE workspace_id=?
            AND status NOT IN (?,?,?,?) ORDER BY updated_at DESC LIMIT 2""",
            (workspace_id, *sorted(_TERMINAL_SESSION_STATES)),
        ).fetchall()
    if len(nonterminal) != 1 or nonterminal[0]["session_id"] != development_session_id:
        raise MyGithub12Error(
            "DEVELOPMENT_SESSION_WORKSPACE_MISMATCH",
            "Workspace must have exactly one non-closed canonical Development Session",
            {"workspace_id": workspace_id, "development_session_ids": [row["session_id"] for row in nonterminal]},
        )

    repo, github_identity = _fresh_github_identity(
        service,
        repository,
        branch,
        expected_current_head_sha,
        expected_current_tree_sha,
        expected_base_branch,
        expected_base_sha,
    )
    ancestry, changed_paths = _verify_forward_only(repo, session, expected_current_head_sha)
    base_advance = _verify_pinned_base_advance(repo, workspace, session, expected_base_sha)
    scope = _verify_scope(workspace, changed_paths, reviewed_scope_expansion_paths)
    ownership = _verify_ownership(
        service, workspace, reviewed_active_overlap_paths, allow_parallel_branch_overlap=True,
    )
    verification = {"github": github_identity, "ancestry": ancestry, "scope": scope, "ownership": ownership, "base_advance": base_advance}
    try:
        recovered = _atomic_recover(service, request=request, idempotency_key=idempotency_key, verification=verification)
    except MyGithub12Error as exc:
        if exc.code not in {"WORKSPACE_REVISION_MISMATCH", "DEVELOPMENT_SESSION_REVISION_MISMATCH"}:
            raise
        reconciled = reconcile_active_recovery(service, request, idempotency_key)
        if reconciled is None:
            raise
        return reconciled
    recovered_workspace = mygithub12.get_workspace(service, workspace_id)
    recovered_session = sessions.get_session(development_session_id)
    index = _index_state(
        service,
        repository,
        expected_current_head_sha,
        expected_current_tree_sha,
        str((recovered.get("before") or {}).get("session_head_sha") or session["head_commit_sha"]),
        development_session_id,
    )
    return {
        "ok": True,
        "control_plane_recovery": "CONTROL_PLANE_RECOVERY_SUCCESS",
        "recovery_kind": "active_workspace_stale_session",
        "replayed": bool(recovered.get("replayed")),
        "workspace": recovered_workspace,
        "development_session": recovered_session,
        "before": recovered.get("before"),
        "after": recovered.get("after"),
        "audit": recovered.get("audit"),
        "verification": verification,
        "index": index,
        "index_required": index["index_required"],
        "writer_ready": index["ready"],
        "git_ref_changed": False,
    }


def reconcile_active_recovery(service: Any, request: dict[str, Any], idempotency_key: str) -> dict[str, Any] | None:
    """Fresh-read after CAS: accept only the exact completed operation, never retry a write."""
    fresh_session = sessions.get_session(request["development_session_id"])
    record = (fresh_session.get("metadata") or {}).get("last_active_workspace_stale_session_recovery") or {}
    if record.get("request") != request:
        return None
    workspace = mygithub12.get_workspace(service, request["workspace_id"])
    winner_key = record["idempotency_key"]
    _replay_result(fresh_session, workspace, request, winner_key)
    result = recover_active_workspace_stale_session(
        service, **{key: value for key, value in request.items()
                    if key not in {"pinned_base_sha", "recovery_kind", "reviewed_scope_expansion_paths", "reviewed_active_overlap_paths"}},
        idempotency_key=winner_key,
        reviewed_scope_expansion_paths_json=json.dumps(request.get("reviewed_scope_expansion_paths") or []),
        reviewed_active_overlap_paths_json=json.dumps(request.get("reviewed_active_overlap_paths") or []),
    )
    result["reconciled"] = True
    result["recovery_performed"] = False
    return result
