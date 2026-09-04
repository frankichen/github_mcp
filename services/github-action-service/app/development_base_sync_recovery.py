"""Fail-stop recovery for development branches that already completed a safe base synchronization.

This module is intentionally separate from same-base drift recovery. It adopts an
already-existing GitHub branch identity only after proving the old pinned base,
new live base, old task HEAD, current HEAD/Tree, path deltas, Workspace scope,
and ownership evidence. It never moves Git refs or writes repository files.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from app import development_drift_recovery as same_base
from app import development_session_store as sessions
from app import mygithub12

MyGithub12Error = mygithub12.MyGithub12Error

def _base_sync_request_identity(
    repository: str,
    branch: str,
    workspace_id: str,
    development_session_id: str,
    expected_workspace_revision: int,
    expected_session_revision: int,
    expected_old_base_sha: str,
    expected_new_base_sha: str,
    expected_base_branch: str,
    expected_old_session_head_sha: str,
    expected_current_head_sha: str,
    expected_current_tree_sha: str,
    lease_seconds: int,
) -> dict[str, Any]:
    return {
        "repository": repository,
        "branch": branch,
        "workspace_id": workspace_id,
        "development_session_id": development_session_id,
        "expected_workspace_revision": int(expected_workspace_revision),
        "expected_session_revision": int(expected_session_revision),
        "expected_old_base_sha": expected_old_base_sha,
        "expected_new_base_sha": expected_new_base_sha,
        "expected_base_branch": expected_base_branch,
        "expected_old_session_head_sha": expected_old_session_head_sha,
        "expected_current_head_sha": expected_current_head_sha,
        "expected_current_tree_sha": expected_current_tree_sha,
        "lease_seconds": int(lease_seconds),
    }


def _compare_delta(
    repo: Any,
    base_sha: str,
    head_sha: str,
    *,
    label: str,
    require_advance: bool = False,
) -> tuple[dict[str, Any], list[str]]:
    """Return bounded rename-aware forward-ancestry evidence for one Git delta."""
    try:
        comparison = repo.compare(base_sha, head_sha)
        merge_base = str(comparison.merge_base_commit.sha) if getattr(comparison, "merge_base_commit", None) else ""
        ahead_by = int(getattr(comparison, "ahead_by", 0) or 0)
        behind_by = int(getattr(comparison, "behind_by", 0) or 0)
        files = list(getattr(comparison, "files", []) or [])
    except Exception as exc:
        raise MyGithub12Error(
            "RECOVERY_ANCESTRY_MISMATCH",
            f"{label} ancestry could not be verified",
            {"base_sha": base_sha, "head_sha": head_sha, "cause_type": type(exc).__name__},
        ) from exc
    verified = merge_base == base_sha and behind_by == 0 and (ahead_by > 0 if require_advance else ahead_by >= 0)
    evidence = {
        "verified": verified,
        "label": label,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "merge_base": merge_base,
        "ahead_by": ahead_by,
        "behind_by": behind_by,
    }
    if not verified:
        raise MyGithub12Error(
            "RECOVERY_ANCESTRY_MISMATCH",
            f"{label} is not a verified forward-only ancestry relation",
            {"ancestry": evidence},
        )
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
    return evidence, changed_paths


def _fresh_base_sync_github_identity(
    service: Any,
    repository: str,
    branch: str,
    expected_head_sha: str,
    expected_tree_sha: str,
    base_branch: str,
    expected_new_base_sha: str,
) -> tuple[Any, dict[str, str]]:
    """Verify only live identities for base-sync; the old base is pinned control-plane state."""
    return same_base._fresh_github_identity(
        service,
        repository,
        branch,
        expected_head_sha,
        expected_tree_sha,
        base_branch,
        expected_new_base_sha,
    )


def _verify_base_sync_deltas(
    repo: Any,
    session: dict[str, Any],
    old_base_sha: str,
    new_base_sha: str,
    old_session_head_sha: str,
    current_head_sha: str,
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
    base_ancestry, base_delta_paths = _compare_delta(
        repo, old_base_sha, new_base_sha, label="old_base_to_new_base", require_advance=True,
    )
    old_task_ancestry, old_task_delta_paths = _compare_delta(
        repo, old_base_sha, old_session_head_sha, label="old_base_to_old_task_head",
    )
    task_ancestry, forward_task_delta_paths = _compare_delta(
        repo, old_session_head_sha, current_head_sha, label="old_task_head_to_current_head",
    )
    new_base_ancestry, new_task_delta_paths = _compare_delta(
        repo, new_base_sha, current_head_sha, label="new_base_to_current_head",
    )
    overlap = sorted(set(old_task_delta_paths) & set(base_delta_paths))
    if overlap:
        raise MyGithub12Error(
            "RECOVERY_BASE_SYNC_OVERLAP",
            "base synchronization overlaps the task's pre-sync changed paths",
            {
                "overlapping_paths": overlap,
                "old_task_delta_paths": old_task_delta_paths,
                "base_delta_paths": base_delta_paths,
            },
        )
    # A canonical task may continue forward after the base sync. Path-set
    # changes are therefore valid only when the verified H0 -> H1 comparison
    # explains every difference.
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
    return {
        "base_ancestry": base_ancestry,
        "old_task_ancestry": old_task_ancestry,
        "task_ancestry": task_ancestry,
        "new_base_ancestry": new_base_ancestry,
        "base_delta_paths": base_delta_paths,
        "old_task_delta_paths": old_task_delta_paths,
        "new_task_delta_paths": new_task_delta_paths,
        "forward_task_delta_paths": forward_task_delta_paths,
        "task_path_changes": task_path_changes,
        "unexplained_task_path_changes": unexplained_task_path_changes,
        "base_task_overlap_paths": overlap,
    }


def _verify_base_sync_scope(workspace: dict[str, Any], changed_paths: list[str]) -> dict[str, Any]:
    """Preserve legacy empty scope without treating it as unrestricted scope."""
    scope = workspace.get("scope") if isinstance(workspace.get("scope"), dict) else {}
    declared = [str(value).strip().strip("/") for value in (scope.get("paths") or []) if str(value).strip().strip("/")]
    if declared:
        evidence = same_base._verify_scope(workspace, changed_paths)
        return {**evidence, "declaration_required": False, "enforcement": "declared_scope"}
    return {
        "verified": True,
        "declared_paths": [],
        "changed_paths": changed_paths,
        "outside_scope_paths": [],
        "declaration_required": True,
        "enforcement": "actual_changed_paths_overlap_only",
    }


def _current_task_overlap(overlap: dict[str, Any], current_task_paths: list[str]) -> dict[str, Any]:
    """Remove imported-base paths from current-task overlap evidence."""
    task_paths = set(current_task_paths)
    normalized_items: list[dict[str, Any]] = []
    for raw_item in overlap.get("items") or []:
        item = dict(raw_item)
        evidence: list[dict[str, Any]] = []
        had_changed_paths = False
        for raw_evidence in item.get("evidence") or []:
            entry = dict(raw_evidence)
            if entry.get("kind") == "changed_paths":
                had_changed_paths = True
                items = sorted(task_paths & {str(path) for path in (entry.get("items") or [])})
                if items:
                    evidence.append({**entry, "items": items})
            else:
                evidence.append(entry)
        item["evidence"] = evidence
        if had_changed_paths:
            kinds = {entry.get("kind") for entry in evidence}
            item["level"] = "high" if kinds & {"changed_paths", "migrations", "tables", "apis"} else "medium" if evidence else "none"
        normalized_items.append(item)
    return {**overlap, "items": normalized_items, "current_task_delta_paths": current_task_paths}


def _latest_workspace_session_status(workspace_id: str) -> dict[str, Any] | None:
    sessions.init_session_db()
    with sessions._db() as db:
        row = db.execute(
            "SELECT * FROM development_sessions WHERE workspace_id=? ORDER BY updated_at DESC LIMIT 1",
            (workspace_id,),
        ).fetchone()
    if not row:
        return None
    value = dict(row)
    value["metadata"] = json.loads(value.pop("metadata_json") or "{}")
    return value


def _merged_workspace_in_new_base(repo: Any, other_workspace: dict[str, Any], new_base_sha: str) -> dict[str, Any] | None:
    """Return exact evidence only when a historical merged Writer is already in the new base."""
    other_session = _latest_workspace_session_status(str(other_workspace.get("workspace_id") or ""))
    if not other_session or other_session.get("status") != "merged":
        return None
    other_head = str(other_workspace.get("head_sha") or "")
    if not other_head:
        return None
    try:
        actual_tree = mygithub12._tree_sha(repo.get_commit(other_head))
        ancestry, _ = _compare_delta(repo, other_head, new_base_sha, label="merged_workspace_head_to_new_base")
    except MyGithub12Error:
        return None
    if actual_tree != other_workspace.get("tree_sha"):
        return None
    return {
        "workspace_id": other_workspace.get("workspace_id"),
        "development_session_id": other_session.get("session_id"),
        "session_status": other_session.get("status"),
        "workspace_head_sha": other_head,
        "workspace_tree_sha": actual_tree,
        "new_base_sha": new_base_sha,
        "ancestry": ancestry,
    }


def _verify_base_sync_ownership(
    service: Any,
    repo: Any,
    workspace: dict[str, Any],
    new_base_sha: str,
    current_task_paths: list[str],
) -> dict[str, Any]:
    repository = str(workspace["repository"])
    branch = str(workspace["branch"])
    workspace_id = str(workspace["workspace_id"])
    listing = mygithub12.list_workspaces(service, repository=repository, branch=branch, limit=100)
    candidates = [item for item in (listing.get("items") or []) if item.get("branch") == branch]
    if not any(item.get("workspace_id") == workspace_id for item in candidates):
        raise MyGithub12Error(
            "RECOVERY_BRANCH_OWNERSHIP_CONFLICT",
            "target drifted Workspace is no longer the branch owner",
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
    overlap = _current_task_overlap(
        mygithub12.workspace_overlap(service, workspace_id), current_task_paths,
    )
    blocked: list[dict[str, Any]] = []
    ignored_merged: list[dict[str, Any]] = []
    for item in (overlap.get("items") or []):
        if item.get("level") != "high":
            continue
        other_id = str(item.get("workspace_id") or "")
        try:
            other_workspace = mygithub12.get_workspace(service, other_id)
        except Exception:
            blocked.append(item)
            continue
        merged_evidence = _merged_workspace_in_new_base(repo, other_workspace, new_base_sha)
        if merged_evidence:
            ignored_merged.append({"overlap": item, "merged_evidence": merged_evidence})
        else:
            blocked.append(item)
    if blocked:
        raise MyGithub12Error(
            "RECOVERY_WORKSPACE_OVERLAP",
            "high-overlap active Workspace blocks base-sync recovery",
            {"workspace_id": workspace_id, "overlap": blocked},
        )
    return {
        "verified": True,
        "workspace_id": workspace_id,
        "overlap": overlap,
        "ignored_merged_workspaces": ignored_merged,
    }


def _replay_base_sync_result(
    session: dict[str, Any],
    workspace: dict[str, Any],
    request: dict[str, Any],
    idempotency_key: str,
) -> dict[str, Any] | None:
    metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    record = metadata.get("last_base_sync_recovery") if isinstance(metadata, dict) else None
    if not isinstance(record, dict) or record.get("idempotency_key") != idempotency_key:
        return None
    if record.get("request") != request:
        raise MyGithub12Error(
            "IDEMPOTENCY_CONFLICT",
            "base-sync recovery idempotency key was reused with a different payload",
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
        and workspace.get("base_commit_sha") == after.get("base_sha")
        and session.get("base_commit_sha") == after.get("base_sha")
        and workspace.get("head_sha") == after.get("head_sha")
        and workspace.get("tree_sha") == after.get("tree_sha")
        and session.get("head_commit_sha") == after.get("head_sha")
        and session.get("tree_sha") == after.get("tree_sha")
    )
    if not matches:
        raise MyGithub12Error(
            "IDEMPOTENCY_CONFLICT",
            "recorded base-sync recovery result no longer matches current control-plane state",
            {"workspace_id": workspace.get("workspace_id"), "development_session_id": session.get("session_id")},
        )
    return record


def _atomic_recover_base_sync(
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
    expected_session_revision = request["expected_session_revision"]
    old_base_sha = request["expected_old_base_sha"]
    new_base_sha = request["expected_new_base_sha"]
    base_branch = request["expected_base_branch"]
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
            replay = metadata.get("last_base_sync_recovery") if isinstance(metadata, dict) else None
            if isinstance(replay, dict) and replay.get("idempotency_key") == idempotency_key:
                if replay.get("request") != request:
                    raise MyGithub12Error("IDEMPOTENCY_CONFLICT", "base-sync recovery idempotency key payload changed")
                after = replay.get("after") if isinstance(replay.get("after"), dict) else {}
                state_matches = (
                    workspace_row["status"] == "active"
                    and not workspace_row["drift_reason"]
                    and session_row["status"] == "active"
                    and int(workspace_row["revision"]) == int(after.get("workspace_revision") or -1)
                    and int(session_row["workspace_revision"]) == int(after.get("workspace_revision") or -1)
                    and int(session_row["session_revision"]) == int(after.get("session_revision") or -1)
                    and workspace_row["base_commit_sha"] == after.get("base_sha")
                    and session_row["base_commit_sha"] == after.get("base_sha")
                    and workspace_row["head_sha"] == after.get("head_sha")
                    and workspace_row["tree_sha"] == after.get("tree_sha")
                    and session_row["head_commit_sha"] == after.get("head_sha")
                    and session_row["tree_sha"] == after.get("tree_sha")
                )
                if not state_matches:
                    raise MyGithub12Error(
                        "IDEMPOTENCY_CONFLICT",
                        "recorded base-sync recovery result no longer matches current control-plane state",
                    )
                _fresh_base_sync_github_identity(
                    service, repository, branch, current_head, current_tree, base_branch, new_base_sha,
                )
                return {"replayed": True, "before": replay["before"], "after": replay["after"], "audit": replay["audit"]}
            if int(workspace_row["revision"]) != int(expected_workspace_revision):
                raise MyGithub12Error("WORKSPACE_REVISION_MISMATCH", "workspace revision changed before base-sync recovery")
            if int(session_row["session_revision"]) != int(expected_session_revision):
                raise MyGithub12Error("DEVELOPMENT_SESSION_REVISION_MISMATCH", "development session revision changed before base-sync recovery")
            if workspace_row["status"] == "closed":
                raise MyGithub12Error("WORKSPACE_CLOSED", "closed Workspace cannot be recovered")
            if workspace_row["status"] != "drifted" or workspace_row["drift_reason"] != "branch_moved_externally":
                raise MyGithub12Error(
                    "RECOVERY_DRIFT_REASON_UNSUPPORTED",
                    "only branch_moved_externally drift can use base-sync recovery",
                    {"status": workspace_row["status"], "drift_reason": workspace_row["drift_reason"]},
                )
            if session_row["status"] not in same_base._ALLOWED_SESSION_STATES:
                raise MyGithub12Error(
                    "DEVELOPMENT_SESSION_STATE_INVALID",
                    "development session state does not permit base-sync recovery",
                    {"status": session_row["status"]},
                )
            identity_ok = (
                workspace_row["repository"] == repository
                and workspace_row["branch"] == branch
                and workspace_row["base_branch"] == base_branch
                and session_row["workspace_id"] == workspace_id
                and session_row["repository"] == repository
                and session_row["branch"] == branch
                and session_row["base_branch"] == base_branch
                and workspace_row["head_sha"] == current_head
                and workspace_row["tree_sha"] == current_tree
            )
            if not identity_ok:
                raise MyGithub12Error("RECOVERY_IDENTITY_MISMATCH", "Workspace/Session identity changed before base-sync recovery")
            if workspace_row["base_commit_sha"] != old_base_sha or session_row["base_commit_sha"] != old_base_sha:
                raise MyGithub12Error(
                    "RECOVERY_BASE_CHANGED",
                    "pinned Workspace/Session base changed before base-sync recovery",
                    {
                        "workspace_base_sha": workspace_row["base_commit_sha"],
                        "session_base_sha": session_row["base_commit_sha"],
                        "expected_old_base_sha": old_base_sha,
                    },
                )
            if session_row["head_commit_sha"] != old_session_head:
                raise MyGithub12Error(
                    "RECOVERY_ANCESTRY_MISMATCH",
                    "Development Session HEAD changed before base-sync recovery",
                    {"expected_old_session_head": old_session_head, "actual": session_row["head_commit_sha"]},
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
            _fresh_base_sync_github_identity(
                service, repository, branch, current_head, current_tree, base_branch, new_base_sha,
            )
            before = {
                "workspace_revision": int(workspace_row["revision"]),
                "session_revision": int(session_row["session_revision"]),
                "workspace_base_sha": workspace_row["base_commit_sha"],
                "session_base_sha": session_row["base_commit_sha"],
                "workspace_head_sha": workspace_row["head_sha"],
                "workspace_tree_sha": workspace_row["tree_sha"],
                "session_head_sha": session_row["head_commit_sha"],
                "session_tree_sha": session_row["tree_sha"],
                "workspace_status": workspace_row["status"],
                "session_status": session_row["status"],
                "drift_reason": workspace_row["drift_reason"],
            }
            after = {
                "workspace_revision": int(workspace_row["revision"]) + 1,
                "session_revision": int(session_row["session_revision"]) + 1,
                "base_sha": new_base_sha,
                "head_sha": current_head,
                "tree_sha": current_tree,
                "status": "active",
                "lease_expires_at": lease_expires_at,
            }
            audit = {
                "repository": repository,
                "branch": branch,
                "workspace_id": workspace_id,
                "development_session_id": session_id,
                "old_base_sha": old_base_sha,
                "new_base_sha": new_base_sha,
                "old_session_head": old_session_head,
                "adopted_head": current_head,
                "adopted_tree": current_tree,
                "base_ancestry": verification["deltas"]["base_ancestry"],
                "task_ancestry": verification["deltas"]["task_ancestry"],
                "new_base_ancestry": verification["deltas"]["new_base_ancestry"],
                "base_delta_paths": verification["deltas"]["base_delta_paths"],
                "old_task_delta_paths": verification["deltas"]["old_task_delta_paths"],
                "new_task_delta_paths": verification["deltas"]["new_task_delta_paths"],
                "forward_task_delta_paths": verification["deltas"]["forward_task_delta_paths"],
                "task_path_changes": verification["deltas"]["task_path_changes"],
                "unexplained_task_path_changes": verification["deltas"]["unexplained_task_path_changes"],
                "overlap_result": {
                    "base_task_overlap_paths": verification["deltas"]["base_task_overlap_paths"],
                    "workspace": verification["ownership"],
                },
                "scope": verification["scope"],
                "old_workspace_revision": before["workspace_revision"],
                "new_workspace_revision": after["workspace_revision"],
                "old_session_revision": before["session_revision"],
                "new_session_revision": after["session_revision"],
                "idempotency_identity": hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest(),
            }
            metadata["last_base_sync_recovery"] = {
                "idempotency_key": idempotency_key,
                "request": request,
                "before": before,
                "after": after,
                "audit": audit,
            }
            ws_update = db.execute(
                """UPDATE workspaces SET base_commit_sha=?,head_sha=?,tree_sha=?,status='active',drift_reason=NULL,
                lease_expires_at=?,index_commit_sha=NULL,revision=revision+1,updated_at=?
                WHERE workspace_id=? AND revision=? AND status='drifted' AND drift_reason='branch_moved_externally'
                AND base_commit_sha=?""",
                (
                    new_base_sha, current_head, current_tree, lease_expires_at, now,
                    workspace_id, expected_workspace_revision, old_base_sha,
                ),
            )
            if ws_update.rowcount != 1:
                raise MyGithub12Error("WORKSPACE_REVISION_MISMATCH", "Workspace changed while applying base-sync recovery")
            session_update = db.execute(
                """UPDATE development_sessions SET status='active',base_commit_sha=?,head_commit_sha=?,tree_sha=?,
                workspace_revision=?,lease_expires_at=?,index_commit_sha=NULL,last_fast_ci_job_id=NULL,
                last_full_ci_job_id=NULL,last_attestation_id=NULL,last_failure_resource_uri=NULL,metadata_json=?,
                session_revision=session_revision+1,updated_at=?
                WHERE session_id=? AND session_revision=? AND base_commit_sha=? AND head_commit_sha=?""",
                (
                    new_base_sha, current_head, current_tree, after["workspace_revision"], lease_expires_at,
                    json.dumps(metadata, ensure_ascii=False, separators=(",", ":")), now,
                    session_id, expected_session_revision, old_base_sha, old_session_head,
                ),
            )
            if session_update.rowcount != 1:
                raise MyGithub12Error(
                    "DEVELOPMENT_SESSION_REVISION_MISMATCH",
                    "Development Session changed while applying base-sync recovery",
                )
            updated_row = db.execute("SELECT * FROM development_sessions WHERE session_id=?", (session_id,)).fetchone()
            sessions._append_event(
                db,
                updated_row,
                "base_sync_recovery",
                session_row["status"],
                "active",
                after["session_revision"],
                audit,
            )
            # Re-read exact live identities before allowing this transaction to commit.
            _fresh_base_sync_github_identity(
                service, repository, branch, current_head, current_tree, base_branch, new_base_sha,
            )
        return {"replayed": False, "before": before, "after": after, "audit": audit}
    except sqlite3.IntegrityError as exc:
        raise MyGithub12Error(
            "RECOVERY_BRANCH_OWNERSHIP_CONFLICT",
            "Workspace ownership changed while activating base-synced state",
            {"workspace_id": workspace_id, "repository": repository, "branch": branch},
        ) from exc


def _base_sync_index_state(
    service: Any,
    repository: str,
    head_sha: str,
    tree_sha: str,
    new_base_sha: str,
    session_id: str,
) -> dict[str, Any]:
    try:
        request = mygithub12.request_index_build(
            service,
            repository,
            head_sha,
            "auto",
            new_base_sha,
            "interactive",
            f"base-sync-recovery-index:{session_id}:{head_sha}:{new_base_sha}",
            False,
        )
        status = mygithub12.get_index_status(service, repository, commit_sha=head_sha)
        ready = status.get("status") == "ready" and status.get("commit_sha") == head_sha and status.get("tree_sha") == tree_sha
        return {"ready": ready, "index_required": not ready, "status": status, "request": request, "error": None}
    except Exception as exc:
        return {
            "ready": False,
            "index_required": True,
            "status": None,
            "request": None,
            "error": {"code": getattr(exc, "code", type(exc).__name__), "message": getattr(exc, "message", str(exc))},
        }


def recover_base_synced_task(
    service: Any,
    repository: str,
    branch: str,
    workspace_id: str,
    development_session_id: str,
    expected_workspace_revision: int,
    expected_session_revision: int,
    expected_old_base_sha: str,
    expected_new_base_sha: str,
    expected_base_branch: str,
    expected_old_session_head_sha: str,
    expected_current_head_sha: str,
    expected_current_tree_sha: str,
    idempotency_key: str,
    lease_seconds: int = mygithub12.DEFAULT_LEASE_SECONDS,
) -> dict[str, Any]:
    """Adopt an already-completed, conflict-free base synchronization without moving Git refs."""
    if not repository or "/" not in repository or not branch:
        raise MyGithub12Error("SEARCH_QUERY_INVALID", "repository and branch are required")
    if not workspace_id or not development_session_id or not idempotency_key:
        raise MyGithub12Error("SEARCH_QUERY_INVALID", "workspace_id, development_session_id and idempotency_key are required")
    if int(expected_workspace_revision) <= 0 or int(expected_session_revision) <= 0:
        raise MyGithub12Error("SEARCH_QUERY_INVALID", "positive expected Workspace/Session revisions are required")
    if not all((
        expected_old_base_sha, expected_new_base_sha, expected_base_branch,
        expected_old_session_head_sha, expected_current_head_sha, expected_current_tree_sha,
    )):
        raise MyGithub12Error("SEARCH_QUERY_INVALID", "exact old/new base, old Session HEAD and current HEAD/Tree are required")
    if expected_old_base_sha == expected_new_base_sha:
        raise MyGithub12Error(
            "RECOVERY_BASE_CHANGED",
            "base-sync recovery requires distinct old pinned and new live base SHAs",
            {"expected_old_base_sha": expected_old_base_sha, "expected_new_base_sha": expected_new_base_sha},
        )
    workspace = mygithub12.get_workspace(service, workspace_id)
    session = sessions.get_session(development_session_id)
    request = _base_sync_request_identity(
        repository,
        branch,
        workspace_id,
        development_session_id,
        expected_workspace_revision,
        expected_session_revision,
        expected_old_base_sha,
        expected_new_base_sha,
        expected_base_branch,
        expected_old_session_head_sha,
        expected_current_head_sha,
        expected_current_tree_sha,
        lease_seconds,
    )
    identity_ok = (
        workspace.get("repository") == repository
        and workspace.get("branch") == branch
        and workspace.get("base_branch") == expected_base_branch
        and session.get("workspace_id") == workspace_id
        and session.get("repository") == repository
        and session.get("branch") == branch
        and session.get("base_branch") == expected_base_branch
    )
    if not identity_ok:
        raise MyGithub12Error(
            "RECOVERY_IDENTITY_MISMATCH",
            "Workspace/Development Session do not match the requested repository, branch, or base branch",
            {"workspace_id": workspace_id, "development_session_id": development_session_id},
        )
    replay = _replay_base_sync_result(session, workspace, request, idempotency_key)
    if replay:
        repo, github_identity = _fresh_base_sync_github_identity(
            service,
            repository,
            branch,
            expected_current_head_sha,
            expected_current_tree_sha,
            expected_base_branch,
            expected_new_base_sha,
        )
        replay_audit = replay.get("audit") if isinstance(replay.get("audit"), dict) else {}
        ownership = _verify_base_sync_ownership(
            service, repo, workspace, expected_new_base_sha,
            list(replay_audit.get("new_task_delta_paths") or []),
        )
        index = _base_sync_index_state(
            service, repository, expected_current_head_sha, expected_current_tree_sha,
            expected_new_base_sha, development_session_id,
        )
        scope_declaration_required = bool((replay_audit.get("scope") or {}).get("declaration_required"))
        return {
            "ok": True,
            "control_plane_recovery": "CONTROL_PLANE_BASE_SYNC_RECOVERY_SUCCESS",
            "replayed": True,
            "workspace": workspace,
            "development_session": session,
            "before": replay.get("before"),
            "after": replay.get("after"),
            "audit": replay.get("audit"),
            "verification": {"github": github_identity, "ownership": ownership},
            "index": index,
            "index_required": index["index_required"],
            "scope_declaration_required": scope_declaration_required,
            "writer_ready": index["ready"] and not scope_declaration_required,
        }
    if workspace.get("status") == "closed":
        raise MyGithub12Error("WORKSPACE_CLOSED", "closed Workspace cannot be recovered")
    if workspace.get("status") != "drifted" or workspace.get("drift_reason") != "branch_moved_externally":
        raise MyGithub12Error(
            "RECOVERY_DRIFT_REASON_UNSUPPORTED",
            "only a drifted Workspace with branch_moved_externally may use base-sync recovery",
            {"status": workspace.get("status"), "drift_reason": workspace.get("drift_reason")},
        )
    if int(workspace.get("revision") or 0) != int(expected_workspace_revision):
        raise MyGithub12Error("WORKSPACE_REVISION_MISMATCH", "workspace revision changed before base-sync recovery")
    if int(session.get("session_revision") or 0) != int(expected_session_revision):
        raise MyGithub12Error("DEVELOPMENT_SESSION_REVISION_MISMATCH", "development session revision changed before base-sync recovery")
    if workspace.get("base_commit_sha") != expected_old_base_sha or session.get("base_commit_sha") != expected_old_base_sha:
        raise MyGithub12Error(
            "RECOVERY_BASE_CHANGED",
            "old pinned base differs from Workspace/Development Session state",
            {
                "workspace_base_sha": workspace.get("base_commit_sha"),
                "session_base_sha": session.get("base_commit_sha"),
                "expected_old_base_sha": expected_old_base_sha,
            },
        )
    if session.get("head_commit_sha") != expected_old_session_head_sha:
        raise MyGithub12Error(
            "RECOVERY_ANCESTRY_MISMATCH",
            "old Development Session HEAD differs from the explicit recovery identity",
            {"expected": expected_old_session_head_sha, "actual": session.get("head_commit_sha")},
        )
    if workspace.get("head_sha") != expected_current_head_sha or workspace.get("tree_sha") != expected_current_tree_sha:
        raise MyGithub12Error(
            "RECOVERY_IDENTITY_MISMATCH",
            "drifted Workspace does not carry the expected current branch identity",
            {"workspace_head": workspace.get("head_sha"), "workspace_tree": workspace.get("tree_sha")},
        )
    repo, github_identity = _fresh_base_sync_github_identity(
        service,
        repository,
        branch,
        expected_current_head_sha,
        expected_current_tree_sha,
        expected_base_branch,
        expected_new_base_sha,
    )
    deltas = _verify_base_sync_deltas(
        repo,
        session,
        expected_old_base_sha,
        expected_new_base_sha,
        expected_old_session_head_sha,
        expected_current_head_sha,
    )
    # Scope belongs to the task under the new base, never to the imported base delta.
    scope = _verify_base_sync_scope(workspace, deltas["new_task_delta_paths"])
    ownership = _verify_base_sync_ownership(
        service, repo, workspace, expected_new_base_sha, deltas["new_task_delta_paths"],
    )
    verification = {"github": github_identity, "deltas": deltas, "scope": scope, "ownership": ownership}
    recovered = _atomic_recover_base_sync(
        service, request=request, idempotency_key=idempotency_key, verification=verification,
    )
    recovered_workspace = mygithub12.get_workspace(service, workspace_id)
    recovered_session = sessions.get_session(development_session_id)
    index = _base_sync_index_state(
        service,
        repository,
        expected_current_head_sha,
        expected_current_tree_sha,
        expected_new_base_sha,
        development_session_id,
    )
    return {
        "ok": True,
        "control_plane_recovery": "CONTROL_PLANE_BASE_SYNC_RECOVERY_SUCCESS",
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
