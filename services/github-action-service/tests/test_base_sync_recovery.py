import json
from types import SimpleNamespace

import pytest

from app import development_base_sync_recovery as recovery
from app import development_resume as resume
from app import development_session_store as sessions
from app import mygithub12

REPO = "owner/repo"
BRANCH = "ai/base-sync"
BASE_BRANCH = "main"
OLD_BASE = "d" * 40
NEW_BASE = "e" * 40
OLD_HEAD = "a" * 40
CURRENT_HEAD = "b" * 40
OTHER_HEAD = "c" * 40
MERGED_HEAD = "f" * 40
OLD_TREE = "1" * 40
CURRENT_TREE = "2" * 40
OTHER_TREE = "3" * 40
WORKSPACE_ID = "ws_base_sync"


class FakeRepo:
    def __init__(self):
        self.trees = {
            OLD_BASE: "4" * 40,
            NEW_BASE: "5" * 40,
            OLD_HEAD: OLD_TREE,
            CURRENT_HEAD: CURRENT_TREE,
            OTHER_HEAD: OTHER_TREE,
            MERGED_HEAD: "6" * 40,
        }
        self.comparisons = {
            (OLD_BASE, NEW_BASE): self._cfg(OLD_BASE, 1, 0, ["base/region.py"]),
            (OLD_BASE, OLD_HEAD): self._cfg(OLD_BASE, 1, 0, ["allowed/feature.py"]),
            (OLD_HEAD, CURRENT_HEAD): self._cfg(OLD_HEAD, 2, 0, ["base/region.py", "allowed/feature.py"]),
            (NEW_BASE, CURRENT_HEAD): self._cfg(NEW_BASE, 1, 0, ["allowed/feature.py"]),
            (MERGED_HEAD, NEW_BASE): self._cfg(MERGED_HEAD, 1, 0, ["base/region.py"]),
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
        self.heads = {BRANCH: CURRENT_HEAD, BASE_BRANCH: NEW_BASE, "ai/merged-a": MERGED_HEAD}

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
        if repository != REPO:
            raise AssertionError(f"unexpected repository: {repository}")


def _workspace_row(
    *,
    workspace_id=WORKSPACE_ID,
    branch=BRANCH,
    status="active",
    revision=4,
    base_sha=OLD_BASE,
    head=OLD_HEAD,
    tree=OLD_TREE,
    scope=None,
    drift_reason=None,
    lease_seconds=7200,
):
    now = sessions._now()
    return (
        workspace_id,
        REPO,
        branch,
        BASE_BRANCH,
        base_sha,
        head,
        tree,
        status,
        revision,
        "chatgpt",
        now + lease_seconds,
        head,
        json.dumps(scope or {"paths": ["allowed/**"]}, separators=(",", ":")),
        drift_reason,
        None,
        now,
        now,
    )


def _seed(tmp_path, monkeypatch):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "base-sync.db"))
    service = FakeService()
    sessions.init_session_db()
    with sessions._LOCK, sessions._db() as db:
        db.execute("INSERT INTO workspaces VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", _workspace_row())
    initial = mygithub12.get_workspace(service, WORKSPACE_ID)
    session = sessions.create_session(initial, idempotency_key="seed-base-sync")
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
    index_requests = []

    def request_index(*args, **kwargs):
        index_requests.append((args, kwargs))
        return {"ok": True, "job_id": "idx-base-sync", "status": "queued"}

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
    monkeypatch.setattr(recovery.mygithub12, "request_index_build", request_index)
    monkeypatch.setattr(
        recovery.mygithub12,
        "workspace_overlap",
        lambda service, workspace_id: {"ok": True, "workspace_id": workspace_id, "items": []},
    )
    return service, sessions.get_session(session["session_id"]), index_requests


def _args(session, **overrides):
    values = {
        "repository": REPO,
        "branch": BRANCH,
        "workspace_id": WORKSPACE_ID,
        "development_session_id": session["session_id"],
        "expected_workspace_revision": 5,
        "expected_session_revision": session["session_revision"],
        "expected_old_base_sha": OLD_BASE,
        "expected_new_base_sha": NEW_BASE,
        "expected_base_branch": BASE_BRANCH,
        "expected_old_session_head_sha": OLD_HEAD,
        "expected_current_head_sha": CURRENT_HEAD,
        "expected_current_tree_sha": CURRENT_TREE,
        "idempotency_key": "base-sync-once",
        "lease_seconds": 7200,
    }
    values.update(overrides)
    return values


def _call(service, session, **overrides):
    return recovery.recover_base_synced_task(service, **_args(session, **overrides))


def _db_state(session_id):
    with sessions._db() as db:
        workspace = dict(db.execute("SELECT * FROM workspaces WHERE workspace_id=?", (WORKSPACE_ID,)).fetchone())
        session = dict(db.execute("SELECT * FROM development_sessions WHERE session_id=?", (session_id,)).fetchone())
        events = [dict(row) for row in db.execute(
            "SELECT * FROM development_session_events WHERE session_id=? ORDER BY id", (session_id,)
        ).fetchall()]
    return workspace, session, events


def test_t1_base_sync_happy_path_advances_base_head_atomically_and_requests_new_base_index(tmp_path, monkeypatch):
    service, session, index_requests = _seed(tmp_path, monkeypatch)
    result = _call(service, session)

    assert result["control_plane_recovery"] == "CONTROL_PLANE_BASE_SYNC_RECOVERY_SUCCESS"
    assert result["replayed"] is False
    assert result["writer_ready"] is True
    assert result["index_required"] is False
    workspace = result["workspace"]
    recovered = result["development_session"]
    assert workspace["status"] == recovered["status"] == "active"
    assert workspace["drift_reason"] is None
    assert workspace["base_commit_sha"] == recovered["base_commit_sha"] == NEW_BASE
    assert workspace["head_sha"] == recovered["head_commit_sha"] == CURRENT_HEAD
    assert workspace["tree_sha"] == recovered["tree_sha"] == CURRENT_TREE
    assert workspace["revision"] == recovered["workspace_revision"] == 6
    assert recovered["session_revision"] == session["session_revision"] + 1
    assert recovered["last_fast_ci_job_id"] is None
    assert recovered["last_full_ci_job_id"] is None
    assert recovered["last_attestation_id"] is None
    assert recovered["last_failure_resource_uri"] is None
    assert result["audit"]["old_base_sha"] == OLD_BASE
    assert result["audit"]["new_base_sha"] == NEW_BASE
    assert result["audit"]["old_task_delta_paths"] == ["allowed/feature.py"]
    assert result["audit"]["base_delta_paths"] == ["base/region.py"]
    assert result["audit"]["new_task_delta_paths"] == ["allowed/feature.py"]
    assert result["audit"]["overlap_result"]["base_task_overlap_paths"] == []
    assert index_requests
    request_args = index_requests[0][0]
    assert request_args[2] == CURRENT_HEAD
    assert request_args[4] == NEW_BASE
    _, _, events = _db_state(session["session_id"])
    assert any(item["event_type"] == "base_sync_recovery" for item in events)


def test_t2_base_sync_contract_does_not_replace_same_base_recovery(tmp_path, monkeypatch):
    service, session, _ = _seed(tmp_path, monkeypatch)
    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session, expected_new_base_sha=OLD_BASE)
    assert exc.value.code == "RECOVERY_BASE_CHANGED"


def test_t3_old_base_must_be_new_base_ancestor(tmp_path, monkeypatch):
    service, session, _ = _seed(tmp_path, monkeypatch)
    service.repo.set_compare(OLD_BASE, NEW_BASE, merge_base=OTHER_HEAD)
    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session)
    assert exc.value.code == "RECOVERY_ANCESTRY_MISMATCH"


def test_t4_old_session_head_must_be_current_head_ancestor(tmp_path, monkeypatch):
    service, session, _ = _seed(tmp_path, monkeypatch)
    service.repo.set_compare(OLD_HEAD, CURRENT_HEAD, merge_base=OTHER_HEAD)
    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session)
    assert exc.value.code == "RECOVERY_ANCESTRY_MISMATCH"


def test_t5_new_base_must_be_in_current_head_history(tmp_path, monkeypatch):
    service, session, _ = _seed(tmp_path, monkeypatch)
    service.repo.set_compare(NEW_BASE, CURRENT_HEAD, merge_base=OTHER_HEAD, behind_by=1)
    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session)
    assert exc.value.code == "RECOVERY_ANCESTRY_MISMATCH"


def test_t6_base_and_old_task_path_overlap_fails_stop_including_rename_paths(tmp_path, monkeypatch):
    service, session, _ = _seed(tmp_path, monkeypatch)
    service.repo.set_compare(
        OLD_BASE,
        NEW_BASE,
        paths=["base/renamed.py"],
        previous={"base/renamed.py": "allowed/feature.py"},
    )
    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session)
    assert exc.value.code == "RECOVERY_BASE_SYNC_OVERLAP"
    assert exc.value.details["overlapping_paths"] == ["allowed/feature.py"]


def test_t7_scope_applies_only_to_new_base_task_delta(tmp_path, monkeypatch):
    service, session, _ = _seed(tmp_path, monkeypatch)
    service.repo.set_compare(OLD_BASE, OLD_HEAD, paths=["outside/task.py"])
    service.repo.set_compare(NEW_BASE, CURRENT_HEAD, paths=["outside/task.py"])
    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session)
    assert exc.value.code == "RECOVERY_SCOPE_VIOLATION"
    assert exc.value.details["outside_scope_paths"] == ["outside/task.py"]


def test_base_delta_outside_workspace_scope_is_not_a_scope_violation(tmp_path, monkeypatch):
    service, session, _ = _seed(tmp_path, monkeypatch)
    service.repo.set_compare(OLD_BASE, NEW_BASE, paths=["outside/base-owned.py"])
    result = _call(service, session)
    assert result["verification"]["scope"]["changed_paths"] == ["allowed/feature.py"]


def test_t8_task_path_set_must_match_before_and_after_base_sync(tmp_path, monkeypatch):
    service, session, _ = _seed(tmp_path, monkeypatch)
    service.repo.set_compare(NEW_BASE, CURRENT_HEAD, paths=["allowed/different.py"])
    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session)
    assert exc.value.code == "RECOVERY_TASK_DIFF_MISMATCH"


def test_t9_workspace_revision_cas(tmp_path, monkeypatch):
    service, session, _ = _seed(tmp_path, monkeypatch)
    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session, expected_workspace_revision=4)
    assert exc.value.code == "WORKSPACE_REVISION_MISMATCH"


def test_t10_session_revision_cas(tmp_path, monkeypatch):
    service, session, _ = _seed(tmp_path, monkeypatch)
    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session, expected_session_revision=session["session_revision"] + 1)
    assert exc.value.code == "DEVELOPMENT_SESSION_REVISION_MISMATCH"


@pytest.mark.parametrize(("kind", "error_code"), [("head", "RECOVERY_HEAD_MISMATCH"), ("tree", "RECOVERY_TREE_MISMATCH")])
def test_t11_live_head_tree_mismatch_fails_stop(tmp_path, monkeypatch, kind, error_code):
    service, session, _ = _seed(tmp_path, monkeypatch)
    if kind == "head":
        service.client.heads[BRANCH] = OTHER_HEAD
    else:
        service.repo.trees[CURRENT_HEAD] = OTHER_TREE
    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session)
    assert exc.value.code == error_code


def test_t12_base_advancing_during_atomic_recovery_rolls_back(tmp_path, monkeypatch):
    service, session, _ = _seed(tmp_path, monkeypatch)
    original_append = sessions._append_event

    def advance_base(*args, **kwargs):
        original_append(*args, **kwargs)
        service.client.heads[BASE_BRANCH] = OTHER_HEAD

    monkeypatch.setattr(sessions, "_append_event", advance_base)
    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session)
    assert exc.value.code == "RECOVERY_BASE_CHANGED"
    workspace, stored_session, _ = _db_state(session["session_id"])
    assert workspace["status"] == "drifted"
    assert workspace["base_commit_sha"] == OLD_BASE
    assert stored_session["base_commit_sha"] == OLD_BASE
    assert stored_session["head_commit_sha"] == OLD_HEAD


def test_t13_idempotent_retry_does_not_repeat_revisions_or_side_effects(tmp_path, monkeypatch):
    service, session, index_requests = _seed(tmp_path, monkeypatch)
    first = _call(service, session)
    second = _call(service, session)
    assert second["replayed"] is True
    assert second["after"] == first["after"]
    assert second["workspace"]["revision"] == first["workspace"]["revision"]
    assert second["development_session"]["session_revision"] == first["development_session"]["session_revision"]
    assert len(index_requests) == 2  # same exact request identity; index layer is responsible for deduplication


def test_t14_transaction_failure_rolls_back_base_head_and_status_together(tmp_path, monkeypatch):
    service, session, _ = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(sessions, "_append_event", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("injected failure")))
    with pytest.raises(RuntimeError, match="injected failure"):
        _call(service, session)
    workspace, stored_session, _ = _db_state(session["session_id"])
    assert workspace["status"] == "drifted"
    assert workspace["base_commit_sha"] == OLD_BASE
    assert workspace["head_sha"] == CURRENT_HEAD
    assert stored_session["status"] == "pr_ready"
    assert stored_session["base_commit_sha"] == OLD_BASE
    assert stored_session["head_commit_sha"] == OLD_HEAD
    assert stored_session["last_full_ci_job_id"] == "old-full"
    assert stored_session["last_attestation_id"] == "old-att"


def test_t15_old_ci_attestation_and_failure_evidence_are_not_current_after_success(tmp_path, monkeypatch):
    service, session, _ = _seed(tmp_path, monkeypatch)
    result = _call(service, session)
    recovered = result["development_session"]
    assert recovered["index_commit_sha"] is None
    assert recovered["last_fast_ci_job_id"] is None
    assert recovered["last_full_ci_job_id"] is None
    assert recovered["last_attestation_id"] is None
    assert recovered["last_failure_resource_uri"] is None


def test_t16_legacy_merged_workspace_high_overlap_is_ignored_only_with_exact_merged_ancestry(tmp_path, monkeypatch):
    service, session, _ = _seed(tmp_path, monkeypatch)
    with sessions._LOCK, sessions._db() as db:
        db.execute(
            "INSERT INTO workspaces VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            _workspace_row(
                workspace_id="ws_merged_a",
                branch="ai/merged-a",
                status="active",
                revision=1,
                base_sha=OLD_BASE,
                head=MERGED_HEAD,
                tree=service.repo.trees[MERGED_HEAD],
                scope={"paths": ["base/**"]},
            ),
        )
    merged_ws = mygithub12.get_workspace(service, "ws_merged_a")
    merged_session = sessions.create_session(merged_ws, idempotency_key="merged-a")
    sessions.transition(
        merged_session["session_id"],
        merged_session["session_revision"],
        "merged",
        event_type="pull_request_merged",
        allowed_from={"active"},
    )
    monkeypatch.setattr(
        recovery.mygithub12,
        "workspace_overlap",
        lambda *args, **kwargs: {
            "ok": True,
            "workspace_id": WORKSPACE_ID,
            "items": [{"workspace_id": "ws_merged_a", "branch": "ai/merged-a", "level": "high", "evidence": [{"kind": "changed_paths", "items": ["base/region.py"]}]}],
        },
    )
    result = _call(service, session)
    ignored = result["verification"]["ownership"]["ignored_merged_workspaces"]
    assert len(ignored) == 1
    assert ignored[0]["merged_evidence"]["session_status"] == "merged"
    assert ignored[0]["merged_evidence"]["workspace_head_sha"] == MERGED_HEAD


def test_t16_active_overlapping_writer_is_never_ignored_without_terminal_merged_evidence(tmp_path, monkeypatch):
    service, session, _ = _seed(tmp_path, monkeypatch)
    with sessions._LOCK, sessions._db() as db:
        db.execute(
            "INSERT INTO workspaces VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            _workspace_row(
                workspace_id="ws_active_a",
                branch="ai/merged-a",
                status="active",
                revision=1,
                base_sha=OLD_BASE,
                head=MERGED_HEAD,
                tree=service.repo.trees[MERGED_HEAD],
                scope={"paths": ["base/**"]},
            ),
        )
    active_ws = mygithub12.get_workspace(service, "ws_active_a")
    sessions.create_session(active_ws, idempotency_key="active-a")
    monkeypatch.setattr(
        recovery.mygithub12,
        "workspace_overlap",
        lambda *args, **kwargs: {
            "ok": True,
            "workspace_id": WORKSPACE_ID,
            "items": [{"workspace_id": "ws_active_a", "branch": "ai/merged-a", "level": "high", "evidence": []}],
        },
    )
    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session)
    assert exc.value.code == "RECOVERY_WORKSPACE_OVERLAP"


def test_managed_merge_finalization_atomically_closes_workspace_and_releases_writer(tmp_path, monkeypatch):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "merge-finalize.db"))
    service = FakeService()
    sessions.init_session_db()
    with sessions._LOCK, sessions._db() as db:
        db.execute(
            "INSERT INTO workspaces VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            _workspace_row(status="active", revision=4, head=OLD_HEAD, tree=OLD_TREE),
        )
    workspace = mygithub12.get_workspace(service, WORKSPACE_ID)
    session = sessions.create_session(workspace, idempotency_key="merge-finalize")
    session = sessions.transition(
        session["session_id"], session["session_revision"], "pr_ready",
        event_type="pull_request_prepared", allowed_from={"active"}, fields={"pull_number": 99},
    )
    result = sessions.finalize_merged_session_workspace(
        session["session_id"], session["session_revision"], WORKSPACE_ID, 4,
        merge_evidence={"pull_number": 99, "merge_commit_sha": NEW_BASE},
    )
    assert result["session"]["status"] == "merged"
    assert result["session"]["workspace_revision"] == 5
    assert result["session"]["lease_valid"] is False
    assert result["workspace"]["status"] == "closed"
    assert result["workspace"]["lease_expires_at"] == 0
    assert result["workspace"]["index_commit_sha"] is None
    assert result["workspace"]["revision"] == 5


def test_resume_returns_explicit_base_sync_recovery_action_when_pinned_base_lags_current_main(tmp_path, monkeypatch):
    service, session, _ = _seed(tmp_path, monkeypatch)
    workspace = mygithub12.get_workspace(service, WORKSPACE_ID)
    plan = resume._workspace_recovery_plan(
        workspace,
        service=service,
        session=session,
        current_main={"branch": BASE_BRANCH, "commit_sha": NEW_BASE},
        branch_state={"commit_sha": CURRENT_HEAD, "tree_sha": CURRENT_TREE},
    )
    assert plan["action"] == "recover_base_synced_development_task"
    assert plan["recovery_tool"] == "recover_base_synced_development_task"
    assert plan["expected_old_base_sha"] == OLD_BASE
    assert plan["expected_new_base_sha"] == NEW_BASE
    assert plan["preflight"]["verified"] is True


def test_resume_keeps_same_base_drift_on_existing_recovery_action(tmp_path, monkeypatch):
    service, session, _ = _seed(tmp_path, monkeypatch)
    workspace = mygithub12.get_workspace(service, WORKSPACE_ID)
    plan = resume._workspace_recovery_plan(
        workspace,
        service=service,
        session=session,
        current_main={"branch": BASE_BRANCH, "commit_sha": OLD_BASE},
        branch_state={"commit_sha": CURRENT_HEAD, "tree_sha": CURRENT_TREE},
    )
    assert plan["action"] == "recover_drifted_development_task"
