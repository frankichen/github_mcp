import hashlib

import pytest

from app import development_orchestrator as development_dx
from app import development_session_store as sessions
from app import mcp_server, mygithub10, mygithub12


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
    monkeypatch.setattr(
        development_dx,
        "resolve_generated_write_context",
        lambda *_args, **_kwargs: {"managed": False, "workspace": None, "session": None},
    )
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
    assert "workspace_session_resolution" in capabilities["generated_files_put_semantics"]["server_manages"]
    assert "workspace_session_revision_cas" in capabilities["generated_files_put_semantics"]["server_manages"]
    assert "put_generated_files" in capabilities["legacy_upload_guidance"]
    for name in ("begin_github_file_upload", "append_github_file_upload_chunk", "finalize_github_file_upload"):
        assert "put_generated_files" in tools[name].description
    monkeypatch.setenv("MYGITHUB12_EXPOSE_DEPRECATED_TOOLS", "false")
    canonical_names = {tool.name for tool in await mcp_server.mcp.list_tools()}
    assert "put_generated_files" in canonical_names
    assert {"edit_github_file_ranges", "replace_github_text_once", "apply_github_patch"} <= canonical_names
    assert {
        "begin_github_file_upload",
        "append_github_file_upload_chunk",
        "finalize_github_file_upload",
        "commit_github_uploaded_files",
        "put_github_file",
        "put_github_files",
        "put_github_file_from_local_candidate",
    }.isdisjoint(canonical_names)


def _workspace(**overrides):
    value = {
        "workspace_id": "ws-generated",
        "repository": "owner/allowed-repo",
        "branch": "ai/generated-files-test",
        "head_sha": HEAD,
        "tree_sha": TREE,
        "revision": 7,
        "status": "active",
        "lease_valid": True,
    }
    value.update(overrides)
    return value


def _session(**overrides):
    value = {
        "session_id": "dev-generated",
        "workspace_id": "ws-generated",
        "repository": "owner/allowed-repo",
        "branch": "ai/generated-files-test",
        "head_commit_sha": HEAD,
        "tree_sha": TREE,
        "workspace_revision": 7,
        "session_revision": 11,
        "status": "active",
        "lease_valid": True,
    }
    value.update(overrides)
    return value


def test_resolve_generated_write_context_binds_unique_workspace_and_session(monkeypatch):
    workspace = _workspace()
    session = _session()
    monkeypatch.setattr(mygithub12, "list_workspaces", lambda *args, **kwargs: {"items": [workspace]})
    monkeypatch.setattr(sessions, "find_active_session_for_workspace", lambda workspace_id: session)
    observed = {}

    def fake_require(_service, session_id, session_revision, workspace_revision, expected_head_sha):
        observed.update({
            "session_id": session_id,
            "session_revision": session_revision,
            "workspace_revision": workspace_revision,
            "expected_head_sha": expected_head_sha,
        })
        return session, workspace

    monkeypatch.setattr(development_dx, "require_session_workspace", fake_require)
    result = development_dx.resolve_generated_write_context(object(), "owner/allowed-repo", "ai/generated-files-test", HEAD)
    assert result == {"managed": True, "workspace": workspace, "session": session}
    assert observed == {
        "session_id": "dev-generated",
        "session_revision": 11,
        "workspace_revision": 7,
        "expected_head_sha": HEAD,
    }


@pytest.mark.parametrize(
    ("workspace", "error_code"),
    [
        (_workspace(status="expired", lease_valid=False), "WORKSPACE_LEASE_REQUIRED"),
        (_workspace(status="drifted", lease_valid=False), "WORKSPACE_BRANCH_DRIFTED"),
    ],
)
def test_resolve_generated_write_context_fails_closed_for_stale_workspace(monkeypatch, workspace, error_code):
    monkeypatch.setattr(mygithub12, "list_workspaces", lambda *args, **kwargs: {"items": [workspace]})
    with pytest.raises(mygithub12.MyGithub12Error) as exc:
        development_dx.resolve_generated_write_context(object(), "owner/allowed-repo", "ai/generated-files-test", HEAD)
    assert exc.value.code == error_code


def test_resolve_generated_write_context_allows_unmanaged_branch(monkeypatch):
    monkeypatch.setattr(mygithub12, "list_workspaces", lambda *args, **kwargs: {"items": []})
    assert development_dx.resolve_generated_write_context(
        object(), "owner/allowed-repo", "feature/no-workspace", HEAD
    ) == {"managed": False, "workspace": None, "session": None}


@pytest.mark.asyncio
async def test_generated_write_uses_internal_workspace_and_session_cas_without_schema_fields(generated_env, monkeypatch):
    workspace = _workspace()
    session = _session()
    monkeypatch.setattr(
        development_dx,
        "resolve_generated_write_context",
        lambda *_args: {"managed": True, "workspace": workspace, "session": session},
    )
    run_calls = []

    async def fake_run(*args):
        run_calls.append(args)
        return {
            "ok": True,
            "commit_sha": COMMIT,
            "tree_sha": TREE,
            "write_verified": True,
            "workspace": {**workspace, "revision": 8, "head_sha": COMMIT},
            "workspace_revision_after": 8,
        }

    def fake_after(_service, session_id, session_revision, result, event_type):
        assert session_id == "dev-generated"
        assert session_revision == 11
        assert result["commit_sha"] == COMMIT
        assert event_type == "generated_files_committed"
        return {"session": {**session, "session_revision": 12, "head_commit_sha": COMMIT}, "index": None, "index_error": None}

    monkeypatch.setattr(mcp_server, "_run_high_level_put", fake_run)
    monkeypatch.setattr(development_dx, "after_verified_change", fake_after)
    result = await _call(_args(dry_run=False, idempotency_key="managed-generated-write"))
    assert result["ok"] is True
    assert run_calls[0][7:9] == ("ws-generated", 7)
    assert result["coordination"] == {
        "managed": True,
        "workspace_id": "ws-generated",
        "workspace_revision": 7,
        "development_session_id": "dev-generated",
        "session_revision": 11,
        "workspace_revision_after": 8,
        "session_revision_after": 12,
    }
    assert result["development_session"]["head_commit_sha"] == COMMIT
    stored = mygithub10._idempotent_existing("managed-generated-write")
    assert stored["workspace_id"] == "ws-generated"
    assert stored["workspace_revision"] == 7


@pytest.mark.asyncio
async def test_generated_dry_run_reports_internal_coordination_without_advancing_session(generated_env, monkeypatch):
    workspace = _workspace()
    session = _session()
    monkeypatch.setattr(
        development_dx,
        "resolve_generated_write_context",
        lambda *_args: {"managed": True, "workspace": workspace, "session": session},
    )

    async def fake_run(*args):
        assert args[5] is True
        assert args[7:9] == ("ws-generated", 7)
        return {"ok": True, "dry_run": True, "canonical_payload_hash": "f" * 64, "changed_files": []}

    monkeypatch.setattr(mcp_server, "_run_high_level_put", fake_run)
    monkeypatch.setattr(
        development_dx,
        "after_verified_change",
        lambda *_args: pytest.fail("dry-run must not advance the development session"),
    )
    result = await _call(_args(dry_run=True, idempotency_key="managed-generated-dry-run"))
    assert result["ok"] is True
    assert result["coordination"] == {
        "managed": True,
        "workspace_id": "ws-generated",
        "workspace_revision": 7,
        "development_session_id": "dev-generated",
        "session_revision": 11,
    }


@pytest.mark.asyncio
async def test_session_finalize_failure_preserves_verified_commit_and_marks_idempotency_indeterminate(generated_env, monkeypatch):
    workspace = _workspace()
    session = _session()
    monkeypatch.setattr(
        development_dx,
        "resolve_generated_write_context",
        lambda *_args: {"managed": True, "workspace": workspace, "session": session},
    )

    async def fake_run(*_args):
        return {
            "ok": True,
            "commit_sha": COMMIT,
            "tree_sha": TREE,
            "write_verified": True,
            "workspace": {**workspace, "revision": 8, "head_sha": COMMIT},
            "workspace_revision_after": 8,
        }

    def fail_after(*_args):
        raise mygithub12.MyGithub12Error(
            "DEVELOPMENT_SESSION_REVISION_MISMATCH",
            "session changed while finalizing",
        )

    monkeypatch.setattr(mcp_server, "_run_high_level_put", fake_run)
    monkeypatch.setattr(development_dx, "after_verified_change", fail_after)
    result = await _call(_args(dry_run=False, idempotency_key="managed-session-finalize-failure"))
    assert result["ok"] is False
    assert result["commit_sha"] == COMMIT
    assert result["write_verified"] is True
    assert result["failed_stage"] == "development_session_finalize"
    assert result["recovery_required"] is True
    stored = mygithub10._idempotent_existing("managed-session-finalize-failure")
    assert stored["status"] == "indeterminate"
