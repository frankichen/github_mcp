import asyncio
import json
from types import SimpleNamespace

import pytest

from app import ci_database
from app import development_drift_recovery as recovery
from app import development_orchestrator as dx
from app import development_resume as resume
from app import development_session_store as sessions
from app import mygithub10
from app import mygithub12
from app import mygithub12_dx_mcp as dx_mcp
from app.mcp_response import StructuredFastMCP


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


@pytest.fixture(autouse=True)
def _close_ci_database_connection():
    yield
    connection = getattr(ci_database._local, "db", None)
    if connection is not None:
        connection.close()
    ci_database._local.db = None


class FakeRepo:
    def __init__(self):
        self.trees = {OLD_HEAD: OLD_TREE, NEW_HEAD: NEW_TREE, OTHER_HEAD: OTHER_TREE, BASE_SHA: "4" * 40}
        self.merge_base = OLD_HEAD
        self.ahead_by = 1
        self.behind_by = 0
        self.changed_paths = ["allowed/feature.py"]
        self.previous_filenames = {}

    def get_commit(self, sha):
        return SimpleNamespace(tree=SimpleNamespace(sha=self.trees[sha]))

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


def _workspace_row():
    now = sessions._now()
    return (
        WORKSPACE_ID, REPO, BRANCH, BASE, BASE_SHA, OLD_HEAD, OLD_TREE,
        "active", 4, "chatgpt", now + 7200, OLD_HEAD,
        json.dumps({"paths": ["allowed/**"]}, separators=(",", ":")),
        None, None, now, now,
    )


def _seed_active_stale(tmp_path, monkeypatch, *, session_status="active"):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "recovery.db"))
    monkeypatch.setenv("CI_DB_PATH", str(tmp_path / "ci.db"))
    monkeypatch.setenv("IDEMPOTENCY_DB_PATH", str(tmp_path / "idempotency.db"))
    monkeypatch.setenv("MYGITHUB12_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    current_ci_db = getattr(ci_database._local, "db", None)
    if current_ci_db is not None:
        current_ci_db.close()
    ci_database._local.db = None
    monkeypatch.setattr(ci_database, "DB_PATH", str(tmp_path / "ci.db"))
    ci_database.init_db()
    service = FakeService()
    sessions.init_session_db()
    with sessions._LOCK, sessions._db() as db:
        db.execute("INSERT INTO workspaces VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", _workspace_row())
    workspace = mygithub12.get_workspace(service, WORKSPACE_ID)
    session = sessions.create_session(workspace, idempotency_key="seed-session")
    sessions.record_validation(
        session["session_id"],
        session["session_revision"],
        "full",
        OLD_HEAD,
        OLD_TREE,
        request_id="old-head-request",
        job_id="old-head-job",
        status="passed",
        attestation_id="old-head-attestation",
        evidence={"failure_pack": {"resource_uri": "old-head-failure-pack"}},
        finished=True,
    )
    with sessions._LOCK, sessions._db() as db:
        db.execute(
            """UPDATE workspaces SET head_sha=?,tree_sha=?,status='active',revision=5,
               drift_reason=NULL,lease_expires_at=? WHERE workspace_id=?""",
            (NEW_HEAD, NEW_TREE, sessions._now() + 7200, WORKSPACE_ID),
        )
        db.execute(
            """UPDATE development_sessions SET status=?,last_fast_ci_job_id='old-fast',
               last_full_ci_job_id='old-full',last_attestation_id='old-att',
               last_failure_resource_uri='old-failure',index_commit_sha=? WHERE session_id=?""",
            (session_status, OLD_HEAD, session["session_id"]),
        )
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
        lambda *args, **kwargs: pytest.fail("exact current-HEAD Index must be reused"),
    )
    return service, sessions.get_session(session["session_id"])


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
        "idempotency_key": "active-stale-session-recovery",
        "lease_seconds": 7200,
    }
    values.update(overrides)
    return values


def _call(service, session, **overrides):
    return recovery.recover_active_workspace_stale_session(service, **_args(session, **overrides))


def _db_state(session_id):
    with sessions._db() as db:
        workspace = dict(db.execute("SELECT * FROM workspaces WHERE workspace_id=?", (WORKSPACE_ID,)).fetchone())
        session = dict(db.execute("SELECT * FROM development_sessions WHERE session_id=?", (session_id,)).fetchone())
        events = [dict(row) for row in db.execute(
            "SELECT * FROM development_session_events WHERE session_id=? ORDER BY id", (session_id,),
        ).fetchall()]
    return workspace, session, events


@pytest.mark.parametrize("status", ["active", "validating_fast", "validating_full", "blocked", "validation_failed"])
def test_active_workspace_adopts_stale_session_from_supported_open_states(tmp_path, monkeypatch, status):
    service, session = _seed_active_stale(tmp_path, monkeypatch, session_status=status)
    refs_before = dict(service.client.heads)

    result = _call(service, session)

    workspace = result["workspace"]
    adopted = result["development_session"]
    assert result["control_plane_recovery"] == "CONTROL_PLANE_RECOVERY_SUCCESS"
    assert result["recovery_kind"] == "active_workspace_stale_session"
    assert result["writer_ready"] is True and result["index_required"] is False
    assert result["git_ref_changed"] is False
    assert workspace["status"] == adopted["status"] == "active"
    assert workspace["drift_reason"] is None
    assert workspace["revision"] == 5
    assert workspace["head_sha"] == adopted["head_commit_sha"] == NEW_HEAD
    assert workspace["tree_sha"] == adopted["tree_sha"] == NEW_TREE
    assert adopted["workspace_revision"] == workspace["revision"]
    assert adopted["session_revision"] == session["session_revision"] + 1
    assert adopted["last_fast_ci_job_id"] is None
    assert adopted["last_full_ci_job_id"] is None
    assert adopted["last_attestation_id"] is None
    assert adopted["last_failure_resource_uri"] is None
    assert adopted["index_commit_sha"] is None
    assert sessions.validation_correlations(
        adopted["session_id"], adopted["session_revision"], "full", NEW_HEAD, NEW_TREE,
    ) == []
    assert sessions.validation_correlations(
        adopted["session_id"], adopted["session_revision"], "full", OLD_HEAD, OLD_TREE,
    )[0]["attestation_id"] == "old-head-attestation"
    assert service.client.heads == refs_before

    audit = result["audit"]
    assert audit["recovery_kind"] == "active_workspace_stale_session"
    assert audit["before"]["session_head"] == OLD_HEAD
    assert audit["before"]["session_tree"] == OLD_TREE
    assert audit["workspace"]["workspace_head"] == NEW_HEAD
    assert audit["github"]["current_head"] == NEW_HEAD
    assert audit["github"]["current_tree"] == NEW_TREE
    assert audit["ancestry"]["merge_base"] == OLD_HEAD
    assert audit["ancestry"]["ahead_by"] == 1
    assert audit["ancestry"]["behind_by"] == 0
    assert audit["scope"]["declared_paths"] == ["allowed/**"]
    assert audit["scope"]["changed_paths"] == ["allowed/feature.py"]
    assert audit["scope"]["verified"] is True
    assert audit["after"]["session_head"] == NEW_HEAD
    assert audit["after"]["session_workspace_revision"] == workspace["revision"]
    assert audit["stale_evidence_cleared"] is True
    _, stored, events = _db_state(session["session_id"])
    assert stored["metadata_json"]
    event = next(item for item in events if item["event_type"] == "active_workspace_stale_session_recovery")
    assert json.loads(event["data_json"])["recovery_kind"] == "active_workspace_stale_session"


@pytest.mark.parametrize(
    ("mutation", "error_code"),
    [
        ("not_descendant", "RECOVERY_ANCESTRY_MISMATCH"),
        ("diverged", "RECOVERY_ANCESTRY_MISMATCH"),
        ("branch_rewritten", "RECOVERY_HEAD_MISMATCH"),
        ("base_changed", "RECOVERY_BASE_CHANGED"),
        ("workspace_revision", "WORKSPACE_REVISION_MISMATCH"),
        ("session_revision", "DEVELOPMENT_SESSION_REVISION_MISMATCH"),
        ("overlap", "RECOVERY_WORKSPACE_OVERLAP"),
        ("overlap_unavailable", "RECOVERY_WORKSPACE_OVERLAP_UNAVAILABLE"),
        ("closed", "DEVELOPMENT_SESSION_CLOSED"),
    ],
)
def test_active_workspace_stale_session_guards_fail_closed_without_mutation(
    tmp_path, monkeypatch, mutation, error_code,
):
    service, session = _seed_active_stale(tmp_path, monkeypatch)
    overrides = {}
    if mutation == "not_descendant":
        service.repo.merge_base = OTHER_HEAD
    elif mutation == "diverged":
        service.repo.merge_base = OTHER_HEAD
        service.repo.ahead_by = 1
        service.repo.behind_by = 1
    elif mutation == "branch_rewritten":
        service.client.heads[BRANCH] = OTHER_HEAD
    elif mutation == "base_changed":
        service.client.heads[BASE] = OTHER_HEAD
    elif mutation == "workspace_revision":
        overrides["expected_workspace_revision"] = 4
    elif mutation == "session_revision":
        overrides["expected_session_revision"] = session["session_revision"] + 1
    elif mutation == "overlap":
        monkeypatch.setattr(
            recovery.mygithub12, "workspace_overlap",
            lambda *_args, **_kwargs: {"ok": True, "items": [{"workspace_id": "ws_other", "level": "high"}]},
        )
    elif mutation == "overlap_unavailable":
        monkeypatch.setattr(
            recovery.mygithub12, "workspace_overlap",
            lambda *_args, **_kwargs: {"ok": True, "items": [], "comparison_verified": False},
        )
    elif mutation == "closed":
        with sessions._LOCK, sessions._db() as db:
            db.execute("UPDATE development_sessions SET status='closed',closed_at=? WHERE session_id=?", (sessions._now(), session["session_id"]))

    before_workspace, before_session, _ = _db_state(session["session_id"])
    with pytest.raises(mygithub12.MyGithub12Error) as exc:
        _call(service, session, **overrides)

    assert exc.value.code == error_code
    after_workspace, after_session, _ = _db_state(session["session_id"])
    assert after_workspace["revision"] == before_workspace["revision"] == 5
    assert after_workspace["status"] == "active" and after_workspace["drift_reason"] is None
    assert after_session["session_revision"] == before_session["session_revision"]
    assert after_session["head_commit_sha"] == OLD_HEAD


def test_unreviewed_scope_expansion_fails_and_exact_review_is_persisted(tmp_path, monkeypatch):
    service, session = _seed_active_stale(tmp_path, monkeypatch)
    service.repo.changed_paths = ["allowed/feature.py", "docs/new.md"]
    with pytest.raises(mygithub12.MyGithub12Error) as exc:
        _call(service, session)
    assert exc.value.code == "RECOVERY_SCOPE_VIOLATION"
    assert exc.value.details["required_scope_expansion_paths"] == ["docs/new.md"]

    result = _call(
        service,
        session,
        reviewed_scope_expansion_paths_json='["docs/new.md"]',
        idempotency_key="reviewed-active-stale-session-recovery",
    )
    assert result["workspace"]["revision"] == 6
    assert result["workspace"]["scope"]["paths"] == ["allowed/**", "docs/new.md"]
    assert result["development_session"]["workspace_revision"] == 6
    assert result["audit"]["scope"]["reviewed_expansion_paths"] == ["docs/new.md"]


def test_idempotent_active_workspace_recovery_does_not_bump_revisions_twice(tmp_path, monkeypatch):
    service, session = _seed_active_stale(tmp_path, monkeypatch)
    first = _call(service, session)
    second = _call(service, session)
    assert second["replayed"] is True
    assert second["after"] == first["after"]
    assert second["workspace"]["revision"] == first["workspace"]["revision"]
    assert second["development_session"]["session_revision"] == first["development_session"]["session_revision"]


def _patch_resume(service, monkeypatch):
    monkeypatch.setattr(resume, "_repository_policy", lambda repository: {
        "ok": True, "repository": repository, "policy": {"github": True, "private_ci": True},
    })
    monkeypatch.setattr(resume, "_discover_pr_by_branch", lambda *args, **kwargs: None)
    monkeypatch.setattr(resume, "_current_main", lambda _service, repository: {
        "branch": BASE, "repository": repository, "commit_sha": BASE_SHA, "tree_sha": "4" * 40,
    })
    monkeypatch.setattr(resume, "_resolve_branch", lambda _service, repository, branch, base_branch: {
        "ok": True, "repository": repository, "branch": branch, "base_branch": base_branch,
        "commit_sha": NEW_HEAD, "tree_sha": NEW_TREE,
    })
    monkeypatch.setattr(resume, "_select_workspace", lambda _service, repository, branch: (
        mygithub12.get_workspace(service, WORKSPACE_ID),
        [mygithub12.get_workspace(service, WORKSPACE_ID)],
    ))
    monkeypatch.setattr(resume.mygithub12, "workspace_overlap", lambda _service, workspace_id: {
        "ok": True, "workspace_id": workspace_id, "items": [],
    })
    monkeypatch.setattr(resume.mygithub12, "get_index_status", lambda _service, repository, commit_sha="", ref="": {
        "ok": True, "repository": repository, "commit_sha": commit_sha,
        "tree_sha": NEW_TREE, "status": "ready",
    })


def test_resume_auto_recovers_with_exact_cas_then_reports_exact_head(tmp_path, monkeypatch):
    service, session = _seed_active_stale(tmp_path, monkeypatch, session_status="validating_full")
    _patch_resume(service, monkeypatch)

    resumed = resume.resume_task(
        service,
        REPO,
        branch=BRANCH,
        expected_workspace_revision=5,
        expected_session_revision=session["session_revision"],
        idempotency_key="resume-active-stale-session",
    )

    assert resumed["exact_head"] is True
    assert resumed["recovery_required"] is False
    assert resumed["recovery_performed"] is True
    assert resumed["recovery_kind"] == "active_workspace_stale_session"
    assert resumed["recovery_classification"] == "ACTIVE_WORKSPACE_STALE_SESSION"
    assert resumed["recovery_blocker"] is None
    assert resumed["development_session"]["status"] == "active"
    assert resumed["development_session"]["head_commit_sha"] == NEW_HEAD

    repeated = resume.resume_task(
        service,
        REPO,
        branch=BRANCH,
        expected_workspace_revision=5,
        expected_session_revision=resumed["development_session"]["session_revision"],
        idempotency_key="resume-active-stale-session",
    )
    assert repeated["exact_head"] is True
    assert repeated["recovery_required"] is False
    assert repeated["recovery_performed"] is False
    assert repeated["development_session"]["session_revision"] == resumed["development_session"]["session_revision"]


def test_resume_classifies_active_stale_session_when_cas_is_missing(tmp_path, monkeypatch):
    service, session = _seed_active_stale(tmp_path, monkeypatch)
    _patch_resume(service, monkeypatch)

    result = resume.resume_task(service, REPO, branch=BRANCH, recover_stale_session=True)

    assert result["recovery_required"] is True
    assert result["recovery_classification"] == "ACTIVE_WORKSPACE_STALE_SESSION"
    assert result["recovery_blocker"] == "RECOVERY_CAS_REQUIRED"
    assert result["recovery"]["required_inputs"] == ["expected_workspace_revision", "expected_session_revision"]
    assert result["development_session"]["head_commit_sha"] == OLD_HEAD


def test_recovered_writer_accepts_normal_change_set_dry_run(tmp_path, monkeypatch):
    service, session = _seed_active_stale(tmp_path, monkeypatch)
    result = _call(service, session)
    adopted = result["development_session"]
    write_context = dx.resolve_generated_write_context(service, REPO, BRANCH, NEW_HEAD)
    checked_session, checked_workspace = dx.require_session_workspace(
        service,
        adopted["session_id"],
        adopted["session_revision"],
        result["workspace"]["revision"],
        NEW_HEAD,
    )
    assert write_context["session"]["head_commit_sha"] == NEW_HEAD

    monkeypatch.setattr(dx.mygithub10, "apply_patch", lambda _service, repository, branch, head, *_args: {
        "ok": True, "dry_run": True, "repository": repository, "branch": branch,
        "expected_head_sha": head, "changed_files": [{"path": "allowed/feature.py"}],
    })
    parsed = dx.parse_change_set(json.dumps({
        "schema_version": 1,
        "mode": "patch",
        "expected_blob_shas": {},
        "patch": "diff --git a/allowed/feature.py b/allowed/feature.py\n",
    }))
    dry_run = dx.execute_change_set(
        service,
        checked_session,
        checked_workspace,
        parsed,
        NEW_HEAD,
        result["workspace"]["revision"],
        "dry run after stale Session recovery",
        True,
        "dry-run-after-recovery",
        {},
    )
    assert dry_run["ok"] is True and dry_run["dry_run"] is True
    assert dry_run["expected_head_sha"] == NEW_HEAD


def test_recovered_session_can_start_fresh_exact_head_validation(tmp_path, monkeypatch):
    service, session = _seed_active_stale(tmp_path, monkeypatch)
    recovered = _call(service, session)
    adopted = recovered["development_session"]

    monkeypatch.setattr(dx, "maybe_auto_renew_session_workspace", lambda _service, session_id, *_args, **_kwargs: {
        "session": sessions.get_session(session_id),
        "workspace": mygithub12.get_workspace(service, WORKSPACE_ID),
        "renewed": False,
    })
    monkeypatch.setattr(dx, "validation_preflight", lambda _service, current, mode, base_sha: {
        "profile": "repo-auto-check",
        "base_sha": base_sha,
        "selection": {"complete": True, "changed_paths": ["allowed/feature.py"]},
    })
    monkeypatch.setattr(dx, "start_validation_request", lambda _service, current, mode, base_sha, *_args: ({
        "request_id": "request-after-recovery",
        "worker_job_id": None,
        "repository": REPO,
        "branch": BRANCH,
        "commit_sha": current["head_commit_sha"],
        "tree_sha": current["tree_sha"],
        "profile": "repo-auto-check",
        "phase": "queued",
        "status": "queued",
        "revision": 1,
    }, {"complete": True, "changed_paths": ["allowed/feature.py"]}))
    monkeypatch.setattr(dx, "validation_observation", lambda session_id, revision, mode, request, selection, include_failure_pack: {
        "terminal": False,
        "merge_eligible": False,
        "attestation": None,
        "failure_pack": None,
        "request": {"request_id": request["request_id"], "phase": "queued", "status": "queued"},
        "job": {"job_id": None, "status": "queued", "profile": "repo-auto-check", "commit_sha": NEW_HEAD},
    })
    mcp = StructuredFastMCP("active-stale-session-validation-test")

    async def github_call(function, *args, **kwargs):
        return function(*args, **kwargs)

    async def finalize_write(*_args, **_kwargs):
        return {}

    dx_mcp.register_dx_tools(mcp, github_call, service, finalize_write)
    async def call_validation():
        return await mcp.call_tool("validate_development_task", {
            "development_session_id": adopted["session_id"],
            "expected_session_revision": adopted["session_revision"],
            "mode": "full",
            "base_sha": BASE_SHA,
            "idempotency_key": "validate-adopted-head",
        })

    called = asyncio.run(call_validation())
    payload = called[1] if isinstance(called, tuple) else called.structured_content
    if payload is None:
        payload = json.loads(called.content[0].text)

    assert payload["ok"] is True
    assert payload["job"]["commit_sha"] == NEW_HEAD
    assert payload["job"]["status"] == "queued"
    assert payload["request"]["request_id"] == "request-after-recovery"
    assert payload["development_session"]["status"] == "validating_full"
    assert payload.get("recovery_required") is not True
    rows = sessions.validation_correlations(
        adopted["session_id"],
        payload["development_session"]["session_revision"],
        "full",
        NEW_HEAD,
        NEW_TREE,
    )
    assert rows and rows[-1]["request_id"] == "request-after-recovery"
