import hashlib
import json
from types import SimpleNamespace

import pytest

from app import mygithub10, mygithub12
from app.exceptions import WriteVerifyError
from app.github_write_verify import WriteVerificationError, post_write_verify
from app.services.github_service import GitHubService


OLD = "a" * 40
NEW = "b" * 40
TREE = "c" * 40
WRONG_TREE = "d" * 40
BLOB = "e" * 40


class VerifyClient:
    def __init__(self, *, branch=NEW, commit=NEW, tree=TREE, tree_object=TREE, blob=BLOB):
        self.branch = branch
        self.commit = commit
        self.tree = tree
        self.tree_object = tree_object
        self.blob = blob
        self.calls = []

    def get_branch_head_fresh(self, repository, branch):
        self.calls.append(("branch", repository, branch))
        return self.branch

    def get_commit_state_fresh(self, repository, sha):
        self.calls.append(("commit", repository, sha))
        if self.commit is None:
            return None
        return {"commit_sha": self.commit, "tree_sha": self.tree}

    def get_tree_sha_fresh(self, repository, sha):
        self.calls.append(("tree", repository, sha))
        return self.tree_object

    def get_file_sha_fresh(self, repository, path, ref):
        self.calls.append(("path", repository, path, ref))
        return self.blob


def test_01_normal_post_write_verify_confirms_branch_commit_tree_and_path():
    client = VerifyClient()
    result = post_write_verify(
        client, "owner/repo", "ai/test", OLD, NEW, TREE, {"a.txt": BLOB},
        attempts=1, retry_delay_seconds=0,
    )
    assert result["write_verified"] is True
    assert result["verified_branch_head_sha"] == NEW
    assert result["verified_commit_sha"] == NEW
    assert result["verified_tree_sha"] == TREE
    assert result["verified_paths"] == [{"path": "a.txt", "blob_sha": BLOB}]
    assert [call[0] for call in client.calls] == ["branch", "commit", "tree", "path"]


def test_03_ref_update_success_but_branch_still_old_is_not_success():
    client = VerifyClient(branch=OLD)
    with pytest.raises(WriteVerificationError) as exc:
        post_write_verify(
            client, "owner/repo", "ai/test", OLD, NEW, TREE, {"a.txt": BLOB},
            attempts=1, retry_delay_seconds=0,
        )
    assert exc.value.details["failed_stage"] == "branch_ref_readback"
    assert exc.value.details["observed_branch_head"] == OLD


def test_04_branch_is_new_but_commit_readback_missing_is_not_success():
    client = VerifyClient(commit=None)
    with pytest.raises(WriteVerificationError) as exc:
        post_write_verify(
            client, "owner/repo", "ai/test", OLD, NEW, TREE, {"a.txt": BLOB},
            attempts=1, retry_delay_seconds=0,
        )
    assert exc.value.details["failed_stage"] == "commit_readback"
    assert exc.value.details["observed_branch_head"] == NEW


def test_05_commit_tree_mismatch_is_not_success():
    client = VerifyClient(tree=WRONG_TREE)
    with pytest.raises(WriteVerificationError) as exc:
        post_write_verify(
            client, "owner/repo", "ai/test", OLD, NEW, TREE, {"a.txt": BLOB},
            attempts=1, retry_delay_seconds=0,
        )
    assert exc.value.details["failed_stage"] == "tree_readback"
    assert exc.value.details["observed_tree_sha"] == WRONG_TREE


class CommitClient:
    def __init__(self, mode="success"):
        self.mode = mode
        self.branch_head = OLD
        self.reset_calls = []
        self.created_blob = SimpleNamespace(sha=BLOB)
        self.created_tree = SimpleNamespace(sha=TREE)
        self.created_commit = SimpleNamespace(sha=NEW)

    def get_default_branch(self, repository):
        return "main"

    def get_branch(self, repository, branch):
        return SimpleNamespace(commit=SimpleNamespace(sha=OLD))

    def get_file_sha(self, repository, path, ref):
        return None

    def create_blob(self, repository, content):
        return self.created_blob

    def get_git_tree(self, repository, sha):
        return SimpleNamespace(sha="0" * 40)

    def create_git_tree(self, repository, elements, base_tree_sha=""):
        return self.created_tree

    def create_commit(self, repository, message, tree_sha, parent_shas):
        return self.created_commit

    def update_ref(self, repository, ref_name, sha, force=False):
        if self.mode == "raise_no_move":
            raise RuntimeError("simulated lost ref update")
        if self.mode != "phantom_success":
            self.branch_head = sha
        return SimpleNamespace(object=SimpleNamespace(sha=sha))

    def get_branch_head_fresh(self, repository, branch):
        return self.branch_head

    def get_commit_state_fresh(self, repository, sha):
        if self.mode == "missing_commit":
            return None
        tree = WRONG_TREE if self.mode == "wrong_tree" else TREE
        return {"commit_sha": NEW, "tree_sha": tree}

    def get_tree_sha_fresh(self, repository, sha):
        return TREE

    def get_file_sha_fresh(self, repository, path, ref):
        return BLOB

    def get_file(self, repository, path, ref=""):
        return "new\n", BLOB, 4


def _commit_request():
    file_op = SimpleNamespace(path="a.txt", operation="upsert", content="new\n", expected_sha=None)
    return SimpleNamespace(
        repository="owner/repo",
        branch="ai/test",
        base_branch="main",
        create_branch_if_missing=False,
        commit_message="write",
        expected_head_sha=OLD,
        files=[file_op],
        pull_request=None,
    )


def _service(mode="success"):
    service = GitHubService(CommitClient(mode))
    service._check_repository_allowed = lambda repository: None
    service._check_default_branch_write = lambda repository, branch: None
    return service


def test_02_commit_created_but_ref_update_not_applied_returns_verify_failure():
    service = _service("raise_no_move")
    with pytest.raises(WriteVerifyError) as exc:
        service.commit_files(_commit_request())
    assert exc.value.error == "write_verify_failed"
    assert exc.value.details["failed_stage"] == "branch_ref_update"
    assert exc.value.details["observed_branch_head"] == OLD


def test_10_commit_github_files_backend_uses_shared_durable_verify():
    result = _service("success").commit_files(_commit_request())
    assert result["success"] is True
    assert result["write_verified"] is True
    assert result["previous_head_sha"] == OLD
    assert result["verified_branch_head_sha"] == NEW
    assert result["verified_commit_sha"] == NEW
    assert result["verified_tree_sha"] == TREE


def test_service_phantom_ref_success_is_rejected():
    service = _service("phantom_success")
    with pytest.raises(WriteVerifyError) as exc:
        service.commit_files(_commit_request())
    assert exc.value.details["failed_stage"] == "branch_ref_readback"


def _seed_workspace(tmp_path, monkeypatch, revision=1):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "workspace.db"))
    mygithub12.init_db()
    now = mygithub12._now()
    with mygithub12._db() as db:
        db.execute(
            "INSERT INTO workspaces VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "ws_verify", "owner/repo", "ai/test", "main", OLD,
                OLD, "f" * 40, "active", revision, "test", now + 600,
                OLD, "{}", None, None, now, now,
            ),
        )


def _evidence():
    return {
        "write_verified": True,
        "repository": "owner/repo",
        "branch": "ai/test",
        "previous_head_sha": OLD,
        "commit_sha": NEW,
        "tree_sha": TREE,
        "verified_branch_head_sha": NEW,
        "verified_commit_sha": NEW,
        "verified_tree_sha": TREE,
    }


def test_01_verified_write_can_advance_workspace_only_after_github_evidence(tmp_path, monkeypatch):
    _seed_workspace(tmp_path, monkeypatch)
    result = mygithub12.workspace_write_complete("ws_verify", 1, NEW, TREE, _evidence())
    assert result["head_sha"] == NEW
    assert result["tree_sha"] == TREE
    assert result["revision"] == 2


def test_workspace_rejects_unverified_local_commit_object(tmp_path, monkeypatch):
    _seed_workspace(tmp_path, monkeypatch)
    with pytest.raises(mygithub12.MyGithub12Error) as exc:
        mygithub12.workspace_write_complete("ws_verify", 1, NEW, TREE, {})
    assert exc.value.code == "WRITE_VERIFY_FAILED"
    with mygithub12._db() as db:
        row = db.execute("SELECT head_sha FROM workspaces WHERE workspace_id=?", ("ws_verify",)).fetchone()
    assert row[0] == OLD


def test_06_workspace_cas_failure_after_github_move_returns_recovery_state_without_reset(tmp_path, monkeypatch):
    _seed_workspace(tmp_path, monkeypatch, revision=2)
    with pytest.raises(mygithub12.MyGithub12Error) as exc:
        mygithub12.workspace_write_complete("ws_verify", 1, NEW, TREE, _evidence())
    assert exc.value.code == "WORKSPACE_REVISION_MISMATCH"
    assert exc.value.details["github_write_verified"] is True
    assert exc.value.details["github_branch_head"] == NEW
    assert exc.value.details["recovery_required"] is True
    assert exc.value.details["recommended_action"] == "refresh_workspace"


def test_07_failed_verify_idempotency_never_replays_fake_success(tmp_path, monkeypatch):
    monkeypatch.setattr(mygithub10.settings, "IDEMPOTENCY_DB_PATH", str(tmp_path / "ops.db"))
    request = {"tool_name": "apply_github_patch", "repository": "owner/repo", "branch": "ai/test", "expected_head_sha": OLD}
    operation_id, replay = mygithub10._idempotent_start("apply_github_patch", "same-key", request)
    assert replay is None
    mygithub10._idempotent_finish(
        operation_id,
        "failed",
        NEW,
        "WRITE_VERIFY_FAILED",
        {"failed_stage": "branch_ref_readback", "error": {"code": "WRITE_VERIFY_FAILED", "details": {"new_commit_sha": NEW, "expected_previous_head": OLD}}},
    )
    with pytest.raises(mygithub10.MyGithub10Error) as exc:
        mygithub10._idempotent_start("apply_github_patch", "same-key", request)
    assert exc.value.code == "WRITE_VERIFY_FAILED"


class ReadRepo:
    default_branch = "main"

    def get_commit(self, ref):
        return SimpleNamespace(sha=OLD)

    def get_contents(self, path, ref=None):
        if path == "missing.txt":
            err = RuntimeError("missing")
            err.status = 404
            raise err
        data = b"old\n"
        return SimpleNamespace(sha="1" * 40, size=len(data))

    def get_git_blob(self, sha):
        import base64
        return SimpleNamespace(encoding="base64", content=base64.b64encode(b"old\n").decode())


class ReadService:
    def __init__(self):
        self.client = SimpleNamespace(_pygithub=SimpleNamespace(get_repo=lambda repository: ReadRepo()))

    def _check_repository_allowed(self, repository):
        return None

    def _check_default_branch_write(self, repository, branch):
        return None


def _verified_shared_result(path="a.txt"):
    return {
        "repository": "owner/repo", "branch": "ai/test",
        "old_head_sha": OLD, "new_head_sha": NEW, "commit_sha": NEW, "tree_sha": TREE,
        "changed_files": [{"path": path}], "write_verified": True,
        "previous_head_sha": OLD, "verified_branch_head_sha": NEW,
        "verified_commit_sha": NEW, "verified_tree_sha": TREE,
    }


def test_08_apply_github_patch_stops_at_git_verified_until_outer_finalize(tmp_path, monkeypatch):
    monkeypatch.setattr(mygithub10.settings, "IDEMPOTENCY_DB_PATH", str(tmp_path / "patch.db"))
    calls = []
    monkeypatch.setattr(mygithub10, "_commit_files", lambda *args: calls.append(args) or _verified_shared_result("a.txt"))
    patch = "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-old\n+new\n"
    result = mygithub10.apply_patch(ReadService(), "owner/repo", "ai/test", OLD, json.dumps({"a.txt": "1" * 40}), patch, "patch", False, "patch-key")
    assert calls
    row = mygithub10._idempotent_existing_by_operation(result["_operation_id"])
    assert row["status"] == "git_verified"
    assert row["result_commit_sha"] == NEW


def test_09_edit_github_file_ranges_stops_at_git_verified_until_outer_finalize(tmp_path, monkeypatch):
    monkeypatch.setattr(mygithub10.settings, "IDEMPOTENCY_DB_PATH", str(tmp_path / "range.db"))
    monkeypatch.setattr(mygithub10, "_commit_files", lambda *args: _verified_shared_result("a.txt"))
    ops = [{
        "path": "a.txt", "operation": "replace", "start_line": 1, "end_line": 1,
        "expected_blob_sha": "1" * 40, "expected_old_text": "old\n", "replacement_text": "new\n",
    }]
    result = mygithub10.edit_ranges(ReadService(), "owner/repo", "ai/test", OLD, json.dumps(ops), "range", False, "range-key")
    row = mygithub10._idempotent_existing_by_operation(result["_operation_id"])
    assert row["status"] == "git_verified"


def test_11_commit_github_uploaded_files_keeps_upload_until_outer_finalize(tmp_path, monkeypatch):
    monkeypatch.setattr(mygithub10, "_UPLOAD_ROOT", tmp_path / "uploads")
    monkeypatch.setattr(mygithub10.settings, "IDEMPOTENCY_DB_PATH", str(tmp_path / "upload.db"))
    upload = mygithub10.begin_upload()
    data = b"new\n"
    digest = hashlib.sha256(data).hexdigest()
    mygithub10.append_upload(upload["upload_id"], 0, data, digest)
    mygithub10.finalize_upload(upload["upload_id"], len(data), digest)
    monkeypatch.setattr(mygithub10, "_commit_files", lambda *args: _verified_shared_result("upload.txt"))
    result = mygithub10.commit_upload(ReadService(), "owner/repo", "ai/test", OLD, "upload.txt", "", upload["upload_id"], "upload", "upload-key")
    row = mygithub10._idempotent_existing_by_operation(result["_operation_id"])
    assert row["status"] == "git_verified"
    assert result["_cleanup_upload_id"] == upload["upload_id"]
    assert mygithub10._upload_paths(upload["upload_id"])[0].exists()
