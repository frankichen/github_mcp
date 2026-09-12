import pytest

from app import development_resume as resume
from app import development_session_store as sessions

SHA_A = "a" * 40
SHA_B = "b" * 40
TREE_A = "1" * 40
TREE_B = "2" * 40


def _workspace(status="active", revision=2, lease=9999999999.0):
    return {
        "workspace_id": "ws_resume",
        "repository": "owner/repo",
        "branch": "ai/resume",
        "base_branch": "main",
        "base_commit_sha": SHA_A,
        "head_sha": SHA_A,
        "tree_sha": TREE_A,
        "status": status,
        "revision": revision,
        "lease_expires_at": lease,
        "lease_valid": status == "active",
        "scope": {"paths": ["x.py"]},
        "drift_reason": None,
        "index_commit_sha": SHA_A,
        "pr_number": None,
    }


class FakeClient:
    def get_repo(self, repository):
        class Repo:
            default_branch = "main"
        return Repo()


class FakeService:
    client = FakeClient()


def test_find_sessions_for_workspace_returns_active_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "resume.db"))
    ws = _workspace()
    created = sessions.create_session(ws, idempotency_key="resume-session")

    found = resume.find_sessions_for_workspace(ws["workspace_id"])

    assert [item["session_id"] for item in found] == [created["session_id"]]
    assert found[0]["workspace_revision"] == ws["revision"]


def test_resume_task_branch_only_allows_continue_when_workspace_session_and_index_ready(monkeypatch):
    service = FakeService()
    ws = _workspace()
    session = {"session_id": "dev_resume", "status": "active", "workspace_revision": 2, "head_commit_sha": SHA_A, "tree_sha": TREE_A, "lease_expires_at": ws["lease_expires_at"], "pull_number": None}
    monkeypatch.setattr(resume, "_repository_policy", lambda repository: {"ok": True, "repository": repository, "policy": {"github": True}})
    monkeypatch.setattr(resume.github_utils, "get_github_branch", lambda repository, branch, base_branch="": {"ok": True, "repository": repository, "branch": branch, "commit_sha": SHA_A, "base_branch": base_branch, "ahead_by": 0, "behind_by": 0})
    monkeypatch.setattr(resume.mygithub12, "resolve_identity", lambda service, repository, commit_sha="", ref="": {"repository": repository, "commit_sha": commit_sha or SHA_A, "tree_sha": TREE_A})
    monkeypatch.setattr(resume, "_discover_pr_by_branch", lambda repository, branch, base_branch: None)
    monkeypatch.setattr(resume.mygithub12, "list_workspaces", lambda service, repository="", status="", branch="", owner="", limit=50, offset=0: {"ok": True, "items": [ws]})
    monkeypatch.setattr(resume, "find_sessions_for_workspace", lambda workspace_id, include_terminal=False, limit=20: [session])
    monkeypatch.setattr(resume.mygithub12, "get_index_status", lambda service, repository, commit_sha="", ref="": {"ok": True, "repository": repository, "commit_sha": commit_sha, "tree_sha": TREE_A, "status": "ready"})
    monkeypatch.setattr(resume.mygithub12, "workspace_overlap", lambda service, workspace_id: {"ok": True, "workspace_id": workspace_id, "items": []})
    monkeypatch.setattr(resume, "db_list_jobs", lambda **kwargs: [])

    result = resume.resume_task(service, "owner/repo", branch="ai/resume")

    assert result["blockers"] == []
    assert "continue_write" in result["next_allowed_actions"]
    assert result["workspace"]["workspace_id"] == "ws_resume"
    assert result["development_session"]["session_id"] == "dev_resume"


def test_resume_task_does_not_continue_for_expired_workspace(monkeypatch):
    service = FakeService()
    ws = _workspace(status="expired")
    monkeypatch.setattr(resume, "_repository_policy", lambda repository: {"ok": True, "repository": repository, "policy": {"github": True}})
    monkeypatch.setattr(resume.github_utils, "get_github_branch", lambda repository, branch, base_branch="": {"ok": True, "repository": repository, "branch": branch, "commit_sha": SHA_A})
    monkeypatch.setattr(resume.mygithub12, "resolve_identity", lambda service, repository, commit_sha="", ref="": {"repository": repository, "commit_sha": commit_sha or SHA_A, "tree_sha": TREE_A})
    monkeypatch.setattr(resume, "_discover_pr_by_branch", lambda repository, branch, base_branch: None)
    monkeypatch.setattr(resume.mygithub12, "list_workspaces", lambda service, repository="", status="", branch="", owner="", limit=50, offset=0: {"ok": True, "items": [ws]})
    monkeypatch.setattr(resume, "find_sessions_for_workspace", lambda workspace_id, include_terminal=False, limit=20: [])
    monkeypatch.setattr(resume.mygithub12, "get_index_status", lambda service, repository, commit_sha="", ref="": {"ok": True, "status": "ready", "commit_sha": commit_sha, "tree_sha": TREE_A})
    monkeypatch.setattr(resume.mygithub12, "workspace_overlap", lambda service, workspace_id: {"ok": True, "items": []})
    monkeypatch.setattr(resume, "db_list_jobs", lambda **kwargs: [])

    result = resume.resume_task(service, "owner/repo", branch="ai/resume")

    assert "continue_write" not in result["next_allowed_actions"]
    assert result["recovery"]["action"] == "resume_development_workspace"
    assert "WORKSPACE_EXPIRED" in result["blockers"]


def test_resume_task_rejects_branch_pr_mismatch(monkeypatch):
    monkeypatch.setattr(resume, "_repository_policy", lambda repository: {"ok": True, "repository": repository, "policy": {"github": True}})
    monkeypatch.setattr(resume.github_utils, "get_github_pull_request", lambda repository, pull_number: {"ok": True, "pull_number": pull_number, "head_branch": "ai/other", "head_sha": SHA_A})
    with pytest.raises(resume.MyGithub12Error) as exc:
        resume.resume_task(FakeService(), "owner/repo", branch="ai/resume", pull_number=12)
    assert exc.value.code == "DEVELOPMENT_RESUME_INPUT_MISMATCH"


def _ready_session(*, head=SHA_A, tree=TREE_A, workspace_revision=2, lease=9999999999.0):
    return {
        "session_id": "dev_resume", "status": "active", "session_revision": 4,
        "repository": "owner/repo", "branch": "ai/resume", "base_branch": "main",
        "base_commit_sha": SHA_A,
        "workspace_revision": workspace_revision, "head_commit_sha": head, "tree_sha": tree,
        "lease_expires_at": lease, "pull_number": None, "last_fast_ci_job_id": None,
        "last_full_ci_job_id": None, "last_attestation_id": None, "last_failure_resource_uri": None,
    }


def _stub_resume_context(monkeypatch, *, ws=None, session=None, pr=None, branch_head=SHA_A, branch_tree=TREE_A, readiness=None):
    ws = ws or _workspace()
    session = session or _ready_session(workspace_revision=ws["revision"], lease=ws["lease_expires_at"])
    monkeypatch.setattr(resume, "_repository_policy", lambda repository: {"ok": True, "repository": repository, "policy": {"github": True, "private_ci": True}})
    monkeypatch.setattr(resume, "_resolve_pr", lambda repository, pull_number, branch: pr if pull_number else None)
    monkeypatch.setattr(resume, "_discover_pr_by_branch", lambda repository, branch, base_branch: pr)
    monkeypatch.setattr(resume, "_current_main", lambda service, repository: {"branch": "main", "repository": repository, "commit_sha": SHA_B, "tree_sha": "2" * 40})
    monkeypatch.setattr(resume, "_resolve_branch", lambda service, repository, branch, base_branch: {"ok": True, "repository": repository, "branch": branch, "base_branch": base_branch, "commit_sha": branch_head, "tree_sha": branch_tree})
    monkeypatch.setattr(resume, "_select_workspace", lambda service, repository, branch: (ws, [ws]))
    monkeypatch.setattr(resume, "find_sessions_for_workspace", lambda workspace_id, include_terminal=False, limit=20: [session] if session else [])
    monkeypatch.setattr(resume.mygithub12, "get_index_status", lambda service, repository, commit_sha="", ref="": {"ok": True, "repository": repository, "commit_sha": commit_sha, "tree_sha": branch_tree, "status": "ready"})
    monkeypatch.setattr(resume.mygithub12, "workspace_overlap", lambda service, workspace_id: {"ok": True, "workspace_id": workspace_id, "items": []})
    monkeypatch.setattr(resume, "db_list_jobs", lambda **kwargs: [])
    monkeypatch.setattr(resume.github_utils, "get_github_pull_request_merge_readiness", lambda *args, **kwargs: readiness or {"ok": True, "ready": False})


def _validation_job(*, mode="fast", status="passed", job_id="job-exact", tree=TREE_A):
    return {
        "job_id": job_id,
        "repository": "owner/repo",
        "branch": "ai/resume",
        "commit_sha": SHA_A,
        "base_sha": SHA_A,
        "profile": "repo-fast-check" if mode == "fast" else "repo-auto-check",
        "status": status,
        "exit_code": 0 if status == "passed" else 1,
        "superseded_by_job_id": None,
        "summary": {"git_tree_sha": tree} if tree else {},
    }


def _stub_transient_recovery(monkeypatch, *, mode="fast", status="passed", correlations=1):
    ws = _workspace()
    phase = "validating_fast" if mode == "fast" else "validating_full"
    session = {**_ready_session(), "workspace_id": ws["workspace_id"], "base_commit_sha": ws["base_commit_sha"], "status": phase}
    _stub_resume_context(monkeypatch, ws=ws, session=session)
    job = _validation_job(mode=mode, status=status)
    request_ids = ["ci_req_exact" if i == 0 else f"ci_req_other_{i}" for i in range(correlations)]
    rows = [
        {
            "request_id": request_ids[i],
            "request_id_source": "legacy_evidence",
            "job_id": None,
            "session_revision": session["session_revision"],
            "tree_sha": session["tree_sha"],
            "evidence": {"selection": {"complete": True, "changed_paths": ["x.py"]}, "request_id": request_ids[i]},
        }
        for i in range(correlations)
    ]
    request = {
        "request_id": "ci_req_exact", "repository": session["repository"], "branch": session["branch"],
        "commit_sha": session["head_commit_sha"], "tree_sha": session["tree_sha"],
        "profile": job["profile"], "worker_job_id": job["job_id"], "phase": "queued", "status": "queued",
    }
    payload = {
        "development_session_id": session["session_id"], "workspace_id": ws["workspace_id"],
        "repository": session["repository"], "branch": session["branch"],
        "commit_sha": session["head_commit_sha"], "tree_sha": session["tree_sha"],
        "profile": job["profile"], "mode": mode, "base_sha": session["base_commit_sha"],
    }
    generation = {
        "generation_revision": session["session_revision"],
        "generation_workspace_revision": session["workspace_revision"],
        "current_session_revision": session["session_revision"],
        "current_workspace_revision": session["workspace_revision"],
        "source": "validation_started_event",
        "maintenance_events": [],
    }
    monkeypatch.setattr(resume.sessions, "validation_generation_context", lambda *args, **kwargs: generation)
    monkeypatch.setattr(resume.sessions, "validation_correlations", lambda *args, **kwargs: rows)
    monkeypatch.setattr(resume.ci_request_store, "get_ci_request", lambda request_id: request if request_id == "ci_req_exact" else None)
    monkeypatch.setattr(resume.ci_request_store, "get_ci_request_by_worker_job_id", lambda job_id: request if job_id == job["job_id"] else None)
    monkeypatch.setattr(resume.ci_request_store, "get_ci_request_payload", lambda request_id: payload if request_id == "ci_req_exact" else {})
    monkeypatch.setattr(resume, "db_get_job", lambda job_id: job if job_id == job["job_id"] else None)
    monkeypatch.setattr(
        resume.sessions, "bind_validation_request_worker",
        lambda *args, **kwargs: {"request_id": "ci_req_exact", "job_id": job["job_id"], "logical_duplicate_count": 0},
    )
    if mode == "full":
        attestation = {
            "attestation_id": "att-exact", "repository": session["repository"],
            "tested_commit_sha": session["head_commit_sha"], "tested_tree_sha": session["tree_sha"],
            "base_sha": session["base_commit_sha"], "private_ci_job_id": job["job_id"], "profile": job["profile"],
        }
        monkeypatch.setattr(
            resume.attestation_registry, "find_reusable_attestation_for_job",
            lambda job_id: {"ok": True, "reusable": True, "attestation": attestation},
        )
    monkeypatch.setattr(resume.dx, "start_validation_request", lambda *args, **kwargs: pytest.fail("recovery must not start a second CI Request"))
    monkeypatch.setattr(resume.dx, "start_validation_job", lambda *args, **kwargs: pytest.fail("recovery must not start a second Worker"))
    monkeypatch.setattr(resume.dx, "create_or_get_job", lambda *args, **kwargs: pytest.fail("recovery must not create or reuse a new execution"))
    return ws, session, job


def _stub_request_only_transient_recovery(monkeypatch, *, mode="fast", status="preflight_failed"):
    ws, session, _ = _stub_transient_recovery(monkeypatch, mode=mode, status="queued")
    rows = [{
        "request_id": "ci_req_exact", "request_id_source": "column", "job_id": None,
        "session_revision": session["session_revision"], "tree_sha": session["tree_sha"],
        "evidence": {"selection": {"complete": False}, "request_id": "ci_req_exact"},
    }]
    request = {
        "request_id": "ci_req_exact", "repository": session["repository"], "branch": session["branch"],
        "commit_sha": session["head_commit_sha"], "tree_sha": session["tree_sha"],
        "profile": "repo-fast-check" if mode == "fast" else "repo-auto-check",
        "worker_job_id": None, "phase": "terminal", "status": status,
        "preflight_error_code": "CI_PREFLIGHT_TEST" if status == "preflight_failed" else None,
    }
    payload = {
        "development_session_id": session["session_id"], "workspace_id": ws["workspace_id"],
        "repository": session["repository"], "branch": session["branch"],
        "commit_sha": session["head_commit_sha"], "tree_sha": session["tree_sha"],
        "profile": request["profile"], "mode": mode, "base_sha": session["base_commit_sha"],
    }
    monkeypatch.setattr(resume.sessions, "validation_correlations", lambda *args, **kwargs: rows)
    monkeypatch.setattr(resume.ci_request_store, "get_ci_request", lambda request_id: request if request_id == "ci_req_exact" else None)
    monkeypatch.setattr(resume.ci_request_store, "get_ci_request_payload", lambda request_id: payload if request_id == "ci_req_exact" else {})
    monkeypatch.setattr(resume, "db_get_job", lambda *args: pytest.fail("request-only terminal must not read a Worker job"))
    return ws, session, request


@pytest.mark.parametrize("status", ["preflight_failed", "cancelled", "superseded", "internal_error"])
def test_resume_reconciles_proven_request_only_terminal(monkeypatch, status):
    _, session, request = _stub_request_only_transient_recovery(monkeypatch, status=status)
    captured = {}
    monkeypatch.setattr(
        resume.sessions, "bind_validation_request_worker",
        lambda *args, **kwargs: captured.update(kwargs) or {
            "request_id": request["request_id"], "job_id": None,
            "request_only_terminal": True, "request_terminal_status": status,
        },
    )
    monkeypatch.setattr(
        resume.sessions, "transition",
        lambda *args, **kwargs: {**session, "status": "active", "session_revision": session["session_revision"] + 1},
    )

    result = resume.resume_task(FakeService(), "owner/repo", branch="ai/resume")

    assert result["development_session"]["status"] == "active"
    assert result["recovery"]["transient"]["reconciled"] is True
    assert result["recovery"]["transient"]["job"] is None
    assert result["recovery"]["transient"]["validation_result"]["status"] == status
    assert captured["request_terminal_status"] == status
    assert "continue_write" in result["next_allowed_actions"]


@pytest.mark.parametrize("status", ["passed", "failed", "timed_out", "worker_lost"])
def test_resume_rejects_worker_required_terminal_without_worker(monkeypatch, status):
    _, session, _ = _stub_request_only_transient_recovery(monkeypatch, status=status)
    monkeypatch.setattr(resume.sessions, "transition", lambda *args, **kwargs: pytest.fail("unproven terminal must not change Session"))
    result = resume.resume_task(FakeService(), "owner/repo", branch="ai/resume")
    assert result["development_session"] == session
    assert result["recovery"]["transient"]["reason"] == "validation_request_worker_missing_terminal"
    assert "DEVELOPMENT_SESSION_RECOVERY_REQUIRED" in result["blockers"]


def test_resume_reconciles_request_only_terminal_before_drift_recovery(monkeypatch):
    ws, session, request = _stub_request_only_transient_recovery(monkeypatch, status="preflight_failed")
    ws.update({
        "status": "drifted",
        "revision": int(session["workspace_revision"]) + 1,
        "head_sha": SHA_B,
        "tree_sha": TREE_B,
        "drift_reason": "branch_moved_externally",
        "lease_valid": False,
    })
    _stub_resume_context(monkeypatch, ws=ws, session=session, branch_head=SHA_B, branch_tree=TREE_B)
    monkeypatch.setattr(
        resume, "_current_main",
        lambda service, repository: {"branch": "main", "repository": repository, "commit_sha": SHA_A, "tree_sha": TREE_A},
    )
    captured = {}
    monkeypatch.setattr(
        resume.sessions, "bind_validation_request_worker",
        lambda *args, **kwargs: captured.update(kwargs) or {
            "request_id": request["request_id"], "job_id": None,
            "request_only_terminal": True, "request_terminal_status": "preflight_failed",
            "workspace_drift_reconciliation": True,
        },
    )
    monkeypatch.setattr(
        resume.sessions, "transition",
        lambda *args, **kwargs: {**session, "status": "active", "session_revision": session["session_revision"] + 1},
    )

    result = resume.resume_task(FakeService(), "owner/repo", branch="ai/resume")

    assert captured["allow_branch_drift"] is True
    assert result["development_session"]["session_id"] == session["session_id"]
    assert result["development_session"]["status"] == "active"
    assert result["workspace"]["workspace_id"] == ws["workspace_id"]
    assert result["workspace"]["status"] == "drifted"
    assert result["recovery"]["transient"]["workspace_drift_pending_recovery"] is True
    assert result["next_allowed_actions"][0] == "recover_drifted_development_task"
    assert "continue_write" not in result["next_allowed_actions"]


def test_resume_reconciles_exact_terminal_fast_validation(monkeypatch):
    _, session, job = _stub_transient_recovery(monkeypatch)
    result_payload = {"terminal": True, "merge_eligible": False, "attestation": None, "failure_pack": None}
    monkeypatch.setattr(resume.dx, "validation_result", lambda *args, **kwargs: result_payload)
    recovered = {**session, "status": "active", "session_revision": 5, "last_fast_ci_job_id": job["job_id"]}
    monkeypatch.setattr(resume.sessions, "transition", lambda *args, **kwargs: recovered)

    result = resume.resume_task(FakeService(), "owner/repo", branch="ai/resume", expected_session_revision=4)

    assert result["recovery"]["transient"]["reconciled"] is True
    assert result["development_session"]["status"] == "active"
    assert result["development_session"]["last_fast_ci_job_id"] == "job-exact"
    assert "continue_write" in result["next_allowed_actions"]


def test_resume_keeps_running_validation_fail_closed(monkeypatch):
    _, session, _ = _stub_transient_recovery(monkeypatch, status="running")
    monkeypatch.setattr(resume.dx, "validation_result", lambda *args, **kwargs: pytest.fail("running CI must not be observed as terminal"))
    monkeypatch.setattr(resume.sessions, "transition", lambda *args, **kwargs: pytest.fail("running CI must not change Session state"))

    result = resume.resume_task(FakeService(), "owner/repo", branch="ai/resume")

    assert result["development_session"] == session
    assert result["recovery"]["transient"]["validation_in_progress"] is True
    assert "DEVELOPMENT_SESSION_VALIDATION_IN_PROGRESS" in result["blockers"]
    assert result["next_allowed_actions"] == ["get_private_ci_job", "resume_development_task"]
    assert "continue_write" not in result["next_allowed_actions"]


def test_resume_reconciles_terminal_failed_without_forging_pass(monkeypatch):
    _, session, job = _stub_transient_recovery(monkeypatch, status="failed")
    failure = {"resource_uri": "mygithub12://response/failure"}
    monkeypatch.setattr(resume.dx, "validation_result", lambda *args, **kwargs: {"terminal": True, "merge_eligible": False, "attestation": None, "failure_pack": failure})
    captured = {}

    def transition(*args, **kwargs):
        captured.update(kwargs)
        return {**session, "status": "active", "session_revision": 5, "last_fast_ci_job_id": job["job_id"], "last_failure_resource_uri": failure["resource_uri"]}

    monkeypatch.setattr(resume.sessions, "transition", transition)
    result = resume.resume_task(FakeService(), "owner/repo", branch="ai/resume")

    assert result["development_session"]["status"] == "active"
    assert captured["fields"]["last_failure_resource_uri"] == failure["resource_uri"]
    assert captured["fields"].get("last_attestation_id") is None


def test_resume_reconciles_terminal_full_pass_to_pr_ready(monkeypatch):
    _, session, job = _stub_transient_recovery(monkeypatch, mode="full")
    attestation = {"attestation_id": "att-exact"}
    monkeypatch.setattr(resume.dx, "validation_result", lambda *args, **kwargs: {"terminal": True, "merge_eligible": True, "attestation": attestation, "failure_pack": None})
    monkeypatch.setattr(resume.sessions, "transition", lambda *args, **kwargs: {**session, "status": "pr_ready", "session_revision": 5, "last_full_ci_job_id": job["job_id"], "last_attestation_id": "att-exact"})

    result = resume.resume_task(FakeService(), "owner/repo", branch="ai/resume")

    assert result["development_session"]["status"] == "pr_ready"
    assert result["development_session"]["last_attestation_id"] == "att-exact"


def _stub_terminal_correlation_set(monkeypatch, *, statuses=("cancelled", "cancelled"), mode="full"):
    ws, session, _ = _stub_transient_recovery(monkeypatch, mode=mode, status=statuses[0])
    ws.update({
        "status": "drifted",
        "revision": int(session["workspace_revision"]) + 1,
        "head_sha": SHA_B,
        "tree_sha": TREE_B,
        "drift_reason": "branch_moved_externally",
        "lease_valid": False,
    })
    _stub_resume_context(
        monkeypatch, ws=ws, session=session, branch_head=SHA_B, branch_tree=TREE_B,
    )
    monkeypatch.setattr(
        resume,
        "_current_main",
        lambda service, repository: {
            "branch": "main", "repository": repository,
            "commit_sha": SHA_A, "tree_sha": TREE_A,
        },
    )

    requests = {}
    payloads = {}
    jobs = {}
    rows = []
    for index, status in enumerate(statuses):
        suffix = chr(ord("a") + index)
        request_id = f"ci_req_set_{suffix}"
        job_id = f"job-set-{suffix}"
        job = _validation_job(mode=mode, status=status, job_id=job_id)
        request = {
            "request_id": request_id,
            "repository": session["repository"],
            "branch": session["branch"],
            "commit_sha": session["head_commit_sha"],
            "tree_sha": session["tree_sha"],
            "profile": job["profile"],
            "worker_job_id": job_id,
            # Reproduce the durable Request lag seen in production: the Worker
            # is terminal even though the Request phase can still read queued.
            "phase": "queued",
            "status": "queued",
            "revision": 2,
        }
        payload = {
            "schema": "development-validation-v1",
            "development_session_id": session["session_id"],
            "expected_session_revision": session["session_revision"],
            "workspace_id": ws["workspace_id"],
            "workspace_revision": session["workspace_revision"],
            "repository": session["repository"],
            "branch": session["branch"],
            "commit_sha": session["head_commit_sha"],
            "tree_sha": session["tree_sha"],
            "profile": job["profile"],
            "mode": mode,
            "base_branch": session["base_branch"],
            "base_sha": session["base_commit_sha"],
        }
        requests[request_id] = request
        payloads[request_id] = payload
        jobs[job_id] = job
        rows.append({
            "request_id": request_id,
            "request_id_source": "column",
            "job_id": job_id,
            "session_revision": session["session_revision"],
            "tree_sha": session["tree_sha"],
            "evidence": {
                "request_id": request_id,
                "selection": {"complete": True, "changed_paths": ["x.py"]},
            },
        })

    generation = {
        "generation_revision": session["session_revision"],
        "generation_workspace_revision": session["workspace_revision"],
        "current_session_revision": session["session_revision"],
        "current_workspace_revision": session["workspace_revision"],
        "source": "validation_started_event",
        "maintenance_events": [],
    }
    monkeypatch.setattr(resume.sessions, "validation_generation_context", lambda *args, **kwargs: generation)
    monkeypatch.setattr(resume.sessions, "validation_correlations", lambda *args, **kwargs: rows)
    monkeypatch.setattr(resume.ci_request_store, "get_ci_request", lambda request_id: requests.get(request_id))
    monkeypatch.setattr(
        resume.ci_request_store, "get_ci_request_payload",
        lambda request_id: payloads.get(request_id, {}),
    )
    monkeypatch.setattr(resume, "db_get_job", lambda job_id: jobs.get(job_id))
    monkeypatch.setattr(
        resume.attestation_registry,
        "find_reusable_attestation_for_job",
        lambda *args, **kwargs: pytest.fail("a terminal correlation set must never reuse an attestation"),
    )
    captured = {}

    def reconcile_set(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        bindings = args[6]
        recovered = {
            **session,
            "status": "active",
            "session_revision": session["session_revision"] + 1,
            "last_fast_ci_job_id": None,
            "last_full_ci_job_id": None,
            "last_attestation_id": None,
            "last_failure_resource_uri": None,
        }
        return {
            "session": recovered,
            "audit": {
                "mode": mode,
                "request_ids": sorted(requests),
                "job_ids": sorted(jobs),
                "terminal_correlations": bindings,
                "workspace_drift_reconciliation": True,
            },
        }

    monkeypatch.setattr(resume.sessions, "reconcile_terminal_validation_set", reconcile_set)
    return ws, session, {"requests": requests, "payloads": payloads, "jobs": jobs, "rows": rows}, captured


@pytest.mark.parametrize(
    "statuses",
    [
        ("cancelled", "cancelled"),
        ("failed", "cancelled"),
        ("passed", "cancelled"),
    ],
)
def test_resume_reconciles_exact_terminal_correlation_set_without_merge_evidence(monkeypatch, statuses):
    ws, session, state, captured = _stub_terminal_correlation_set(monkeypatch, statuses=statuses)

    result = resume.resume_task(FakeService(), "owner/repo", branch="ai/resume")

    recovered = result["development_session"]
    transient = result["recovery"]["transient"]
    assert recovered["session_id"] == session["session_id"]
    assert recovered["status"] == "active"
    assert recovered["session_revision"] == session["session_revision"] + 1
    assert recovered["last_full_ci_job_id"] is None
    assert recovered["last_attestation_id"] is None
    assert recovered["last_failure_resource_uri"] is None
    assert result["workspace"]["workspace_id"] == ws["workspace_id"]
    assert result["workspace"]["status"] == "drifted"
    assert transient["reconciled"] is True
    assert transient["correlation_source"] == "persisted_terminal_set"
    assert transient["validation_result"]["merge_eligible"] is False
    assert transient["validation_result"]["attestation"] is None
    assert transient["correlation_set"]["request_ids"] == sorted(state["requests"])
    assert transient["correlation_set"]["job_ids"] == sorted(state["jobs"])
    assert captured["kwargs"]["allow_branch_drift"] is True
    assert result["next_allowed_actions"][0] == "recover_drifted_development_task"
    assert "continue_write" not in result["next_allowed_actions"]


def test_resume_terminal_correlation_set_identity_conflict_remains_fail_closed(monkeypatch):
    _, session, state, captured = _stub_terminal_correlation_set(monkeypatch)
    state["payloads"]["ci_req_set_b"]["tree_sha"] = TREE_B

    result = resume.resume_task(FakeService(), "owner/repo", branch="ai/resume")

    assert result["development_session"] == session
    assert result["recovery"]["transient"]["reason"] == "validation_request_identity_mismatch"
    assert "DEVELOPMENT_SESSION_RECOVERY_REQUIRED" in result["blockers"]
    assert captured == {}


@pytest.mark.parametrize("live_status", ["running", "queued", "preparing"])
def test_resume_terminal_correlation_set_with_live_worker_remains_fail_closed(monkeypatch, live_status):
    _, session, _state, captured = _stub_terminal_correlation_set(
        monkeypatch, statuses=("cancelled", live_status),
    )

    result = resume.resume_task(FakeService(), "owner/repo", branch="ai/resume")

    assert result["development_session"] == session
    assert result["recovery"]["transient"]["validation_in_progress"] is True
    assert "DEVELOPMENT_SESSION_VALIDATION_IN_PROGRESS" in result["blockers"]
    assert result["next_allowed_actions"] == ["get_private_ci_job", "resume_development_task"]
    assert captured == {}


def test_resume_terminal_correlation_set_retry_is_idempotent_after_single_transition(monkeypatch):
    ws, _session, _state, _captured = _stub_terminal_correlation_set(monkeypatch)
    first = resume.resume_task(FakeService(), "owner/repo", branch="ai/resume")
    recovered = first["development_session"]

    _stub_resume_context(
        monkeypatch, ws=ws, session=recovered, branch_head=SHA_B, branch_tree=TREE_B,
    )
    monkeypatch.setattr(
        resume,
        "_current_main",
        lambda service, repository: {
            "branch": "main", "repository": repository,
            "commit_sha": SHA_A, "tree_sha": TREE_A,
        },
    )
    monkeypatch.setattr(
        resume,
        "_reconcile_transient_validation",
        lambda *args, **kwargs: pytest.fail("an active Session must not reconcile the terminal set twice"),
    )
    second = resume.resume_task(FakeService(), "owner/repo", branch="ai/resume")

    assert second["development_session"]["session_id"] == recovered["session_id"]
    assert second["development_session"]["session_revision"] == recovered["session_revision"]
    assert second["development_session"]["status"] == "active"
    # The idempotency contract is the absence of a second validation
    # reconciliation/Session revision bump. Recovery-plan reconstruction is
    # covered separately by the production-shaped drift regression.
    assert "continue_write" not in second["next_allowed_actions"]


def test_resume_fails_stop_when_validation_job_is_not_unique(monkeypatch):
    _, session, _ = _stub_transient_recovery(monkeypatch, correlations=2)
    monkeypatch.setattr(resume.dx, "validation_result", lambda *args, **kwargs: pytest.fail("ambiguous CI must not be observed"))
    monkeypatch.setattr(resume.sessions, "transition", lambda *args, **kwargs: pytest.fail("ambiguous CI must not change Session state"))

    result = resume.resume_task(FakeService(), "owner/repo", branch="ai/resume")

    assert result["development_session"] == session
    assert result["recovery"]["transient"]["error_code"] == "DEVELOPMENT_SESSION_RECOVERY_REQUIRED"
    assert "DEVELOPMENT_SESSION_RECOVERY_REQUIRED" in result["blockers"]


def test_resume_transient_recovery_does_not_bypass_live_branch_drift(monkeypatch):
    ws, session, _ = _stub_transient_recovery(monkeypatch)
    ws["head_sha"] = SHA_B
    monkeypatch.setattr(resume, "_select_workspace", lambda *args: (ws, [ws]))
    monkeypatch.setattr(resume.sessions, "validation_correlations", lambda *args: pytest.fail("drift must win before transient recovery"))

    result = resume.resume_task(FakeService(), "owner/repo", branch="ai/resume")

    assert "WORKSPACE_BRANCH_DRIFTED" in result["blockers"]
    assert result["development_session"] == session


def _stub_drifted_transient_recovery(monkeypatch, *, mode="full", status="cancelled"):
    ws, session, job = _stub_transient_recovery(monkeypatch, mode=mode, status=status)
    ws.update({
        "status": "drifted",
        "revision": int(session["workspace_revision"]) + 1,
        "head_sha": SHA_B,
        "tree_sha": TREE_B,
        "drift_reason": "branch_moved_externally",
        "lease_valid": False,
    })
    _stub_resume_context(
        monkeypatch, ws=ws, session=session, branch_head=SHA_B, branch_tree=TREE_B,
    )
    monkeypatch.setattr(
        resume,
        "_current_main",
        lambda service, repository: {
            "branch": "main", "repository": repository,
            "commit_sha": SHA_A, "tree_sha": TREE_A,
        },
    )
    return ws, session, job


@pytest.mark.parametrize(
    ("mode", "status"),
    [("full", "cancelled"), ("full", "failed"), ("fast", "cancelled"), ("fast", "failed")],
)
def test_resume_drifted_terminal_validation_reconciles_before_formal_recovery(monkeypatch, mode, status):
    ws, session, job = _stub_drifted_transient_recovery(monkeypatch, mode=mode, status=status)
    failure = {"resource_uri": f"mygithub12://response/{mode}-{status}"}
    monkeypatch.setattr(
        resume.dx,
        "validation_result",
        lambda *args, **kwargs: {
            "terminal": True, "merge_eligible": False,
            "attestation": None, "failure_pack": failure,
        },
    )
    captured = {}

    def transition(_session_id, _revision, to_status, **kwargs):
        captured.update(to_status=to_status, **kwargs)
        job_field = "last_fast_ci_job_id" if mode == "fast" else "last_full_ci_job_id"
        return {
            **session,
            "status": to_status,
            "session_revision": session["session_revision"] + 1,
            job_field: job["job_id"],
            "last_failure_resource_uri": failure["resource_uri"],
        }

    monkeypatch.setattr(resume.sessions, "transition", transition)

    result = resume.resume_task(FakeService(), "owner/repo", branch="ai/resume")

    assert captured["to_status"] == "active"
    assert result["workspace"]["workspace_id"] == ws["workspace_id"]
    assert result["development_session"]["session_id"] == session["session_id"]
    assert result["development_session"]["status"] == "active"
    assert result["recovery"]["transient"]["reconciled"] is True
    assert result["recovery"]["transient"]["workspace_drift_pending_recovery"] is True
    assert "WORKSPACE_DRIFTED" in result["blockers"]
    assert result["next_allowed_actions"][0] == "recover_drifted_development_task"
    assert "continue_write" not in result["next_allowed_actions"]


def test_resume_drifted_terminal_full_pass_keeps_attestation_historical(monkeypatch):
    _, session, job = _stub_drifted_transient_recovery(monkeypatch, status="passed")
    attestation = {"attestation_id": "att-exact"}
    monkeypatch.setattr(
        resume.dx,
        "validation_result",
        lambda *args, **kwargs: {
            "terminal": True, "merge_eligible": True,
            "attestation": attestation, "failure_pack": None,
        },
    )
    captured = {}

    def transition(_session_id, _revision, to_status, **kwargs):
        captured.update(to_status=to_status, **kwargs)
        return {
            **session,
            "status": to_status,
            "session_revision": session["session_revision"] + 1,
            "last_full_ci_job_id": job["job_id"],
            "last_attestation_id": "att-exact",
        }

    monkeypatch.setattr(resume.sessions, "transition", transition)
    result = resume.resume_task(FakeService(), "owner/repo", branch="ai/resume")

    assert captured["to_status"] == "active"
    assert result["development_session"]["last_attestation_id"] == "att-exact"
    assert result["development_session"]["status"] != "pr_ready"
    assert result["recovery"]["transient"]["validation_result"]["merge_eligible"] is True
    assert result["recovery"]["transient"]["workspace_drift_pending_recovery"] is True
    assert result["next_allowed_actions"][0] == "recover_drifted_development_task"


@pytest.mark.parametrize("status", ["preparing", "queued", "running"])
def test_resume_drifted_live_validation_stays_fail_closed(monkeypatch, status):
    _, session, _ = _stub_drifted_transient_recovery(monkeypatch, status=status)
    monkeypatch.setattr(
        resume.dx, "validation_result",
        lambda *args, **kwargs: pytest.fail("non-terminal validation cannot be finalized"),
    )
    monkeypatch.setattr(
        resume.sessions, "transition",
        lambda *args, **kwargs: pytest.fail("non-terminal validation cannot change Session state"),
    )

    result = resume.resume_task(FakeService(), "owner/repo", branch="ai/resume")

    assert result["development_session"] == session
    assert "DEVELOPMENT_SESSION_VALIDATION_IN_PROGRESS" in result["blockers"]
    assert "WORKSPACE_DRIFTED" in result["blockers"]
    assert result["next_allowed_actions"] == ["get_private_ci_job", "resume_development_task"]
    assert "recover_drifted_development_task" not in result["next_allowed_actions"]


def test_resume_drifted_validation_identity_mismatch_remains_fail_closed(monkeypatch):
    _, session, job = _stub_drifted_transient_recovery(monkeypatch, status="cancelled")
    job["commit_sha"] = SHA_B
    monkeypatch.setattr(
        resume.dx, "validation_result",
        lambda *args, **kwargs: pytest.fail("identity mismatch cannot finalize"),
    )
    monkeypatch.setattr(
        resume.sessions, "transition",
        lambda *args, **kwargs: pytest.fail("identity mismatch cannot change Session state"),
    )

    result = resume.resume_task(FakeService(), "owner/repo", branch="ai/resume")

    assert result["development_session"] == session
    assert result["recovery"]["transient"]["reason"] == "validation_job_identity_mismatch"
    assert "DEVELOPMENT_SESSION_RECOVERY_REQUIRED" in result["blockers"]
    assert "continue_write" not in result["next_allowed_actions"]


def test_validation_correlation_store_allows_only_verified_branch_drift_cas(tmp_path, monkeypatch):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "drift-validation.db"))
    resume.mygithub12.init_db()
    now = resume.mygithub12._now()
    with sessions._LOCK, sessions._db() as db:
        db.execute(
            "INSERT INTO workspaces VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "ws_resume", "owner/repo", "ai/resume", "main", SHA_A,
                SHA_A, TREE_A, "active", 1, "test", now + 600,
                SHA_A, "{}", None, None, now, now,
            ),
        )
    created = sessions.create_session(_workspace(revision=1, lease=now + 600), idempotency_key="drift-store")
    validating = sessions.transition(
        created["session_id"], created["session_revision"], "validating_fast", allowed_from={"active"},
    )
    sessions.record_validation(
        validating["session_id"], validating["session_revision"], "fast", SHA_A, TREE_A,
        request_id="ci_req_drift", evidence={"request_id": "ci_req_drift", "selection": {"complete": True}},
    )
    with sessions._LOCK, sessions._db() as db:
        db.execute(
            "UPDATE workspaces SET head_sha=?,tree_sha=?,status='drifted',drift_reason='branch_moved_externally',revision=2 WHERE workspace_id=?",
            (SHA_B, TREE_B, "ws_resume"),
        )

    with pytest.raises(resume.MyGithub12Error):
        sessions.bind_validation_request_worker(
            validating["session_id"], validating["session_revision"], 2, "fast",
            SHA_A, TREE_A, "ci_req_drift", "job-drift",
        )

    binding = sessions.bind_validation_request_worker(
        validating["session_id"], validating["session_revision"], 2, "fast",
        SHA_A, TREE_A, "ci_req_drift", "job-drift", allow_branch_drift=True,
    )
    assert binding["request_id"] == "ci_req_drift"
    assert binding["job_id"] == "job-drift"
    assert binding["workspace_drift_reconciliation"] is True


def test_validation_correlation_store_request_only_terminal_is_cas_bound(tmp_path, monkeypatch):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "request-terminal.db"))
    resume.mygithub12.init_db()
    now = resume.mygithub12._now()
    with sessions._LOCK, sessions._db() as db:
        db.execute(
            "INSERT INTO workspaces VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "ws_resume", "owner/repo", "ai/resume", "main", SHA_A,
                SHA_A, TREE_A, "active", 1, "test", now + 600,
                SHA_A, "{}", None, None, now, now,
            ),
        )
    created = sessions.create_session(_workspace(revision=1, lease=now + 600), idempotency_key="request-terminal")
    validating = sessions.transition(
        created["session_id"], created["session_revision"], "validating_fast", allowed_from={"active"},
    )
    sessions.record_validation(
        validating["session_id"], validating["session_revision"], "fast", SHA_A, TREE_A,
        request_id="ci_req_terminal", status="preparing",
        evidence={"request_id": "ci_req_terminal", "selection": {"complete": False}},
    )

    with pytest.raises(resume.MyGithub12Error):
        sessions.bind_validation_request_worker(
            validating["session_id"], validating["session_revision"], 1, "fast",
            SHA_A, TREE_A, "ci_req_terminal", "", request_terminal_status="failed",
        )

    binding = sessions.bind_validation_request_worker(
        validating["session_id"], validating["session_revision"], 1, "fast",
        SHA_A, TREE_A, "ci_req_terminal", "", request_terminal_status="preflight_failed",
    )
    assert binding["request_only_terminal"] is True
    assert binding["request_terminal_status"] == "preflight_failed"
    correlations = sessions.validation_correlations(
        validating["session_id"], validating["session_revision"], "fast", SHA_A, TREE_A,
    )
    assert correlations[0]["status"] == "preflight_failed"
    assert correlations[0]["job_id"] is None


def test_validation_terminal_correlation_set_store_is_atomic_and_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "terminal-set.db"))
    resume.mygithub12.init_db()
    now = resume.mygithub12._now()
    with sessions._LOCK, sessions._db() as db:
        db.execute(
            "INSERT INTO workspaces VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "ws_resume", "owner/repo", "ai/resume", "main", SHA_A,
                SHA_A, TREE_A, "active", 1, "test", now + 600,
                SHA_A, "{}", None, None, now, now,
            ),
        )
    created = sessions.create_session(
        _workspace(revision=1, lease=now + 600), idempotency_key="terminal-set-store",
    )
    validating = sessions.transition(
        created["session_id"],
        created["session_revision"],
        "validating_full",
        allowed_from={"active"},
        fields={
            "last_full_ci_job_id": "old-job",
            "last_attestation_id": "old-attestation",
            "last_failure_resource_uri": "mygithub12://response/old-failure",
        },
    )
    pairs = [
        {"request_id": "ci_req_set_a", "job_id": "job-set-a", "status": "cancelled"},
        {"request_id": "ci_req_set_b", "job_id": "job-set-b", "status": "failed"},
    ]
    for pair in pairs:
        sessions.record_validation(
            validating["session_id"], validating["session_revision"], "full", SHA_A, TREE_A,
            request_id=pair["request_id"], job_id=pair["job_id"], status="running",
            evidence={"request_id": pair["request_id"], "selection": {"complete": True}},
        )
    with sessions._LOCK, sessions._db() as db:
        db.execute(
            """UPDATE workspaces
               SET head_sha=?,tree_sha=?,status='drifted',drift_reason='branch_moved_externally',revision=2,
                   index_commit_sha=NULL,lease_expires_at=0
               WHERE workspace_id=?""",
            (SHA_B, TREE_B, "ws_resume"),
        )

    settled = sessions.reconcile_terminal_validation_set(
        validating["session_id"], validating["session_revision"], 2, "full",
        SHA_A, TREE_A, pairs,
        validation_generation_revision=validating["session_revision"],
        validation_generation_workspace_revision=validating["workspace_revision"],
        allow_branch_drift=True,
    )

    recovered = settled["session"]
    assert recovered["session_id"] == validating["session_id"]
    assert recovered["status"] == "active"
    assert recovered["session_revision"] == validating["session_revision"] + 1
    assert recovered["head_commit_sha"] == SHA_A
    assert recovered["tree_sha"] == TREE_A
    assert recovered["last_full_ci_job_id"] is None
    assert recovered["last_attestation_id"] is None
    assert recovered["last_failure_resource_uri"] is None
    assert settled["audit"]["request_ids"] == ["ci_req_set_a", "ci_req_set_b"]
    assert settled["audit"]["job_ids"] == ["job-set-a", "job-set-b"]
    assert settled["audit"]["workspace_drift_reconciliation"] is True
    correlations = sessions.validation_correlations(
        validating["session_id"], recovered["session_revision"], "full", SHA_A, TREE_A,
    )
    assert {item["request_id"]: item["status"] for item in correlations} == {
        "ci_req_set_a": "cancelled",
        "ci_req_set_b": "failed",
    }
    events = sessions.list_events(validating["session_id"], limit=20)
    event = next(item for item in events if item["event_type"] == "validation_terminal_correlation_set_reconciled")
    assert event["data"]["request_ids"] == ["ci_req_set_a", "ci_req_set_b"]

    with pytest.raises(resume.MyGithub12Error) as exc:
        sessions.reconcile_terminal_validation_set(
            validating["session_id"], validating["session_revision"], 2, "full",
            SHA_A, TREE_A, pairs,
            validation_generation_revision=validating["session_revision"],
            validation_generation_workspace_revision=validating["workspace_revision"],
            allow_branch_drift=True,
        )
    assert exc.value.code == "DEVELOPMENT_SESSION_REVISION_MISMATCH"
    unchanged = sessions.get_session(validating["session_id"])
    assert unchanged["session_revision"] == recovered["session_revision"]


def test_resume_transient_recovery_preserves_session_revision_cas(monkeypatch):
    _stub_transient_recovery(monkeypatch)

    with pytest.raises(resume.MyGithub12Error) as exc:
        resume.resume_task(FakeService(), "owner/repo", branch="ai/resume", expected_session_revision=3)

    assert exc.value.code == "DEVELOPMENT_SESSION_REVISION_MISMATCH"


def test_resume_recovers_restart_after_nonterminal_observation_with_legacy_fast_tree(monkeypatch):
    _, session, job = _stub_transient_recovery(monkeypatch)
    session["last_fast_ci_job_id"] = job["job_id"]
    job["summary"] = {}
    generation_revision = session["session_revision"] - 1
    generation = {
        "generation_revision": generation_revision,
        "generation_workspace_revision": session["workspace_revision"],
        "current_session_revision": session["session_revision"],
        "current_workspace_revision": session["workspace_revision"],
        "source": "validation_started_event",
        "maintenance_events": [{
            "session_revision": session["session_revision"],
            "event_type": "validation_observed",
            "before_workspace_revision": session["workspace_revision"],
            "after_workspace_revision": session["workspace_revision"],
        }],
    }
    monkeypatch.setattr(resume.sessions, "validation_generation_context", lambda *args, **kwargs: generation)
    monkeypatch.setattr(
        resume.sessions,
        "validation_correlations",
        lambda *args, **kwargs: [{
            "job_id": job["job_id"], "session_revision": generation_revision,
            "tree_sha": "", "evidence": {"selection": {"complete": True}},
        }],
    )
    monkeypatch.setattr(
        resume.ci_request_store,
        "get_ci_request_payload",
        lambda request_id: {
            "development_session_id": session["session_id"],
            "expected_session_revision": generation_revision,
            "workspace_id": session["workspace_id"],
            "workspace_revision": session["workspace_revision"],
            "repository": session["repository"],
            "branch": session["branch"],
            "commit_sha": session["head_commit_sha"],
            "tree_sha": session["tree_sha"],
            "profile": "repo-fast-check",
            "mode": "fast",
            "base_branch": session["base_branch"],
            "base_sha": session["base_commit_sha"],
        },
    )
    monkeypatch.setattr(resume.dx, "validation_result", lambda *args, **kwargs: {"terminal": True, "merge_eligible": False, "attestation": None, "failure_pack": None})
    monkeypatch.setattr(resume.sessions, "transition", lambda *args, **kwargs: {**session, "status": "active", "session_revision": session["session_revision"] + 1})

    result = resume.resume_task(FakeService(), "owner/repo", branch="ai/resume")

    assert result["development_session"]["status"] == "active"
    assert result["recovery"]["transient"]["correlation_source"] == "legacy_exact_job_to_request"
    assert result["recovery"]["transient"]["tree_evidence"] == "request_tree_worker_pair"


def test_resume_task_rejects_repository_before_github_reads(monkeypatch):
    monkeypatch.setattr(resume, "_repository_policy", lambda repository: {"ok": True, "repository": repository, "policy": {"github": False}})
    monkeypatch.setattr(resume, "_resolve_pr", lambda *args, **kwargs: pytest.fail("GitHub read must not run for denied repository"))
    with pytest.raises(resume.MyGithub12Error) as exc:
        resume.resume_task(FakeService(), "owner/denied", branch="ai/resume")
    assert exc.value.code == "REPOSITORY_NOT_ALLOWED"


def test_resume_task_pr_only_resolves_same_branch_and_readiness(monkeypatch):
    ws = _workspace()
    session = _ready_session(workspace_revision=ws["revision"], lease=ws["lease_expires_at"])
    pr = {"pull_number": 7, "head_branch": "ai/resume", "head_sha": SHA_A, "base_branch": "main", "state": "open", "draft": True}
    readiness = {"ok": True, "pull_number": 7, "ready": False}
    _stub_resume_context(monkeypatch, ws=ws, session=session, pr=pr, readiness=readiness)

    result = resume.resume_task(FakeService(), "owner/repo", pull_number=7)

    assert result["input"] == {"branch": "", "pull_number": 7}
    assert result["branch"]["branch"] == "ai/resume"
    assert result["pull_request_readiness"] == readiness
    assert "readiness" in result["next_allowed_actions"]


def test_resume_task_safely_recovers_stale_session(monkeypatch):
    ws = _workspace(revision=3)
    ws.update({"head_sha": SHA_B, "tree_sha": "2" * 40})
    stale = _ready_session(head=SHA_A, tree=TREE_A, workspace_revision=2, lease=ws["lease_expires_at"])
    recovered = {**stale, "head_commit_sha": SHA_B, "tree_sha": "2" * 40, "workspace_revision": 3, "session_revision": 5}
    _stub_resume_context(monkeypatch, ws=ws, session=stale, branch_head=SHA_B, branch_tree="2" * 40)
    captured = {}

    def fake_recover(service, session_id, session_revision, workspace_revision, expected_head_sha, idempotency_key):
        captured.update(session_id=session_id, session_revision=session_revision, workspace_revision=workspace_revision, expected_head_sha=expected_head_sha, idempotency_key=idempotency_key)
        return {"session": recovered, "workspace": ws, "recovered": True}

    monkeypatch.setattr(resume.dx, "recover_stale_session", fake_recover)
    result = resume.resume_task(FakeService(), "owner/repo", branch="ai/resume", idempotency_key="resume-idem")

    assert captured == {"session_id": "dev_resume", "session_revision": 4, "workspace_revision": 3, "expected_head_sha": SHA_B, "idempotency_key": "resume-idem"}
    assert result["development_session"]["head_commit_sha"] == SHA_B
    assert result["recovery"]["session"]["recovered"] is True
    assert "continue_write" in result["next_allowed_actions"]


def test_resume_task_drifted_workspace_never_invokes_session_recovery(monkeypatch):
    ws = _workspace(status="drifted", revision=3)
    ws["drift_reason"] = "branch moved externally"
    stale = _ready_session(workspace_revision=2, lease=ws["lease_expires_at"])
    _stub_resume_context(monkeypatch, ws=ws, session=stale)
    monkeypatch.setattr(resume.dx, "recover_stale_session", lambda *args, **kwargs: pytest.fail("drifted Workspace must not auto-recover"))

    result = resume.resume_task(FakeService(), "owner/repo", branch="ai/resume")

    assert "WORKSPACE_DRIFTED" in result["blockers"]
    assert result["recovery"]["action"] == "recover_drifted_development_task"
    assert result["recovery"]["recovery_tool"] == "recover_drifted_development_task"
    assert result["recovery"]["manual_recovery_required"] is True
    assert result["next_allowed_actions"] == ["recover_drifted_development_task", "recovery_required"]


def test_stacked_resume_uses_live_workspace_pr_base_branch_head_not_repository_main(monkeypatch):
    stacked_branch = "ai/stacked-base"
    old_base = "d" * 40
    stacked_head = "e" * 40
    current_head = "f" * 40
    current_tree = "3" * 40
    ws = _workspace(status="drifted", revision=3)
    ws.update({
        "base_branch": stacked_branch,
        "base_commit_sha": old_base,
        "head_sha": current_head,
        "tree_sha": current_tree,
        "drift_reason": "branch_moved_externally",
    })
    session = {
        **_ready_session(head=SHA_A, tree=TREE_A, workspace_revision=2, lease=ws["lease_expires_at"]),
        "base_branch": stacked_branch,
        "base_commit_sha": old_base,
    }
    pr = {
        "pull_number": 7,
        "head_branch": "ai/resume",
        "head_sha": current_head,
        "base_branch": stacked_branch,
        "state": "open",
        "draft": True,
    }
    _stub_resume_context(
        monkeypatch, ws=ws, session=session, pr=pr, branch_head=current_head, branch_tree=current_tree,
    )
    monkeypatch.setattr(
        resume.mygithub12,
        "resolve_identity",
        lambda service, repository, commit_sha="", ref="": {
            "repository": repository,
            "commit_sha": stacked_head if ref == stacked_branch else (commit_sha or SHA_B),
            "tree_sha": "4" * 40,
        },
    )
    monkeypatch.setattr(
        resume,
        "_resume_ancestry_evidence",
        lambda service, repository, ancestor, descendant: {
            "verified": True, "ancestor": ancestor, "descendant": descendant,
        },
    )

    result = resume.resume_task(FakeService(), "owner/repo", pull_number=7)

    assert result["current_main"]["commit_sha"] == SHA_B
    assert result["recovery_base"]["branch"] == stacked_branch
    assert result["recovery_base"]["commit_sha"] == stacked_head
    assert result["recovery"]["action"] == "recover_base_synced_development_task"
    assert result["recovery"]["expected_new_base_sha"] == stacked_head
    assert result["recovery"]["expected_new_base_sha"] != SHA_B
    assert result["recovery"]["expected_base_branch"] == stacked_branch


def _stacked_pinned_new_resume_case(
    monkeypatch, *, metadata=None, events=None, ancestry_predicate=None, merge_base_evidence=None,
):
    stacked_branch = "ai/issue-186-p0-14-share-invite-global-locator-20260904"
    historical_old_base = "20e8e5a5a411c55e830db33daca5cf3ab6f97db9"
    live_new_base = "43ba158333f06e30210dca596f3b7eae204d149a"
    old_session_head = "23aab1b9f80296d0e88c552ddbdac54c56939bc9"
    integrated_head = "249f4dc68200e83b4fd73a8bbe43608beaac5d42"
    integrated_tree = "7" * 40
    ws = _workspace(status="drifted", revision=3)
    ws.update({
        "base_branch": stacked_branch,
        "base_commit_sha": live_new_base,
        "head_sha": integrated_head,
        "tree_sha": integrated_tree,
        "drift_reason": "branch_moved_externally",
    })
    if metadata is None:
        metadata = {
            "prepared_base_identity": {
                "repository": "owner/repo",
                "commit_sha": historical_old_base,
                "tree_sha": "6" * 40,
            }
        }
    session = {
        **_ready_session(
            head=old_session_head, tree="5" * 40, workspace_revision=2,
            lease=ws["lease_expires_at"],
        ),
        "base_branch": stacked_branch,
        "base_commit_sha": live_new_base,
        "metadata": metadata,
    }
    pr = {
        "pull_number": 823,
        "head_branch": "ai/resume",
        "head_sha": integrated_head,
        "base_branch": stacked_branch,
        "state": "open",
        "draft": True,
    }
    _stub_resume_context(
        monkeypatch, ws=ws, session=session, pr=pr,
        branch_head=integrated_head, branch_tree=integrated_tree,
    )
    monkeypatch.setattr(
        resume.mygithub12,
        "resolve_identity",
        lambda service, repository, commit_sha="", ref="": {
            "repository": repository,
            "commit_sha": live_new_base if ref == stacked_branch else (commit_sha or SHA_B),
            "tree_sha": "4" * 40,
        },
    )
    monkeypatch.setattr(resume.sessions, "list_events", lambda *args, **kwargs: list(events or []))
    if merge_base_evidence is not None:
        monkeypatch.setattr(
            resume, "_resume_merge_base_evidence",
            lambda *args, **kwargs: dict(merge_base_evidence),
        )
    predicate = ancestry_predicate or (lambda ancestor, descendant: True)
    monkeypatch.setattr(
        resume,
        "_resume_ancestry_evidence",
        lambda service, repository, ancestor, descendant: {
            "verified": bool(predicate(ancestor, descendant)),
            "ancestor": ancestor, "descendant": descendant,
        },
    )
    result = resume.resume_task(FakeService(), "owner/repo", pull_number=823)
    return result, {
        "base_branch": stacked_branch,
        "historical_old_base": historical_old_base,
        "live_new_base": live_new_base,
        "old_session_head": old_session_head,
        "integrated_head": integrated_head,
        "integrated_tree": integrated_tree,
    }


def test_already_pinned_new_base_stacked_resume_uses_audited_historical_old_base(monkeypatch):
    result, ids = _stacked_pinned_new_resume_case(monkeypatch)

    plan = result["recovery"]
    assert plan["action"] == "recover_base_synced_development_task"
    assert plan["action"] != "recover_drifted_development_task"
    assert plan["expected_old_base_sha"] == ids["historical_old_base"]
    assert plan["expected_new_base_sha"] == ids["live_new_base"]
    assert plan["expected_base_branch"] == ids["base_branch"]
    assert plan["expected_old_session_head_sha"] == ids["old_session_head"]
    assert plan["expected_current_head_sha"] == ids["integrated_head"]
    assert plan["expected_current_tree_sha"] == ids["integrated_tree"]
    assert plan["repository"] == "owner/repo"
    assert plan["branch"] == "ai/resume"
    assert plan["expected_workspace_revision"] == 3
    assert plan["expected_session_revision"] == 4
    assert plan["preflight"]["verified"] is True


def test_already_pinned_new_base_stacked_resume_fails_stop_on_ambiguous_historical_old_base(monkeypatch):
    result, _ = _stacked_pinned_new_resume_case(
        monkeypatch,
        events=[{
            "id": 9, "event_type": "base_sync_recovery", "session_revision": 3,
            "data": {"old_base_sha": "8" * 40, "new_base_sha": "9" * 40},
        }],
    )

    assert result["recovery"]["action"] == "recovery_required"
    assert result["recovery"]["reason"] == "RECOVERY_HISTORICAL_OLD_BASE_AMBIGUOUS"
    assert result["recovery"]["action"] != "recover_drifted_development_task"


def test_already_pinned_new_base_stacked_resume_fails_stop_when_historical_old_base_unavailable(monkeypatch):
    result, _ = _stacked_pinned_new_resume_case(
        monkeypatch, metadata={},
        merge_base_evidence={"verified": False, "reason": "merge_base_unavailable"},
    )

    assert result["recovery"]["action"] == "recovery_required"
    assert result["recovery"]["reason"] == "RECOVERY_HISTORICAL_OLD_BASE_UNAVAILABLE"
    assert result["recovery"]["action"] != "recover_drifted_development_task"


def test_already_pinned_new_base_stacked_resume_fails_stop_when_recorded_old_base_ancestry_is_inconsistent(monkeypatch):
    old_base = "20e8e5a5a411c55e830db33daca5cf3ab6f97db9"
    new_base = "43ba158333f06e30210dca596f3b7eae204d149a"
    result, _ = _stacked_pinned_new_resume_case(
        monkeypatch,
        ancestry_predicate=lambda ancestor, descendant: not (ancestor == old_base and descendant == new_base),
    )

    assert result["recovery"]["action"] == "recovery_required"
    assert result["recovery"]["reason"] == "RECOVERY_ANCESTRY_MISMATCH"
    assert result["recovery"]["preflight"]["base_ancestry"]["verified"] is False


def test_already_pinned_new_base_stacked_resume_fails_stop_when_current_head_is_not_old_session_forward_descendant(monkeypatch):
    old_head = "23aab1b9f80296d0e88c552ddbdac54c56939bc9"
    current_head = "249f4dc68200e83b4fd73a8bbe43608beaac5d42"
    result, _ = _stacked_pinned_new_resume_case(
        monkeypatch,
        ancestry_predicate=lambda ancestor, descendant: not (ancestor == old_head and descendant == current_head),
    )

    assert result["recovery"]["action"] == "recovery_required"
    assert result["recovery"]["reason"] == "RECOVERY_ANCESTRY_MISMATCH"
    assert result["recovery"]["preflight"]["task_ancestry"]["verified"] is False


def test_already_pinned_new_base_stacked_resume_fails_stop_when_current_head_does_not_include_live_new_base(monkeypatch):
    new_base = "43ba158333f06e30210dca596f3b7eae204d149a"
    current_head = "249f4dc68200e83b4fd73a8bbe43608beaac5d42"
    result, _ = _stacked_pinned_new_resume_case(
        monkeypatch,
        ancestry_predicate=lambda ancestor, descendant: not (ancestor == new_base and descendant == current_head),
    )

    assert result["recovery"]["action"] == "recovery_required"
    assert result["recovery"]["reason"] == "RECOVERY_ANCESTRY_MISMATCH"
    assert result["recovery"]["preflight"]["new_base_ancestry"]["verified"] is False


def test_main_based_resume_keeps_repository_main_as_live_base(monkeypatch):
    current_head = "f" * 40
    current_tree = "3" * 40
    ws = _workspace(status="drifted", revision=3)
    ws.update({"head_sha": current_head, "tree_sha": current_tree, "drift_reason": "branch_moved_externally"})
    session = {
        **_ready_session(head=SHA_A, tree=TREE_A, workspace_revision=2, lease=ws["lease_expires_at"]),
        "base_branch": "main",
        "base_commit_sha": SHA_A,
    }
    _stub_resume_context(monkeypatch, ws=ws, session=session, branch_head=current_head, branch_tree=current_tree)
    monkeypatch.setattr(resume, "_resume_ancestry_evidence", lambda *args, **kwargs: {"verified": True})

    result = resume.resume_task(FakeService(), "owner/repo", branch="ai/resume")

    assert result["recovery_base"] == result["current_main"]
    assert result["recovery"]["action"] == "recover_base_synced_development_task"
    assert result["recovery"]["expected_new_base_sha"] == SHA_B
    assert result["recovery"]["expected_base_branch"] == "main"


def test_session_evidence_never_promotes_old_head_or_invalid_attestation(monkeypatch):
    historical = {"head_commit_sha": SHA_A, "last_full_ci_job_id": "old-full", "last_attestation_id": "old-att", "last_fast_ci_job_id": None, "last_failure_resource_uri": None}
    evidence = resume._session_evidence(historical, SHA_B)
    assert evidence["current_head"] is None
    assert evidence["historical"]["last_full_ci_job_id"] == "old-full"

    current = {**historical, "head_commit_sha": SHA_B, "last_attestation_id": "current-att"}
    monkeypatch.setattr(resume.attestation_registry, "validate_attestation", lambda attestation_id: {"ok": True, "reusable": True, "attestation": {"attestation_id": attestation_id, "tested_commit_sha": SHA_B}})
    exact = resume._session_evidence(current, SHA_B)
    assert exact["historical"] is None
    assert exact["current_head"]["validated_attestation"]["ok"] is True


def test_resume_task_expired_workspace_renew_requires_revision_cas(monkeypatch):
    ws = _workspace(status="expired", revision=9)
    _stub_resume_context(monkeypatch, ws=ws, session=None)
    with pytest.raises(resume.MyGithub12Error) as exc:
        resume.resume_task(FakeService(), "owner/repo", branch="ai/resume", renew_lease=True)
    assert exc.value.code == "WORKSPACE_REVISION_MISMATCH"


def test_resume_task_explicitly_groups_live_historical_and_candidate_actions(monkeypatch):
    ws = _workspace()
    session = _ready_session(workspace_revision=ws["revision"], lease=ws["lease_expires_at"])
    _stub_resume_context(monkeypatch, ws=ws, session=session)

    result = resume.resume_task(FakeService(), "owner/repo", branch="ai/resume")

    assert result["live_facts"]["branch"]["commit_sha"] == SHA_A
    assert result["historical_evidence"] == {"session": None}
    assert result["candidate_next_actions"] == result["next_allowed_actions"]
    assert "continue_write" in result["candidate_next_actions"]


def test_resume_task_candidate_actions_respect_private_ci_policy(monkeypatch):
    ws = _workspace()
    session = _ready_session(workspace_revision=ws["revision"], lease=ws["lease_expires_at"])
    _stub_resume_context(monkeypatch, ws=ws, session=session)
    monkeypatch.setattr(resume, "_repository_policy", lambda repository: {"ok": True, "repository": repository, "policy": {"github": True, "private_ci": False}})

    result = resume.resume_task(FakeService(), "owner/repo", branch="ai/resume")

    assert "continue_write" in result["candidate_next_actions"]
    assert "prepare_pr" in result["candidate_next_actions"]
    assert "run_fast_ci" not in result["candidate_next_actions"]
    assert "run_full_ci" not in result["candidate_next_actions"]


def test_session_evidence_does_not_promote_invalid_current_attestation(monkeypatch):
    current = {"head_commit_sha": SHA_B, "last_full_ci_job_id": "full", "last_attestation_id": "invalid-att", "last_fast_ci_job_id": None, "last_failure_resource_uri": None}
    monkeypatch.setattr(resume.attestation_registry, "validate_attestation", lambda attestation_id: {"ok": False, "reusable": False, "error_code": "ATTESTATION_EXPIRED"})

    evidence = resume._session_evidence(current, SHA_B)

    assert evidence["historical"] is None
    assert evidence["current_head"]["last_attestation_id"] == "invalid-att"
    assert evidence["current_head"]["validated_attestation"] is None


def test_discover_pr_by_branch_uses_exact_open_head_and_base(monkeypatch):
    captured = {}

    def fake_list(repository, state="open", head_branch="", base_branch="", sort="updated", direction="desc", limit=30, page=1):
        captured.update(repository=repository, state=state, head_branch=head_branch, base_branch=base_branch, sort=sort, direction=direction, limit=limit, page=page)
        return {"ok": True, "pull_requests": [{"pull_number": 7, "head_branch": "ai/resume", "base_branch": "main"}]}

    monkeypatch.setattr(resume.github_utils, "list_github_pull_requests", fake_list)
    monkeypatch.setattr(resume.github_utils, "get_github_pull_request", lambda repository, pull_number: {"ok": True, "pull_number": pull_number, "head_branch": "ai/resume", "head_sha": SHA_A, "base_branch": "main", "state": "open", "draft": True})

    pr = resume._discover_pr_by_branch("owner/repo", "ai/resume", "main")

    assert captured == {"repository": "owner/repo", "state": "open", "head_branch": "ai/resume", "base_branch": "main", "sort": "updated", "direction": "desc", "limit": 2, "page": 1}
    assert pr["pull_number"] == 7


def test_resume_task_branch_only_discovers_existing_pr_and_readiness(monkeypatch):
    ws = _workspace()
    session = _ready_session(workspace_revision=ws["revision"], lease=ws["lease_expires_at"])
    pr = {"pull_number": 7, "head_branch": "ai/resume", "head_sha": SHA_A, "base_branch": "main", "state": "open", "draft": True}
    readiness = {"ok": True, "pull_number": 7, "ready": False}
    _stub_resume_context(monkeypatch, ws=ws, session=session, pr=pr, readiness=readiness)

    result = resume.resume_task(FakeService(), "owner/repo", branch="ai/resume")

    assert result["pull_request"]["pull_number"] == 7
    assert result["pull_request"]["draft"] is True
    assert result["pull_request_readiness"] == readiness
    assert "readiness" in result["candidate_next_actions"]


def test_resume_task_reconciles_historical_merged_managed_pr(monkeypatch):
    ws = _workspace(status="expired", lease=0)
    session = {
        "session_id": "dev_resume", "workspace_id": ws["workspace_id"],
        "repository": "owner/repo", "branch": "ai/resume", "base_branch": "main",
        "status": "pr_ready", "session_revision": 4,
        "workspace_revision": ws["revision"], "head_commit_sha": SHA_A,
        "tree_sha": TREE_A, "lease_expires_at": ws["lease_expires_at"],
        "pull_number": None, "last_fast_ci_job_id": None,
        "last_full_ci_job_id": "job-full", "last_attestation_id": "att",
        "last_failure_resource_uri": None,
    }
    pr = {
        "ok": True, "pull_number": 7, "merged": True, "state": "closed",
        "head_branch": "ai/resume", "head_sha": SHA_A,
        "base_branch": "main", "base_sha": SHA_B,
        "merge_commit_sha": "c" * 40,
    }
    closed_ws = {**ws, "status": "closed", "persisted_status": "closed", "revision": 3, "lease_valid": False}
    merged_session = {**session, "status": "merged", "pull_number": 7, "session_revision": 5, "workspace_revision": 3, "lease_valid": False}
    _stub_resume_context(monkeypatch, ws=ws, session=session, pr=pr)

    def finalize(*args, **kwargs):
        assert args[5]["merge_commit_sha"] == "c" * 40
        assert kwargs["expected_workspace_id"] == ws["workspace_id"]
        assert kwargs["expected_session_id"] == session["session_id"]
        return {
            "ok": True, "status": "finalized", "managed": True,
            "workspace": closed_ws, "development_session": merged_session,
            "evidence": {"merge_commit_sha": "c" * 40},
        }

    monkeypatch.setattr(resume.managed_merge, "finalize_managed_pr_merge", finalize)

    result = resume.resume_task(FakeService(), "owner/repo", pull_number=7)

    assert result["recovery"]["managed_merge_reconciliation"]["status"] == "finalized"
    assert result["workspace"]["status"] == "closed"
    assert result["development_session"]["status"] == "merged"
    assert result["development_session"]["pull_number"] == 7
    assert result["blockers"] == []
    assert result["next_allowed_actions"] == ["managed_merge_finalized"]


def test_resume_merged_legacy_task_never_suggests_writer_lease_recovery(monkeypatch):
    ws = _workspace(status="expired", lease=0)
    session = {
        "session_id": "dev_resume", "workspace_id": ws["workspace_id"],
        "repository": "owner/repo", "branch": "ai/resume", "base_branch": "main",
        "status": "pr_ready", "session_revision": 4,
        "workspace_revision": ws["revision"], "head_commit_sha": SHA_A,
        "tree_sha": TREE_A, "lease_expires_at": 0,
        "pull_number": None, "last_fast_ci_job_id": None,
        "last_full_ci_job_id": "job-full", "last_attestation_id": "att",
        "last_failure_resource_uri": None,
    }
    pr = {
        "ok": True, "pull_number": 7, "merged": True, "state": "closed",
        "head_branch": "ai/resume", "head_sha": SHA_A,
        "base_branch": "main", "base_sha": SHA_B,
        "merge_commit_sha": "c" * 40,
    }
    _stub_resume_context(monkeypatch, ws=ws, session=session, pr=pr)

    result = resume.resume_task(
        FakeService(), "owner/repo", pull_number=7, recover_stale_session=False,
    )

    assert "MANAGED_MERGE_RECONCILIATION_REQUIRED" in result["blockers"]
    assert result["recovery"]["managed_merge_reconciliation"]["reason"] == "MANAGED_MERGE_RECONCILIATION_REQUIRED"
    assert result["next_allowed_actions"] == ["resume_development_task"]
    assert "resume_development_workspace" not in result["next_allowed_actions"]


def _production_validation_case(monkeypatch, *, mode, repository, branch, head, tree, base, request_id, job_id, status="passed", worker_tree=True, attestation_id=""):
    ws = {
        "workspace_id": "ws-production", "repository": repository, "branch": branch,
        "base_branch": "main", "base_commit_sha": base, "head_sha": head, "tree_sha": tree,
        "status": "active", "revision": 7, "lease_expires_at": 9999999999.0, "drift_reason": None,
    }
    session = {
        "session_id": "dev-production", "workspace_id": ws["workspace_id"], "repository": repository,
        "branch": branch, "base_branch": "main", "base_commit_sha": base,
        "head_commit_sha": head, "tree_sha": tree, "workspace_revision": 7, "session_revision": 7,
        "lease_expires_at": ws["lease_expires_at"],
        "status": "validating_fast" if mode == "fast" else "validating_full",
        "last_fast_ci_job_id": None, "last_full_ci_job_id": None,
        "last_attestation_id": None, "last_failure_resource_uri": None,
    }
    profile = "repo-fast-check" if mode == "fast" else "repo-auto-check"
    correlation = {
        "request_id": request_id, "request_id_source": "legacy_evidence", "job_id": None,
        "session_revision": 7, "tree_sha": tree,
        "evidence": {"request_id": request_id, "selection": {"complete": True, "changed_paths": ["production-shape"]}},
    }
    request = {
        "request_id": request_id, "repository": repository, "branch": branch, "commit_sha": head,
        "tree_sha": tree, "profile": profile, "worker_job_id": job_id, "phase": "queued", "status": "queued",
    }
    payload = {
        "development_session_id": session["session_id"], "workspace_id": ws["workspace_id"],
        "repository": repository, "branch": branch, "commit_sha": head, "tree_sha": tree,
        "profile": profile, "mode": mode, "base_sha": base,
    }
    job = {
        "job_id": job_id, "repository": repository, "branch": branch, "commit_sha": head,
        "base_sha": base, "profile": profile, "status": status,
        "exit_code": 0 if status == "passed" else 1, "superseded_by_job_id": None,
        "summary": {"git_tree_sha": tree} if worker_tree else {},
    }
    monkeypatch.setattr(
        resume.sessions, "validation_generation_context",
        lambda *args, **kwargs: {
            "generation_revision": session["session_revision"],
            "generation_workspace_revision": session["workspace_revision"],
            "current_session_revision": session["session_revision"],
            "current_workspace_revision": session["workspace_revision"],
            "source": "validation_started_event",
            "maintenance_events": [],
        },
    )
    monkeypatch.setattr(
        resume.sessions, "validation_correlations",
        lambda *args, **kwargs: [{**correlation, "session_revision": session["session_revision"]}],
    )
    monkeypatch.setattr(resume.ci_request_store, "get_ci_request", lambda value: request if value == request_id else None)
    monkeypatch.setattr(
        resume.ci_request_store, "get_ci_request_payload",
        lambda value: {
            **payload,
            "expected_session_revision": session["session_revision"],
            "workspace_revision": session["workspace_revision"],
        } if value == request_id else {},
    )
    monkeypatch.setattr(resume, "db_get_job", lambda value: job if value == job_id else None)
    monkeypatch.setattr(
        resume.sessions, "bind_validation_request_worker",
        lambda *args, **kwargs: {"request_id": request_id, "job_id": job_id, "logical_duplicate_count": 0},
    )
    no_new_ci = {"requests": 0, "workers": 0}
    def forbidden_request(*args, **kwargs):
        no_new_ci["requests"] += 1
        pytest.fail("recovery must not start a new CI Request")
    def forbidden_worker(*args, **kwargs):
        no_new_ci["workers"] += 1
        pytest.fail("recovery must not create a second Worker")
    monkeypatch.setattr(resume.dx, "start_validation_request", forbidden_request)
    monkeypatch.setattr(resume.dx, "start_validation_job", forbidden_worker)
    monkeypatch.setattr(resume.dx, "create_or_get_job", forbidden_worker)
    if mode == "full" and attestation_id:
        attestation = {
            "attestation_id": attestation_id, "repository": repository, "tested_commit_sha": head,
            "tested_tree_sha": tree, "base_sha": base, "private_ci_job_id": job_id, "profile": profile,
        }
        monkeypatch.setattr(
            resume.attestation_registry, "find_reusable_attestation_for_job",
            lambda value: {"ok": True, "reusable": True, "attestation": attestation} if value == job_id else {"ok": False, "reusable": False},
        )
    return ws, session, request, payload, job, no_new_ci


def test_p0_02b_full_late_bound_request_recovers_existing_worker_without_new_ci(monkeypatch):
    ws, session, _, _, job, no_new_ci = _production_validation_case(
        monkeypatch, mode="full", repository="frankichen/sxt",
        branch="ai/issue-186-p0-02b-gateway-ingress-tenant-isolation-20260909-r2",
        head="47a4219649318a6ad265e21dc16f2e7506c47862",
        tree="03086bb9ca1ecce64570cd40987b9da2a0cc9936",
        base="973d3b06340e5dd511b9f62fa199fb79aec6d47e",
        request_id="ci_req_ab87697ec3bd4a7399a3d3e8", job_id="7d6709dca1154434",
        attestation_id="1e26fb25-641c-443c-820a-a2a600a08a60",
    )
    expected_attestation = {
        "attestation_id": "1e26fb25-641c-443c-820a-a2a600a08a60",
        "repository": "frankichen/sxt", "tested_commit_sha": session["head_commit_sha"],
        "tested_tree_sha": session["tree_sha"], "base_sha": session["base_commit_sha"],
        "private_ci_job_id": job["job_id"], "profile": "repo-auto-check",
    }
    monkeypatch.setattr(
        resume.dx, "validation_result",
        lambda *args, **kwargs: {"terminal": True, "merge_eligible": True, "attestation": expected_attestation, "failure_pack": None},
    )
    captured = {}
    def transition(*args, **kwargs):
        captured.update(kwargs)
        return {**session, "status": "pr_ready", "session_revision": 8,
                "last_full_ci_job_id": job["job_id"], "last_attestation_id": expected_attestation["attestation_id"]}
    monkeypatch.setattr(resume.sessions, "transition", transition)

    recovered, evidence, blocker = resume._reconcile_transient_validation(session, ws)

    assert blocker is None
    assert recovered["status"] == "pr_ready"
    assert captured["fields"]["last_full_ci_job_id"] == "7d6709dca1154434"
    assert captured["fields"]["last_attestation_id"] == "1e26fb25-641c-443c-820a-a2a600a08a60"
    assert evidence["request"]["request_id"] == "ci_req_ab87697ec3bd4a7399a3d3e8"
    assert no_new_ci == {"requests": 0, "workers": 0}


def test_p0_23a_fast_late_bound_request_recovers_existing_worker_to_active_without_new_ci(monkeypatch):
    ws, session, _, _, job, no_new_ci = _production_validation_case(
        monkeypatch, mode="fast", repository="frankichen/sxt",
        branch="ai/issue-186-p0-23a-p2p-boundary-regression-20260907",
        head="f44f03506bc17643d620545cadadda5d8667612b",
        tree="6998f06fc01cacb2b51c82d1e20790d7b7528daf",
        base="973d3b06340e5dd511b9f62fa199fb79aec6d47e",
        request_id="ci_req_97db061be503489fa02a7ad5", job_id="8593feac57224840",
        worker_tree=False,
    )
    monkeypatch.setattr(
        resume.dx, "validation_result",
        lambda *args, **kwargs: {"terminal": True, "merge_eligible": False, "attestation": None, "failure_pack": None},
    )
    captured = {}
    def transition(*args, **kwargs):
        captured.update(kwargs)
        return {**session, "status": "active", "session_revision": 8, "last_fast_ci_job_id": job["job_id"]}
    monkeypatch.setattr(resume.sessions, "transition", transition)

    recovered, evidence, blocker = resume._reconcile_transient_validation(session, ws)

    assert blocker is None
    assert recovered["status"] == "active"
    assert captured["fields"] == {"last_fast_ci_job_id": "8593feac57224840"}
    assert evidence["tree_evidence"] == "request_tree_worker_pair"
    assert evidence["validation_result"]["merge_eligible"] is False
    assert no_new_ci == {"requests": 0, "workers": 0}


def test_resume_revision_only_workspace_recovery_reconciles_queued_request_and_passed_worker(monkeypatch):
    ws, session, _, _, job, no_new_ci = _production_validation_case(
        monkeypatch, mode="fast", repository="frankichen/sxt",
        branch="ai/issue-186-p0-23a-p2p-boundary-regression-20260907",
        head="f44f03506bc17643d620545cadadda5d8667612b",
        tree="6998f06fc01cacb2b51c82d1e20790d7b7528daf",
        base="973d3b06340e5dd511b9f62fa199fb79aec6d47e",
        request_id="ci_req_97db061be503489fa02a7ad5", job_id="8593feac57224840",
        worker_tree=False,
    )
    session.update({"session_revision": 12, "workspace_revision": 3})
    ws.update({"revision": 4})
    _stub_resume_context(
        monkeypatch, ws=ws, session=session,
        branch_head=session["head_commit_sha"], branch_tree=session["tree_sha"],
    )
    monkeypatch.setattr(
        resume, "_repository_policy",
        lambda repository: {"ok": True, "repository": repository, "policy": {"github": True, "private_ci": True}},
    )
    recovered_session = {**session, "session_revision": 13, "workspace_revision": 4}
    recovery_calls = []

    def recover(service, session_id, session_revision, workspace_revision, expected_head_sha, idempotency_key):
        recovery_calls.append((session_id, session_revision, workspace_revision, expected_head_sha, idempotency_key))
        return {"session": recovered_session, "workspace": ws, "recovered": True}

    monkeypatch.setattr(resume.dx, "recover_stale_session", recover)
    monkeypatch.setattr(
        resume.dx, "validation_result",
        lambda *args, **kwargs: {
            "terminal": True, "merge_eligible": False,
            "attestation": None, "failure_pack": None,
        },
    )
    monkeypatch.setattr(
        resume.sessions, "transition",
        lambda *args, **kwargs: {
            **recovered_session, "status": "active", "session_revision": 14,
            "last_fast_ci_job_id": job["job_id"],
        },
    )

    result = resume.resume_task(
        FakeService(), "frankichen/sxt",
        branch="ai/issue-186-p0-23a-p2p-boundary-regression-20260907",
        expected_session_revision=12,
    )

    assert recovery_calls == [
        (
            session["session_id"], 12, 4, session["head_commit_sha"],
            f"resume:{session['session_id']}:4",
        )
    ]
    assert result["development_session"]["status"] == "active"
    assert result["recovery"]["transient"]["reconciled"] is True
    assert result["recovery"]["transient"]["request"]["request_id"] == "ci_req_97db061be503489fa02a7ad5"
    assert no_new_ci == {"requests": 0, "workers": 0}


def test_request_exists_without_worker_keeps_validation_in_progress_and_starts_nothing(monkeypatch):
    ws, session, request, _, _, no_new_ci = _production_validation_case(
        monkeypatch, mode="fast", repository="owner/repo", branch="ai/resume",
        head=SHA_A, tree=TREE_A, base=SHA_A,
        request_id="ci_req_waiting", job_id="job-not-yet-created",
    )
    request["worker_job_id"] = None
    request["phase"] = "preparing"
    monkeypatch.setattr(resume, "db_get_job", lambda *args: pytest.fail("no Worker should be looked up before Request binds one"))
    monkeypatch.setattr(resume.sessions, "bind_validation_request_worker", lambda *args, **kwargs: pytest.fail("cannot bind before Worker exists"))

    recovered, evidence, blocker = resume._reconcile_transient_validation(session, ws)

    assert recovered is session
    assert blocker == "DEVELOPMENT_SESSION_VALIDATION_IN_PROGRESS"
    assert evidence["validation_in_progress"] is True
    assert no_new_ci == {"requests": 0, "workers": 0}


def test_same_request_legacy_duplicate_placeholders_are_one_logical_correlation(monkeypatch):
    ws, session, request, _, job, _ = _production_validation_case(
        monkeypatch, mode="fast", repository="owner/repo", branch="ai/resume",
        head=SHA_A, tree=TREE_A, base=SHA_A, request_id="ci_req_dup", job_id="job-dup",
    )
    duplicate_rows = [
        {"request_id": "ci_req_dup", "job_id": None, "session_revision": 7, "tree_sha": TREE_A,
         "evidence": {"request_id": "ci_req_dup", "selection": {"complete": True}}},
        {"request_id": "ci_req_dup", "job_id": None, "session_revision": 6, "tree_sha": TREE_A,
         "evidence": {"request_id": "ci_req_dup", "selection": {"complete": True}}},
    ]
    monkeypatch.setattr(resume.sessions, "validation_correlations", lambda *args: duplicate_rows)
    monkeypatch.setattr(
        resume.sessions, "bind_validation_request_worker",
        lambda *args, **kwargs: {"request_id": request["request_id"], "job_id": job["job_id"], "logical_duplicate_count": 1},
    )
    monkeypatch.setattr(resume.dx, "validation_result", lambda *args, **kwargs: {"terminal": True, "merge_eligible": False, "attestation": None, "failure_pack": None})
    monkeypatch.setattr(resume.sessions, "transition", lambda *args, **kwargs: {**session, "status": "active", "session_revision": 8})

    _, evidence, blocker = resume._reconcile_transient_validation(session, ws)

    assert blocker is None
    assert evidence["correlation_backfill"]["logical_duplicate_count"] == 1


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        ("missing_request", "validation_request_not_found"),
        ("request_repository", "validation_request_identity_mismatch"),
        ("request_branch", "validation_request_identity_mismatch"),
        ("request_profile", "validation_request_identity_mismatch"),
        ("request_tree", "validation_request_identity_mismatch"),
        ("request_base", "validation_request_identity_mismatch"),
        ("worker_pair", "validation_request_worker_pair_mismatch"),
        ("worker_repository", "validation_job_identity_mismatch"),
        ("worker_branch", "validation_job_identity_mismatch"),
        ("worker_profile", "validation_job_identity_mismatch"),
        ("worker_base", "validation_job_identity_mismatch"),
        ("worker_tree", "validation_job_identity_mismatch"),
        ("worker_superseded", "validation_job_identity_mismatch"),
    ],
)
def test_request_worker_recovery_identity_matrix_fails_closed(monkeypatch, mutation, expected_reason):
    ws, session, request, payload, job, _ = _production_validation_case(
        monkeypatch, mode="fast", repository="owner/repo", branch="ai/resume",
        head=SHA_A, tree=TREE_A, base=SHA_A, request_id="ci_req_matrix", job_id="job-matrix",
    )
    if mutation == "missing_request":
        monkeypatch.setattr(resume.ci_request_store, "get_ci_request", lambda *args: None)
    elif mutation == "request_repository": request["repository"] = "owner/other"
    elif mutation == "request_branch": request["branch"] = "ai/other"
    elif mutation == "request_profile": request["profile"] = "repo-auto-check"
    elif mutation == "request_tree": request["tree_sha"] = "9" * 40
    elif mutation == "request_base": payload["base_sha"] = SHA_B
    elif mutation == "worker_pair":
        monkeypatch.setattr(
            resume.sessions, "validation_correlations",
            lambda *args: [{"request_id": request["request_id"], "job_id": "job-other", "session_revision": 7,
                           "tree_sha": TREE_A, "evidence": {"request_id": request["request_id"]}},],
        )
    elif mutation == "worker_repository": job["repository"] = "owner/other"
    elif mutation == "worker_branch": job["branch"] = "ai/other"
    elif mutation == "worker_profile": job["profile"] = "repo-auto-check"
    elif mutation == "worker_base": job["base_sha"] = SHA_B
    elif mutation == "worker_tree": job["summary"] = {"git_tree_sha": "8" * 40}
    elif mutation == "worker_superseded": job["superseded_by_job_id"] = "job-newer"

    recovered, evidence, blocker = resume._reconcile_transient_validation(session, ws)

    assert recovered is session
    assert blocker == "DEVELOPMENT_SESSION_RECOVERY_REQUIRED"
    assert evidence["reason"] == expected_reason


def test_full_pass_without_reusable_attestation_fails_closed_without_transition(monkeypatch):
    ws, session, _, _, _, no_new_ci = _production_validation_case(
        monkeypatch, mode="full", repository="owner/repo", branch="ai/resume",
        head=SHA_A, tree=TREE_A, base=SHA_A, request_id="ci_req_no_att", job_id="job-no-att",
    )
    monkeypatch.setattr(
        resume.attestation_registry, "find_reusable_attestation_for_job",
        lambda *args: {"ok": False, "reusable": False, "error_code": "ATTESTATION_NOT_FOUND"},
    )
    monkeypatch.setattr(resume.sessions, "bind_validation_request_worker", lambda *args, **kwargs: pytest.fail("invalid full evidence must not be backfilled as complete"))
    monkeypatch.setattr(resume.sessions, "transition", lambda *args, **kwargs: pytest.fail("invalid full evidence must not change Session fields"))

    recovered, evidence, blocker = resume._reconcile_transient_validation(session, ws)

    assert recovered is session
    assert blocker == "DEVELOPMENT_SESSION_RECOVERY_REQUIRED"
    assert evidence["reason"] == "validation_full_attestation_not_reusable"
    assert no_new_ci == {"requests": 0, "workers": 0}


def test_no_request_id_and_no_strict_job_correlation_fails_closed(monkeypatch):
    ws, session, _, _, _, _ = _production_validation_case(
        monkeypatch, mode="fast", repository="owner/repo", branch="ai/resume",
        head=SHA_A, tree=TREE_A, base=SHA_A, request_id="ci_req_unused", job_id="job-unused",
    )
    monkeypatch.setattr(
        resume.sessions, "validation_correlations",
        lambda *args: [{"request_id": None, "job_id": None, "session_revision": 7, "tree_sha": TREE_A, "evidence": {}}],
    )

    recovered, evidence, blocker = resume._reconcile_transient_validation(session, ws)

    assert recovered is session
    assert blocker == "DEVELOPMENT_SESSION_RECOVERY_REQUIRED"
    assert evidence["reason"] == "validation_request_correlation_missing"


def test_resume_after_fast_correlation_backfill_is_idempotent_and_starts_no_ci(monkeypatch):
    ws = _workspace()
    session = {
        **_ready_session(),
        "workspace_id": ws["workspace_id"],
        "base_commit_sha": ws["base_commit_sha"],
        "status": "active",
        "last_fast_ci_job_id": "job-existing",
    }
    _stub_resume_context(monkeypatch, ws=ws, session=session)
    monkeypatch.setattr(
        resume, "_reconcile_transient_validation",
        lambda *args, **kwargs: pytest.fail("completed correlation must not be reconciled twice"),
    )
    monkeypatch.setattr(
        resume.sessions, "transition",
        lambda *args, **kwargs: pytest.fail("completed correlation must not repeat a Session transition"),
    )
    monkeypatch.setattr(
        resume.dx, "start_validation_request",
        lambda *args, **kwargs: pytest.fail("idempotent resume must not start another CI Request"),
    )
    monkeypatch.setattr(
        resume.dx, "start_validation_job",
        lambda *args, **kwargs: pytest.fail("idempotent resume must not start another Worker"),
    )
    monkeypatch.setattr(
        resume.dx, "create_or_get_job",
        lambda *args, **kwargs: pytest.fail("idempotent resume must not create/reuse another execution"),
    )

    result = resume.resume_task(FakeService(), "owner/repo", branch="ai/resume")

    assert result["development_session"]["status"] == "active"
    assert result["development_session"]["last_fast_ci_job_id"] == "job-existing"


def test_same_head_unowned_historical_worker_is_never_claimed(monkeypatch):
    ws, session, _, _, historical_job, no_new_ci = _production_validation_case(
        monkeypatch, mode="fast", repository="owner/repo", branch="ai/resume",
        head=SHA_A, tree=TREE_A, base=SHA_A, request_id="ci_req_unowned", job_id="job-historical",
    )
    monkeypatch.setattr(
        resume.sessions, "validation_correlations",
        lambda *args: [{
            "request_id": None, "job_id": None, "session_revision": 7,
            "tree_sha": TREE_A, "evidence": {},
        }],
    )
    monkeypatch.setattr(
        resume.ci_request_store, "get_ci_request_by_worker_job_id",
        lambda *args: pytest.fail("no strict persisted job anchor exists for reverse Request lookup"),
    )
    monkeypatch.setattr(
        resume, "db_get_job",
        lambda *args: pytest.fail("same-HEAD historical Worker without Request ownership must not be inspected"),
    )
    monkeypatch.setattr(
        resume, "db_list_jobs",
        lambda **kwargs: [historical_job],
    )

    recovered, evidence, blocker = resume._reconcile_transient_validation(session, ws)

    assert recovered is session
    assert blocker == "DEVELOPMENT_SESSION_RECOVERY_REQUIRED"
    assert evidence["reason"] == "validation_request_correlation_missing"
    assert no_new_ci == {"requests": 0, "workers": 0}
