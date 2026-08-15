import json

import pytest

from app import mygithub10, mygithub12
from app import mcp_server


OLD = "a" * 40
NEW = "b" * 40
TREE = "c" * 40
BLOB = "d" * 40


def verified_result(path="a.txt"):
    return {
        "ok": True,
        "repository": "owner/allowed-repo",
        "branch": "ai/test",
        "old_head_sha": OLD,
        "previous_head_sha": OLD,
        "new_head_sha": NEW,
        "commit_sha": NEW,
        "tree_sha": TREE,
        "changed_files": [{"path": path, "operation": "modify", "new_blob_sha": BLOB}],
        "write_verified": True,
        "verified_branch_head_sha": NEW,
        "verified_commit_sha": NEW,
        "verified_tree_sha": TREE,
        "verified_paths": [{"path": path, "blob_sha": BLOB}],
    }


def make_git_verified(tool_name, key, request, result):
    operation_id, replay = mygithub10._idempotent_start(tool_name, key, request)
    assert replay is None
    mygithub10._idempotent_mark_git_verified(operation_id, result)
    output = dict(result)
    output["_operation_id"] = operation_id
    return output


@pytest.mark.asyncio
async def test_08_apply_github_patch_mcp_only_returns_after_success_verified(tmp_path, monkeypatch):
    monkeypatch.setattr(mygithub10.settings, "IDEMPOTENCY_DB_PATH", str(tmp_path / "apply.db"))
    monkeypatch.setattr(mygithub12, "workspace_write_preflight", lambda *args, **kwargs: None)

    def fake_apply(*args, **kwargs):
        result = verified_result()
        return make_git_verified(
            "apply_github_patch", "apply-key",
            {"tool_name": "apply_github_patch", "repository": "owner/allowed-repo", "branch": "ai/test", "expected_head_sha": OLD},
            result,
        )

    monkeypatch.setattr(mygithub10, "apply_patch", fake_apply)
    raw = await mcp_server.apply_github_patch(
        "owner/allowed-repo", "ai/test", OLD, "{}",
        "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-old\n+new\n", "write", False, "apply-key",
    )
    data = json.loads(raw)
    assert data["write_verified"] is True
    assert data["operation_id"]
    assert mygithub10._idempotent_existing("apply-key")["status"] == "success_verified"


@pytest.mark.asyncio
async def test_09_edit_github_file_ranges_mcp_only_returns_after_success_verified(tmp_path, monkeypatch):
    monkeypatch.setattr(mygithub10.settings, "IDEMPOTENCY_DB_PATH", str(tmp_path / "range.db"))
    monkeypatch.setattr(mygithub12, "workspace_write_preflight", lambda *args, **kwargs: None)

    def fake_edit(*args, **kwargs):
        result = verified_result()
        return make_git_verified(
            "edit_github_file_ranges", "range-key",
            {"tool_name": "edit_github_file_ranges", "repository": "owner/allowed-repo", "branch": "ai/test", "expected_head_sha": OLD},
            result,
        )

    monkeypatch.setattr(mygithub10, "edit_ranges", fake_edit)
    raw = await mcp_server.edit_github_file_ranges(
        "owner/allowed-repo", "ai/test", OLD,
        json.dumps([{"path": "a.txt", "operation": "replace", "start_line": 1, "end_line": 1, "expected_blob_sha": BLOB, "expected_old_text": "old\n", "replacement_text": "new\n"}]),
        "write", False, "range-key",
    )
    data = json.loads(raw)
    assert data["write_verified"] is True
    assert mygithub10._idempotent_existing("range-key")["status"] == "success_verified"


@pytest.mark.asyncio
async def test_11_commit_github_uploaded_files_mcp_only_returns_after_success_verified(tmp_path, monkeypatch):
    monkeypatch.setattr(mygithub10.settings, "IDEMPOTENCY_DB_PATH", str(tmp_path / "upload.db"))
    monkeypatch.setattr(mygithub12, "workspace_write_preflight", lambda *args, **kwargs: None)
    monkeypatch.setattr(mygithub10, "abort_upload", lambda upload_id: {"ok": True, "upload_id": upload_id})

    def fake_upload(*args, **kwargs):
        result = verified_result("upload.txt")
        result["upload_id"] = "upload-test"
        output = make_git_verified(
            "commit_github_uploaded_files", "upload-key",
            {"tool_name": "commit_github_uploaded_files", "repository": "owner/allowed-repo", "branch": "ai/test", "expected_head_sha": OLD},
            result,
        )
        output["_cleanup_upload_id"] = "upload-test"
        return output

    monkeypatch.setattr(mygithub10, "commit_upload", fake_upload)
    raw = await mcp_server.commit_github_uploaded_files(
        "owner/allowed-repo", "ai/test", OLD, "upload.txt", "", "upload-test", "write", "upload-key",
    )
    data = json.loads(raw)
    assert data["write_verified"] is True
    assert mygithub10._idempotent_existing("upload-key")["status"] == "success_verified"


@pytest.mark.asyncio
async def test_10_commit_github_files_mcp_has_audit_and_success_verified(tmp_path, monkeypatch):
    monkeypatch.setattr(mygithub10.settings, "IDEMPOTENCY_DB_PATH", str(tmp_path / "files.db"))
    monkeypatch.setattr(mygithub12, "workspace_write_preflight", lambda *args, **kwargs: None)
    monkeypatch.setattr(mcp_server._service, "commit_files", lambda request: {"success": True, **verified_result()})
    raw = await mcp_server.commit_github_files(
        "owner/allowed-repo", "ai/test", "write",
        json.dumps([{"path": "a.txt", "operation": "upsert", "content": "new\n"}]),
        expected_head_sha=OLD,
    )
    data = json.loads(raw)
    assert data["write_verified"] is True
    assert data["operation_id"]
    row = mygithub10._idempotent_existing_by_operation(data["operation_id"])
    assert row["tool_name"] == "commit_github_files"
    assert row["status"] == "success_verified"
    request = json.loads(row["request_json"])
    assert "content" not in request["files"][0]
    assert request["files"][0]["content_sha256"]


@pytest.mark.asyncio
async def test_06_workspace_finalize_failure_marks_operation_indeterminate(tmp_path, monkeypatch):
    monkeypatch.setattr(mygithub10.settings, "IDEMPOTENCY_DB_PATH", str(tmp_path / "indeterminate.db"))
    result = verified_result()
    operation_id, _ = mygithub10._idempotent_start(
        "apply_github_patch", "ws-key",
        {"tool_name": "apply_github_patch", "repository": "owner/allowed-repo", "branch": "ai/test", "expected_head_sha": OLD},
    )
    mygithub10._idempotent_mark_git_verified(operation_id, result)
    result["_operation_id"] = operation_id

    def fail_workspace(*args, **kwargs):
        raise mygithub12.MyGithub12Error(
            "WORKSPACE_REVISION_MISMATCH", "workspace revision changed", {"expected": 1, "actual": 2}
        )

    monkeypatch.setattr(mygithub12, "workspace_write_complete", fail_workspace)
    with pytest.raises(mygithub12.MyGithub12Error) as exc:
        await mcp_server._finalize_durable_write(result, "ws-test", 1)
    assert exc.value.code == "WORKSPACE_REVISION_MISMATCH"
    assert exc.value.details["github_write_verified"] is True
    assert exc.value.details["github_branch_head"] == NEW
    assert exc.value.details["recovery_required"] is True
    row = mygithub10._idempotent_existing_by_operation(operation_id)
    assert row["status"] == "indeterminate"
    assert row["result_commit_sha"] == NEW
