from types import SimpleNamespace

import pytest

from app import development_managed_merge as managed_merge
from app import development_session_store as sessions
from app import mygithub12

REPO = "owner/repo"
BRANCH = "ai/managed"
BASE_BRANCH = "main"
BASE = "b" * 40
HEAD = "a" * 40
TREE = "1" * 40
MERGE = "c" * 40
CURRENT_BASE = "d" * 40


class FakeRepo:
    def compare(self, base, head):
        if (base, head) != (MERGE, CURRENT_BASE):
            raise AssertionError((base, head))
        return SimpleNamespace(
            merge_base_commit=SimpleNamespace(sha=MERGE),
            ahead_by=1,
            behind_by=0,
        )


class FakeGitHub:
    def get_repo(self, repository):
        assert repository == REPO
        return FakeRepo()


class FakeClient:
    _pygithub = FakeGitHub()

    def get_branch(self, repository, branch):
        assert repository == REPO
        assert branch == BASE_BRANCH
        return SimpleNamespace(commit=SimpleNamespace(sha=CURRENT_BASE))


class FakeService:
    client = FakeClient()

    def _check_repository_allowed(self, repository):
        assert repository == REPO


def _insert_workspace(*, workspace_id="ws_managed", lease=9999999999.0, revision=1, status="active"):
    mygithub12.init_db()
    with mygithub12._LOCK, mygithub12._db() as db:
        now = mygithub12._now()
        db.execute(
            """INSERT INTO workspaces(
                 workspace_id,repository,branch,base_branch,base_commit_sha,
                 head_sha,tree_sha,status,revision,owner,lease_expires_at,
                 index_commit_sha,scope_json,drift_reason,pr_number,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                workspace_id, REPO, BRANCH, BASE_BRANCH, BASE,
                HEAD, TREE, status, revision, "chatgpt", lease,
                HEAD, "{}", None, 7, now, now,
            ),
        )
    return mygithub12.get_workspace(FakeService(), workspace_id)


def _merged_pr():
    return {
        "ok": True,
        "repository": REPO,
        "pull_number": 7,
        "merged": True,
        "state": "closed",
        "head_branch": BRANCH,
        "head_sha": HEAD,
        "base_branch": BASE_BRANCH,
        "base_sha": CURRENT_BASE,
        "merge_commit_sha": MERGE,
        "merged_at": "2026-09-02T00:00:00+00:00",
    }


def _merge_result():
    return {
        "ok": True,
        "merged": True,
        "repository": REPO,
        "pull_number": 7,
        "head_branch": BRANCH,
        "head_sha": HEAD,
        "base_branch": BASE_BRANCH,
        "base_head_after": CURRENT_BASE,
        "merge_commit_sha": MERGE,
    }


def test_public_merge_managed_pr_finalizes_session_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "managed.db"))
    workspace = _insert_workspace()
    session = sessions.create_session(workspace, status="pr_ready")

    result = managed_merge.finalize_managed_pr_merge(
        FakeService(), REPO, 7, HEAD, BASE_BRANCH, _merge_result(),
        pull_request=_merged_pr(),
        expected_workspace_id=workspace["workspace_id"],
        expected_session_id=session["session_id"],
        expected_workspace_revision=workspace["revision"],
        expected_session_revision=session["session_revision"],
        allow_no_context=False,
    )

    assert result["ok"] is True
    assert result["status"] == "finalized"
    assert result["development_session"]["status"] == "merged"
    assert result["workspace"]["status"] == "closed"
    assert result["workspace"]["lease_expires_at"] == 0
    assert result["workspace"]["index_commit_sha"] is None
    assert result["evidence"]["ancestry"]["verified"] is True


def test_public_merge_unmanaged_pr_is_not_applicable(tmp_path, monkeypatch):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "unmanaged.db"))
    mygithub12.init_db(); sessions.init_session_db()
    pr = _merged_pr(); pr["merge_commit_sha"] = CURRENT_BASE
    merge_result = _merge_result(); merge_result["merge_commit_sha"] = CURRENT_BASE

    result = managed_merge.finalize_managed_pr_merge(
        FakeService(), REPO, 7, HEAD, BASE_BRANCH, merge_result,
        pull_request=pr,
        allow_no_context=True,
    )

    assert result["ok"] is True
    assert result["status"] == "not_applicable"
    assert result["managed"] is False


def test_ambiguous_managed_workspace_fails_closed(monkeypatch):
    workspace = {
        "workspace_id": "ws1", "repository": REPO, "branch": BRANCH,
        "base_branch": BASE_BRANCH, "head_sha": HEAD, "tree_sha": TREE,
        "pr_number": 7, "revision": 1,
    }
    monkeypatch.setattr(managed_merge, "_workspace_candidates", lambda *args, **kwargs: [workspace, {**workspace, "workspace_id": "ws2"}])
    monkeypatch.setattr(managed_merge, "_sessions_for_pr", lambda *args, **kwargs: [])

    with pytest.raises(managed_merge.MyGithub12Error) as exc:
        managed_merge.preflight_managed_pr_context(
            FakeService(), REPO, 7, HEAD, BASE_BRANCH, pull_request={**_merged_pr(), "merged": False, "state": "open"}
        )

    assert exc.value.code == "MANAGED_PR_CONTEXT_AMBIGUOUS"


def test_finalization_revision_mismatch_after_merge_needs_recovery(tmp_path, monkeypatch):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "revision.db"))
    workspace = _insert_workspace()
    session = sessions.create_session(workspace, status="pr_ready")

    with pytest.raises(managed_merge.MyGithub12Error) as exc:
        managed_merge.finalize_managed_pr_merge(
            FakeService(), REPO, 7, HEAD, BASE_BRANCH, _merge_result(),
            pull_request=_merged_pr(),
            expected_workspace_id=workspace["workspace_id"],
            expected_session_id=session["session_id"],
            expected_workspace_revision=workspace["revision"] + 1,
            expected_session_revision=session["session_revision"],
            allow_no_context=False,
        )

    assert exc.value.code == "WORKSPACE_REVISION_MISMATCH"


def test_historical_expired_lease_workspace_can_reconcile(tmp_path, monkeypatch):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "expired.db"))
    workspace = _insert_workspace(lease=0)
    assert workspace["status"] == "expired"
    session = sessions.create_session({**workspace, "status": "active"}, status="pr_ready")

    result = managed_merge.finalize_managed_pr_merge(
        FakeService(), REPO, 7, HEAD, BASE_BRANCH, _merge_result(),
        pull_request=_merged_pr(),
        expected_workspace_id=workspace["workspace_id"],
        expected_session_id=session["session_id"],
        expected_workspace_revision=workspace["revision"],
        expected_session_revision=session["session_revision"],
        allow_no_context=False,
    )

    assert result["status"] == "finalized"
    assert result["development_session"]["status"] == "merged"
    assert result["workspace"]["status"] == "closed"


def test_historical_reconciliation_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "idempotent.db"))
    workspace = _insert_workspace()
    session = sessions.create_session(workspace, status="pr_ready")
    first = managed_merge.finalize_managed_pr_merge(
        FakeService(), REPO, 7, HEAD, BASE_BRANCH, _merge_result(),
        pull_request=_merged_pr(),
        expected_workspace_id=workspace["workspace_id"],
        expected_session_id=session["session_id"],
        expected_workspace_revision=workspace["revision"],
        expected_session_revision=session["session_revision"],
        allow_no_context=False,
    )
    second = managed_merge.finalize_managed_pr_merge(
        FakeService(), REPO, 7, HEAD, BASE_BRANCH, _merge_result(),
        pull_request=_merged_pr(),
        expected_workspace_id=workspace["workspace_id"],
        expected_session_id=session["session_id"],
        expected_workspace_revision=first["workspace"]["revision"],
        expected_session_revision=first["development_session"]["session_revision"],
        allow_no_context=False,
    )

    assert second["status"] == "already_finalized"
    assert second["development_session"]["session_revision"] == first["development_session"]["session_revision"]
    assert second["workspace"]["revision"] == first["workspace"]["revision"]
