import hashlib
import json
from pathlib import Path

import pytest

from app import development_failure_pack as failure_pack
from app import mcp_response


def _reconstruct_resource(resource_uri: str) -> tuple[bytes, list[dict]]:
    parts: list[bytes] = []
    pages: list[dict] = []
    cursor = 0
    while True:
        page = mcp_response.read_response_resource_chunk(
            resource_uri,
            offset_bytes=cursor,
            limit_bytes=mcp_response.MAX_RESPONSE_RESOURCE_CHUNK_BYTES,
        )
        chunk = page["content"].encode("utf-8")
        pages.append(page)
        parts.append(chunk)
        assert page["cursor"] == page["offset_from"] == cursor
        assert page["next_cursor"] == page["next_offset"]
        assert page["chunk_size_bytes"] == len(chunk)
        assert page["chunk_size_bytes"] <= mcp_response.MAX_RESPONSE_RESOURCE_CHUNK_BYTES
        assert page["chunk_sha256"] == hashlib.sha256(chunk).hexdigest()
        if not page["has_more"]:
            assert page["next_cursor"] is None
            assert page["eof"] is True
            break
        assert page["next_cursor"] is not None
        cursor = int(page["next_cursor"])
    return b"".join(parts), pages


def test_inline_budget_falls_back_before_64k_transport_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_RESPONSE_RESOURCE_DIR", str(tmp_path))
    payload = {
        "ok": True,
        "job_id": "job-dev015-budget",
        "status": "failed",
        "diagnostics": "D" * 40000,
    }
    expected = mcp_response.json_bytes(payload)

    assert mcp_response.MAX_SAFE_INLINE_BYTES < len(expected) < 64 * 1024
    result = mcp_response.prepare_tool_response(payload)
    meta = result["response_meta"]

    assert meta["mode"] == "resource"
    assert meta["truncated"] is True
    assert meta["inline_bytes"] <= mcp_response.MAX_SAFE_INLINE_BYTES
    assert meta["total_bytes"] == meta["size"] == len(expected)
    assert meta["content_sha256"] == meta["sha256"] == hashlib.sha256(expected).hexdigest()
    assert meta["cursor"] == 0
    assert meta["has_more"] is True
    assert mcp_response.read_response_resource_text(meta["resource_uri"]).encode("utf-8") == expected


def test_resource_chunks_are_24k_bounded_and_utf8_exact(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_RESPONSE_RESOURCE_DIR", str(tmp_path))
    payload = {
        "ok": True,
        "text": "中😀abc" * 7000,
        "note": "多字节 reconstruction ✅",
    }
    expected = mcp_response.json_bytes(payload)
    resource = mcp_response.store_response_resource(payload)

    reconstructed, pages = _reconstruct_resource(resource["resource_uri"])

    assert len(pages) >= 2
    assert reconstructed == expected
    assert resource["size"] == len(expected)
    assert resource["sha256"] == hashlib.sha256(expected).hexdigest()
    for page in pages:
        assert page["size"] == page["total_bytes"] == len(expected)
        assert page["sha256"] == page["content_sha256"] == resource["sha256"]

    emoji = "😀".encode("utf-8")
    middle_of_emoji = expected.index(emoji) + 1
    with pytest.raises(ValueError, match="UTF-8 boundary"):
        mcp_response.read_response_resource_chunk(
            resource["resource_uri"],
            offset_bytes=middle_of_emoji,
            limit_bytes=mcp_response.MAX_RESPONSE_RESOURCE_CHUNK_BYTES,
        )


def test_5000_plus_warnings_have_truthful_bounded_summary_and_resource(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_RESPONSE_RESOURCE_DIR", str(tmp_path))
    warnings = [f"warning-{index:04d} 中文 😀 diagnostic" for index in range(5001)]
    payload = {
        "ok": False,
        "job_id": "job-dev015-warnings",
        "status": "failed",
        "warnings": warnings,
        "diagnostics": {"source": "compiler", "count": len(warnings)},
    }
    expected = mcp_response.json_bytes(payload)

    result = mcp_response.prepare_tool_response(payload)
    meta = result["response_meta"]

    assert meta["mode"] == "resource"
    assert mcp_response.response_size_bytes(result) <= mcp_response.MAX_SAFE_INLINE_BYTES
    assert result["warnings_total"] == 5001
    assert result["warnings_truncated"] is True
    assert 0 < len(result["warnings"]) <= 100
    assert len(result["warnings"]) < result["warnings_total"]
    assert result["diagnostics"] == payload["diagnostics"]
    assert meta["size"] == len(expected)
    assert meta["sha256"] == hashlib.sha256(expected).hexdigest()

    reconstructed, pages = _reconstruct_resource(meta["resource_uri"])
    assert reconstructed == expected
    assert pages[-1]["has_more"] is False


def _failure_job() -> dict:
    return {
        "job_id": "job-dev015-failure-pack",
        "repository": "owner/repo",
        "branch": "ai/dev015",
        "commit_sha": "a" * 40,
        "base_sha": "b" * 40,
        "profile": "repo-auto-check",
        "status": "failed",
        "exit_code": 1,
        "error_code": "CI_STEP_FAILED",
        "error_message": "pytest failed",
        "changed_files": [{"path": "tests/test_resource.py", "operation": "modified"}],
        "changed_files_total": 1,
        "summary": {
            "steps": [
                {
                    "step_name": "pytest",
                    "status": "failed",
                    "exit_code": 1,
                    "command": "pytest tests/test_resource.py",
                }
            ]
        },
    }


def test_oversized_failure_pack_resource_rematerializes_exactly_without_ci(tmp_path, monkeypatch):
    db_path = tmp_path / "controller.db"
    resource_dir = tmp_path / "resources"
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(db_path))
    monkeypatch.setenv("MCP_RESPONSE_RESOURCE_DIR", str(resource_dir))
    monkeypatch.setattr(failure_pack, "get_steps", lambda job_id: [])
    monkeypatch.setattr(failure_pack, "get_log_tail", lambda *args, **kwargs: {"lines": []})
    log = "".join(
        f"WARNING {index:04d} resource fallback 中文 😀\n" for index in range(6000)
    ) + "FAILED tests/test_resource.py::test_resource - AssertionError\n"

    first = failure_pack.build_failure_pack(_failure_job(), log_tail=log)
    durable = failure_pack.read_failure_pack(first["failure_pack_id"])
    assert durable is not None
    expected = mcp_response.json_bytes(durable)
    expected_sha = hashlib.sha256(expected).hexdigest()

    assert len(expected) > mcp_response.MAX_SAFE_INLINE_BYTES
    assert first["total_bytes"] == len(expected)
    assert first["content_sha256"] == expected_sha
    assert first["resource_sha256"] == expected_sha
    assert first["resource_continuation"]["chunk_limit_bytes"] == mcp_response.MAX_RESPONSE_RESOURCE_CHUNK_BYTES
    assert first["resource_continuation"]["has_more"] is True
    assert mcp_response.response_size_bytes(first) <= mcp_response.MAX_SAFE_INLINE_BYTES

    reconstructed, pages = _reconstruct_resource(first["resource_uri"])
    assert reconstructed == expected
    assert pages[0]["size"] == len(expected)
    assert pages[0]["sha256"] == expected_sha

    resource_id = first["resource_uri"].rsplit("/", 1)[-1]
    meta_path = Path(resource_dir / f"{resource_id}.meta.json")
    expired = json.loads(meta_path.read_text(encoding="utf-8"))
    expired["expires_at"] = 0
    meta_path.write_text(json.dumps(expired), encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        mcp_response.read_response_resource_text(first["resource_uri"])

    def forbid_ci_read(*args, **kwargs):
        raise AssertionError("rematerialization must use durable evidence only")

    monkeypatch.setattr(failure_pack, "get_log_tail", forbid_ci_read)
    monkeypatch.setattr(failure_pack, "get_steps", forbid_ci_read)
    second = failure_pack.materialize_failure_pack(first["failure_pack_id"])

    assert second["failure_pack_id"] == first["failure_pack_id"]
    assert second["resource_uri"] != first["resource_uri"]
    assert second["total_bytes"] == first["total_bytes"]
    assert second["content_sha256"] == first["content_sha256"] == expected_sha
    assert second["resource_sha256"] == first["resource_sha256"] == expected_sha
    assert second["rematerialize"]["rerun_ci"] is False
    assert mcp_response.read_response_resource_text(second["resource_uri"]).encode("utf-8") == expected
