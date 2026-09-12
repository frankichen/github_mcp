import json
from types import SimpleNamespace

import pytest

from app import ci_database
from app import ci_request_store
from app import development_drift_recovery as recovery
from app import development_orchestrator as dx
from app import development_resume as resume
from app import development_session_store as sessions
from app import mygithub12
from app import mygithub12_workspace as workspace_scope

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
        json.dumps({"paths": ["allowed/**"]}, separators=(",", ":")),
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


@pytest.mark.parametrize(
    ("declaration", "path", "expected"),
    [
        ("allowed/**", "allowed/feature.py", True),
        ("allowed/**", "allowed/sub/feature.py", True),
        ("allowed/**", "outside/feature.py", False),
        ("allowed", "allowed/feature.py", True),
        ("allowed/feature.py", "allowed/feature.py", True),
        ("allowed/feature.py", "allowed/other.py", False),
        ("allowed/*.py", "allowed/feature.py", True),
        ("allowed/feature?.py", "allowed/feature1.py", True),
        ("allowed/[ab].py", "allowed/a.py", True),
    ],
)
def test_workspace_scope_path_matcher_supports_prefix_exact_and_glob(declaration, path, expected):
    assert workspace_scope.scope_path_matches(path, declaration) is expected


def test_multiple_glob_declarations_accept_real_p2p_recovery_paths():
    workspace = {
        "workspace_id": WORKSPACE_ID,
        "scope": {
            "paths": [
                "internal/app/api/**",
                "internal/modules/admindevice/**",
                "internal/modules/devicebind/**",
            ]
        },
    }
    changed_paths = [
        "internal/app/api/app.go",
        "internal/modules/admindevice/bind_report_backend_cache_test.go",
        "internal/modules/admindevice/service.go",
        "internal/modules/devicebind/bind_report_projection_test.go",
        "internal/modules/devicebind/service.go",
        "internal/modules/devicebind/service_detail.go",
        "internal/modules/devicebind/service_list.go",
        "internal/modules/devicebind/types_detail.go",
        "internal/modules/devicebind/types_list.go",
    ]

    evidence = recovery._verify_scope(workspace, changed_paths)

    assert evidence["verified"] is True
    assert evidence["outside_scope_paths"] == []
    assert evidence["changed_paths"] == changed_paths


def test_empty_scope_rejects_arbitrary_changed_path():
    with pytest.raises(recovery.MyGithub12Error) as exc:
        recovery._verify_scope({"workspace_id": WORKSPACE_ID, "scope": {"paths": []}}, ["allowed/feature.py"])
    assert exc.value.code == "RECOVERY_SCOPE_VIOLATION"
    assert exc.value.details["outside_scope_paths"] == ["allowed/feature.py"]


def test_scope_matcher_exception_fails_recovery_closed(monkeypatch):
    def fail_match(*args, **kwargs):
        raise RuntimeError("injected matcher failure")

    monkeypatch.setattr(workspace_scope, "fnmatchcase", fail_match)
    with pytest.raises(recovery.MyGithub12Error) as exc:
        recovery._verify_scope(
            {"workspace_id": WORKSPACE_ID, "scope": {"paths": ["allowed/**"]}},
            ["allowed/feature.py"],
        )
    assert exc.value.code == "RECOVERY_SCOPE_VIOLATION"
    assert exc.value.details["outside_scope_paths"] == ["allowed/feature.py"]


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


def test_terminal_cancelled_full_validation_then_forward_drift_recovers_same_writer(tmp_path, monkeypatch):
    """Regression for terminal CI + stale validating_full + branch_moved_externally deadlock."""
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "managed-recovery.db"))
    monkeypatch.setattr(ci_database, "DB_PATH", str(tmp_path / "private-ci.db"))
    connection = getattr(ci_database._local, "db", None)
    if connection is not None:
        connection.close()
    ci_database._local.db = None
    ci_database.init_db()
    sessions.init_session_db()
    service = FakeService()

    with sessions._LOCK, sessions._db() as db:
        db.execute("INSERT INTO workspaces VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", _workspace_row())
    initial_workspace = mygithub12.get_workspace(service, WORKSPACE_ID)
    created = sessions.create_session(initial_workspace, idempotency_key="terminal-drift-e2e")
    validating = sessions.transition(
        created["session_id"], created["session_revision"], "validating_full", allowed_from={"active"},
    )

    request_payload = {
        "schema": "development-validation-v1",
        "development_session_id": validating["session_id"],
        "expected_session_revision": validating["session_revision"],
        "workspace_id": WORKSPACE_ID,
        "workspace_revision": validating["workspace_revision"],
        "repository": REPO,
        "branch": BRANCH,
        "commit_sha": OLD_HEAD,
        "tree_sha": OLD_TREE,
        "profile": "repo-auto-check",
        "mode": "full",
        "base_sha": BASE_SHA,
    }
    request_hash = ci_request_store.compute_normalized_request_hash(request_payload)
    request = ci_request_store.create_or_get_ci_request(
        repository=REPO,
        branch=BRANCH,
        commit_sha=OLD_HEAD,
        tree_sha=OLD_TREE,
        profile="repo-auto-check",
        effective_config_digest="d" * 64,
        idempotency_key="terminal-drift-ci-request",
        normalized_request_hash=request_hash,
        request_payload=request_payload,
    )
    request = ci_request_store.transition_ci_request(
        request["request_id"], request["revision"], "preparing", "preparing",
    )
    job = ci_database.create_or_get_job(
        REPO, BRANCH, OLD_HEAD, "repo-auto-check", 100, 900, True, False,
        BASE_SHA, ["allowed/feature.py"], 1, False,
    )
    request = ci_request_store.transition_ci_request(
        request["request_id"], request["revision"], "queued", "queued", worker_job_id=job["job_id"],
    )
    request = ci_request_store.transition_ci_request(
        request["request_id"], request["revision"], "running", "running",
    )
    sessions.record_validation(
        validating["session_id"], validating["session_revision"], "full", OLD_HEAD, OLD_TREE,
        request_id=request["request_id"], job_id=job["job_id"], status="running",
        evidence={
            "request_id": request["request_id"],
            "selection": {"complete": True, "changed_paths": ["allowed/feature.py"]},
        },
    )
    assert ci_database.complete_job(
        job["job_id"], -1, "cancelled", summary={"git_tree_sha": OLD_TREE},
    ) is True
    request = ci_request_store.transition_ci_request(
        request["request_id"], request["revision"], "terminal", "cancelled",
        terminal_reason="regression_cancelled",
    )
    assert request["phase"] == "terminal" and request["status"] == "cancelled"

    with sessions._LOCK, sessions._db() as db:
        db.execute(
            """UPDATE workspaces SET head_sha=?,tree_sha=?,status='drifted',revision=5,
               drift_reason='branch_moved_externally',index_commit_sha=NULL,lease_expires_at=0
               WHERE workspace_id=?""",
            (NEW_HEAD, NEW_TREE, WORKSPACE_ID),
        )

    monkeypatch.setattr(
        resume, "_repository_policy",
        lambda repository: {"ok": True, "repository": repository, "policy": {"github": True, "private_ci": True}},
    )
    monkeypatch.setattr(resume, "_discover_pr_by_branch", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        resume, "_current_main",
        lambda _service, repository: {
            "branch": BASE, "repository": repository, "commit_sha": BASE_SHA, "tree_sha": "4" * 40,
        },
    )
    monkeypatch.setattr(
        resume, "_resolve_branch",
        lambda _service, repository, branch, base_branch: {
            "ok": True, "repository": repository, "branch": branch, "base_branch": base_branch,
            "commit_sha": NEW_HEAD, "tree_sha": NEW_TREE,
        },
    )
    monkeypatch.setattr(
        resume, "_select_workspace",
        lambda _service, repository, branch: (
            mygithub12.get_workspace(service, WORKSPACE_ID),
            [mygithub12.get_workspace(service, WORKSPACE_ID)],
        ),
    )
    monkeypatch.setattr(
        resume.mygithub12, "get_index_status",
        lambda _service, repository, commit_sha="", ref="": {
            "ok": True, "repository": repository, "commit_sha": commit_sha,
            "tree_sha": NEW_TREE, "status": "ready",
        },
    )
    monkeypatch.setattr(
        resume.mygithub12, "workspace_overlap",
        lambda _service, workspace_id: {"ok": True, "workspace_id": workspace_id, "items": []},
    )

    resumed = resume.resume_task(service, REPO, branch=BRANCH, recover_stale_session=True)
    resumed_session = resumed["development_session"]
    assert resumed["workspace"]["workspace_id"] == WORKSPACE_ID
    assert resumed["workspace"]["status"] == "drifted"
    assert resumed_session["session_id"] == validating["session_id"]
    assert resumed_session["status"] == "active"
    assert resumed_session["head_commit_sha"] == OLD_HEAD
    assert resumed_session["last_full_ci_job_id"] == job["job_id"]
    assert resumed["recovery"]["transient"]["reconciled"] is True
    assert resumed["recovery"]["transient"]["workspace_drift_pending_recovery"] is True
    assert resumed["next_allowed_actions"][0] == "recover_drifted_development_task"
    assert "continue_write" not in resumed["next_allowed_actions"]

    monkeypatch.setattr(
        recovery.mygithub12, "workspace_overlap",
        lambda _service, workspace_id: {"ok": True, "workspace_id": workspace_id, "items": []},
    )
    monkeypatch.setattr(
        recovery.mygithub12, "get_index_status",
        lambda _service, repository, commit_sha="", ref="": {
            "ok": True, "repository": repository, "commit_sha": commit_sha,
            "tree_sha": NEW_TREE, "status": "ready",
        },
    )
    monkeypatch.setattr(
        recovery.mygithub12, "request_index_build",
        lambda *args, **kwargs: pytest.fail("ready exact-head index must be reused"),
    )
    recovered = recovery.recover_drifted_task(
        service,
        repository=REPO,
        branch=BRANCH,
        workspace_id=WORKSPACE_ID,
        development_session_id=resumed_session["session_id"],
        expected_workspace_revision=5,
        expected_session_revision=resumed_session["session_revision"],
        expected_current_head_sha=NEW_HEAD,
        expected_current_tree_sha=NEW_TREE,
        expected_base_branch=BASE,
        expected_base_sha=BASE_SHA,
        idempotency_key="terminal-drift-recover",
        lease_seconds=7200,
    )
    recovered_session = recovered["development_session"]
    assert recovered["workspace"]["workspace_id"] == WORKSPACE_ID
    assert recovered_session["session_id"] == validating["session_id"]
    assert recovered["workspace"]["status"] == recovered_session["status"] == "active"
    assert recovered_session["head_commit_sha"] == NEW_HEAD
    assert recovered_session["tree_sha"] == NEW_TREE
    assert recovered_session["last_full_ci_job_id"] is None
    assert recovered_session["last_attestation_id"] is None

    context = dx.resolve_generated_write_context(service, REPO, BRANCH, NEW_HEAD)
    assert context["managed"] is True
    assert context["workspace"]["workspace_id"] == WORKSPACE_ID
    assert context["session"]["session_id"] == validating["session_id"]
    write_preflight = mygithub12.workspace_write_preflight(
        service, REPO, BRANCH, NEW_HEAD, WORKSPACE_ID, recovered["workspace"]["revision"],
    )
    assert write_preflight["workspace_id"] == WORKSPACE_ID
    assert write_preflight["head_sha"] == NEW_HEAD
    assert write_preflight["tree_sha"] == NEW_TREE
    with pytest.raises(sessions.MyGithub12Error) as exc:
        sessions._require_revision(resumed_session["session_id"], resumed_session["session_revision"])
    assert exc.value.code == "DEVELOPMENT_SESSION_REVISION_MISMATCH"
    with pytest.raises(mygithub12.MyGithub12Error) as exc:
        mygithub12.workspace_write_preflight(service, REPO, BRANCH, NEW_HEAD, WORKSPACE_ID, 5)
    assert exc.value.code == "WORKSPACE_REVISION_MISMATCH"

    with sessions._db() as db:
        active_workspaces = db.execute(
            "SELECT COUNT(*) FROM workspaces WHERE repository=? AND branch=? AND status='active'",
            (REPO, BRANCH),
        ).fetchone()[0]
        active_sessions = db.execute(
            """SELECT COUNT(*) FROM development_sessions WHERE workspace_id=?
               AND status IN ('preparing','active','validating_fast','validating_full','pr_ready','drifted','blocked','closing')""",
            (WORKSPACE_ID,),
        ).fetchone()[0]
    assert active_workspaces == 1
    assert active_sessions == 1


def test_two_terminal_cancelled_correlations_then_forward_drift_recovers_same_writer(tmp_path, monkeypatch):
    """Production-shaped regression for #823 multi-correlation stale validation deadlock."""
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "managed-set-recovery.db"))
    monkeypatch.setattr(ci_database, "DB_PATH", str(tmp_path / "private-ci-set.db"))
    connection = getattr(ci_database._local, "db", None)
    if connection is not None:
        connection.close()
    ci_database._local.db = None
    ci_database.init_db()
    sessions.init_session_db()
    service = FakeService()

    with sessions._LOCK, sessions._db() as db:
        db.execute("INSERT INTO workspaces VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", _workspace_row())
    initial_workspace = mygithub12.get_workspace(service, WORKSPACE_ID)
    created = sessions.create_session(initial_workspace, idempotency_key="terminal-set-drift-e2e")
    validating = sessions.transition(
        created["session_id"], created["session_revision"], "validating_full", allowed_from={"active"},
    )

    requests = []
    jobs = []
    for index in range(2):
        request_payload = {
            "schema": "development-validation-v1",
            "development_session_id": validating["session_id"],
            "expected_session_revision": validating["session_revision"],
            "workspace_id": WORKSPACE_ID,
            "workspace_revision": validating["workspace_revision"],
            "repository": REPO,
            "branch": BRANCH,
            "commit_sha": OLD_HEAD,
            "tree_sha": OLD_TREE,
            "profile": "repo-auto-check",
            "mode": "full",
            "base_branch": BASE,
            "base_sha": BASE_SHA,
        }
        request_hash = ci_request_store.compute_normalized_request_hash(request_payload)
        request = ci_request_store.create_or_get_ci_request(
            repository=REPO,
            branch=BRANCH,
            commit_sha=OLD_HEAD,
            tree_sha=OLD_TREE,
            profile="repo-auto-check",
            effective_config_digest="d" * 64,
            idempotency_key=f"terminal-set-drift-ci-request-{index}",
            normalized_request_hash=request_hash,
            request_payload=request_payload,
        )
        request = ci_request_store.transition_ci_request(
            request["request_id"], request["revision"], "preparing", "preparing",
        )
        job = ci_database.create_or_get_job(
            REPO, BRANCH, OLD_HEAD, "repo-auto-check", 100, 900, True, False,
            BASE_SHA, ["allowed/feature.py"], 1, False,
        )
        request = ci_request_store.transition_ci_request(
            request["request_id"], request["revision"], "queued", "queued",
            worker_job_id=job["job_id"],
        )
        request = ci_request_store.transition_ci_request(
            request["request_id"], request["revision"], "running", "running",
        )
        sessions.record_validation(
            validating["session_id"], validating["session_revision"], "full", OLD_HEAD, OLD_TREE,
            request_id=request["request_id"], job_id=job["job_id"], status="running",
            evidence={
                "request_id": request["request_id"],
                "selection": {"complete": True, "changed_paths": ["allowed/feature.py"]},
            },
        )
        assert ci_database.complete_job(
            job["job_id"], -1, "cancelled", summary={"git_tree_sha": OLD_TREE},
        ) is True
        request = ci_request_store.transition_ci_request(
            request["request_id"], request["revision"], "terminal", "cancelled",
            terminal_reason=f"regression_set_cancelled_{index}",
        )
        requests.append(request)
        jobs.append(job)

    with sessions._LOCK, sessions._db() as db:
        db.execute(
            """UPDATE workspaces SET head_sha=?,tree_sha=?,status='drifted',revision=5,
               drift_reason='branch_moved_externally',index_commit_sha=NULL,lease_expires_at=0
               WHERE workspace_id=?""",
            (NEW_HEAD, NEW_TREE, WORKSPACE_ID),
        )

    monkeypatch.setattr(
        resume, "_repository_policy",
        lambda repository: {
            "ok": True, "repository": repository,
            "policy": {"github": True, "private_ci": True},
        },
    )
    monkeypatch.setattr(resume, "_discover_pr_by_branch", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        resume, "_current_main",
        lambda _service, repository: {
            "branch": BASE, "repository": repository, "commit_sha": BASE_SHA, "tree_sha": "4" * 40,
        },
    )
    monkeypatch.setattr(
        resume, "_resolve_branch",
        lambda _service, repository, branch, base_branch: {
            "ok": True, "repository": repository, "branch": branch, "base_branch": base_branch,
            "commit_sha": NEW_HEAD, "tree_sha": NEW_TREE,
        },
    )
    monkeypatch.setattr(
        resume, "_select_workspace",
        lambda _service, repository, branch: (
            mygithub12.get_workspace(service, WORKSPACE_ID),
            [mygithub12.get_workspace(service, WORKSPACE_ID)],
        ),
    )
    monkeypatch.setattr(
        resume.mygithub12, "get_index_status",
        lambda _service, repository, commit_sha="", ref="": {
            "ok": True, "repository": repository, "commit_sha": commit_sha,
            "tree_sha": NEW_TREE, "status": "ready",
        },
    )
    monkeypatch.setattr(
        resume.mygithub12, "workspace_overlap",
        lambda _service, workspace_id: {"ok": True, "workspace_id": workspace_id, "items": []},
    )
    monkeypatch.setattr(
        resume.attestation_registry, "find_reusable_attestation_for_job",
        lambda *args, **kwargs: pytest.fail("multi-correlation recovery must not reuse attestation"),
    )

    resumed = resume.resume_task(service, REPO, branch=BRANCH, recover_stale_session=True)
    resumed_session = resumed["development_session"]
    expected_request_ids = sorted(request["request_id"] for request in requests)
    expected_job_ids = sorted(job["job_id"] for job in jobs)
    assert resumed["workspace"]["workspace_id"] == WORKSPACE_ID
    assert resumed["workspace"]["status"] == "drifted"
    assert resumed_session["session_id"] == validating["session_id"]
    assert resumed_session["status"] == "active"
    assert resumed_session["head_commit_sha"] == OLD_HEAD
    assert resumed_session["tree_sha"] == OLD_TREE
    assert resumed_session["last_full_ci_job_id"] is None
    assert resumed_session["last_attestation_id"] is None
    assert resumed_session["last_failure_resource_uri"] is None
    transient = resumed["recovery"]["transient"]
    assert transient["reconciled"] is True
    assert transient["correlation_source"] == "persisted_terminal_set"
    assert transient["correlation_set"]["request_ids"] == expected_request_ids
    assert transient["correlation_set"]["job_ids"] == expected_job_ids
    assert transient["validation_result"]["merge_eligible"] is False
    assert transient["validation_result"]["attestation"] is None
    assert transient["workspace_drift_pending_recovery"] is True
    assert resumed["next_allowed_actions"][0] == "recover_drifted_development_task"
    assert "continue_write" not in resumed["next_allowed_actions"]

    monkeypatch.setattr(
        recovery.mygithub12, "workspace_overlap",
        lambda _service, workspace_id: {"ok": True, "workspace_id": workspace_id, "items": []},
    )
    monkeypatch.setattr(
        recovery.mygithub12, "get_index_status",
        lambda _service, repository, commit_sha="", ref="": {
            "ok": True, "repository": repository, "commit_sha": commit_sha,
            "tree_sha": NEW_TREE, "status": "ready",
        },
    )
    monkeypatch.setattr(
        recovery.mygithub12, "request_index_build",
        lambda *args, **kwargs: pytest.fail("ready exact-head index must be reused"),
    )
    recovered = recovery.recover_drifted_task(
        service,
        repository=REPO,
        branch=BRANCH,
        workspace_id=WORKSPACE_ID,
        development_session_id=resumed_session["session_id"],
        expected_workspace_revision=5,
        expected_session_revision=resumed_session["session_revision"],
        expected_current_head_sha=NEW_HEAD,
        expected_current_tree_sha=NEW_TREE,
        expected_base_branch=BASE,
        expected_base_sha=BASE_SHA,
        idempotency_key="terminal-set-drift-recover",
        lease_seconds=7200,
    )
    recovered_session = recovered["development_session"]
    assert recovered["workspace"]["workspace_id"] == WORKSPACE_ID
    assert recovered_session["session_id"] == validating["session_id"]
    assert recovered["workspace"]["status"] == recovered_session["status"] == "active"
    assert recovered_session["head_commit_sha"] == NEW_HEAD
    assert recovered_session["tree_sha"] == NEW_TREE
    assert recovered_session["last_full_ci_job_id"] is None
    assert recovered_session["last_attestation_id"] is None

    context = dx.resolve_generated_write_context(service, REPO, BRANCH, NEW_HEAD)
    assert context["managed"] is True
    assert context["workspace"]["workspace_id"] == WORKSPACE_ID
    assert context["session"]["session_id"] == validating["session_id"]
    write_preflight = mygithub12.workspace_write_preflight(
        service, REPO, BRANCH, NEW_HEAD, WORKSPACE_ID, recovered["workspace"]["revision"],
    )
    assert write_preflight["workspace_id"] == WORKSPACE_ID
    assert write_preflight["head_sha"] == NEW_HEAD
    assert write_preflight["tree_sha"] == NEW_TREE
    with pytest.raises(sessions.MyGithub12Error) as exc:
        sessions._require_revision(resumed_session["session_id"], resumed_session["session_revision"])
    assert exc.value.code == "DEVELOPMENT_SESSION_REVISION_MISMATCH"
    with pytest.raises(mygithub12.MyGithub12Error) as exc:
        mygithub12.workspace_write_preflight(service, REPO, BRANCH, NEW_HEAD, WORKSPACE_ID, 5)
    assert exc.value.code == "WORKSPACE_REVISION_MISMATCH"

    with sessions._db() as db:
        active_workspaces = db.execute(
            "SELECT COUNT(*) FROM workspaces WHERE repository=? AND branch=? AND status='active'",
            (REPO, BRANCH),
        ).fetchone()[0]
        active_sessions = db.execute(
            """SELECT COUNT(*) FROM development_sessions WHERE workspace_id=?
               AND status IN ('preparing','active','validating_fast','validating_full','pr_ready','drifted','blocked','closing')""",
            (WORKSPACE_ID,),
        ).fetchone()[0]
    assert active_workspaces == 1
    assert active_sessions == 1
