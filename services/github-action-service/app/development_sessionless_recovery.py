"""Guarded first-session reconciliation for an existing legacy Workspace.

This path never creates a Workspace, branch, PR, or Git commit. It is intended
only for a Workspace that demonstrably never had a Development Session.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from typing import Any

from app import development_session_store as sessions
from app import development_drift_recovery as drift
from app import mygithub12

Error = mygithub12.MyGithub12Error


def _identity_payload(
    repository: str,
    branch: str,
    workspace_id: str,
    owner: str,
    revision: int,
    previous_head_sha: str,
    head_sha: str,
    tree_sha: str,
    base_branch: str,
    base_sha: str,
    lease_seconds: int,
    paths: list[str],
    expected_live_base_sha: str,
    next_owner: str = "",
    reviewed_base_overlap_paths: list[str] | None = None,
    expected_writer_lease_id: int = 0,
    expected_active_binding_id: int = 0,
) -> dict[str, Any]:
    request = {
        "repository": repository,
        "branch": branch,
        "workspace_id": workspace_id,
        "expected_owner": owner,
        "expected_workspace_revision": int(revision),
        "expected_previous_head_sha": previous_head_sha,
        "expected_current_head_sha": head_sha,
        "expected_current_tree_sha": tree_sha,
        "expected_base_branch": base_branch,
        "expected_base_sha": base_sha,
        "lease_seconds": int(lease_seconds),
        "reviewed_scope_expansion_paths": sorted(paths),
    }
    if expected_live_base_sha:
        request["expected_live_base_sha"] = expected_live_base_sha
    if next_owner:
        request["next_owner"] = next_owner
        request["expected_writer_lease_id"] = expected_writer_lease_id
        request["expected_active_binding_id"] = expected_active_binding_id
    if reviewed_base_overlap_paths:
        request["reviewed_base_overlap_paths"] = sorted(reviewed_base_overlap_paths)
    return request


def _strict_compare_delta(
    repo: Any, base_sha: str, head_sha: str, *,
    label: str, require_advance: bool = False,
) -> tuple[dict[str, Any], list[str]]:
    """Never interpret incomplete GitHub compare evidence as a disjoint diff.

    Copied files modify only their destination; renamed files modify both old
    and new paths. Neither case may silently omit mandatory API metadata.
    """
    try:
        comparison = repo.compare(base_sha, head_sha)
        raw_ahead = getattr(comparison, "ahead_by", None)
        raw_behind = getattr(comparison, "behind_by", None)
        merge_base_commit = getattr(comparison, "merge_base_commit", None)
        merge_base = getattr(merge_base_commit, "sha", None)
        raw_files = getattr(comparison, "files", None)
        if type(raw_ahead) is not int or type(raw_behind) is not int:
            raise Error("RECOVERY_ANCESTRY_MISMATCH",
                        f"{label} compare lacks exact ahead/behind counts")
        if not isinstance(merge_base, str) or not re.fullmatch(r"[0-9a-f]{40}", merge_base):
            raise Error("RECOVERY_ANCESTRY_MISMATCH",
                        f"{label} compare lacks a valid merge-base SHA")
        # PyGithub Comparison.files is a list[github.File.File]. Do not accept
        # arbitrary iterables: empty mapping/string/set shapes silently become
        # [] or iterate mapping keys, hiding an actual changed-file delta.
        if not isinstance(raw_files, (list, tuple)):
            raise Error("RECOVERY_SCOPE_INCOMPLETE",
                        f"{label} compare omitted a valid changed-file sequence")
        files = list(raw_files)
        if (raw_ahead == 0) != (base_sha == head_sha):
            raise Error("RECOVERY_ANCESTRY_MISMATCH",
                        f"{label} zero ahead count conflicts with exact Git SHA identity")
        if (raw_ahead == 0 and files) or (raw_ahead > 0 and not files):
            raise Error("RECOVERY_SCOPE_INCOMPLETE",
                        f"{label} changed-file sequence conflicts with ahead count")
    except Error:
        raise
    except Exception as exc:
        raise Error("RECOVERY_SCOPE_INCOMPLETE",
                    f"{label} compare evidence could not be read",
                    {"cause_type": type(exc).__name__}) from exc
    verified = (
        merge_base == base_sha and raw_behind == 0
        and (raw_ahead > 0 if require_advance else raw_ahead >= 0)
    )
    evidence = {
        "verified": verified, "label": label, "base_sha": base_sha,
        "head_sha": head_sha, "merge_base": merge_base,
        "ahead_by": raw_ahead, "behind_by": raw_behind,
    }
    if not verified:
        raise Error("RECOVERY_ANCESTRY_MISMATCH",
                    f"{label} is not a verified forward-only ancestry relation",
                    {"ancestry": evidence})
    if len(files) >= drift._COMPARE_FILE_LIMIT:
        raise Error("RECOVERY_SCOPE_INCOMPLETE",
                    f"{label} compare reached its bounded changed-file limit",
                    {"file_count": len(files), "limit": drift._COMPARE_FILE_LIMIT})
    changed: set[str] = set()
    for item in files:
        filename = getattr(item, "filename", None)
        status = getattr(item, "status", None)
        if not isinstance(filename, str) or not filename or status not in {
            "added", "removed", "modified", "changed", "renamed", "copied", "unchanged"
        }:
            raise Error("RECOVERY_SCOPE_INCOMPLETE",
                        f"{label} compare omitted a valid file path/status")
        mygithub12._safe_path(filename)
        if status == "unchanged":
            continue
        changed.add(filename)
        if status == "renamed":
            old_path = getattr(item, "previous_filename", None)
            if not isinstance(old_path, str) or not old_path:
                raise Error("RECOVERY_SCOPE_INCOMPLETE",
                            f"{label} rename missing its previous path")
            mygithub12._safe_path(old_path)
            changed.add(old_path)
        # A copy does not modify the source, even if previous_filename exists.
    if raw_ahead > 0 and not changed:
        raise Error("RECOVERY_SCOPE_INCOMPLETE",
                    f"{label} advanced but no changed file paths were validated")
    return evidence, sorted(changed)


def _fresh_pinned_base_proof(
    service: Any, repository: str, branch: str, current_head_sha: str,
    current_tree_sha: str, base_branch: str, pinned_base_sha: str,
    expected_live_base_sha: str, confirm: bool,
    reviewed_base_overlap_paths: list[str] | None = None,
) -> tuple[Any, dict[str, Any]]:
    """Keep the historical pinned base, but prove disjoint forward main progress.

    This does not sync/merge the task branch and never silently treats a newer
    main as the original pinned base. A caller must explicitly pin the live
    base SHA to confirm an advanced-base first-session recovery.
    """
    base_state = service.client.get_branch(repository, base_branch)
    if not base_state:
        raise Error("RECOVERY_BASE_CHANGED", "recovery base branch not found")
    live_base_sha = str(base_state.commit.sha)
    if expected_live_base_sha and live_base_sha != expected_live_base_sha:
        raise Error("RECOVERY_BASE_CHANGED", "reviewed live base SHA changed",
                    {"reviewed": expected_live_base_sha, "actual": live_base_sha})
    if live_base_sha != pinned_base_sha and confirm and expected_live_base_sha != live_base_sha:
        raise Error("RECOVERY_BASE_ADVANCE_REVIEW_REQUIRED",
                    "caller must explicitly review and pin the advanced base SHA",
                    {"pinned_base_sha": pinned_base_sha, "live_base_sha": live_base_sha})
    repo, _ = drift._fresh_github_identity(
        service, repository, branch, current_head_sha, current_tree_sha,
        base_branch, live_base_sha,
    )
    if live_base_sha == pinned_base_sha:
        return repo, {"advanced": False, "pinned_base_sha": pinned_base_sha,
                      "live_base_sha": live_base_sha, "overlapping_paths": []}
    base_ancestry, base_paths = _strict_compare_delta(
        repo, pinned_base_sha, live_base_sha,
        label="sessionless_live_base_advance", require_advance=True,
    )
    task_ancestry, task_paths = _strict_compare_delta(
        repo, pinned_base_sha, current_head_sha, label="sessionless_pinned_task",
    )
    overlapping = sorted(set(base_paths) & set(task_paths))
    evidence = {"advanced": True, "pinned_base_sha": pinned_base_sha,
                "live_base_sha": live_base_sha,
                "base_ancestry": base_ancestry, "task_ancestry": task_ancestry,
                "base_changed_paths": base_paths, "task_changed_paths": task_paths,
                "overlapping_paths": overlapping}
    # Creating a Session does NOT sync or move the task branch. An operator can
    # acknowledge an EXACT reviewed overlap under the live-base SHA guard.
    # Existing public MCP callers do not expose this operator-only parameter.
    approved = sorted(reviewed_base_overlap_paths or [])
    if overlapping and not reviewed_base_overlap_paths:
        raise Error("RECOVERY_BASE_OVERLAP",
                    "advanced main overlaps the pinned task delta; manual review required",
                    evidence)
    if approved != overlapping:
        raise Error("RECOVERY_BASE_OVERLAP_REVIEW_MISMATCH",
                    "explicit reviewed overlap paths must match the fresh exact path set",
                    {**evidence, "reviewed_overlap_paths": approved})
    evidence["reviewed_overlap_paths"] = approved
    evidence["no_base_sync_or_merge"] = True
    return repo, evidence


def recover_sessionless_workspace(
    service: Any,
    repository: str,
    branch: str,
    workspace_id: str,
    expected_owner: str,
    expected_workspace_revision: int,
    expected_previous_head_sha: str,
    expected_current_head_sha: str,
    expected_current_tree_sha: str,
    expected_base_branch: str,
    expected_base_sha: str,
    idempotency_key: str,
    confirm: bool = False,
    lease_seconds: int = mygithub12.DEFAULT_LEASE_SECONDS,
    reviewed_scope_expansion_paths_json: str = "[]",
    expected_live_base_sha: str = "",
    next_owner: str = "",
    reviewed_base_overlap_paths_json: str = "[]",
    expected_writer_lease_id: int = 0,
    expected_active_binding_id: int = 0,
) -> dict[str, Any]:
    """Preview or CAS-reconcile a unique original Workspace without any Session.

    For externally drifted Workspaces, the prior index-pinned commit must be a
    verifiable forward ancestor. For an active exact-HEAD Workspace, no commit
    movement is permitted. No caller may supply a fabricated Session identity.
    """
    if (
        not repository
        or "/" not in repository
        or not branch
        or not workspace_id
        or not expected_owner
    ):
        raise Error(
            "SEARCH_QUERY_INVALID",
            "repository, branch, Workspace and existing owner are required",
        )
    if (
        not idempotency_key
        or len(idempotency_key) > 160
        or int(expected_workspace_revision) <= 0
    ):
        raise Error(
            "SEARCH_QUERY_INVALID",
            "stable idempotency key and positive Workspace CAS are required",
        )
    if (
        not expected_current_head_sha
        or not expected_current_tree_sha
        or not expected_base_sha
        or not expected_base_branch
    ):
        raise Error(
            "SEARCH_QUERY_INVALID",
            "exact current HEAD/Tree and pinned base are required",
        )
    if not expected_previous_head_sha or len(expected_previous_head_sha) != 40:
        raise Error("SEARCH_QUERY_INVALID", "exact previously indexed HEAD is required")
    if expected_live_base_sha and not re.fullmatch(r"[0-9a-f]{40}", expected_live_base_sha):
        raise Error("SEARCH_QUERY_INVALID", "expected_live_base_sha must be a full lowercase Git SHA")
    if int(lease_seconds) < 60 or int(lease_seconds) > mygithub12.MAX_LEASE_SECONDS:
        raise Error("SEARCH_QUERY_INVALID", "lease_seconds outside supported bounds")
    reviewed = drift._parse_reviewed_scope_expansion_paths(
        reviewed_scope_expansion_paths_json
    )
    reviewed_base_overlap = drift._parse_reviewed_scope_expansion_paths(
        reviewed_base_overlap_paths_json
    )
    if next_owner:
        before = re.fullmatch(r"(WORKER-[ABC]):#([1-9][0-9]*):([1-9][0-9]*)", expected_owner)
        after = re.fullmatch(r"(WORKER-[ABC]):#([1-9][0-9]*):([1-9][0-9]*)", next_owner)
        if (not before or not after or before.group(1,2) != after.group(1,2)
                or int(after.group(3)) != int(before.group(3)) + 1):
            raise Error("RECOVERY_OWNER_TRANSITION_INVALID",
                        "only same Worker and Task to immediate next Revision is allowed")
        if expected_writer_lease_id <= 0 or expected_active_binding_id <= 0:
            raise Error("RECOVERY_OPERATOR_ATTESTATION_REQUIRED",
                        "operator must cite exact fresh DevHub Writer Lease and active new Revision Binding")
        if not expected_live_base_sha:
            raise Error("RECOVERY_BASE_ADVANCE_REVIEW_REQUIRED",
                        "owner transfer must pin the exact live main SHA")
    elif expected_writer_lease_id or expected_active_binding_id:
        raise Error("SEARCH_QUERY_INVALID", "lease/binding attestation requires a new owner")
    request = _identity_payload(
        repository,
        branch,
        workspace_id,
        expected_owner,
        expected_workspace_revision,
        expected_previous_head_sha,
        expected_current_head_sha,
        expected_current_tree_sha,
        expected_base_branch,
        expected_base_sha,
        lease_seconds,
        reviewed,
        expected_live_base_sha,
        next_owner,
        reviewed_base_overlap,
        expected_writer_lease_id,
        expected_active_binding_id,
    )
    digest = hashlib.sha256(
        json.dumps(request, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    workspace = mygithub12.get_workspace(service, workspace_id)
    existing = sessions.find_session_by_idempotency(repository, idempotency_key)
    if existing:
        record = (existing.get("metadata") or {}).get("sessionless_recovery") or {}
        if (
            record.get("request_sha256") != digest
            or existing.get("workspace_id") != workspace_id
        ):
            raise Error(
                "IDEMPOTENCY_CONFLICT", "recovery key belongs to another request"
            )
        if (
            existing.get("repository") != repository
            or existing.get("branch") != branch
            or workspace.get("repository") != repository
            or workspace.get("branch") != branch
            or existing.get("owner") != (next_owner or expected_owner)
            or workspace.get("owner") != (next_owner or expected_owner)
            or workspace.get("base_branch") != expected_base_branch
            or workspace.get("base_commit_sha") != expected_base_sha
            or existing.get("base_commit_sha") != expected_base_sha
            or existing.get("head_commit_sha") != expected_current_head_sha
            or workspace.get("head_sha") != expected_current_head_sha
            or existing.get("tree_sha") != expected_current_tree_sha
            or workspace.get("tree_sha") != expected_current_tree_sha
        ):
            raise Error(
                "IDEMPOTENCY_CONFLICT",
                "replayed recovery no longer matches original identity",
            )
        if (
            existing.get("workspace_revision") != workspace.get("revision")
            or workspace.get("status") != "active"
            or not workspace.get("lease_valid")
            or not existing.get("lease_valid")
        ):
            raise Error(
                "IDEMPOTENCY_CONFLICT", "replayed recovery state has since changed"
            )
        _fresh_pinned_base_proof(
            service, repository, branch, expected_current_head_sha,
            expected_current_tree_sha, expected_base_branch,
            expected_base_sha, expected_live_base_sha, True,
            reviewed_base_overlap,
        )
        drift._verify_ownership(service, workspace)
        return {
            "ok": True,
            "replayed": True,
            "recovery_kind": "first_canonical_session",
            "workspace": workspace,
            "development_session": existing,
            "git_ref_changed": False,
        }
    if workspace.get("repository") != repository or workspace.get("branch") != branch:
        raise Error(
            "RECOVERY_IDENTITY_MISMATCH", "Workspace repository or branch differs"
        )
    if workspace.get("owner") != expected_owner:
        raise Error("RECOVERY_OWNER_MISMATCH", "existing Workspace owner differs")
    if int(workspace["revision"]) != int(expected_workspace_revision):
        raise Error("WORKSPACE_REVISION_MISMATCH", "Workspace CAS changed")
    if (
        workspace.get("base_branch") != expected_base_branch
        or workspace.get("base_commit_sha") != expected_base_sha
    ):
        raise Error("RECOVERY_BASE_CHANGED", "Workspace pinned base differs")
    if (
        workspace.get("head_sha") != expected_current_head_sha
        or workspace.get("tree_sha") != expected_current_tree_sha
    ):
        raise Error("RECOVERY_IDENTITY_MISMATCH", "Workspace current HEAD/Tree differs")
    status = workspace.get("status")
    drifted = (
        status == "drifted"
        and workspace.get("drift_reason") == "branch_moved_externally"
    )
    active = (
        status == "active"
        and workspace.get("drift_reason") is None
        and workspace.get("lease_valid")
    )
    if next_owner and not active:
        raise Error("RECOVERY_OWNER_TRANSITION_INVALID",
                    "revision handoff requires active no-drift Workspace and valid original lease")
    if not (drifted or active):
        raise Error(
            "RECOVERY_DRIFT_REASON_UNSUPPORTED",
            "only exact active or forward-drifted Workspaces can be reconciled",
        )
    if workspace.get("index_commit_sha") != expected_previous_head_sha:
        raise Error(
            "RECOVERY_ANCESTRY_MISMATCH",
            "previous HEAD must equal durable Workspace index pin",
        )
    if active and expected_previous_head_sha != expected_current_head_sha:
        raise Error(
            "RECOVERY_IDENTITY_MISMATCH", "active Workspace must already be exact-HEAD"
        )
    with sessions._db() as db:
        another = db.execute(
            "SELECT session_id FROM development_sessions WHERE workspace_id=? OR (repository=? AND branch=?) LIMIT 1",
            (workspace_id, repository, branch),
        ).fetchone()
    if another:
        raise Error(
            "DEVELOPMENT_SESSION_WORKSPACE_MISMATCH",
            "Workspace/branch already has a Development Session",
        )
    repo, base_advance = _fresh_pinned_base_proof(
        service, repository, branch, expected_current_head_sha,
        expected_current_tree_sha, expected_base_branch,
        expected_base_sha, expected_live_base_sha, confirm,
        reviewed_base_overlap,
    )
    if drifted:
        old_tree = mygithub12._tree_sha(repo.get_commit(expected_previous_head_sha))
        ancestry, changed = _strict_compare_delta(
            repo, expected_previous_head_sha, expected_current_head_sha,
            label="sessionless_original_pin_to_head", require_advance=True,
        )
        ancestry["old_commit_tree"] = old_tree
    else:
        ancestry, changed = {"verified": True, "kind": "exact_active_head"}, []
    scope_obj = (
        workspace.get("scope") if isinstance(workspace.get("scope"), dict) else {}
    )
    declared = [p for p in scope_obj.get("paths", []) if isinstance(p, str)]
    from app.mygithub12_workspace import scope_path_matches

    required = sorted(
        p for p in changed if not any(scope_path_matches(p, d) for d in declared)
    )
    scope = drift._verify_scope(workspace, changed, reviewed if confirm else required)
    ownership = drift._verify_ownership(service, workspace)
    preview = {
        "ok": True,
        "recovery_kind": "first_canonical_session",
        "confirmed": False,
        "no_existing_session": True,
        "git_ref_changed": False,
        "workspace_id": workspace_id,
        "owner": expected_owner,
        "next_owner": next_owner or expected_owner,
        "expected_writer_lease_id": expected_writer_lease_id or None,
        "expected_active_binding_id": expected_active_binding_id or None,
        "reviewed_base_overlap_paths": reviewed_base_overlap,
        "workspace_revision": int(expected_workspace_revision),
        "current_head_sha": expected_current_head_sha,
        "current_tree_sha": expected_current_tree_sha,
        "previous_head_sha": expected_previous_head_sha,
        "base_advance": base_advance,
        "required_expected_live_base_sha": (
            base_advance["live_base_sha"] if base_advance["advanced"] else ""
        ),
        "required_scope_expansion_paths": required,
        "reviewed_scope_expansion_paths": reviewed,
        "ancestry": ancestry,
        "overlap": ownership.get("overlap"),
        "next_allowed_action": "Review exact changed paths, then repeat with confirm=true and exact CAS",
    }
    if not confirm:
        return preview
    sessions.init_session_db()
    now = sessions._now()
    new_lease = (
        now + int(lease_seconds) if drifted else float(workspace["lease_expires_at"])
    )
    session_id = "dev_" + uuid.uuid4().hex[:20]
    try:
        with sessions._LOCK, sessions._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM workspaces WHERE workspace_id=?", (workspace_id,)
            ).fetchone()
            if not row:
                raise Error("WORKSPACE_NOT_FOUND", "original Workspace disappeared")
            expected_fields = {
                "repository": repository,
                "branch": branch,
                "owner": expected_owner,
                "revision": int(expected_workspace_revision),
                "head_sha": expected_current_head_sha,
                "tree_sha": expected_current_tree_sha,
                "base_branch": expected_base_branch,
                "base_commit_sha": expected_base_sha,
                "index_commit_sha": expected_previous_head_sha,
                "status": "drifted" if drifted else "active",
                "drift_reason": "branch_moved_externally" if drifted else None,
            }
            if any(row[k] != v for k, v in expected_fields.items()):
                raise Error(
                    "WORKSPACE_REVISION_MISMATCH",
                    "Workspace state changed during recovery",
                )
            if json.loads(row["scope_json"] or "{}") != scope["previous_scope"]:
                raise Error(
                    "WORKSPACE_REVISION_MISMATCH",
                    "Workspace scope changed during recovery",
                )
            if active and float(row["lease_expires_at"] or 0) <= sessions._now():
                raise Error(
                    "WORKSPACE_LEASE_REQUIRED", "active Workspace lease expired"
                )
            if db.execute(
                "SELECT 1 FROM development_sessions WHERE workspace_id=? OR (repository=? AND branch=?) LIMIT 1",
                (workspace_id, repository, branch),
            ).fetchone():
                raise Error(
                    "DEVELOPMENT_SESSION_WORKSPACE_MISMATCH",
                    "concurrent Session creation detected",
                )
            if db.execute(
                "SELECT 1 FROM development_sessions WHERE repository=? AND idempotency_key=? LIMIT 1",
                (repository, idempotency_key),
            ).fetchone():
                raise Error(
                    "IDEMPOTENCY_CONFLICT", "recovery idempotency key already used"
                )
            drift._verify_ownership_snapshot(db, ownership)
            _fresh_pinned_base_proof(
                service, repository, branch, expected_current_head_sha,
                expected_current_tree_sha, expected_base_branch,
                expected_base_sha, expected_live_base_sha, True,
                reviewed_base_overlap,
            )
            # Expand scope only to the exact, reviewed forward delta. No refs or code files move.
            cursor = db.execute(
                """UPDATE workspaces SET status='active',drift_reason=NULL,
                   lease_expires_at=?,index_commit_sha=?,scope_json=?,owner=?,
                   revision=revision+1,updated_at=?
                   WHERE workspace_id=? AND revision=?""",
                (
                    new_lease,
                    expected_current_head_sha,
                    json.dumps(
                        scope["resulting_scope"],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    next_owner or expected_owner,
                    now,
                    workspace_id,
                    int(expected_workspace_revision),
                ),
            )
            if cursor.rowcount != 1:
                raise Error(
                    "WORKSPACE_REVISION_MISMATCH", "Workspace CAS lost during recovery"
                )
            new_revision = int(expected_workspace_revision) + 1
            metadata = {
                "sessionless_recovery": {
                    "request_sha256": digest,
                    "source": "first_canonical_session",
                    "previous_head_sha": expected_previous_head_sha,
                    "reviewed_paths": reviewed,
                    "reviewed_base_overlap_paths": reviewed_base_overlap,
                    "old_owner": expected_owner,
                    "new_owner": next_owner or expected_owner,
                    "devhub_writer_lease_id": expected_writer_lease_id or None,
                    "devhub_new_binding_id": expected_active_binding_id or None,
                    "git_ref_changed": False,
                    "base_advance": base_advance,
                }
            }
            values = (
                session_id,
                workspace_id,
                repository,
                branch,
                expected_base_branch,
                expected_base_sha,
                expected_current_head_sha,
                expected_current_tree_sha,
                1,
                new_revision,
                "active",
                next_owner or expected_owner,
                new_lease,
                expected_current_head_sha,
                None,
                None,
                None,
                None,
                None,
                idempotency_key,
                json.dumps(metadata, ensure_ascii=False),
                now,
                now,
                None,
            )
            db.execute(
                "INSERT INTO development_sessions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                values,
            )
            session_row = db.execute(
                "SELECT * FROM development_sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            sessions._append_event(
                db,
                session_row,
                "legacy_workspace_session_reconciled",
                None,
                "active",
                1,
                {
                    "workspace_id": workspace_id,
                    "old_revision": expected_workspace_revision,
                    "new_revision": new_revision,
                    "old_head_sha": expected_previous_head_sha,
                    "current_head_sha": expected_current_head_sha,
                    "reviewed_paths": reviewed,
                    "reviewed_base_overlap_paths": reviewed_base_overlap,
                    "old_owner": expected_owner,
                    "new_owner": next_owner or expected_owner,
                    "devhub_writer_lease_id": expected_writer_lease_id or None,
                    "devhub_new_binding_id": expected_active_binding_id or None,
                    "base_advance": base_advance,
                    "request_sha256": digest,
                },
            )
    except sqlite3.IntegrityError as exc:
        raise Error(
            "RECOVERY_BRANCH_OWNERSHIP_CONFLICT", "conflicting canonical writer state"
        ) from exc
    recovered_workspace = mygithub12.get_workspace(service, workspace_id)
    recovered_session = sessions.get_session(session_id)
    index = drift._index_state(
        service,
        repository,
        expected_current_head_sha,
        expected_current_tree_sha,
        expected_previous_head_sha,
        session_id,
    )
    return {
        "ok": True,
        "confirmed": True,
        "recovery_kind": "revision_handoff_first_session" if next_owner else "first_canonical_session",
        "created_first_session": True,
        "replayed": False,
        "no_replacement_workspace": True,
        "git_ref_changed": False,
        "workspace": recovered_workspace,
        "development_session": recovered_session,
        "ancestry": ancestry,
        "base_advance": base_advance,
        "scope": scope,
        "index": index,
        "writer_ready": bool(index["ready"]),
    }
