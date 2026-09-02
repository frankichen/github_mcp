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


def _insert_workspace(*, workspace_id="ws_managed", lease=9999999999.0, revision=1, status="active", head=HEAD):
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
                head, TREE, status, revision, "chatgpt", lease,
                head, "{}", None, 7, now, now,
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


def test_historical_real_squash_expired_workspace_reconciles(tmp_path, monkeypatch):
    real_head = "4f3ec9b426b482352d1b76bc242d7ea34903d7af"
    real_merge = "c400eb191e7698cd466246dc91bb04ba08fcd453"
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "real-squash.db"))
    workspace = _insert_workspace(lease=0, head=real_head)
    assert workspace["status"] == "expired"
    session = sessions.create_session({**workspace, "status": "active"}, status="pr_ready")
    pr = {
        **_merged_pr(),
        "head_sha": real_head,
        "base_sha": real_merge,
        "merge_commit_sha": real_merge,
    }
    merge_result = {
        **_merge_result(),
        "head_sha": real_head,
        "base_head_after": real_merge,
        "merge_commit_sha": real_merge,
    }
    monkeypatch.setattr(managed_merge, "_current_base_sha", lambda *args, **kwargs: real_merge)

    result = managed_merge.finalize_managed_pr_merge(
        FakeService(), REPO, 7, real_head, BASE_BRANCH, merge_result,
        pull_request=pr,
        expected_workspace_id=workspace["workspace_id"],
        expected_session_id=session["session_id"],
        expected_workspace_revision=workspace["revision"],
        expected_session_revision=session["session_revision"],
        allow_no_context=False,
    )

    assert real_head != real_merge
    assert result["development_session"]["status"] == "merged"
    assert result["workspace"]["status"] == "closed"
    assert result["evidence"]["merge_commit_sha"] == real_merge


def test_historical_merge_commit_ancestor_of_advanced_main_reconciles(tmp_path, monkeypatch):
    real_head = "4f3ec9b426b482352d1b76bc242d7ea34903d7af"
    real_merge = "c400eb191e7698cd466246dc91bb04ba08fcd453"
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "advanced-main.db"))
    workspace = _insert_workspace(head=real_head)
    session = sessions.create_session(workspace, status="pr_ready")
    pr = {**_merged_pr(), "head_sha": real_head, "merge_commit_sha": real_merge}
    merge_result = {**_merge_result(), "head_sha": real_head, "merge_commit_sha": real_merge}

    class AdvancedMainRepo:
        def compare(self, base, head):
            assert (base, head) == (real_merge, CURRENT_BASE)
            return SimpleNamespace(
                merge_base_commit=SimpleNamespace(sha=real_merge),
                ahead_by=2,
                behind_by=0,
            )

    monkeypatch.setattr(managed_merge.mygithub12, "_service_repo", lambda *args, **kwargs: AdvancedMainRepo())

    result = managed_merge.finalize_managed_pr_merge(
        FakeService(), REPO, 7, real_head, BASE_BRANCH, merge_result,
        pull_request=pr,
        expected_workspace_id=workspace["workspace_id"],
        expected_session_id=session["session_id"],
        expected_workspace_revision=workspace["revision"],
        expected_session_revision=session["session_revision"],
        allow_no_context=False,
    )

    assert result["development_session"]["status"] == "merged"
    assert result["workspace"]["status"] == "closed"
    assert result["evidence"]["ancestry"]["method"] == "github_compare"
    assert result["evidence"]["ancestry"]["verified"] is True


def test_merge_evidence_missing_merge_commit_sha_fails_stop():
    pr = _merged_pr(); pr["merge_commit_sha"] = None
    merge_result = _merge_result(); merge_result["merge_commit_sha"] = None

    with pytest.raises(managed_merge.MyGithub12Error) as exc:
        managed_merge._verify_merge_evidence(
            FakeService(), REPO, 7, HEAD, BASE_BRANCH, pr, merge_result,
        )

    assert exc.value.code == "MERGE_EVIDENCE_INCOMPLETE"


def test_merge_evidence_short_merge_commit_sha_fails_stop():
    pr = _merged_pr(); pr["merge_commit_sha"] = "short"
    merge_result = _merge_result(); merge_result["merge_commit_sha"] = None

    with pytest.raises(managed_merge.MyGithub12Error) as exc:
        managed_merge._verify_merge_evidence(
            FakeService(), REPO, 7, HEAD, BASE_BRANCH, pr, merge_result,
        )

    assert exc.value.code == "MERGE_EVIDENCE_INCOMPLETE"


def test_merge_commit_not_in_current_base_fails_stop():
    pr = _merged_pr(); pr["merge_commit_sha"] = "e" * 40
    merge_result = _merge_result(); merge_result["merge_commit_sha"] = None

    with pytest.raises(managed_merge.MyGithub12Error) as exc:
        managed_merge._verify_merge_evidence(
            FakeService(), REPO, 7, HEAD, BASE_BRANCH, pr, merge_result,
        )

    assert exc.value.code == "MERGE_EVIDENCE_UNVERIFIED"


def test_merged_pr_head_must_match_managed_session_head():
    pr = _merged_pr(); pr["head_sha"] = "e" * 40

    with pytest.raises(managed_merge.MyGithub12Error) as exc:
        managed_merge._verify_merge_evidence(
            FakeService(), REPO, 7, HEAD, BASE_BRANCH, pr, _merge_result(),
        )

    assert exc.value.code == "MANAGED_PR_IDENTITY_MISMATCH"
