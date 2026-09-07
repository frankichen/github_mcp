import importlib

import pytest

from app import ci_database
from app import development_convergence_store as store
from app import development_resume as resume


HEAD = "a" * 40
TREE = "b" * 40
BASE = "c" * 40
OLD_HEAD = "d" * 40
OLD_TREE = "e" * 40


class FakeService:
    pass


def _workspace(*, head=HEAD, tree=TREE, revision=2):
    return {
        "workspace_id": "ws_dev009",
        "repository": "owner/repo",
        "branch": "ai/dev009",
        "base_branch": "main",
        "base_commit_sha": BASE,
        "head_sha": head,
        "tree_sha": tree,
        "status": "active",
        "revision": revision,
        "lease_expires_at": 9999999999.0,
        "lease_valid": True,
        "scope": {"paths": ["app.py"]},
        "drift_reason": None,
        "index_commit_sha": head,
        "pr_number": None,
    }


def _session(*, head=HEAD, tree=TREE, revision=4):
    return {
        "session_id": "dev_session_009",
        "workspace_id": "ws_dev009",
        "repository": "owner/repo",
        "branch": "ai/dev009",
        "base_branch": "main",
        "base_commit_sha": BASE,
        "status": "active",
        "session_revision": revision,
        "workspace_revision": 2,
        "head_commit_sha": head,
        "tree_sha": tree,
        "lease_expires_at": 9999999999.0,
        "pull_number": None,
        "last_fast_ci_job_id": None,
        "last_full_ci_job_id": None,
        "last_attestation_id": None,
        "last_failure_resource_uri": None,
    }


@pytest.fixture(autouse=True)
def isolated_databases(monkeypatch, tmp_path):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "mygithub12.db"))
    monkeypatch.setattr(ci_database, "DB_PATH", str(tmp_path / "ci.db"))
    connection = getattr(ci_database._local, "db", None)
    if connection is not None:
        connection.close()
    ci_database._local.db = None
    store.init_convergence_db()
    ci_database.init_db()


@pytest.fixture
def resume_context(monkeypatch):
    workspace = _workspace()
    session = _session()
    monkeypatch.setattr(
        resume,
        "_repository_policy",
        lambda repository: {
            "ok": True,
            "repository": repository,
            "policy": {"github": True, "private_ci": True},
        },
    )
    monkeypatch.setattr(
        resume,
        "_current_main",
        lambda _service, repository: {
            "branch": "main",
            "repository": repository,
            "commit_sha": BASE,
            "tree_sha": "f" * 40,
        },
    )
    monkeypatch.setattr(
        resume,
        "_resolve_branch",
        lambda _service, repository, branch, base_branch: {
            "ok": True,
            "repository": repository,
            "branch": branch,
            "base_branch": base_branch,
            "commit_sha": HEAD,
            "tree_sha": TREE,
        },
    )
    monkeypatch.setattr(resume, "_discover_pr_by_branch", lambda *_args: None)
    monkeypatch.setattr(
        resume,
        "_select_workspace",
        lambda *_args: (workspace, [workspace]),
    )
    monkeypatch.setattr(
        resume,
        "find_sessions_for_workspace",
        lambda *_args, **_kwargs: [session],
    )
    monkeypatch.setattr(
        resume.mygithub12,
        "get_index_status",
        lambda _service, repository, commit_sha="", ref="": {
            "ok": True,
            "repository": repository,
            "commit_sha": commit_sha,
            "tree_sha": TREE,
            "status": "ready",
        },
    )
    monkeypatch.setattr(
        resume.mygithub12,
        "workspace_overlap",
        lambda _service, workspace_id: {"ok": True, "workspace_id": workspace_id, "items": []},
    )
    monkeypatch.setattr(resume, "db_list_jobs", lambda **_kwargs: [])
    monkeypatch.setattr(
        resume.github_utils,
        "get_github_pull_request_merge_readiness",
        lambda *_args, **_kwargs: {"ok": True, "ready": False},
    )
    return workspace, session


def _create_convergence(*, mode="full", head=HEAD, tree=TREE, key=None):
    return store.create_or_get_convergence(
        repository="owner/repo",
        branch="ai/dev009",
        development_session_id="dev_session_009",
        workspace_id="ws_dev009",
        session_revision=4,
        workspace_revision=2,
        head_sha=head,
        tree_sha=tree,
        base_branch="main",
        base_sha=BASE,
        mode=mode,
        idempotency_key=key or f"dev009-{mode}-{head[:4]}",
    )


def _fake_request(*, request_id, mode="full", phase="accepted", status="accepted", job_id=None, revision=0):
    return {
        "request_id": request_id,
        "repository": "owner/repo",
        "branch": "ai/dev009",
        "commit_sha": HEAD,
        "tree_sha": TREE,
        "profile": "repo-auto-check" if mode == "full" else "repo-fast-check",
        "status": status,
        "phase": phase,
        "revision": revision,
        "worker_job_id": job_id,
        "terminal": phase == "terminal",
    }


def _fake_job(job_id, status):
    return {
        "job_id": job_id,
        "repository": "owner/repo",
        "branch": "ai/dev009",
        "commit_sha": HEAD,
        "profile": "repo-auto-check",
        "status": status,
        "summary": {"git_tree_sha": TREE},
        "exit_code": None,
    }


def test_resume_finds_exact_active_convergence_without_creating_one(
    monkeypatch, resume_context
):
    convergence = _create_convergence()
    monkeypatch.setattr(
        resume.convergence_store,
        "create_or_get_convergence",
        lambda **_kwargs: pytest.fail("resume must never create a convergence"),
    )

    result = resume.resume_task(FakeService(), "owner/repo", branch="ai/dev009")

    assert result["convergence"]["convergence_id"] == convergence["convergence_id"]
    assert result["convergence_identity"]["convergence_id"] == convergence["convergence_id"]
    assert result["convergence_phase"] == "accepted"
    assert result["convergence_status"] == "accepted"
    assert result["convergence_revision"] == 0
    assert result["pending_convergence"]["resume_classification"] == "pending"
    assert result["convergence_evidence"]["pending"][0]["convergence_id"] == convergence["convergence_id"]
    assert "run_full_ci" not in result["next_allowed_actions"]


def test_resume_reads_ci_request_accepted_and_preparing_without_restarting(
    monkeypatch, resume_context
):
    convergence = _create_convergence()
    request = _fake_request(request_id="ci_req_accepted")
    monkeypatch.setattr(
        resume.ci_request_store,
        "get_ci_request",
        lambda request_id: request if request_id == request["request_id"] else None,
    )
    current = store.bind_ci_request(convergence["convergence_id"], convergence["revision"], request["request_id"])
    current = store.transition_convergence(
        current["convergence_id"], current["revision"], "ci_requested", status="pending"
    )

    result = resume.resume_task(FakeService(), "owner/repo", branch="ai/dev009")

    assert result["convergence"]["convergence_id"] == current["convergence_id"]
    assert result["convergence"]["phase"] == "ci_requested"
    assert result["convergence"]["status"] == "pending"
    assert result["convergence"]["revision"] == current["revision"]
    assert result["convergence"]["ci_request"]["request_id"] == request["request_id"]
    assert result["convergence"]["ci_request_phase"] == "accepted"
    assert result["convergence"]["ci_request_status"] == "accepted"
    assert result["convergence"]["ci_request_revision"] == 0

    preparing = {**request, "phase": "preparing", "status": "preparing", "revision": 1}
    monkeypatch.setattr(resume.ci_request_store, "get_ci_request", lambda _request_id: preparing)
    resumed = resume.resume_task(FakeService(), "owner/repo", branch="ai/dev009")
    assert resumed["convergence"]["ci_request_phase"] == "preparing"
    assert resumed["convergence"]["ci_request_status"] == "preparing"
    assert resumed["convergence"]["ci_request_revision"] == 1


@pytest.mark.parametrize("worker_status", ["queued", "running"])
def test_resume_reads_worker_queue_and_running_state(
    monkeypatch, resume_context, worker_status
):
    convergence = _create_convergence()
    request = _fake_request(
        request_id="ci_req_worker",
        phase="queued" if worker_status == "queued" else "running",
        status=worker_status,
        job_id="worker_009",
        revision=2,
    )
    monkeypatch.setattr(resume.ci_request_store, "get_ci_request", lambda _request_id: request)
    monkeypatch.setattr(resume, "db_get_job", lambda job_id: _fake_job(job_id, worker_status))
    current = store.bind_ci_request(convergence["convergence_id"], convergence["revision"], request["request_id"])
    store.bind_ci_job(
        current["convergence_id"],
        current["revision"],
        ci_request_id=request["request_id"],
        ci_job_id=request["worker_job_id"],
    )

    result = resume.resume_task(FakeService(), "owner/repo", branch="ai/dev009")

    assert result["pending_work"]["ci_requests"][0]["status"] == worker_status
    assert result["pending_work"]["ci_requests"][0]["phase"] == request["phase"]
    assert result["pending_work"]["worker_jobs"][0]["job_id"] == "worker_009"
    assert result["pending_work"]["worker_jobs"][0]["status"] == worker_status


def test_resume_separates_current_active_from_terminal_historical_evidence(
    monkeypatch, resume_context
):
    old = _create_convergence(mode="full", key="dev009-old")
    old = store.transition_convergence(old["convergence_id"], old["revision"], "passed", status="passed")
    active = _create_convergence(mode="full", key="dev009-current")
    result = resume.resume_task(FakeService(), "owner/repo", branch="ai/dev009")

    assert result["convergence"]["convergence_id"] == active["convergence_id"]
    assert result["pending_convergence"]["convergence_id"] == active["convergence_id"]
    assert result["historical_convergences"][0]["convergence_id"] == old["convergence_id"]
    assert result["historical_convergences"][0]["resume_classification"] == "historical"
    assert result["historical_convergences"][0]["terminal"] is True
    assert result["historical_evidence"]["convergences"][0]["convergence_id"] == old["convergence_id"]


def test_resume_keeps_terminal_exact_head_convergence_historical_only(
    monkeypatch, resume_context
):
    terminal = _create_convergence(mode="full", key="dev009-terminal-current-head")
    terminal = store.transition_convergence(
        terminal["convergence_id"], terminal["revision"], "passed", status="passed"
    )

    result = resume.resume_task(FakeService(), "owner/repo", branch="ai/dev009")

    assert result["convergence"] is None
    assert result["pending_convergence"] is None
    assert result["convergence_evidence"]["live"]["exact_head"] is False
    assert result["convergence_evidence"]["live"]["convergence"] is None
    assert result["historical_convergences"][0]["convergence_id"] == terminal["convergence_id"]
    assert result["historical_convergences"][0]["resume_classification"] == "historical"
    assert result["historical_convergences"][0]["terminal"] is True


def test_resume_does_not_promote_old_head_convergence_to_current(
    monkeypatch, resume_context
):
    workspace, session = resume_context
    old = _create_convergence(head=OLD_HEAD, tree=OLD_TREE, key="dev009-old-head")
    old = store.transition_convergence(old["convergence_id"], old["revision"], "passed", status="passed")
    workspace.update({"head_sha": OLD_HEAD, "tree_sha": OLD_TREE})
    session.update({"head_commit_sha": OLD_HEAD, "tree_sha": OLD_TREE})

    result = resume.resume_task(FakeService(), "owner/repo", branch="ai/dev009")

    assert result["convergence"] is None
    assert result["convergence_identity"] is None
    assert result["convergence_evidence"]["live"]["exact_head"] is False
    assert result["historical_convergences"][0]["convergence_id"] == old["convergence_id"]
    assert result["historical_convergences"][0]["head_sha"] == OLD_HEAD
    assert result["historical_convergences"][0]["terminal"] is True


def test_resume_branch_and_pr_windows_reuse_one_convergence_and_store_reopen(
    monkeypatch, resume_context
):
    convergence = _create_convergence()
    pr = {
        "pull_number": 9,
        "head_branch": "ai/dev009",
        "head_sha": HEAD,
        "base_branch": "main",
        "state": "open",
        "draft": True,
    }
    monkeypatch.setattr(resume, "_discover_pr_by_branch", lambda *_args: pr)
    monkeypatch.setattr(
        resume,
        "_resolve_pr",
        lambda _repository, pull_number, _branch: pr if pull_number else None,
    )
    branch_result = resume.resume_task(FakeService(), "owner/repo", branch="ai/dev009")

    importlib.reload(store)
    pr_result = resume.resume_task(FakeService(), "owner/repo", pull_number=9)

    assert branch_result["convergence"]["convergence_id"] == convergence["convergence_id"]
    assert pr_result["convergence"]["convergence_id"] == convergence["convergence_id"]
    assert pr_result["convergence_revision"] == convergence["revision"]
    assert pr_result["pending_convergences"]


def test_expired_resource_reference_is_evidence_only_and_never_restarts_ci(
    monkeypatch, resume_context
):
    convergence = _create_convergence()
    resource_uri = "mygithub12://response/expired-dev009-resource"
    monkeypatch.setattr(
        resume.convergence_store,
        "create_or_get_convergence",
        lambda **_kwargs: pytest.fail("expired Resource must not rerun convergence"),
    )
    current = store.record_analysis_state(
        convergence["convergence_id"],
        convergence["revision"],
        stage="change_context",
        state="ready",
        resource_uri=resource_uri,
        resource_identity="expired-resource-digest",
    )

    result = resume.resume_task(FakeService(), "owner/repo", branch="ai/dev009")

    assert result["convergence"]["convergence_id"] == current["convergence_id"]
    assert result["convergence"]["analysis"]["change_context"]["resource_uri"] == resource_uri
    assert result["pending_work"]["convergences"][0]["convergence_id"] == current["convergence_id"]
