import pytest

from app import development_resume as resume
from app import development_session_store as sessions

SHA_A = "a" * 40
SHA_B = "b" * 40
TREE_A = "1" * 40


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
        "repository": "owner/repo", "branch": "ai/resume",
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
        "profile": "repo-fast-check" if mode == "fast" else "repo-auto-check",
        "status": status,
        "exit_code": 0 if status == "passed" else 1,
        "summary": {"git_tree_sha": tree},
    }


def _stub_transient_recovery(monkeypatch, *, mode="fast", status="passed", correlations=1):
    ws = _workspace()
    phase = "validating_fast" if mode == "fast" else "validating_full"
    session = {**_ready_session(), "status": phase}
    _stub_resume_context(monkeypatch, ws=ws, session=session)
    job = _validation_job(mode=mode, status=status)
    rows = [
        {
            "job_id": job["job_id"] if i == 0 else f"job-other-{i}",
            "session_revision": session["session_revision"],
            "tree_sha": session["tree_sha"],
            "evidence": {"selection": {"complete": True, "changed_paths": ["x.py"]}},
        }
        for i in range(correlations)
    ]
    monkeypatch.setattr(resume.sessions, "validation_correlations", lambda *args: rows)
    monkeypatch.setattr(resume, "db_get_job", lambda job_id: job if job_id == job["job_id"] else {**job, "job_id": job_id})
    return ws, session, job


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
    assert result["next_allowed_actions"] == ["wait_private_ci_job", "resume_development_task"]
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


def test_resume_transient_recovery_preserves_session_revision_cas(monkeypatch):
    _stub_transient_recovery(monkeypatch)

    with pytest.raises(resume.MyGithub12Error) as exc:
        resume.resume_task(FakeService(), "owner/repo", branch="ai/resume", expected_session_revision=3)

    assert exc.value.code == "DEVELOPMENT_SESSION_REVISION_MISMATCH"


def test_resume_recovers_restart_after_nonterminal_observation_with_legacy_fast_tree(monkeypatch):
    _, session, job = _stub_transient_recovery(monkeypatch)
    session["last_fast_ci_job_id"] = job["job_id"]
    job["summary"] = {}
    monkeypatch.setattr(
        resume.sessions,
        "validation_correlations",
        lambda *args: [{
            "job_id": job["job_id"], "session_revision": session["session_revision"] - 1,
            "tree_sha": "", "evidence": {"selection": {"complete": True}},
        }],
    )
    monkeypatch.setattr(resume.dx, "validation_result", lambda *args, **kwargs: {"terminal": True, "merge_eligible": False, "attestation": None, "failure_pack": None})
    monkeypatch.setattr(resume.sessions, "transition", lambda *args, **kwargs: {**session, "status": "active", "session_revision": session["session_revision"] + 1})

    result = resume.resume_task(FakeService(), "owner/repo", branch="ai/resume")

    assert result["development_session"]["status"] == "active"
    assert result["recovery"]["transient"]["correlation_source"] == "session_last_observed_job"
    assert result["recovery"]["transient"]["tree_evidence"] == "exact_immutable_commit_identity"


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
    ws = _workspace(status="active")
    session = {
        "session_id": "dev_resume", "workspace_id": ws["workspace_id"],
        "repository": "owner/repo", "branch": "ai/resume", "base_branch": "main",
        "status": "pr_ready", "session_revision": 4,
        "workspace_revision": ws["revision"], "head_commit_sha": SHA_A,
        "tree_sha": TREE_A, "lease_expires_at": ws["lease_expires_at"],
        "pull_number": 7, "last_fast_ci_job_id": None,
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
    merged_session = {**session, "status": "merged", "session_revision": 5, "workspace_revision": 3, "lease_valid": False}
    _stub_resume_context(monkeypatch, ws=ws, session=session, pr=pr)

    def finalize(*args, **kwargs):
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
    assert result["blockers"] == []
    assert result["next_allowed_actions"] == ["managed_merge_finalized"]
