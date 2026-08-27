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

# DX2_RESUME_TEST_CHUNK_02
