import json
from types import SimpleNamespace

import pytest

from app import development_retarget_recovery as recovery
from app import development_session_store as sessions
from app import mygithub12

REPO = "owner/repo"
BRANCH = "ai/retarget"
OLD_BASE_BRANCH = "ai/upstream"
NEW_BASE_BRANCH = "main"
TASK_PR = 823
UPSTREAM_PR = 820
OLD_BASE = "d" * 40
NEW_BASE = "e" * 40
OLD_HEAD = "a" * 40
CURRENT_HEAD = "b" * 40
OTHER_HEAD = "c" * 40
MERGE_COMMIT = "f" * 40
OLD_TREE = "1" * 40
CURRENT_TREE = "2" * 40
WORKSPACE_ID = "ws_retarget"


class FakeRepo:
    def __init__(self):
        self.trees = {
            OLD_BASE: "4" * 40,
            NEW_BASE: "5" * 40,
            OLD_HEAD: OLD_TREE,
            CURRENT_HEAD: CURRENT_TREE,
            OTHER_HEAD: "3" * 40,
            MERGE_COMMIT: "6" * 40,
        }
        # OLD_BASE -> NEW_BASE intentionally does NOT prove ancestry: this is the
        # squash-aware path-classification comparison only.
        self.comparisons = {
            (OLD_BASE, NEW_BASE): self._cfg(OTHER_HEAD, 10, 1, ["base/region.py"]),
            (OLD_BASE, OLD_HEAD): self._cfg(OLD_BASE, 2, 0, ["allowed/feature.py"]),
            (OLD_HEAD, CURRENT_HEAD): self._cfg(OLD_HEAD, 3, 0, ["base/region.py"]),
            (NEW_BASE, CURRENT_HEAD): self._cfg(NEW_BASE, 2, 0, ["allowed/feature.py"]),
            (MERGE_COMMIT, NEW_BASE): self._cfg(MERGE_COMMIT, 3, 0, ["later/main.py"]),
        }

    @staticmethod
    def _cfg(merge_base, ahead_by, behind_by, paths, previous=None):
        return {
            "merge_base": merge_base,
            "ahead_by": ahead_by,
            "behind_by": behind_by,
            "paths": list(paths),
            "previous": dict(previous or {}),
        }

    def set_compare(self, base, head, *, merge_base=None, ahead_by=None, behind_by=None, paths=None, previous=None):
        cfg = dict(self.comparisons[(base, head)])
        if merge_base is not None:
            cfg["merge_base"] = merge_base
        if ahead_by is not None:
            cfg["ahead_by"] = ahead_by
        if behind_by is not None:
            cfg["behind_by"] = behind_by
        if paths is not None:
            cfg["paths"] = list(paths)
        if previous is not None:
            cfg["previous"] = dict(previous)
        self.comparisons[(base, head)] = cfg

    def get_commit(self, sha):
        return SimpleNamespace(tree=SimpleNamespace(sha=self.trees[sha]))

    def compare(self, base, head):
        cfg = self.comparisons[(base, head)]
        return SimpleNamespace(
            merge_base_commit=SimpleNamespace(sha=cfg["merge_base"]),
            ahead_by=cfg["ahead_by"],
            behind_by=cfg["behind_by"],
            files=[
                SimpleNamespace(filename=path, previous_filename=cfg["previous"].get(path))
                for path in cfg["paths"]
            ],
        )


class FakeGitHub:
    def __init__(self, repo):
        self.repo = repo

    def get_repo(self, repository):
        assert repository == REPO
        return self.repo


class FakeClient:
    def __init__(self, repo):
        self._pygithub = FakeGitHub(repo)
        self.heads = {
            BRANCH: CURRENT_HEAD,
            OLD_BASE_BRANCH: OLD_BASE,
            NEW_BASE_BRANCH: NEW_BASE,
        }

    def get_branch(self, repository, branch):
        assert repository == REPO
        sha = self.heads.get(branch)
        if not sha:
            return None
        return SimpleNamespace(commit=SimpleNamespace(sha=sha))


class FakeService:
    def __init__(self):
        self.repo = FakeRepo()
        self.client = FakeClient(self.repo)

    def _check_repository_allowed(self, repository):
        assert repository == REPO


def _task_pr(**overrides):
    value = {
        "ok": True,
        "repository": REPO,
        "pull_number": TASK_PR,
        "state": "open",
        "draft": True,
        "merged": False,
        "merge_commit_sha": OTHER_HEAD,
        "head_branch": BRANCH,
        "head_sha": CURRENT_HEAD,
        "base_branch": NEW_BASE_BRANCH,
        "base_sha": NEW_BASE,
    }
    value.update(overrides)
    return value


def _upstream_pr(**overrides):
    value = {
        "ok": True,
        "repository": REPO,
        "pull_number": UPSTREAM_PR,
        "state": "closed",
        "draft": False,
        "merged": True,
        "merge_commit_sha": MERGE_COMMIT,
        "head_branch": OLD_BASE_BRANCH,
        "head_sha": OLD_BASE,
        "base_branch": NEW_BASE_BRANCH,
        "base_sha": OTHER_HEAD,
        "merged_at": "2026-09-11T00:00:00+00:00",
    }
    value.update(overrides)
    return value


def _workspace_row(*, base_branch=OLD_BASE_BRANCH, base_sha=OLD_BASE, status="active", revision=4, head=OLD_HEAD, tree=OLD_TREE):
    now = sessions._now()
    return (
        WORKSPACE_ID,
        REPO,
        BRANCH,
        base_branch,
        base_sha,
        head,
        tree,
        status,
        revision,
        "chatgpt",
        now + 7200,
        head,
        json.dumps({"paths": ["allowed/**"]}, separators=(",", ":")),
        None,
        None,
        now,
        now,
    )


def _seed(tmp_path, monkeypatch):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "retarget.db"))
    service = FakeService()
    sessions.init_session_db()
    with sessions._LOCK, sessions._db() as db:
        db.execute("INSERT INTO workspaces VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", _workspace_row())
    initial = mygithub12.get_workspace(service, WORKSPACE_ID)
    session = sessions.create_session(initial, idempotency_key="seed-retarget")
    with sessions._LOCK, sessions._db() as db:
        db.execute(
            """UPDATE development_sessions SET status='pr_ready',last_fast_ci_job_id='old-fast',
            last_full_ci_job_id='old-full',last_attestation_id='old-att',last_failure_resource_uri='old-failure'
            WHERE session_id=?""",
            (session["session_id"],),
        )
        db.execute(
            """UPDATE workspaces SET head_sha=?,tree_sha=?,status='drifted',revision=5,
            drift_reason='branch_moved_externally',index_commit_sha=NULL,lease_expires_at=0 WHERE workspace_id=?""",
            (CURRENT_HEAD, CURRENT_TREE, WORKSPACE_ID),
        )

    task_pr = _task_pr()
    upstream_pr = _upstream_pr()

    def get_pr(repository, pull_number):
        assert repository == REPO
        if int(pull_number) == TASK_PR:
            return dict(task_pr)
        if int(pull_number) == UPSTREAM_PR:
            return dict(upstream_pr)
        return {"ok": False, "error": {"code": "PULL_REQUEST_NOT_FOUND", "message": "missing"}}

    def list_prs(repository, **kwargs):
        assert repository == REPO
        return {
            "ok": True,
            "repository": REPO,
            "total_count": 1,
            "has_more": False,
            "pull_requests": [{
                "pull_number": UPSTREAM_PR,
                "state": "closed",
                "head_branch": OLD_BASE_BRANCH,
                "head_sha": OLD_BASE,
                "base_branch": NEW_BASE_BRANCH,
            }],
        }

    monkeypatch.setattr(recovery.github_utils, "get_github_pull_request", get_pr)
    monkeypatch.setattr(recovery.github_utils, "list_github_pull_requests", list_prs)
    monkeypatch.setattr(
        recovery.mygithub12,
        "get_index_status",
        lambda service, repository, commit_sha="", ref="": {
            "ok": True,
            "repository": repository,
            "commit_sha": commit_sha,
            "tree_sha": CURRENT_TREE,
            "status": "ready",
        },
    )
    monkeypatch.setattr(
        recovery.mygithub12,
        "request_index_build",
        lambda *args, **kwargs: {"ok": True, "job_id": "idx-retarget", "status": "queued"},
    )
    monkeypatch.setattr(
        recovery.mygithub12,
        "workspace_overlap",
        lambda service, workspace_id: {"ok": True, "workspace_id": workspace_id, "items": []},
    )
    return service, sessions.get_session(session["session_id"]), task_pr, upstream_pr


def _args(session, **overrides):
    values = {
        "repository": REPO,
        "branch": BRANCH,
        "pull_number": TASK_PR,
        "upstream_pull_number": UPSTREAM_PR,
        "workspace_id": WORKSPACE_ID,
        "development_session_id": session["session_id"],
        "expected_workspace_revision": 5,
        "expected_session_revision": session["session_revision"],
        "expected_old_base_branch": OLD_BASE_BRANCH,
        "expected_old_base_sha": OLD_BASE,
        "expected_new_base_branch": NEW_BASE_BRANCH,
        "expected_new_base_sha": NEW_BASE,
        "expected_old_session_head_sha": OLD_HEAD,
        "expected_current_head_sha": CURRENT_HEAD,
        "expected_current_tree_sha": CURRENT_TREE,
        "idempotency_key": "retarget-once",
        "lease_seconds": 7200,
    }
    values.update(overrides)
    return values


def _call(service, session, **overrides):
    return recovery.recover_retargeted_task(service, **_args(session, **overrides))


def _db_state(session_id):
    with sessions._db() as db:
        workspace = dict(db.execute("SELECT * FROM workspaces WHERE workspace_id=?", (WORKSPACE_ID,)).fetchone())
        session = dict(db.execute("SELECT * FROM development_sessions WHERE session_id=?", (session_id,)).fetchone())
        events = [dict(row) for row in db.execute(
            "SELECT * FROM development_session_events WHERE session_id=? ORDER BY id", (session_id,)
        ).fetchall()]
    return workspace, session, events


def test_retarget_happy_path_keeps_canonical_writer_and_invalidates_old_evidence(tmp_path, monkeypatch):
    service, session, _, _ = _seed(tmp_path, monkeypatch)
    result = _call(service, session)

    assert result["control_plane_recovery"] == "CONTROL_PLANE_RETARGET_RECOVERY_SUCCESS"
    assert result["writer_ready"] is True
    workspace = result["workspace"]
    recovered = result["development_session"]
    assert workspace["workspace_id"] == WORKSPACE_ID
    assert recovered["session_id"] == session["session_id"]
    assert workspace["status"] == recovered["status"] == "active"
    assert workspace["drift_reason"] is None
    assert workspace["lease_valid"] is True
    assert workspace["base_branch"] == recovered["base_branch"] == NEW_BASE_BRANCH
    assert workspace["base_commit_sha"] == recovered["base_commit_sha"] == NEW_BASE
    assert workspace["head_sha"] == recovered["head_commit_sha"] == CURRENT_HEAD
    assert workspace["tree_sha"] == recovered["tree_sha"] == CURRENT_TREE
    assert workspace["pr_number"] == recovered["pull_number"] == TASK_PR
    assert recovered["last_fast_ci_job_id"] is None
    assert recovered["last_full_ci_job_id"] is None
    assert recovered["last_attestation_id"] is None
    assert recovered["last_failure_resource_uri"] is None
    assert result["audit"]["stale_ci_evidence_cleared"] is True
    assert result["audit"]["stale_attestation_evidence_cleared"] is True
    assert result["audit"]["upstream_merge"]["merge_commit_sha"] == MERGE_COMMIT
    assert result["audit"]["upstream_merge"]["ancestry"]["verified"] is True
    assert result["audit"]["base_transition"]["ancestry_required"] is False
    assert result["audit"]["excluded_imported_base_paths"] == ["base/region.py"]
    assert result["audit"]["recovery_scope_delta_paths"] == []
    _, _, events = _db_state(session["session_id"])
    assert any(item["event_type"] == "retarget_recovery" for item in events)


def test_squash_merge_proof_does_not_require_old_head_to_be_new_base_ancestor(tmp_path, monkeypatch):
    service, session, _, _ = _seed(tmp_path, monkeypatch)
    assert service.repo.comparisons[(OLD_BASE, NEW_BASE)]["merge_base"] != OLD_BASE
    result = _call(service, session)
    assert result["audit"]["upstream_merge"]["ancestry"]["method"] == "github_compare"
    assert result["audit"]["upstream_merge"]["ancestry"]["verified"] is True


def test_normal_merge_proof_accepts_merge_commit_equal_current_new_base(tmp_path, monkeypatch):
    service, session, _, upstream_pr = _seed(tmp_path, monkeypatch)
    upstream_pr["merge_commit_sha"] = NEW_BASE
    result = _call(service, session)
    assert result["audit"]["upstream_merge"]["merge_commit_sha"] == NEW_BASE
    assert result["audit"]["upstream_merge"]["ancestry"]["method"] == "merge_commit_equals_current_base"


def test_imported_base_delta_is_not_treated_as_task_scope_delta(tmp_path, monkeypatch):
    service, session, _, _ = _seed(tmp_path, monkeypatch)
    result = _call(service, session)
    assert result["audit"]["external_forward_delta_paths"] == ["base/region.py"]
    assert result["audit"]["excluded_imported_base_paths"] == ["base/region.py"]
    assert result["audit"]["outside_scope_paths"] == []


def test_real_outside_scope_forward_delta_fails_closed(tmp_path, monkeypatch):
    service, session, _, _ = _seed(tmp_path, monkeypatch)
    service.repo.set_compare(OLD_HEAD, CURRENT_HEAD, paths=["base/region.py", "outside/bad.py"])
    service.repo.set_compare(NEW_BASE, CURRENT_HEAD, paths=["allowed/feature.py", "outside/bad.py"])
    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session)
    assert exc.value.code == "RECOVERY_SCOPE_VIOLATION"
    assert exc.value.details["outside_scope_paths"] == ["outside/bad.py"]


def test_overlap_requires_exact_reviewed_path_set_and_rename_identity(tmp_path, monkeypatch):
    service, session, _, _ = _seed(tmp_path, monkeypatch)
    service.repo.set_compare(
        OLD_BASE,
        NEW_BASE,
        paths=["base/renamed.py"],
        previous={"base/renamed.py": "allowed/feature.py"},
    )
    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session)
    assert exc.value.code == "RECOVERY_BASE_SYNC_OVERLAP"
    assert exc.value.details["actual_overlap_paths"] == ["allowed/feature.py"]
    with pytest.raises(recovery.MyGithub12Error) as wrong:
        _call(service, session, reviewed_overlap_paths_json=json.dumps(["base/renamed.py"]))
    assert wrong.value.code == "RECOVERY_BASE_SYNC_OVERLAP"
    assert wrong.value.details["unexpected_reviewed_overlap_paths"] == ["base/renamed.py"]


def test_reviewed_overlap_exact_equality_allows_gate_but_remains_scope_checked(tmp_path, monkeypatch):
    service, session, _, _ = _seed(tmp_path, monkeypatch)
    service.repo.set_compare(OLD_BASE, NEW_BASE, paths=["allowed/feature.py"])
    service.repo.set_compare(OLD_HEAD, CURRENT_HEAD, paths=["allowed/feature.py"])
    result = _call(
        service,
        session,
        reviewed_overlap_paths_json=json.dumps(["allowed/feature.py"]),
    )
    assert result["audit"]["actual_overlap_paths"] == ["allowed/feature.py"]
    assert result["audit"]["reviewed_overlap_paths"] == ["allowed/feature.py"]
    assert result["audit"]["recovery_scope_delta_paths"] == ["allowed/feature.py"]


def test_task_pr_head_drift_fails_closed(tmp_path, monkeypatch):
    service, session, task_pr, _ = _seed(tmp_path, monkeypatch)
    task_pr["head_sha"] = OTHER_HEAD
    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session)
    assert exc.value.code == "RECOVERY_PR_IDENTITY_MISMATCH"


def test_task_pr_base_drift_fails_closed(tmp_path, monkeypatch):
    service, session, task_pr, _ = _seed(tmp_path, monkeypatch)
    task_pr["base_sha"] = OTHER_HEAD
    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session)
    assert exc.value.code == "RECOVERY_PR_IDENTITY_MISMATCH"


def test_live_new_base_branch_drift_fails_closed(tmp_path, monkeypatch):
    service, session, _, _ = _seed(tmp_path, monkeypatch)
    service.client.heads[NEW_BASE_BRANCH] = OTHER_HEAD
    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session)
    assert exc.value.code == "RECOVERY_BASE_CHANGED"


def test_historical_old_base_branch_drift_fails_closed(tmp_path, monkeypatch):
    service, session, _, _ = _seed(tmp_path, monkeypatch)
    service.client.heads[OLD_BASE_BRANCH] = OTHER_HEAD
    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session)
    assert exc.value.code == "RECOVERY_BASE_CHANGED"


def test_upstream_merge_proof_requires_exact_old_branch_head(tmp_path, monkeypatch):
    service, session, _, upstream_pr = _seed(tmp_path, monkeypatch)
    upstream_pr["head_sha"] = OTHER_HEAD
    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session)
    assert exc.value.code == "RECOVERY_UPSTREAM_MERGE_PROOF_MISMATCH"


def test_upstream_merge_commit_must_be_in_current_new_base(tmp_path, monkeypatch):
    service, session, _, _ = _seed(tmp_path, monkeypatch)
    service.repo.set_compare(MERGE_COMMIT, NEW_BASE, merge_base=OTHER_HEAD, behind_by=1)
    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session)
    assert exc.value.code == "MERGE_EVIDENCE_UNVERIFIED"


def test_workspace_revision_cas_negative_case(tmp_path, monkeypatch):
    service, session, _, _ = _seed(tmp_path, monkeypatch)
    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session, expected_workspace_revision=4)
    assert exc.value.code == "WORKSPACE_REVISION_MISMATCH"


def test_session_revision_cas_negative_case(tmp_path, monkeypatch):
    service, session, _, _ = _seed(tmp_path, monkeypatch)
    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session, expected_session_revision=session["session_revision"] + 1)
    assert exc.value.code == "DEVELOPMENT_SESSION_REVISION_MISMATCH"


def test_idempotent_replay_returns_same_canonical_writer_without_second_event(tmp_path, monkeypatch):
    service, session, _, _ = _seed(tmp_path, monkeypatch)
    first = _call(service, session)
    _, _, events_before = _db_state(session["session_id"])
    second = _call(service, session)
    _, _, events_after = _db_state(session["session_id"])
    assert first["workspace"]["workspace_id"] == second["workspace"]["workspace_id"] == WORKSPACE_ID
    assert first["development_session"]["session_id"] == second["development_session"]["session_id"] == session["session_id"]
    assert second["replayed"] is True
    assert len(events_after) == len(events_before)


def test_idempotency_key_payload_change_fails_closed(tmp_path, monkeypatch):
    service, session, _, _ = _seed(tmp_path, monkeypatch)
    _call(service, session)
    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session, reviewed_overlap_paths_json=json.dumps(["allowed/feature.py"]))
    assert exc.value.code == "IDEMPOTENCY_CONFLICT"


def test_duplicate_active_writer_fails_before_recovery(tmp_path, monkeypatch):
    service, session, _, _ = _seed(tmp_path, monkeypatch)
    original = recovery.mygithub12.list_workspaces

    def duplicate(service_arg, repository="", branch="", limit=100):
        target = mygithub12.get_workspace(service_arg, WORKSPACE_ID)
        duplicate_ws = {**target, "workspace_id": "ws_other", "status": "active", "lease_valid": True}
        return {"ok": True, "items": [target, duplicate_ws]}

    monkeypatch.setattr(recovery.mygithub12, "list_workspaces", duplicate)
    try:
        with pytest.raises(recovery.MyGithub12Error) as exc:
            _call(service, session)
        assert exc.value.code == "RECOVERY_BRANCH_OWNERSHIP_CONFLICT"
    finally:
        monkeypatch.setattr(recovery.mygithub12, "list_workspaces", original)


def test_partial_already_new_base_state_is_supported_but_old_session_head_stays_exact(tmp_path, monkeypatch):
    service, session, _, _ = _seed(tmp_path, monkeypatch)
    with sessions._LOCK, sessions._db() as db:
        db.execute(
            "UPDATE workspaces SET base_branch=?,base_commit_sha=? WHERE workspace_id=?",
            (NEW_BASE_BRANCH, NEW_BASE, WORKSPACE_ID),
        )
        db.execute(
            "UPDATE development_sessions SET base_branch=?,base_commit_sha=? WHERE session_id=?",
            (NEW_BASE_BRANCH, NEW_BASE, session["session_id"]),
        )
    result = _call(service, session)
    assert result["before"]["pinned_base_state"] == recovery.PINNED_RETARGET_ALREADY_NEW
    assert result["workspace"]["base_branch"] == NEW_BASE_BRANCH
    assert result["development_session"]["head_commit_sha"] == CURRENT_HEAD


def test_mixed_old_new_pinned_base_state_fails_closed(tmp_path, monkeypatch):
    service, session, _, _ = _seed(tmp_path, monkeypatch)
    with sessions._LOCK, sessions._db() as db:
        db.execute(
            "UPDATE workspaces SET base_branch=?,base_commit_sha=? WHERE workspace_id=?",
            (NEW_BASE_BRANCH, NEW_BASE, WORKSPACE_ID),
        )
    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session)
    assert exc.value.code == "RECOVERY_BASE_CHANGED"


def test_planner_finds_exact_merged_upstream_and_returns_full_cas_identity(tmp_path, monkeypatch):
    service, session, _, _ = _seed(tmp_path, monkeypatch)
    workspace = mygithub12.get_workspace(service, WORKSPACE_ID)
    plan = recovery.plan_retargeted_task(
        service,
        workspace,
        session,
        _task_pr(),
        {"branch": NEW_BASE_BRANCH, "commit_sha": NEW_BASE, "tree_sha": "5" * 40},
        {"commit_sha": CURRENT_HEAD, "tree_sha": CURRENT_TREE},
    )
    assert plan["action"] == "recover_retargeted_development_task"
    assert plan["pull_number"] == TASK_PR
    assert plan["upstream_pull_number"] == UPSTREAM_PR
    assert plan["workspace_id"] == WORKSPACE_ID
    assert plan["development_session_id"] == session["session_id"]
    assert plan["expected_workspace_revision"] == 5
    assert plan["expected_session_revision"] == session["session_revision"]
    assert plan["expected_old_base_branch"] == OLD_BASE_BRANCH
    assert plan["expected_new_base_branch"] == NEW_BASE_BRANCH
    assert plan["actual_overlap_paths"] == []


def test_planner_fails_when_no_unique_merged_upstream_pr_exists(tmp_path, monkeypatch):
    service, session, _, _ = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(
        recovery.github_utils,
        "list_github_pull_requests",
        lambda *args, **kwargs: {"ok": True, "pull_requests": [], "has_more": False},
    )
    workspace = mygithub12.get_workspace(service, WORKSPACE_ID)
    with pytest.raises(recovery.MyGithub12Error) as exc:
        recovery.plan_retargeted_task(
            service,
            workspace,
            session,
            _task_pr(),
            {"branch": NEW_BASE_BRANCH, "commit_sha": NEW_BASE, "tree_sha": "5" * 40},
            {"commit_sha": CURRENT_HEAD, "tree_sha": CURRENT_TREE},
        )
    assert exc.value.code == "RECOVERY_UPSTREAM_MERGE_PROOF_REQUIRED"
