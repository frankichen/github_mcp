"""Explicit fail-stop recovery for externally advanced development branches.

This module only adopts an already-existing, freshly verified GitHub branch
identity into Workspace/Development Session control-plane state. It never
moves a Git ref and never writes repository files.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from app import development_session_store as sessions
from app import mygithub12

MyGithub12Error = mygithub12.MyGithub12Error
_ALLOWED_SESSION_STATES = frozenset({"active", "blocked", "drifted", "pr_ready"})
_COMPARE_FILE_LIMIT = 300


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
) -> dict[str, Any]:
    return {
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
    }


def _replay_result(
    session: dict[str, Any],
    workspace: dict[str, Any],
    request: dict[str, Any],
    idempotency_key: str,
) -> dict[str, Any] | None:
    metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    record = metadata.get("last_manual_branch_recovery") if isinstance(metadata, dict) else None
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
        and int(session.get("session_revision") or 0) == int(after.get("session_revision") or -1)
        and workspace.get("head_sha") == after.get("head_sha")
        and workspace.get("tree_sha") == after.get("tree_sha")
        and session.get("head_commit_sha") == after.get("head_sha")
        and session.get("tree_sha") == after.get("tree_sha")
    )
    if not matches:
        raise MyGithub12Error(
            "IDEMPOTENCY_CONFLICT",
            "recorded manual branch recovery result no longer matches current control-plane state",
            {"workspace_id": workspace.get("workspace_id"), "development_session_id": session.get("session_id")},
        )
    return record


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
    changed_paths = sorted({str(item.filename) for item in files if getattr(item, "filename", None)})
    return evidence, changed_paths


def _verify_scope(workspace: dict[str, Any], changed_paths: list[str]) -> dict[str, Any]:
    scope = workspace.get("scope") if isinstance(workspace.get("scope"), dict) else {}
    declared = [str(value).strip().strip("/") for value in (scope.get("paths") or []) if str(value).strip().strip("/")]
    outside = [
        path for path in changed_paths
        if not any(path.strip("/") == prefix or path.strip("/").startswith(prefix + "/") for prefix in declared)
    ]
    evidence = {"verified": not outside, "declared_paths": declared, "changed_paths": changed_paths, "outside_scope_paths": outside}
    if outside:
        raise MyGithub12Error(
            "RECOVERY_SCOPE_VIOLATION",
            "external branch advance changed paths outside the Workspace declared scope",
            {"workspace_id": workspace.get("workspace_id"), "outside_scope_paths": outside},
        )
    return evidence


def _verify_ownership(service: Any, workspace: dict[str, Any]) -> dict[str, Any]:
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
    overlap = mygithub12.workspace_overlap(service, workspace_id)
    high = [item for item in (overlap.get("items") or []) if item.get("level") == "high"]
    if high:
        raise MyGithub12Error(
            "RECOVERY_WORKSPACE_OVERLAP",
            "high-overlap active Workspace blocks drift recovery",
            {"workspace_id": workspace_id, "overlap": high},
        )
    return {"verified": True, "workspace_id": workspace_id, "overlap": overlap}


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
    expected_session_revision = request["expected_session_revision"]
    current_head = request["expected_current_head_sha"]
    current_tree = request["expected_current_tree_sha"]
    base_branch = request["expected_base_branch"]
    base_sha = request["expected_base_sha"]
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
            record = metadata.get("last_manual_branch_recovery") if isinstance(metadata, dict) else None
            if isinstance(record, dict) and record.get("idempotency_key") == idempotency_key:
                if record.get("request") != request:
                    raise MyGithub12Error("IDEMPOTENCY_CONFLICT", "manual branch recovery idempotency key payload changed")
                return {"replayed": True, "before": record["before"], "after": record["after"], "audit": record["audit"]}
            if int(workspace_row["revision"]) != int(expected_workspace_revision):
                raise MyGithub12Error("WORKSPACE_REVISION_MISMATCH", "workspace revision changed before drift recovery")
            if int(session_row["session_revision"]) != int(expected_session_revision):
                raise MyGithub12Error("DEVELOPMENT_SESSION_REVISION_MISMATCH", "development session revision changed before drift recovery")
            if workspace_row["status"] == "closed":
                raise MyGithub12Error("WORKSPACE_CLOSED", "closed Workspace cannot be recovered")
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
                and workspace_row["base_branch"] == base_branch
                and workspace_row["head_sha"] == current_head
                and workspace_row["tree_sha"] == current_tree
                and session_row["workspace_id"] == workspace_id
                and session_row["repository"] == repository
                and session_row["branch"] == branch
                and session_row["base_branch"] == base_branch
            )
            if not identity_ok:
                raise MyGithub12Error("RECOVERY_IDENTITY_MISMATCH", "Workspace/Session identity changed before drift recovery")
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
            _fresh_github_identity(service, repository, branch, current_head, current_tree, base_branch, base_sha)
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
            }
            after = {
                "workspace_revision": int(workspace_row["revision"]) + 1,
                "session_revision": int(session_row["session_revision"]) + 1,
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
                "old_workspace_revision": before["workspace_revision"],
                "new_workspace_revision": after["workspace_revision"],
                "old_session_revision": before["session_revision"],
                "new_session_revision": after["session_revision"],
                "old_session_head": before["session_head_sha"],
                "adopted_head": current_head,
                "adopted_tree": current_tree,
                "base_sha": base_sha,
                "drift_reason": before["drift_reason"],
                "ancestry": verification["ancestry"],
                "scope": verification["scope"],
                "ownership": verification["ownership"],
                "idempotency_identity": hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest(),
            }
            metadata["last_manual_branch_recovery"] = {
                "idempotency_key": idempotency_key,
                "request": request,
                "before": before,
                "after": after,
                "audit": audit,
            }
            ws_update = db.execute(
                """UPDATE workspaces SET head_sha=?,tree_sha=?,status='active',drift_reason=NULL,lease_expires_at=?,
                index_commit_sha=NULL,revision=revision+1,updated_at=?
                WHERE workspace_id=? AND revision=? AND status='drifted' AND drift_reason='branch_moved_externally'""",
                (current_head, current_tree, lease_expires_at, now, workspace_id, expected_workspace_revision),
            )
            if ws_update.rowcount != 1:
                raise MyGithub12Error("WORKSPACE_REVISION_MISMATCH", "Workspace changed while applying drift recovery")
            session_update = db.execute(
                """UPDATE development_sessions SET status='active',head_commit_sha=?,tree_sha=?,workspace_revision=?,
                lease_expires_at=?,index_commit_sha=NULL,last_fast_ci_job_id=NULL,last_full_ci_job_id=NULL,
                last_attestation_id=NULL,last_failure_resource_uri=NULL,metadata_json=?,session_revision=session_revision+1,updated_at=?
                WHERE session_id=? AND session_revision=?""",
                (
                    current_head,
                    current_tree,
                    after["workspace_revision"],
                    lease_expires_at,
                    json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
                    now,
                    session_id,
                    expected_session_revision,
                ),
            )
            if session_update.rowcount != 1:
                raise MyGithub12Error("DEVELOPMENT_SESSION_REVISION_MISMATCH", "Development Session changed while applying drift recovery")
            updated_row = db.execute("SELECT * FROM development_sessions WHERE session_id=?", (session_id,)).fetchone()
            sessions._append_event(
                db,
                updated_row,
                "manual_branch_recovery",
                session_row["status"],
                "active",
                after["session_revision"],
                audit,
            )
            _fresh_github_identity(service, repository, branch, current_head, current_tree, base_branch, base_sha)
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
    replay = _replay_result(session, workspace, request, idempotency_key)
    if replay:
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
    if session.get("status") not in _ALLOWED_SESSION_STATES:
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
    scope = _verify_scope(workspace, changed_paths)
    ownership = _verify_ownership(service, workspace)
    verification = {"github": github_identity, "ancestry": ancestry, "scope": scope, "ownership": ownership}
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
