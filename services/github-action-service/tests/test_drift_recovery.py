import json
from types import SimpleNamespace

import pytest

from app import development_drift_recovery as recovery
from app import development_orchestrator as dx
from app import development_resume as resume
from app import development_session_store as sessions
from app import mygithub12

REPO = "owner/repo"
BRANCH = "ai/recovery"
BASE = "main"
OLD_HEAD = "a" * 40
NEW_HEAD = "b" * 40
OTHER_HEAD = "c" * 40
BASE_SHA = "d" * 40
OLD_TREE = "1" * 40
NEW_TREE = "2" * 40
OTHER_TREE = "3" * 40
WORKSPACE_ID = "ws_recovery"


class FakeRepo:
    def __init__(self):
        self.trees = {OLD_HEAD: OLD_TREE, NEW_HEAD: NEW_TREE, OTHER_HEAD: OTHER_TREE, BASE_SHA: "4" * 40}
        self.merge_base = OLD_HEAD
        self.ahead_by = 1
        self.behind_by = 0
        self.changed_paths = ["allowed/feature.py"]
        self.previous_filenames = {}

    def get_commit(self, sha):
        tree = self.trees[sha]
        return SimpleNamespace(tree=SimpleNamespace(sha=tree))

    def compare(self, base, head):
        assert base == OLD_HEAD
        assert head == NEW_HEAD
        return SimpleNamespace(
            merge_base_commit=SimpleNamespace(sha=self.merge_base),
            ahead_by=self.ahead_by,
            behind_by=self.behind_by,
            files=[
                SimpleNamespace(filename=path, previous_filename=self.previous_filenames.get(path))
                for path in self.changed_paths
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
        self.heads = {BRANCH: NEW_HEAD, BASE: BASE_SHA}

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


def _workspace_row(*, status="active", revision=4, head=OLD_HEAD, tree=OLD_TREE, drift_reason=None):
    now = sessions._now()
    return (
        WORKSPACE_ID,
        REPO,
        BRANCH,
        BASE,
        BASE_SHA,
        head,
        tree,
        status,
        revision,
        "chatgpt",
        now + 7200,
        head,
        json.dumps({"paths": ["allowed"]}, separators=(",", ":")),
        drift_reason,
        None,
        now,
        now,
    )


def _seed(tmp_path, monkeypatch):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "recovery.db"))
    service = FakeService()
    sessions.init_session_db()
    with sessions._LOCK, sessions._db() as db:
        db.execute("INSERT INTO workspaces VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", _workspace_row())
    initial = mygithub12.get_workspace(service, WORKSPACE_ID)
    session = sessions.create_session(initial, idempotency_key="seed-session")
    with sessions._LOCK, sessions._db() as db:
        db.execute(
            """UPDATE workspaces SET head_sha=?,tree_sha=?,status='drifted',revision=5,
            drift_reason='branch_moved_externally',index_commit_sha=NULL,lease_expires_at=0 WHERE workspace_id=?""",
            (NEW_HEAD, NEW_TREE, WORKSPACE_ID),
        )
    monkeypatch.setattr(recovery.mygithub12, "workspace_overlap", lambda service, workspace_id: {"ok": True, "workspace_id": workspace_id, "items": []})
    monkeypatch.setattr(
        recovery.mygithub12,
        "get_index_status",
        lambda service, repository, commit_sha="", ref="": {
            "ok": True,
            "repository": repository,
            "commit_sha": commit_sha,
            "tree_sha": NEW_TREE,
            "status": "ready",
        },
    )
    monkeypatch.setattr(recovery.mygithub12, "request_index_build", lambda *args, **kwargs: pytest.fail("ready index must be reused"))
    return service, session


def _args(session, **overrides):
    values = {
        "repository": REPO,
        "branch": BRANCH,
        "workspace_id": WORKSPACE_ID,
        "development_session_id": session["session_id"],
        "expected_workspace_revision": 5,
        "expected_session_revision": session["session_revision"],
        "expected_current_head_sha": NEW_HEAD,
        "expected_current_tree_sha": NEW_TREE,
        "expected_base_branch": BASE,
        "expected_base_sha": BASE_SHA,
        "idempotency_key": "recover-once",
        "lease_seconds": 7200,
    }
    values.update(overrides)
    return values


def _call(service, session, **overrides):
    return recovery.recover_drifted_task(service, **_args(session, **overrides))


def _request(session):
    args = _args(session)
    return recovery._request_identity(
        args["repository"],
        args["branch"],
        args["workspace_id"],
        args["development_session_id"],
        args["expected_workspace_revision"],
        args["expected_session_revision"],
        args["expected_current_head_sha"],
        args["expected_current_tree_sha"],
        args["expected_base_branch"],
        args["expected_base_sha"],
        args["lease_seconds"],
    )


def _db_state(session_id):
    with sessions._db() as db:
        workspace = dict(db.execute("SELECT * FROM workspaces WHERE workspace_id=?", (WORKSPACE_ID,)).fetchone())
        session = dict(db.execute("SELECT * FROM development_sessions WHERE session_id=?", (session_id,)).fetchone())
        events = [dict(row) for row in db.execute(
            "SELECT * FROM development_session_events WHERE session_id=? ORDER BY id", (session_id,)
        ).fetchall()]
    return workspace, session, events


def test_forward_only_external_branch_advance_recovers_atomically(tmp_path, monkeypatch):
    service, session = _seed(tmp_path, monkeypatch)
    result = _call(service, session)
    assert result["control_plane_recovery"] == "CONTROL_PLANE_RECOVERY_SUCCESS"
    assert result["replayed"] is False
    assert result["writer_ready"] is True
    assert result["index_required"] is False
    workspace = result["workspace"]
    recovered = result["development_session"]
    assert workspace["status"] == recovered["status"] == "active"
    assert workspace["drift_reason"] is None
    assert workspace["head_sha"] == recovered["head_commit_sha"] == NEW_HEAD
    assert workspace["tree_sha"] == recovered["tree_sha"] == NEW_TREE
    assert workspace["revision"] == 6
    assert recovered["workspace_revision"] == 6
    assert recovered["session_revision"] == session["session_revision"] + 1
    assert workspace["lease_valid"] is True and recovered["lease_valid"] is True
    assert result["audit"]["old_session_head"] == OLD_HEAD
    assert result["audit"]["ancestry"]["verified"] is True
    assert result["audit"]["scope"]["verified"] is True
    assert len(result["audit"]["idempotency_identity"]) == 64
    _, _, events = _db_state(session["session_id"])
    audit_event = next(item for item in events if item["event_type"] == "manual_branch_recovery")
    assert json.loads(audit_event["data_json"])["adopted_head"] == NEW_HEAD


@pytest.mark.parametrize(
    ("kind", "error_code"),
    [
        ("head", "RECOVERY_HEAD_MISMATCH"),
        ("tree", "RECOVERY_TREE_MISMATCH"),
        ("base", "RECOVERY_BASE_CHANGED"),
        ("deleted", "RECOVERY_BRANCH_DELETED"),
    ],
)
def test_fresh_github_identity_mismatches_fail_stop(tmp_path, monkeypatch, kind, error_code):
    service, session = _seed(tmp_path, monkeypatch)
    if kind == "head":
        service.client.heads[BRANCH] = OTHER_HEAD
    elif kind == "tree":
        service.repo.trees[NEW_HEAD] = OTHER_TREE
    elif kind == "base":
        service.client.heads[BASE] = OTHER_HEAD
    else:
        service.client.heads[BRANCH] = None
    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session)
    assert exc.value.code == error_code


def test_caller_cannot_adopt_an_advanced_base_as_the_pinned_base(tmp_path, monkeypatch):
    service, session = _seed(tmp_path, monkeypatch)
    service.client.heads[BASE] = OTHER_HEAD
    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session, expected_base_sha=OTHER_HEAD)
    assert exc.value.code == "RECOVERY_BASE_CHANGED"


@pytest.mark.parametrize(
    ("field", "value", "error_code"),
    [
        ("expected_workspace_revision", 4, "WORKSPACE_REVISION_MISMATCH"),
        ("expected_session_revision", 99, "DEVELOPMENT_SESSION_REVISION_MISMATCH"),
        ("repository", "owner/other", "RECOVERY_IDENTITY_MISMATCH"),
        ("branch", "ai/other", "RECOVERY_IDENTITY_MISMATCH"),
    ],
)
def test_explicit_identity_and_revision_cas_fail_stop(tmp_path, monkeypatch, field, value, error_code):
    service, session = _seed(tmp_path, monkeypatch)
    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session, **{field: value})
    assert exc.value.code == error_code


def test_old_session_head_must_be_current_head_ancestor(tmp_path, monkeypatch):
    service, session = _seed(tmp_path, monkeypatch)
    service.repo.merge_base = OTHER_HEAD
    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session)
    assert exc.value.code == "RECOVERY_ANCESTRY_MISMATCH"


def test_force_push_or_history_rewrite_is_rejected(tmp_path, monkeypatch):
    service, session = _seed(tmp_path, monkeypatch)
    service.repo.behind_by = 1
    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session)
    assert exc.value.code == "RECOVERY_ANCESTRY_MISMATCH"


def test_changed_paths_must_stay_inside_declared_workspace_scope(tmp_path, monkeypatch):
    service, session = _seed(tmp_path, monkeypatch)
    service.repo.changed_paths = ["allowed/feature.py", "outside/secret.py"]
    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session)
    assert exc.value.code == "RECOVERY_SCOPE_VIOLATION"
    assert exc.value.details["outside_scope_paths"] == ["outside/secret.py"]


def test_renamed_previous_path_must_also_stay_inside_workspace_scope(tmp_path, monkeypatch):
    service, session = _seed(tmp_path, monkeypatch)
    service.repo.previous_filenames["allowed/feature.py"] = "outside/legacy.py"
    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session)
    assert exc.value.code == "RECOVERY_SCOPE_VIOLATION"
    assert exc.value.details["outside_scope_paths"] == ["outside/legacy.py"]


@pytest.mark.parametrize(("status", "reason", "error_code"), [
    ("closed", "branch_moved_externally", "WORKSPACE_CLOSED"),
    ("drifted", "branch_deleted", "RECOVERY_DRIFT_REASON_UNSUPPORTED"),
    ("drifted", "unknown_reason", "RECOVERY_DRIFT_REASON_UNSUPPORTED"),
])
def test_closed_or_unsupported_drift_reason_is_rejected(tmp_path, monkeypatch, status, reason, error_code):
    service, session = _seed(tmp_path, monkeypatch)
    with sessions._LOCK, sessions._db() as db:
        db.execute("UPDATE workspaces SET status=?,drift_reason=? WHERE workspace_id=?", (status, reason, WORKSPACE_ID))
    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session)
    assert exc.value.code == error_code


def test_second_active_workspace_owner_is_rejected(tmp_path, monkeypatch):
    service, session = _seed(tmp_path, monkeypatch)
    target = mygithub12.get_workspace(service, WORKSPACE_ID)
    other = {**target, "workspace_id": "ws_other", "status": "active", "lease_valid": True}
    monkeypatch.setattr(recovery.mygithub12, "list_workspaces", lambda *args, **kwargs: {"ok": True, "items": [target, other]})
    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session)
    assert exc.value.code == "RECOVERY_BRANCH_OWNERSHIP_CONFLICT"


def test_high_overlap_workspace_is_rejected(tmp_path, monkeypatch):
    service, session = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(
        recovery.mygithub12,
        "workspace_overlap",
        lambda *args, **kwargs: {"ok": True, "items": [{"workspace_id": "ws_other", "level": "high", "evidence": ["path"]}]},
    )
    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session)
    assert exc.value.code == "RECOVERY_WORKSPACE_OVERLAP"


def test_idempotent_replay_returns_same_recovery_result_without_new_revision(tmp_path, monkeypatch):
    service, session = _seed(tmp_path, monkeypatch)
    first = _call(service, session)
    second = _call(service, session)
    assert second["replayed"] is True
    assert second["after"] == first["after"]
    assert second["workspace"]["revision"] == first["workspace"]["revision"]
    assert second["development_session"]["session_revision"] == first["development_session"]["session_revision"]


@pytest.mark.parametrize(
    ("kind", "error_code"),
    [
        ("head", "RECOVERY_HEAD_MISMATCH"),
        ("tree", "RECOVERY_TREE_MISMATCH"),
        ("base", "RECOVERY_BASE_CHANGED"),
    ],
)
def test_idempotent_replay_still_requires_fresh_github_identity(tmp_path, monkeypatch, kind, error_code):
    service, session = _seed(tmp_path, monkeypatch)
    _call(service, session)
    if kind == "head":
        service.client.heads[BRANCH] = OTHER_HEAD
    elif kind == "tree":
        service.repo.trees[NEW_HEAD] = OTHER_TREE
    else:
        service.client.heads[BASE] = OTHER_HEAD
    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session)
    assert exc.value.code == error_code


def test_idempotent_replay_rechecks_current_overlap(tmp_path, monkeypatch):
    service, session = _seed(tmp_path, monkeypatch)
    _call(service, session)
    monkeypatch.setattr(
        recovery.mygithub12,
        "workspace_overlap",
        lambda *args, **kwargs: {"ok": True, "items": [{"workspace_id": "ws_other", "level": "high", "evidence": ["path"]}]},
    )
    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session)
    assert exc.value.code == "RECOVERY_WORKSPACE_OVERLAP"


def test_atomic_replay_path_also_rechecks_fresh_github_identity(tmp_path, monkeypatch):
    service, session = _seed(tmp_path, monkeypatch)
    _call(service, session)
    service.client.heads[BRANCH] = OTHER_HEAD
    with pytest.raises(recovery.MyGithub12Error) as exc:
        recovery._atomic_recover(
            service,
            request=_request(session),
            idempotency_key="recover-once",
            verification={"ancestry": {}, "scope": {}, "ownership": {}},
        )
    assert exc.value.code == "RECOVERY_HEAD_MISMATCH"


def test_same_idempotency_key_with_different_payload_conflicts(tmp_path, monkeypatch):
    service, session = _seed(tmp_path, monkeypatch)
    _call(service, session)
    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session, lease_seconds=3600)
    assert exc.value.code == "IDEMPOTENCY_CONFLICT"


def test_transaction_failure_rolls_back_workspace_and_session_together(tmp_path, monkeypatch):
    service, session = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(sessions, "_append_event", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("injected failure")))
    with pytest.raises(RuntimeError, match="injected failure"):
        _call(service, session)
    workspace, stored_session, _ = _db_state(session["session_id"])
    assert workspace["status"] == "drifted"
    assert workspace["drift_reason"] == "branch_moved_externally"
    assert workspace["revision"] == 5
    assert workspace["head_sha"] == NEW_HEAD
    assert stored_session["head_commit_sha"] == OLD_HEAD
    assert stored_session["tree_sha"] == OLD_TREE
    assert stored_session["workspace_revision"] == 4
    assert stored_session["session_revision"] == session["session_revision"]


def test_success_state_resumes_normal_context_and_rejects_old_revisions(tmp_path, monkeypatch):
    service, session = _seed(tmp_path, monkeypatch)
    result = _call(service, session)
    workspace = result["workspace"]
    recovered = result["development_session"]
    index = {"status": "ready", "commit_sha": NEW_HEAD, "tree_sha": NEW_TREE}
    actions = resume._next_actions([], workspace, recovered, index, None, {"policy": {"private_ci": True}})
    assert "continue_write" in actions
    context = dx.resolve_generated_write_context(service, REPO, BRANCH, NEW_HEAD)
    assert context["managed"] is True
    assert context["workspace"]["revision"] == 6
    assert context["session"]["session_revision"] == session["session_revision"] + 1
    with pytest.raises(sessions.MyGithub12Error) as exc:
        sessions._require_revision(session["session_id"], session["session_revision"])
    assert exc.value.code == "DEVELOPMENT_SESSION_REVISION_MISMATCH"
    with pytest.raises(mygithub12.MyGithub12Error) as exc:
        mygithub12.workspace_write_preflight(service, REPO, BRANCH, NEW_HEAD, WORKSPACE_ID, 5)
    assert exc.value.code == "WORKSPACE_REVISION_MISMATCH"


def test_unrecovered_drifted_workspace_still_blocks_generated_file_writes(tmp_path, monkeypatch):
    service, _ = _seed(tmp_path, monkeypatch)
    with pytest.raises(mygithub12.MyGithub12Error) as exc:
        dx.resolve_generated_write_context(service, REPO, BRANCH, NEW_HEAD)
    assert exc.value.code == "WORKSPACE_BRANCH_DRIFTED"


def test_index_failure_does_not_undo_completed_control_plane_recovery(tmp_path, monkeypatch):
    service, session = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(recovery.mygithub12, "get_index_status", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("index down")))
    result = _call(service, session)
    assert result["control_plane_recovery"] == "CONTROL_PLANE_RECOVERY_SUCCESS"
    assert result["writer_ready"] is False
    assert result["index_required"] is True
    assert result["index"]["error"]["code"] == "RuntimeError"
    workspace, stored_session, _ = _db_state(session["session_id"])
    assert workspace["status"] == "active"
    assert stored_session["status"] == "active"
    assert workspace["head_sha"] == stored_session["head_commit_sha"] == NEW_HEAD
