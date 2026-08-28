import hashlib

import pytest

from app import mcp_server, mygithub10


HEAD = "a" * 40
COMMIT = "b" * 40
TREE = "c" * 40
EXISTING_BLOB = "d" * 40


def _structured_result(call_result):
    if isinstance(call_result, tuple):
        return call_result[1]
    return getattr(call_result, "structured_content", None) or getattr(call_result, "structuredContent", None)


async def _call(arguments: dict):
    return _structured_result(await mcp_server.mcp.call_tool("put_generated_files", arguments))


def _args(**overrides):
    value = {
        "repository": "owner/allowed-repo",
        "branch": "ai/generated-files-test",
        "expected_head_sha": HEAD,
        "files": [{"path": "src/new.py", "content": "print('你好')\n"}],
        "commit_message": "测试生成文件写入",
        "dry_run": True,
        "idempotency_key": "generated-files-key",
    }
    value.update(overrides)
    return value


@pytest.fixture
def generated_env(tmp_path, monkeypatch):
    monkeypatch.setattr(mygithub10, "_UPLOAD_ROOT", tmp_path / "uploads")
    monkeypatch.setattr(mygithub10.settings, "IDEMPOTENCY_DB_PATH", str(tmp_path / "idempotency.db"))
    monkeypatch.delenv("REQUIRE_WORKSPACE_FOR_AI_WRITES", raising=False)
    state = {"existing": {}, "commit_calls": [], "preflight_calls": []}

    def fake_preflight(_service, repository, branch, expected_head_sha, paths, expected_blob_shas):
        state["preflight_calls"].append({
            "expected_head_sha": expected_head_sha,
            "paths": list(paths),
            "expected_blob_shas": dict(expected_blob_shas),
        })
        if expected_head_sha != HEAD:
            raise mygithub10.MyGithub10Error(
                "PATCH_HEAD_CHANGED",
                "branch HEAD changed before write",
                {"expected": expected_head_sha, "actual": HEAD},
            )
        old_shas = {path: state["existing"].get(path) for path in paths}
        for path, expected in expected_blob_shas.items():
            if expected and expected != (old_shas[path] or ""):
                raise mygithub10.MyGithub10Error("BLOB_CHANGED", "blob changed", {"path": path})
        return None, None, None, HEAD, old_shas

    def fake_commit(_service, repository, branch, expected_head_sha, changed, expected_blob_shas, message):
        state["commit_calls"].append({
            "changed": dict(changed),
            "expected_blob_shas": dict(expected_blob_shas),
            "message": message,
        })
        changed_files = []
        for path, data in changed.items():
            changed_files.append({
                "path": path,
                "operation": "modify" if state["existing"].get(path) else "add",
                "old_blob_sha": state["existing"].get(path),
                "new_blob_sha": mygithub10._git_blob_sha(data),
                "content_sha256": hashlib.sha256(data).hexdigest(),
                "size_bytes": len(data),
            })
        return {
            "commit_sha": COMMIT,
            "new_head_sha": COMMIT,
            "old_head_sha": HEAD,
            "tree_sha": TREE,
            "branch": branch,
            "repository": repository,
            "changed_files": changed_files,
            "write_verified": True,
            "previous_head_sha": HEAD,
            "verified_branch_head_sha": COMMIT,
            "verified_commit_sha": COMMIT,
            "verified_tree_sha": TREE,
        }

    monkeypatch.setattr(mygithub10, "_preflight_file_write", fake_preflight)
    monkeypatch.setattr(mygithub10, "_commit_files", fake_commit)
    return state


@pytest.mark.asyncio
async def test_single_new_file_dry_run_is_add_and_does_not_write(generated_env):
    result = await _call(_args())
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["expected_head_sha"] == HEAD
    assert result["would_commit"] is True
    assert len(result["canonical_payload_hash"]) == 64
    assert result["changed_files"][0]["operation"] == "add"
    assert result["changed_files"][0]["predicted_blob_sha"] == mygithub10._git_blob_sha("print('你好')\n".encode())
    assert generated_env["commit_calls"] == []


@pytest.mark.asyncio
async def test_existing_file_dry_run_auto_reads_blob_without_caller_blob(generated_env):
    generated_env["existing"]["src/current.py"] = EXISTING_BLOB
    result = await _call(_args(files=[{"path": "src/current.py", "content": "updated = True\n"}]))
    assert result["changed_files"][0]["operation"] == "modify"
    assert result["changed_files"][0]["old_blob_sha"] == EXISTING_BLOB
    assert generated_env["preflight_calls"][0]["expected_blob_shas"] == {"src/current.py": ""}
    assert generated_env["commit_calls"] == []


@pytest.mark.asyncio
async def test_multi_file_real_write_add_modify_one_commit_and_blob_evidence(generated_env):
    generated_env["existing"]["src/current.py"] = EXISTING_BLOB
    files = [
        {"path": "src/current.py", "content": "updated = True\n"},
        {"path": "docs/new.md", "content": "# 新文档\n"},
    ]
    result = await _call(_args(files=files, dry_run=False, idempotency_key="multi-real"))
    assert result["ok"] is True
    assert result["commit_sha"] == COMMIT
    assert result["tree_sha"] == TREE
    assert result["verified_branch_head_sha"] == COMMIT
    assert result["verified_commit_sha"] == COMMIT
    assert result["verified_tree_sha"] == TREE
    assert len(generated_env["commit_calls"]) == 1
    assert generated_env["commit_calls"][0]["expected_blob_shas"] == {
        "src/current.py": EXISTING_BLOB,
        "docs/new.md": "",
    }
    assert {item["operation"] for item in result["changed_files"]} == {"add", "modify"}
    assert all(item["verified"] is True for item in result["blob_evidence"])


@pytest.mark.asyncio
async def test_dry_run_and_real_write_use_same_canonical_payload_hash(generated_env):
    args = _args(idempotency_key="same-canonical-payload")
    planned = await _call(args)
    written = await _call({**args, "dry_run": False})
    assert planned["canonical_payload_hash"] == written["canonical_payload_hash"]
    assert generated_env["commit_calls"][0]["changed"]["src/new.py"] == "print('你好')\n".encode("utf-8")


@pytest.mark.asyncio
async def test_head_changed_rejects_before_staging_or_write(generated_env):
    result = await _call(_args(expected_head_sha="e" * 40, dry_run=False, idempotency_key="head-change"))
    assert result["ok"] is False
    assert result["error"]["code"] == "HEAD_CHANGED"
    assert generated_env["commit_calls"] == []
    assert not mygithub10._UPLOAD_ROOT.exists()


@pytest.mark.asyncio
async def test_idempotency_replays_and_conflicts(generated_env):
    args = _args(dry_run=False, idempotency_key="stable-key")
    first = await _call(args)
    second = await _call(args)
    assert first["commit_sha"] == second["commit_sha"] == COMMIT
    assert second["replayed"] is True
    assert len(generated_env["commit_calls"]) == 1

    conflict = await _call(_args(
        files=[{"path": "src/other.py", "content": "different\n"}],
        dry_run=False,
        idempotency_key="stable-key",
    ))
    assert conflict["ok"] is False
    assert conflict["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert len(generated_env["commit_calls"]) == 1


@pytest.mark.asyncio
async def test_missing_optional_idempotency_key_still_uses_canonical_payload_identity(generated_env):
    args = _args(dry_run=False, idempotency_key="")
    first = await _call(args)
    second = await _call(args)
    assert first["commit_sha"] == second["commit_sha"] == COMMIT
    assert second["replayed"] is True
    assert len(generated_env["commit_calls"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["", "/abs.py", "../escape.py", "a/../escape.py", ".git/config", "a/.GIT/config", "a//b.py", "a/\x01b.py", "bad\udcff.py", "C:/outside.py", "outside\\file.py"])
async def test_path_safety_rejections(generated_env, path):
    result = await _call(_args(files=[{"path": path, "content": "safe text\n"}]))
    assert result["ok"] is False
    assert result["error"]["code"] == "PATH_INVALID"
    assert generated_env["commit_calls"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["text\x00binary", "bad\udcff"])
async def test_non_utf8_or_binary_like_content_is_rejected(generated_env, content):
    result = await _call(_args(files=[{"path": "src/value.txt", "content": content}]))
    assert result["ok"] is False
    assert result["error"]["code"] == "UTF8_REQUIRED"
    assert generated_env["commit_calls"] == []


@pytest.mark.asyncio
async def test_no_files_and_large_payload_have_window_facing_errors(generated_env):
    empty = await _call(_args(files=[]))
    assert empty["error"]["code"] == "NO_FILES"
    large = await _call(_args(files=[{"path": "large.txt", "content": "x" * (mygithub10.MAX_HIGH_LEVEL_INLINE_CONTENT_BYTES + 1)}]))
    assert large["error"]["code"] == "PAYLOAD_TOO_LARGE"
    assert "chunk" not in large["error"]["message"].lower()
    assert "upload" not in large["error"]["message"].lower()


@pytest.mark.asyncio
async def test_schema_exposes_only_v1_window_payload_and_capabilities_recommend_it(monkeypatch):
    monkeypatch.setenv("MYGITHUB12_EXPOSE_DEPRECATED_TOOLS", "true")
    tools = {tool.name: tool for tool in await mcp_server.mcp.list_tools()}
    tool = tools["put_generated_files"]
    assert set(tool.inputSchema["properties"]) == {
        "repository", "branch", "expected_head_sha", "files", "commit_message", "dry_run", "idempotency_key"
    }
    item_schema = tool.inputSchema["properties"]["files"]["items"]
    if "$ref" in item_schema:
        item_schema = tool.inputSchema["$defs"][item_schema["$ref"].rsplit("/", 1)[-1]]
    item_properties = item_schema["properties"]
    assert set(item_properties) == {"path", "content"}
    assert set(item_schema["required"]) == {"path", "content"}
    assert all(forbidden not in tool.inputSchema["properties"] for forbidden in (
        "expected_blob_sha", "upload_id", "chunk", "offset", "content_base64", "candidate_name"
    ))
    capabilities = mygithub10.capabilities(HEAD)
    assert capabilities["recommended_ai_text_write_workflow"] == ["put_generated_files"]
    assert capabilities["recommended_small_text_workflow"] == ["put_generated_files"]
    assert capabilities["recommended_atomic_multi_upload_workflow"] == ["put_generated_files"]
    assert "put_generated_files" in capabilities["legacy_upload_guidance"]
    for name in ("begin_github_file_upload", "append_github_file_upload_chunk", "finalize_github_file_upload"):
        assert "put_generated_files" in tools[name].description
