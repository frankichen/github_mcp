import hashlib
import pytest

from app import mcp_server
from app import mygithub10


HEAD = "a" * 40
COMMIT = "b" * 40
TREE = "c" * 40
EXISTING_BLOB = "d" * 40


def _structured_result(call_result):
    if isinstance(call_result, tuple):
        return call_result[1]
    structured = getattr(call_result, "structured_content", None)
    if structured is None:
        structured = getattr(call_result, "structuredContent", None)
    return structured


@pytest.fixture
def put_env(tmp_path, monkeypatch):
    uploads = tmp_path / "uploads"
    candidates = tmp_path / "candidates"
    candidates.mkdir()
    monkeypatch.setattr(mygithub10, "_UPLOAD_ROOT", uploads)
    monkeypatch.setattr(mygithub10.settings, "IDEMPOTENCY_DB_PATH", str(tmp_path / "idempotency.db"))
    monkeypatch.setattr(mygithub10.settings, "MYGITHUB12_LOCAL_CANDIDATE_DIR", str(candidates))
    monkeypatch.delenv("REQUIRE_WORKSPACE_FOR_AI_WRITES", raising=False)

    state = {
        "existing": {},
        "commit_calls": [],
    }

    def fake_preflight(_service, repository, branch, expected_head_sha, paths, expected_blob_shas):
        if expected_head_sha != HEAD:
            raise mygithub10.MyGithub10Error(
                "PATCH_HEAD_CHANGED",
                "branch HEAD changed before write",
                {"expected": expected_head_sha, "actual": HEAD, "repository": repository, "branch": branch},
            )
        old_shas = {path: state["existing"].get(path) for path in paths}
        for path, expected_blob_sha in expected_blob_shas.items():
            if expected_blob_sha and expected_blob_sha != (old_shas[path] or ""):
                raise mygithub10.MyGithub10Error(
                    "BLOB_CHANGED",
                    f"file blob changed before write: {path}",
                    {"expected": expected_blob_sha, "actual": old_shas[path], "path": path},
                )
        return None, None, None, HEAD, old_shas

    def fake_commit_files(_service, repository, branch, expected_head_sha, changed, expected_blob_shas, message):
        state["commit_calls"].append({
            "repository": repository,
            "branch": branch,
            "expected_head_sha": expected_head_sha,
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
    monkeypatch.setattr(mygithub10, "_commit_files", fake_commit_files)
    return state, candidates


async def _call(name: str, arguments: dict):
    return _structured_result(await mcp_server.mcp.call_tool(name, arguments))


def _base_args(**overrides):
    value = {
        "repository": "owner/allowed-repo",
        "branch": "ai/web-file-write-test",
        "expected_head_sha": HEAD,
        "commit_message": "测试高层文件写入",
        "dry_run": False,
        "idempotency_key": "put-test-key",
    }
    value.update(overrides)
    return value


@pytest.mark.asyncio
async def test_put_github_file_small_utf8_real_tool_dispatch(put_env):
    state, _ = put_env
    content = "你好，LensHub。\nquote=\"ok\"\\path\\n\n"
    result = await _call("put_github_file", _base_args(
        path="tmp/small.txt",
        content=content,
        expected_blob_sha="",
    ))
    assert result["ok"] is True
    assert result["write_verified"] is True
    assert result["staging"]["strategy"] == "server_internal_chunked_upload"
    assert state["commit_calls"][0]["changed"]["tmp/small.txt"] == content.encode("utf-8")


@pytest.mark.asyncio
async def test_put_github_file_32kb_is_internally_chunked(put_env):
    state, _ = put_env
    content = ('中文 + JSON {"quoted":"value"} \\\\ code() "quotes"\n' * 1200)[:32 * 1024]
    result = await _call("put_github_file", _base_args(
        path="tmp/32k.txt",
        content=content,
        expected_blob_sha="",
        idempotency_key="put-32k",
    ))
    assert result["ok"] is True
    assert result["staging"]["chunk_count"] >= 2
    assert state["commit_calls"][0]["changed"]["tmp/32k.txt"] == content.encode("utf-8")


@pytest.mark.asyncio
async def test_put_github_file_64kb_inline_reports_transport_payload_category(put_env):
    content = "x" * (64 * 1024)
    result = await _call("put_github_file", _base_args(
        path="tmp/64k-inline.txt",
        content=content,
        expected_blob_sha="",
        idempotency_key="put-64k-inline",
    ))
    assert result["ok"] is False
    assert result["error"]["code"] == "PAYLOAD_REQUIRES_LOCAL_CANDIDATE"
    assert result["error"]["details"]["error_category"] == "TRANSPORT/PAYLOAD"


@pytest.mark.asyncio
@pytest.mark.parametrize("size_bytes", [64 * 1024, 100 * 1024])
async def test_put_github_file_from_local_candidate_64k_and_100k_real_tool_dispatch(put_env, size_bytes):
    state, candidates = put_env
    content = (("中文/quotes=\"yes\"/slashes=\\\\/json={\"k\":1}/code=print('x')\n" * 3000).encode("utf-8"))[:size_bytes]
    assert len(content) == size_bytes
    candidate_name = f"candidate-{size_bytes}.txt"
    (candidates / candidate_name).write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    result = await _call("put_github_file_from_local_candidate", _base_args(
        path=f"tmp/{size_bytes}.txt",
        candidate_name=candidate_name,
        expected_size_bytes=len(content),
        expected_sha256=digest,
        expected_blob_sha="",
        idempotency_key=f"put-candidate-{size_bytes}",
    ))
    assert result["ok"] is True
    assert result["staging"]["chunk_count"] >= 4
    assert state["commit_calls"][0]["changed"][f"tmp/{size_bytes}.txt"] == content


@pytest.mark.asyncio
async def test_put_github_files_is_one_atomic_commit_with_structured_files_argument(put_env):
    state, _ = put_env
    files = [
        {"path": "tmp/a.json", "content": '{"hello":"你好","slash":"\\\\"}\n', "expected_blob_sha": ""},
        {"path": "tmp/b.py", "content": 'print("引号 \\\" 和反斜杠 \\\\")\n', "expected_blob_sha": ""},
        {"path": "tmp/c.yaml", "content": "key: '多文件'\nlines: |\n  one\n  two\n", "expected_blob_sha": ""},
    ]
    result = await _call("put_github_files", _base_args(
        files=files,
        idempotency_key="put-multi",
    ))
    assert result["ok"] is True
    assert result["staging"]["file_count"] == 3
    assert len(state["commit_calls"]) == 1
    assert set(state["commit_calls"][0]["changed"]) == {item["path"] for item in files}


@pytest.mark.asyncio
async def test_put_github_file_replays_same_idempotency_key_without_second_commit(put_env):
    state, _ = put_env
    args = _base_args(
        path="tmp/replay.txt",
        content="same request\n",
        expected_blob_sha="",
        idempotency_key="put-replay",
    )
    first = await _call("put_github_file", args)
    second = await _call("put_github_file", args)
    assert first["commit_sha"] == COMMIT
    assert second["commit_sha"] == COMMIT
    assert second["replayed"] is True
    assert len(state["commit_calls"]) == 1


@pytest.mark.asyncio
async def test_local_candidate_replay_does_not_require_candidate_after_verified_success(put_env):
    state, candidates = put_env
    content = b"large-enough-candidate\n" * 3000
    candidate = candidates / "replay-candidate.txt"
    candidate.write_bytes(content)
    args = _base_args(
        path="tmp/local-replay.txt",
        candidate_name=candidate.name,
        expected_size_bytes=len(content),
        expected_sha256=hashlib.sha256(content).hexdigest(),
        expected_blob_sha="",
        idempotency_key="put-local-replay",
    )
    first = await _call("put_github_file_from_local_candidate", args)
    candidate.unlink()
    second = await _call("put_github_file_from_local_candidate", args)
    assert first["commit_sha"] == COMMIT
    assert second["replayed"] is True
    assert len(state["commit_calls"]) == 1


@pytest.mark.asyncio
async def test_put_github_file_head_changed_fail_stops_before_staging(put_env):
    state, _ = put_env
    result = await _call("put_github_file", _base_args(
        path="tmp/head.txt",
        content="head conflict\n",
        expected_blob_sha="",
        expected_head_sha="e" * 40,
        idempotency_key="put-head-conflict",
    ))
    assert result["ok"] is False
    assert result["error"]["code"] == "HEAD_CHANGED"
    assert state["commit_calls"] == []


@pytest.mark.asyncio
async def test_put_github_file_blob_changed_fail_stops_before_staging(put_env):
    state, _ = put_env
    state["existing"]["tmp/existing.txt"] = EXISTING_BLOB
    result = await _call("put_github_file", _base_args(
        path="tmp/existing.txt",
        content="replacement\n",
        expected_blob_sha="e" * 40,
        idempotency_key="put-blob-conflict",
    ))
    assert result["ok"] is False
    assert result["error"]["code"] == "BLOB_CHANGED"
    assert state["commit_calls"] == []


@pytest.mark.asyncio
async def test_put_github_file_requires_blob_cas_when_modifying_existing_file(put_env):
    state, _ = put_env
    state["existing"]["tmp/existing.txt"] = EXISTING_BLOB
    result = await _call("put_github_file", _base_args(
        path="tmp/existing.txt",
        content="replacement\n",
        expected_blob_sha="",
        idempotency_key="put-missing-blob-cas",
    ))
    assert result["ok"] is False
    assert result["error"]["code"] == "BLOB_EXPECTATION_REQUIRED"
    assert state["commit_calls"] == []


def test_append_upload_same_offset_retry_is_idempotent_after_response_loss(tmp_path, monkeypatch):
    monkeypatch.setattr(mygithub10, "_UPLOAD_ROOT", tmp_path / "uploads")
    upload = mygithub10.begin_upload()
    chunk = "响应可能丢失，但服务端已经写入。\n".encode("utf-8")
    sha256 = hashlib.sha256(chunk).hexdigest()
    first = mygithub10.append_upload(upload["upload_id"], 0, chunk, sha256, "chunk-retry-key")
    second = mygithub10.append_upload(upload["upload_id"], 0, chunk, sha256, "chunk-retry-key")
    assert first["replayed"] is False
    assert second["replayed"] is True
    assert second["next_offset"] == len(chunk)
    data_path, _, _ = mygithub10._load_upload(upload["upload_id"])
    assert data_path.read_bytes() == chunk


def test_append_upload_same_idempotency_key_with_different_content_conflicts(tmp_path, monkeypatch):
    monkeypatch.setattr(mygithub10, "_UPLOAD_ROOT", tmp_path / "uploads")
    upload = mygithub10.begin_upload()
    first = b"first"
    second = b"other"
    mygithub10.append_upload(upload["upload_id"], 0, first, hashlib.sha256(first).hexdigest(), "same-key")
    with pytest.raises(mygithub10.MyGithub10Error) as exc:
        mygithub10.append_upload(upload["upload_id"], 0, second, hashlib.sha256(second).hexdigest(), "same-key")
    assert exc.value.code == "IDEMPOTENCY_CONFLICT"


def test_local_candidate_rejects_symlink_and_untrusted_name(put_env, tmp_path):
    _, candidates = put_env
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (candidates / "linked.txt").symlink_to(outside)
    with pytest.raises(mygithub10.MyGithub10Error) as exc:
        mygithub10.prepare_local_candidate_file(
            "tmp/linked.txt", "", "linked.txt", len(b"outside"), hashlib.sha256(b"outside").hexdigest()
        )
    assert exc.value.code == "PAYLOAD_LOCAL_CANDIDATE_NOT_FOUND"
    with pytest.raises(mygithub10.MyGithub10Error) as exc:
        mygithub10.prepare_local_candidate_file(
            "tmp/escape.txt", "", "../outside.txt", len(b"outside"), hashlib.sha256(b"outside").hexdigest()
        )
    assert exc.value.code == "PAYLOAD_INVALID"
