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
    monkeypatch.setattr(resume, "_repository_policy", lambda repository: {"ok": True})
    monkeypatch.setattr(resume.github_utils, "get_github_branch", lambda repository, branch, base_branch="": {"ok": True, "repository": repository, "branch": branch, "commit_sha": SHA_A})
    monkeypatch.setattr(resume.mygithub12, "resolve_identity", lambda service, repository, commit_sha="", ref="": {"repository": repository, "commit_sha": commit_sha or SHA_A, "tree_sha": TREE_A})
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
    monkeypatch.setattr(resume.github_utils, "get_github_pull_request", lambda repository, pull_number: {"ok": True, "pull_number": pull_number, "head_branch": "ai/other", "head_sha": SHA_A})
    with pytest.raises(resume.MyGithub12Error) as exc:
        resume.resume_task(FakeService(), "owner/repo", branch="ai/resume", pull_number=12)
    assert exc.value.code == "DEVELOPMENT_RESUME_INPUT_MISMATCH"
