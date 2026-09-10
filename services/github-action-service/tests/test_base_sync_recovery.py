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
        json.dumps(scope if scope is not None else {"paths": ["allowed/**"]}, separators=(",", ":")),
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


def _pin_bases_to_new(session_id):
    """Model the production partial state: bases already new, Session HEAD still old."""
    with sessions._LOCK, sessions._db() as db:
        db.execute("UPDATE workspaces SET base_commit_sha=? WHERE workspace_id=?", (NEW_BASE, WORKSPACE_ID))
        db.execute(
            "UPDATE development_sessions SET base_commit_sha=? WHERE session_id=?", (NEW_BASE, session_id)
        )


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
    assert result["audit"]["recovery_scope_delta_paths"] == ["allowed/feature.py"]
    assert result["audit"]["excluded_imported_base_paths"] == ["base/region.py"]
    assert result["audit"]["excluded_unchanged_historical_cumulative_paths"] == []
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
    assert exc.value.details["reviewed_overlap_paths"] == []


def test_reviewed_overlap_exact_match_allows_only_overlap_gate_and_audits_paths(tmp_path, monkeypatch):
    service, session, _ = _seed(tmp_path, monkeypatch)
    task_paths = ["allowed/feature.py", "base/region.py"]
    service.repo.set_compare(OLD_BASE, OLD_HEAD, paths=task_paths)
    service.repo.set_compare(NEW_BASE, CURRENT_HEAD, paths=task_paths)

    result = _call(
        service, session, reviewed_overlap_paths_json=json.dumps(["base/region.py"]),
    )

    assert result["control_plane_recovery"] == "CONTROL_PLANE_BASE_SYNC_RECOVERY_SUCCESS"
    assert result["audit"]["actual_overlap_paths"] == ["base/region.py"]
    assert result["audit"]["reviewed_overlap_paths"] == ["base/region.py"]
    assert result["audit"]["old_base_sha"] == OLD_BASE
    assert result["audit"]["new_base_sha"] == NEW_BASE
    assert result["audit"]["old_session_head"] == OLD_HEAD
    assert result["audit"]["current_head"] == CURRENT_HEAD
    assert result["audit"]["current_tree"] == CURRENT_TREE
    assert result["audit"]["old_workspace_revision"] == 5
    assert result["audit"]["new_workspace_revision"] == 6
    assert result["audit"]["new_session_revision"] == result["audit"]["old_session_revision"] + 1


def test_reviewed_overlap_missing_one_actual_path_fails_closed(tmp_path, monkeypatch):
    service, session, _ = _seed(tmp_path, monkeypatch)
    overlap_paths = ["allowed/feature.py", "base/region.py"]
    service.repo.set_compare(OLD_BASE, NEW_BASE, paths=overlap_paths)
    service.repo.set_compare(OLD_BASE, OLD_HEAD, paths=overlap_paths)
    service.repo.set_compare(NEW_BASE, CURRENT_HEAD, paths=overlap_paths)

    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session, reviewed_overlap_paths_json=json.dumps(["base/region.py"]))

    assert exc.value.code == "RECOVERY_BASE_SYNC_OVERLAP"
    assert exc.value.details["actual_overlap_paths"] == sorted(overlap_paths)
    assert exc.value.details["missing_reviewed_overlap_paths"] == ["allowed/feature.py"]
    assert exc.value.details["unexpected_reviewed_overlap_paths"] == []


def test_reviewed_overlap_extra_path_fails_closed(tmp_path, monkeypatch):
    service, session, _ = _seed(tmp_path, monkeypatch)
    task_paths = ["allowed/feature.py", "base/region.py"]
    service.repo.set_compare(OLD_BASE, OLD_HEAD, paths=task_paths)
    service.repo.set_compare(NEW_BASE, CURRENT_HEAD, paths=task_paths)

    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(
            service, session,
            reviewed_overlap_paths_json=json.dumps(["base/region.py", "extra/not-overlap.py"]),
        )

    assert exc.value.code == "RECOVERY_BASE_SYNC_OVERLAP"
    assert exc.value.details["missing_reviewed_overlap_paths"] == []
    assert exc.value.details["unexpected_reviewed_overlap_paths"] == ["extra/not-overlap.py"]


def test_reviewed_overlap_rename_identity_must_match_server_actual_path(tmp_path, monkeypatch):
    service, session, _ = _seed(tmp_path, monkeypatch)
    service.repo.set_compare(
        OLD_BASE,
        NEW_BASE,
        paths=["base/renamed.py"],
        previous={"base/renamed.py": "allowed/feature.py"},
    )

    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session, reviewed_overlap_paths_json=json.dumps(["base/renamed.py"]))

    assert exc.value.code == "RECOVERY_BASE_SYNC_OVERLAP"
    assert exc.value.details["actual_overlap_paths"] == ["allowed/feature.py"]
    assert exc.value.details["missing_reviewed_overlap_paths"] == ["allowed/feature.py"]
    assert exc.value.details["unexpected_reviewed_overlap_paths"] == ["base/renamed.py"]


def test_reviewed_overlap_json_rejects_duplicates_before_recovery(tmp_path, monkeypatch):
    service, session, _ = _seed(tmp_path, monkeypatch)
    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session, reviewed_overlap_paths_json='["base/region.py","base/region.py"]')
    assert exc.value.code == "SEARCH_QUERY_INVALID"


def test_preexisting_historical_task_path_outside_late_phase_scope_is_ignored_when_unchanged(tmp_path, monkeypatch):
    service, session, _ = _seed(tmp_path, monkeypatch)
    historical = ["allowed/feature.py", "outside/historical.py"]
    service.repo.set_compare(OLD_BASE, OLD_HEAD, paths=historical)
    service.repo.set_compare(OLD_HEAD, CURRENT_HEAD, paths=["base/region.py"])
    service.repo.set_compare(NEW_BASE, CURRENT_HEAD, paths=historical)

    result = _call(service, session)

    assert result["verification"]["scope"]["changed_paths"] == []
    assert result["audit"]["recovery_scope_delta_paths"] == []
    assert result["audit"]["excluded_unchanged_historical_cumulative_paths"] == sorted(historical)


def test_exact_824_cumulative_pr_late_phase_workspace_base_sync_recovery_passes(tmp_path, monkeypatch):
    service, session, _ = _seed(tmp_path, monkeypatch)
    old_base = "80eba83ca34d629f051c6f617fbae7fabefbd5a7"
    new_base = "973d3b06340e5dd511b9f62fa199fb79aec6d47e"
    old_head = "cfe73b5cc5e5c6b5295dd41115771280e1eb12d2"
    current_head = "e498519ed064ce9d413c91d1ab1453e1583e65e5"
    old_tree = "4b83b879a2cca31ff3068c60b59e3bfb6c1500b7"
    current_tree = "765027d769b6d6bf06d82d86dbbb86e23a8d57b0"
    cumulative_paths = [
        "h5/lenshub-console-react/region-foundation-component-test.html",
        "h5/lenshub-console-react/sharegroup-component-test.html",
        "h5/lenshub-console-react/src/App.tsx",
        "h5/lenshub-console-react/src/features/appregistration/AppRegistrationWorkbench.tsx",
        "h5/lenshub-console-react/src/features/appregistration/appRegistrationApi.spec.ts",
        "h5/lenshub-console-react/src/features/appregistration/appRegistrationApi.ts",
        "h5/lenshub-console-react/src/features/appregistration/appRegistrationModel.spec.ts",
        "h5/lenshub-console-react/src/features/appregistration/appRegistrationModel.ts",
        "h5/lenshub-console-react/src/features/regionfoundation/RegionFoundationComponentTest.tsx",
        "h5/lenshub-console-react/src/features/regionfoundation/RegionFoundationWorkbench.tsx",
        "h5/lenshub-console-react/src/features/regionfoundation/regionFoundationApi.spec.ts",
        "h5/lenshub-console-react/src/features/regionfoundation/regionFoundationApi.ts",
        "h5/lenshub-console-react/src/features/regionfoundation/regionFoundationModel.spec.ts",
        "h5/lenshub-console-react/src/features/regionfoundation/regionFoundationModel.ts",
        "h5/lenshub-console-react/src/features/sharegroup/ShareGroupComponentTest.tsx",
        "h5/lenshub-console-react/src/features/sharegroup/ShareGroupWorkbench.tsx",
        "h5/lenshub-console-react/src/features/sharegroup/shareGroupApi.ts",
        "h5/lenshub-console-react/src/features/sharegroup/shareGroupModel.spec.ts",
        "h5/lenshub-console-react/src/features/sharegroup/shareGroupModel.ts",
        "h5/lenshub-console-react/src/shared/components/DangerActionConfirm.tsx",
        "h5/lenshub-console-react/src/shared/feedback/ApiErrorPresenter.tsx",
        "h5/lenshub-console-react/vite.config.ts",
        "tests/e2e/specs/12-admin-sharegroup-components.spec.ts",
        "tests/e2e/specs/13-admin-region-foundation-components.spec.ts",
    ]
    late_phase_scope = {
        "paths": [
            "h5/lenshub-console-react/src/App.tsx",
            "h5/lenshub-console-react/src/features/appregistration/AppRegistrationWorkbench.tsx",
            "h5/lenshub-console-react/src/features/appregistration/appRegistrationApi.ts",
            "h5/lenshub-console-react/src/features/appregistration/appRegistrationApi.spec.ts",
            "h5/lenshub-console-react/src/features/appregistration/appRegistrationModel.ts",
            "h5/lenshub-console-react/src/features/appregistration/appRegistrationModel.spec.ts",
        ]
    }
    base_only_paths = [
        "api/openapi/app.yaml",
        "db/migrations/001055_auth_code_resend_count_semantics.sql",
        "docs/contracts/APP_API_Contract_Next.openapi.yaml",
        "internal/modules/account/h5_service.go",
        "internal/modules/account/registration_policy_country_code_test.go",
    ]
    service.repo.trees.update({
        old_base: "7" * 40, new_base: "8" * 40, old_head: old_tree, current_head: current_tree,
    })
    service.repo.comparisons.update({
        (old_base, new_base): service.repo._cfg(old_base, 1, 0, base_only_paths),
        (old_base, old_head): service.repo._cfg(old_base, 8, 0, cumulative_paths),
        (old_head, current_head): service.repo._cfg(old_head, 1, 0, base_only_paths),
        (new_base, current_head): service.repo._cfg(new_base, 9, 0, cumulative_paths),
    })
    service.client.heads[BRANCH] = current_head
    service.client.heads[BASE_BRANCH] = new_base
    with sessions._LOCK, sessions._db() as db:
        db.execute(
            """UPDATE workspaces SET base_commit_sha=?,head_sha=?,tree_sha=?,scope_json=?,
            status='drifted',revision=5,drift_reason='branch_moved_externally',index_commit_sha=NULL,lease_expires_at=0
            WHERE workspace_id=?""",
            (old_base, current_head, current_tree, json.dumps(late_phase_scope, separators=(",", ":")), WORKSPACE_ID),
        )
        db.execute(
            "UPDATE development_sessions SET base_commit_sha=?,head_commit_sha=?,tree_sha=? WHERE session_id=?",
            (old_base, old_head, old_tree, session["session_id"]),
        )
    monkeypatch.setattr(
        recovery.mygithub12,
        "get_index_status",
        lambda service, repository, commit_sha="", ref="": {
            "ok": True, "repository": repository, "commit_sha": commit_sha,
            "tree_sha": current_tree, "status": "ready",
        },
    )

    result = recovery.recover_base_synced_task(
        service,
        **_args(
            session,
            expected_old_base_sha=old_base,
            expected_new_base_sha=new_base,
            expected_old_session_head_sha=old_head,
            expected_current_head_sha=current_head,
            expected_current_tree_sha=current_tree,
            idempotency_key="p0-25d-824-cumulative-late-phase",
        ),
    )

    assert result["control_plane_recovery"] == "CONTROL_PLANE_BASE_SYNC_RECOVERY_SUCCESS"
    assert result["verification"]["scope"]["changed_paths"] == []
    assert result["audit"]["historical_cumulative_task_delta_paths"] == sorted(cumulative_paths)
    assert result["audit"]["external_forward_delta_paths"] == sorted(base_only_paths)
    assert result["audit"]["recovery_scope_delta_paths"] == []
    assert result["audit"]["excluded_imported_base_paths"] == sorted(base_only_paths)
    assert result["audit"]["excluded_unchanged_historical_cumulative_paths"] == sorted(cumulative_paths)
    assert result["workspace"]["scope"] == late_phase_scope


def test_external_advance_adds_outside_scope_task_path_fails_closed(tmp_path, monkeypatch):
    service, session, _ = _seed(tmp_path, monkeypatch)
    task_paths = ["allowed/feature.py", "base/region.py"]
    service.repo.set_compare(OLD_BASE, OLD_HEAD, paths=task_paths)
    service.repo.set_compare(OLD_HEAD, CURRENT_HEAD, paths=["base/region.py", "outside/new.py"])
    service.repo.set_compare(NEW_BASE, CURRENT_HEAD, paths=task_paths + ["outside/new.py"])
    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session, reviewed_overlap_paths_json=json.dumps(["base/region.py"]))
    assert exc.value.code == "RECOVERY_SCOPE_VIOLATION"
    assert exc.value.details["outside_scope_paths"] == ["outside/new.py"]


def test_external_advance_modifies_historical_outside_scope_task_path_fails_closed(tmp_path, monkeypatch):
    service, session, _ = _seed(tmp_path, monkeypatch)
    historical = ["allowed/feature.py", "outside/historical.py"]
    service.repo.set_compare(OLD_BASE, OLD_HEAD, paths=historical)
    service.repo.set_compare(OLD_HEAD, CURRENT_HEAD, paths=["base/region.py", "outside/historical.py"])
    service.repo.set_compare(NEW_BASE, CURRENT_HEAD, paths=historical)
    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session)
    assert exc.value.code == "RECOVERY_SCOPE_VIOLATION"
    assert exc.value.details["outside_scope_paths"] == ["outside/historical.py"]


def test_external_advance_deletes_historical_outside_scope_task_path_fails_closed(tmp_path, monkeypatch):
    service, session, _ = _seed(tmp_path, monkeypatch)
    service.repo.set_compare(OLD_BASE, OLD_HEAD, paths=["allowed/feature.py", "outside/historical.py"])
    service.repo.set_compare(OLD_HEAD, CURRENT_HEAD, paths=["base/region.py", "outside/historical.py"])
    service.repo.set_compare(NEW_BASE, CURRENT_HEAD, paths=["allowed/feature.py"])
    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session)
    assert exc.value.code == "RECOVERY_SCOPE_VIOLATION"
    assert exc.value.details["outside_scope_paths"] == ["outside/historical.py"]


def test_external_advance_rename_checks_previous_and_current_outside_scope_paths(tmp_path, monkeypatch):
    service, session, _ = _seed(tmp_path, monkeypatch)
    service.repo.set_compare(OLD_BASE, OLD_HEAD, paths=["allowed/feature.py", "outside/legacy.py"])
    service.repo.set_compare(
        OLD_HEAD, CURRENT_HEAD,
        paths=["base/region.py", "outside/current.py"],
        previous={"outside/current.py": "outside/legacy.py"},
    )
    service.repo.set_compare(
        NEW_BASE, CURRENT_HEAD,
        paths=["allowed/feature.py", "outside/current.py"],
        previous={"outside/current.py": "outside/legacy.py"},
    )
    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session)
    assert exc.value.code == "RECOVERY_SCOPE_VIOLATION"
    assert exc.value.details["outside_scope_paths"] == ["outside/current.py", "outside/legacy.py"]


def test_current_task_edit_of_imported_base_path_keeps_overlap_fail_closed(tmp_path, monkeypatch):
    service, session, _ = _seed(tmp_path, monkeypatch)
    service.repo.set_compare(NEW_BASE, CURRENT_HEAD, paths=["allowed/feature.py", "base/region.py"])
    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session)
    assert exc.value.code == "RECOVERY_BASE_SYNC_OVERLAP"
    assert exc.value.details["current_base_overlap_paths"] == ["base/region.py"]


def test_base_delta_outside_workspace_scope_is_not_a_scope_violation(tmp_path, monkeypatch):
    service, session, _ = _seed(tmp_path, monkeypatch)
    service.repo.set_compare(OLD_BASE, NEW_BASE, paths=["outside/base-owned.py"])
    service.repo.set_compare(OLD_HEAD, CURRENT_HEAD, paths=["outside/base-owned.py", "allowed/feature.py"])
    result = _call(service, session)
    assert result["verification"]["scope"]["changed_paths"] == ["allowed/feature.py"]


def test_t8_unexplained_task_path_change_fails_closed(tmp_path, monkeypatch):
    service, session, _ = _seed(tmp_path, monkeypatch)
    service.repo.set_compare(NEW_BASE, CURRENT_HEAD, paths=["allowed/different.py"])
    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session)
    assert exc.value.code == "RECOVERY_TASK_DIFF_MISMATCH"


def test_combined_base_sync_and_forward_task_advance_allows_expanded_paths(tmp_path, monkeypatch):
    service, session, _ = _seed(tmp_path, monkeypatch)
    expanded = ["allowed/feature.py", "allowed/new_test.py", "allowed/design.md"]
    service.repo.set_compare(
        OLD_HEAD,
        CURRENT_HEAD,
        paths=["base/region.py", *expanded],
    )
    service.repo.set_compare(NEW_BASE, CURRENT_HEAD, paths=expanded)

    result = _call(service, session)

    assert result["control_plane_recovery"] == "CONTROL_PLANE_BASE_SYNC_RECOVERY_SUCCESS"
    assert result["audit"]["old_task_delta_paths"] == ["allowed/feature.py"]
    assert result["audit"]["new_task_delta_paths"] == sorted(expanded)
    assert result["audit"]["task_path_changes"] == ["allowed/design.md", "allowed/new_test.py"]
    assert result["audit"]["unexplained_task_path_changes"] == []


def test_forward_commit_may_modify_old_task_path_and_add_another_path(tmp_path, monkeypatch):
    service, session, _ = _seed(tmp_path, monkeypatch)
    service.repo.set_compare(
        OLD_HEAD,
        CURRENT_HEAD,
        paths=["base/region.py", "allowed/feature.py", "allowed/regression.py"],
    )
    service.repo.set_compare(
        NEW_BASE,
        CURRENT_HEAD,
        paths=["allowed/feature.py", "allowed/regression.py"],
    )

    result = _call(service, session)

    assert result["audit"]["forward_task_delta_paths"] == [
        "allowed/feature.py",
        "allowed/regression.py",
        "base/region.py",
    ]
    assert result["audit"]["task_path_changes"] == ["allowed/regression.py"]


def test_legacy_empty_scope_recovers_without_becoming_unrestricted_writer(tmp_path, monkeypatch):
    service, session, _ = _seed(tmp_path, monkeypatch)
    with sessions._LOCK, sessions._db() as db:
        db.execute("UPDATE workspaces SET scope_json='{}' WHERE workspace_id=?", (WORKSPACE_ID,))

    result = _call(service, session)

    assert result["workspace"]["status"] == "active"
    assert result["workspace"]["scope"] == {}
    assert result["scope_declaration_required"] is True
    assert result["writer_ready"] is False
    assert result["verification"]["scope"]["enforcement"] == "actual_changed_paths_overlap_only"


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


@pytest.mark.parametrize(
    "overrides",
    [
        {"repository": "other/repo"},
        {"branch": "ai/other-branch"},
        {"expected_base_branch": "release"},
    ],
)
def test_repository_branch_and_base_branch_identity_must_match(tmp_path, monkeypatch, overrides):
    service, session, _ = _seed(tmp_path, monkeypatch)
    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session, **overrides)
    assert exc.value.code == "RECOVERY_IDENTITY_MISMATCH"


def test_workspace_session_binding_mismatch_fails_closed(tmp_path, monkeypatch):
    service, session, _ = _seed(tmp_path, monkeypatch)
    with sessions._LOCK, sessions._db() as db:
        db.execute(
            "UPDATE development_sessions SET branch='ai/other-branch' WHERE session_id=?",
            (session["session_id"],),
        )
    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session)
    assert exc.value.code == "RECOVERY_IDENTITY_MISMATCH"


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


def test_idempotency_key_reuse_with_changed_payload_fails_closed(tmp_path, monkeypatch):
    service, session, _ = _seed(tmp_path, monkeypatch)
    _call(service, session)
    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session, lease_seconds=7100)
    assert exc.value.code == "IDEMPOTENCY_CONFLICT"


def test_old_revisions_cannot_start_second_recovery_after_success(tmp_path, monkeypatch):
    service, session, _ = _seed(tmp_path, monkeypatch)
    _call(service, session)
    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session, idempotency_key="second-recovery")
    assert exc.value.code == "RECOVERY_DRIFT_REASON_UNSUPPORTED"


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
            "items": [{"workspace_id": "ws_merged_a", "branch": "ai/merged-a", "level": "high", "evidence": [{"kind": "changed_paths", "items": ["allowed/feature.py"]}]}],
        },
    )
    result = _call(service, session)
    ignored = result["verification"]["ownership"]["ignored_merged_workspaces"]
    assert len(ignored) == 1
    assert ignored[0]["merged_evidence"]["session_status"] == "merged"
    assert ignored[0]["merged_evidence"]["workspace_head_sha"] == MERGED_HEAD


def test_t16_active_overlapping_writer_is_never_ignored_without_terminal_merged_evidence(tmp_path, monkeypatch):
    service, session, _ = _seed(tmp_path, monkeypatch)
    task_paths = ["allowed/feature.py", "base/region.py"]
    service.repo.set_compare(OLD_BASE, OLD_HEAD, paths=task_paths)
    service.repo.set_compare(NEW_BASE, CURRENT_HEAD, paths=task_paths)
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
            "items": [{"workspace_id": "ws_active_a", "branch": "ai/merged-a", "level": "high", "evidence": [{"kind": "changed_paths", "items": ["allowed/feature.py"]}]}],
        },
    )
    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session, reviewed_overlap_paths_json=json.dumps(["base/region.py"]))
    assert exc.value.code == "RECOVERY_WORKSPACE_OVERLAP"


def test_imported_base_path_does_not_create_false_current_task_overlap(tmp_path, monkeypatch):
    service, session, _ = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(
        recovery.mygithub12,
        "workspace_overlap",
        lambda *args, **kwargs: {
            "ok": True,
            "workspace_id": WORKSPACE_ID,
            "items": [{
                "workspace_id": "ws_other",
                "branch": "ai/other",
                "level": "high",
                "evidence": [{"kind": "changed_paths", "items": ["base/region.py"]}],
            }],
        },
    )

    result = _call(service, session)

    overlap = result["verification"]["ownership"]["overlap"]
    assert overlap["current_task_delta_paths"] == ["allowed/feature.py"]
    assert overlap["items"][0]["level"] == "none"
    assert overlap["items"][0]["evidence"] == []


def test_already_pinned_new_base_recovers_partial_control_plane_state(tmp_path, monkeypatch):
    service, session, _ = _seed(tmp_path, monkeypatch)
    _pin_bases_to_new(session["session_id"])

    result = _call(service, session)

    assert result["control_plane_recovery"] == "CONTROL_PLANE_BASE_SYNC_RECOVERY_SUCCESS"
    assert result["verification"]["pinned_base_state"] == recovery.PINNED_BASE_ALREADY_NEW
    assert result["before"]["pinned_base_state"] == recovery.PINNED_BASE_ALREADY_NEW
    assert result["before"]["workspace_base_sha"] == NEW_BASE
    assert result["before"]["session_base_sha"] == NEW_BASE
    assert result["workspace"]["base_commit_sha"] == NEW_BASE
    assert result["development_session"]["base_commit_sha"] == NEW_BASE
    assert result["workspace"]["head_sha"] == result["development_session"]["head_commit_sha"] == CURRENT_HEAD
    assert result["workspace"]["tree_sha"] == result["development_session"]["tree_sha"] == CURRENT_TREE
    assert result["workspace"]["drift_reason"] is None
    assert result["audit"]["pinned_base_state"] == recovery.PINNED_BASE_ALREADY_NEW


def test_already_pinned_new_base_scope_and_overlap_ignore_large_imported_upstream_delta(tmp_path, monkeypatch):
    service, session, _ = _seed(tmp_path, monkeypatch)
    _pin_bases_to_new(session["session_id"])
    imported = [f"upstream/{index:03d}.py" for index in range(95)]
    task_paths = [f"allowed/task-{index:02d}.py" for index in range(14)]
    service.repo.set_compare(OLD_BASE, NEW_BASE, paths=imported)
    service.repo.set_compare(OLD_BASE, OLD_HEAD, paths=task_paths)
    service.repo.set_compare(OLD_HEAD, CURRENT_HEAD, paths=[*imported, *task_paths])
    service.repo.set_compare(NEW_BASE, CURRENT_HEAD, paths=task_paths)
    monkeypatch.setattr(
        recovery.mygithub12,
        "workspace_overlap",
        lambda *args, **kwargs: {
            "ok": True,
            "workspace_id": WORKSPACE_ID,
            "items": [{
                "workspace_id": "ws_imported_overlap",
                "branch": "ai/other",
                "level": "high",
                "evidence": [{"kind": "changed_paths", "items": [imported[0]]}],
            }],
        },
    )

    result = _call(service, session)

    expected_task_paths = sorted(task_paths)
    assert result["verification"]["scope"]["changed_paths"] == expected_task_paths
    overlap = result["verification"]["ownership"]["overlap"]
    assert overlap["current_task_delta_paths"] == expected_task_paths
    assert overlap["items"][0]["level"] == "none"
    assert overlap["items"][0]["evidence"] == []


def test_already_pinned_new_base_real_task_overlap_still_blocks(tmp_path, monkeypatch):
    service, session, _ = _seed(tmp_path, monkeypatch)
    _pin_bases_to_new(session["session_id"])
    monkeypatch.setattr(
        recovery.mygithub12,
        "workspace_overlap",
        lambda *args, **kwargs: {
            "ok": True,
            "workspace_id": WORKSPACE_ID,
            "items": [{
                "workspace_id": "ws_task_overlap",
                "branch": "ai/other",
                "level": "high",
                "evidence": [{"kind": "changed_paths", "items": ["allowed/feature.py"]}],
            }],
        },
    )
    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session)
    assert exc.value.code == "RECOVERY_WORKSPACE_OVERLAP"


def test_already_pinned_new_base_live_base_moves_again_fails_closed(tmp_path, monkeypatch):
    service, session, _ = _seed(tmp_path, monkeypatch)
    _pin_bases_to_new(session["session_id"])
    service.client.heads[BASE_BRANCH] = OTHER_HEAD
    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session)
    assert exc.value.code == "RECOVERY_BASE_CHANGED"


def test_mixed_partial_base_state_is_not_accepted(tmp_path, monkeypatch):
    service, session, _ = _seed(tmp_path, monkeypatch)
    with sessions._LOCK, sessions._db() as db:
        db.execute("UPDATE workspaces SET base_commit_sha=? WHERE workspace_id=?", (NEW_BASE, WORKSPACE_ID))
    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session)
    assert exc.value.code == "RECOVERY_BASE_CHANGED"


def test_already_pinned_new_base_idempotent_replay_and_conflict(tmp_path, monkeypatch):
    service, session, _ = _seed(tmp_path, monkeypatch)
    _pin_bases_to_new(session["session_id"])
    first = _call(service, session)
    second = _call(service, session)
    assert second["replayed"] is True
    assert second["after"] == first["after"]
    with pytest.raises(recovery.MyGithub12Error) as exc:
        _call(service, session, lease_seconds=7100)
    assert exc.value.code == "IDEMPOTENCY_CONFLICT"


def _exact_production_partial_resume(tmp_path, monkeypatch):
    service, seeded_session, _ = _seed(tmp_path, monkeypatch)
    stacked_branch = "ai/issue-186-p0-14-share-invite-global-locator-20260904"
    historical_old_base = "20e8e5a5a411c55e830db33daca5cf3ab6f97db9"
    live_new_base = "43ba158333f06e30210dca596f3b7eae204d149a"
    old_session_head = "23aab1b9f80296d0e88c552ddbdac54c56939bc9"
    integrated_head = "249f4dc68200e83b4fd73a8bbe43608beaac5d42"
    old_base_tree = "7" * 40
    new_base_tree = "8" * 40
    old_head_tree = "9" * 40
    integrated_tree = "0" * 40
    service.repo.trees.update({
        historical_old_base: old_base_tree,
        live_new_base: new_base_tree,
        old_session_head: old_head_tree,
        integrated_head: integrated_tree,
    })
    service.repo.comparisons.update({
        (historical_old_base, live_new_base): service.repo._cfg(
            historical_old_base, 1, 0, ["base/region.py"],
        ),
        (historical_old_base, old_session_head): service.repo._cfg(
            historical_old_base, 1, 0, ["allowed/feature.py"],
        ),
        (old_session_head, integrated_head): service.repo._cfg(
            old_session_head, 2, 0, ["base/region.py", "allowed/feature.py"],
        ),
        (live_new_base, integrated_head): service.repo._cfg(
            live_new_base, 1, 0, ["allowed/feature.py"],
        ),
    })
    service.client.heads[BRANCH] = integrated_head
    service.client.heads[stacked_branch] = live_new_base
    metadata = dict(seeded_session.get("metadata") or {})
    metadata["prepared_base_identity"] = {
        "repository": REPO, "commit_sha": historical_old_base, "tree_sha": old_base_tree,
    }
    with sessions._LOCK, sessions._db() as db:
        db.execute(
            """UPDATE workspaces SET base_branch=?,base_commit_sha=?,head_sha=?,tree_sha=?,
            status='drifted',drift_reason='branch_moved_externally' WHERE workspace_id=?""",
            (stacked_branch, live_new_base, integrated_head, integrated_tree, WORKSPACE_ID),
        )
        db.execute(
            """UPDATE development_sessions SET base_branch=?,base_commit_sha=?,head_commit_sha=?,
            tree_sha=?,metadata_json=? WHERE session_id=?""",
            (
                stacked_branch, live_new_base, old_session_head, old_head_tree,
                json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
                seeded_session["session_id"],
            ),
        )
    workspace = mygithub12.get_workspace(service, WORKSPACE_ID)
    session = sessions.get_session(seeded_session["session_id"])
    pr = {
        "pull_number": 823, "head_branch": BRANCH, "head_sha": integrated_head,
        "base_branch": stacked_branch, "state": "open", "draft": True, "merged": False,
    }
    monkeypatch.setattr(
        resume, "_repository_policy",
        lambda repository: {"ok": True, "repository": repository, "policy": {"github": True, "private_ci": True}},
    )
    monkeypatch.setattr(resume, "_resolve_pr", lambda repository, pull_number, branch: pr)
    monkeypatch.setattr(
        resume, "_current_main",
        lambda service, repository: {
            "branch": "main", "repository": repository,
            "commit_sha": OTHER_HEAD, "tree_sha": OTHER_TREE,
        },
    )
    monkeypatch.setattr(
        resume, "_resolve_branch",
        lambda service, repository, branch, base_branch: {
            "ok": True, "repository": repository, "branch": branch,
            "base_branch": base_branch, "commit_sha": integrated_head, "tree_sha": integrated_tree,
        },
    )
    monkeypatch.setattr(resume, "_select_workspace", lambda *args: (workspace, [workspace]))
    monkeypatch.setattr(
        resume, "find_sessions_for_workspace",
        lambda workspace_id, include_terminal=False, limit=20: [session],
    )

    def resolve_identity(_service, repository, commit_sha="", ref=""):
        sha = live_new_base if ref == stacked_branch else (commit_sha or OTHER_HEAD)
        return {
            "repository": repository, "commit_sha": sha,
            "tree_sha": service.repo.trees.get(sha, OTHER_TREE),
        }

    monkeypatch.setattr(resume.mygithub12, "resolve_identity", resolve_identity)
    monkeypatch.setattr(
        resume.mygithub12, "get_index_status",
        lambda _service, repository, commit_sha="", ref="": {
            "ok": True, "repository": repository, "commit_sha": commit_sha,
            "tree_sha": service.repo.trees.get(commit_sha, integrated_tree), "status": "ready",
        },
    )
    monkeypatch.setattr(
        resume.mygithub12, "workspace_overlap",
        lambda *args, **kwargs: {"ok": True, "workspace_id": WORKSPACE_ID, "items": []},
    )
    monkeypatch.setattr(resume, "db_list_jobs", lambda **kwargs: [])
    monkeypatch.setattr(
        resume.github_utils, "get_github_pull_request_merge_readiness",
        lambda *args, **kwargs: {"ok": True, "ready": False},
    )
    result = resume.resume_task(
        service, REPO, pull_number=823, recover_stale_session=False,
    )
    return service, result, {
        "stacked_branch": stacked_branch,
        "historical_old_base": historical_old_base,
        "live_new_base": live_new_base,
        "old_session_head": old_session_head,
        "integrated_head": integrated_head,
        "integrated_tree": integrated_tree,
    }


def _recovery_args_from_resume_plan(plan, idempotency_key):
    keys = (
        "repository", "branch", "workspace_id", "development_session_id",
        "expected_workspace_revision", "expected_session_revision",
        "expected_old_base_sha", "expected_new_base_sha", "expected_base_branch",
        "expected_old_session_head_sha", "expected_current_head_sha", "expected_current_tree_sha",
    )
    return {**{key: plan[key] for key in keys}, "idempotency_key": idempotency_key}


def test_exact_823_partial_state_resume_plan_is_directly_consumable_by_base_sync_recovery(tmp_path, monkeypatch):
    service, resumed, ids = _exact_production_partial_resume(tmp_path, monkeypatch)
    plan = resumed["recovery"]

    assert resumed["current_main"]["commit_sha"] != ids["live_new_base"]
    assert resumed["recovery_base"]["branch"] == ids["stacked_branch"]
    assert plan["action"] == "recover_base_synced_development_task"
    assert plan["expected_old_base_sha"] == ids["historical_old_base"]
    assert plan["expected_new_base_sha"] == ids["live_new_base"]
    assert plan["expected_old_session_head_sha"] == ids["old_session_head"]
    assert plan["expected_current_head_sha"] == ids["integrated_head"]
    assert plan["preflight"]["verified"] is True

    recovered = recovery.recover_base_synced_task(
        service, **_recovery_args_from_resume_plan(plan, "823-resume-to-recovery-e2e"),
    )

    assert recovered["control_plane_recovery"] == "CONTROL_PLANE_BASE_SYNC_RECOVERY_SUCCESS"
    assert recovered["verification"]["pinned_base_state"] == recovery.PINNED_BASE_ALREADY_NEW
    assert recovered["workspace"]["head_sha"] == ids["integrated_head"]
    assert recovered["development_session"]["head_commit_sha"] == ids["integrated_head"]


def test_exact_823_resume_plan_fails_with_recovery_base_changed_if_stacked_base_advances_before_apply(tmp_path, monkeypatch):
    service, resumed, _ = _exact_production_partial_resume(tmp_path, monkeypatch)
    plan = resumed["recovery"]
    service.client.heads[plan["expected_base_branch"]] = OTHER_HEAD

    with pytest.raises(recovery.MyGithub12Error) as exc:
        recovery.recover_base_synced_task(
            service, **_recovery_args_from_resume_plan(plan, "823-base-moved-before-apply"),
        )

    assert exc.value.code == "RECOVERY_BASE_CHANGED"


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


def test_resume_requires_refresh_before_base_sync_recovery_when_workspace_identity_is_stale(tmp_path, monkeypatch):
    service, session, _ = _seed(tmp_path, monkeypatch)
    workspace = mygithub12.get_workspace(service, WORKSPACE_ID)
    workspace["head_sha"] = OLD_HEAD
    workspace["tree_sha"] = OLD_TREE

    plan = resume._workspace_recovery_plan(
        workspace,
        service=service,
        session=session,
        current_main={"branch": BASE_BRANCH, "commit_sha": NEW_BASE},
        branch_state={"commit_sha": CURRENT_HEAD, "tree_sha": CURRENT_TREE},
    )

    assert plan["action"] == "refresh_development_workspace"
    assert plan["next_action"] == "recover_base_synced_development_task"
    assert plan["recovery_sequence"] == [
        "refresh_development_workspace",
        "recover_base_synced_development_task",
    ]
    assert plan["expected_workspace_revision"] == workspace["revision"]
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
