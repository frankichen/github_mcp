import inspect
import json
from pathlib import Path

import pytest

from app import ci_database as db
from app import ci_mcp
from app import development_failure_pack as failure_pack
from app import mcp_response
from app.mcp_response import StructuredFastMCP


def _structured_result(call_result):
    if isinstance(call_result, tuple):
        return call_result[1]
    structured = getattr(call_result, "structured_content", None)
    if structured is None:
        structured = getattr(call_result, "structuredContent", None)
    return structured


def _close_db():
    current = getattr(db._local, "db", None)
    if current is not None:
        current.close()
    db._local.db = None


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "ci.db"))
    _close_db()
    db.init_db()
    yield tmp_path
    _close_db()


@pytest.fixture
def get_mcp(isolated_db):
    mcp = StructuredFastMCP("web-ci-dev011-step-logs")
    ci_mcp.register_private_ci_mcp_tools(mcp)
    return mcp


async def _get_logs(mcp, **arguments):
    return _structured_result(await mcp.call_tool("get_private_ci_logs", arguments))


def _new_job():
    return db.create_or_get_job(
        repository="owner/repo",
        branch="feature/step-logs",
        commit_sha="a" * 40,
        profile="repo-auto-check",
        priority=100,
        timeout_seconds=900,
        force_rerun=True,
        supersede_previous=False,
        base_sha="b" * 40,
        changed_files=[],
    )


def _seed_bounded_job():
    job = _new_job()
    prefix = "before-step\n"
    prefix_end = db.append_log_chunk(job["job_id"], prefix)
    step_id = db.add_step(job["job_id"], "pytest", status="running")
    step_content = "pytest-line-1\npytest-line-2\n"
    step_end = db.append_log_chunk(job["job_id"], step_content)
    assert db.finish_step(step_id, "failed", exit_code=1, log_end_offset=step_end)
    suffix = "after-step\n"
    job_end = db.append_log_chunk(job["job_id"], suffix)
    return job["job_id"], step_id, prefix, step_content, suffix, prefix_end, step_end, job_end


@pytest.mark.asyncio
async def test_legacy_job_log_api_remains_complete_and_redacted(get_mcp):
    job_id, _, prefix, step_content, suffix, _, _, _ = _seed_bounded_job()

    first = await _get_logs(get_mcp, job_id=job_id, offset=0, limit=1)
    assert first["ok"] is True
    assert first["mode"] == "job"
    assert first["chunks"][0]["content"] == prefix
    assert first["next_offset"] is not None
    assert first["has_more"] is True

    remainder = await _get_logs(
        get_mcp, job_id=job_id, offset=first["next_offset"], limit=10
    )
    assert "".join(item["content"] for item in first["chunks"] + remainder["chunks"]) == (
        prefix + step_content + suffix
    )
    assert remainder["has_more"] is False
    assert remainder["next_offset"] is None

    secret_job = _new_job()
    db.append_log_chunk(secret_job["job_id"], "token=raw-secret authorization: Bearer raw-token\n")
    redacted = await _get_logs(get_mcp, job_id=secret_job["job_id"])
    serialized = json.dumps(redacted, ensure_ascii=False)
    assert "raw-secret" not in serialized
    assert "raw-token" not in serialized
    assert "[REDACTED]" in serialized


@pytest.mark.asyncio
async def test_failed_step_selector_returns_only_exact_persisted_range(get_mcp):
    job_id, step_id, prefix, step_content, suffix, start, end, job_end = _seed_bounded_job()

    result = await _get_logs(get_mcp, job_id=job_id, step_id=step_id)

    assert result["ok"] is True
    assert result["mode"] == "step"
    assert result["step_selector"] == {"step_id": step_id}
    assert result["step_log_range"] == {
        "start_offset": start,
        "end_offset": end,
        "end_exclusive": True,
    }
    assert result["step"]["step_id"] == step_id
    assert result["step"]["status"] == "failed"
    assert result["step"]["exit_code"] == 1
    assert "".join(item["content"] for item in result["chunks"]) == step_content
    assert prefix not in step_content and suffix not in step_content
    assert all(item["offset_from"] >= start for item in result["chunks"])
    assert all(item["offset_to"] <= end for item in result["chunks"])
    assert result["chunks"][-1]["offset_to"] == end
    assert job_end > end


@pytest.mark.asyncio
async def test_step_range_clips_one_stored_chunk_at_both_boundaries(get_mcp):
    job = _new_job()
    step_id = db.add_step(job["job_id"], "clipped", status="failed")
    db.append_log_chunk(job["job_id"], "prefix|step-only|suffix")
    connection = db._get_db()
    connection.execute(
        "UPDATE ci_job_steps SET log_start_offset=7, log_end_offset=16 WHERE id=?",
        (step_id,),
    )
    connection.commit()

    result = await _get_logs(get_mcp, job_id=job["job_id"], step_id=step_id)

    assert result["step_log_range"]["start_offset"] == 7
    assert result["step_log_range"]["end_offset"] == 16
    assert result["chunks"] == [{
        "chunk_index": 0,
        "offset_from": 7,
        "offset_to": 16,
        "content": "step-only",
    }]


@pytest.mark.asyncio
async def test_step_cursor_is_stable_and_pages_without_overlap_or_omission(get_mcp):
    job = _new_job()
    step_id = db.add_step(job["job_id"], "large-step", status="running")
    step_content = "".join(f"line-{index:06d}\n" for index in range(6000))
    end = db.append_log_chunk(job["job_id"], step_content)
    db.finish_step(step_id, "failed", exit_code=1, log_end_offset=end)
    step = db.get_job_step(job["job_id"], step_id)
    assert step["log_start_offset"] == 0
    assert step["log_end_offset"] == len(step_content)

    first = await _get_logs(get_mcp, job_id=job["job_id"], step_id=step_id, limit=1)
    repeated = await _get_logs(get_mcp, job_id=job["job_id"], step_id=step_id, limit=1)
    assert first["next_cursor"] == repeated["next_cursor"]
    assert first["has_more"] is True
    assert len(first["chunks"]) == 1
    assert len(first["chunks"][0]["content"].encode("utf-8")) <= ci_mcp._MAX_PRIVATE_CI_STEP_PAGE_BYTES

    pages = [first]
    cursor = first["next_cursor"]
    seen_cursors = {cursor}
    while cursor:
        page = await _get_logs(get_mcp, job_id=job["job_id"], cursor=cursor, limit=1)
        pages.append(page)
        assert page["ok"] is True
        assert page["cursor"] == cursor
        next_cursor = page["next_cursor"]
        if next_cursor:
            assert next_cursor not in seen_cursors
            seen_cursors.add(next_cursor)
        cursor = next_cursor

    reconstructed = ""
    previous_end = step["log_start_offset"]
    for page in pages:
        for chunk in page["chunks"]:
            assert chunk["offset_from"] == previous_end
            assert chunk["offset_to"] > chunk["offset_from"]
            reconstructed += chunk["content"]
            previous_end = chunk["offset_to"]
    assert reconstructed == step_content
    assert previous_end == step["log_end_offset"]
    assert pages[-1]["has_more"] is False
    assert pages[-1]["next_cursor"] is None


@pytest.mark.asyncio
async def test_step_name_requires_unique_identity_and_nonexistent_is_truthful(get_mcp):
    job = _new_job()
    first_id = db.add_step(job["job_id"], "duplicate", status="running")
    first_end = db.append_log_chunk(job["job_id"], "first\n")
    db.finish_step(first_id, "failed", exit_code=1, log_end_offset=first_end)
    second_id = db.add_step(job["job_id"], "duplicate", status="running")
    second_end = db.append_log_chunk(job["job_id"], "second\n")
    db.finish_step(second_id, "failed", exit_code=2, log_end_offset=second_end)

    ambiguous = await _get_logs(get_mcp, job_id=job["job_id"], step_name="duplicate")
    assert ambiguous["error"]["code"] == "PRIVATE_CI_STEP_SELECTOR_AMBIGUOUS"
    assert {item["step_id"] for item in ambiguous["error"]["details"]["matches"]} == {
        first_id,
        second_id,
    }

    first = await _get_logs(get_mcp, job_id=job["job_id"], step_id=first_id)
    second = await _get_logs(get_mcp, job_id=job["job_id"], step_id=second_id)
    assert "".join(item["content"] for item in first["chunks"]) == "first\n"
    assert "".join(item["content"] for item in second["chunks"]) == "second\n"

    missing = await _get_logs(get_mcp, job_id=job["job_id"], step_name="missing")
    assert missing["error"]["code"] == "PRIVATE_CI_STEP_NOT_FOUND"
    missing_id = await _get_logs(get_mcp, job_id=job["job_id"], step_id=999999)
    assert missing_id["error"]["code"] == "PRIVATE_CI_STEP_NOT_FOUND"


@pytest.mark.asyncio
async def test_failure_pack_continuation_directly_locates_step_and_survives_resource_expiry(
    isolated_db, get_mcp, monkeypatch
):
    job_id, step_id, _, step_content, _, _, end, _ = _seed_bounded_job()
    connection = db._get_db()
    connection.execute(
        "UPDATE ci_jobs SET status='failed', exit_code=1, error_code='CI_STEP_FAILED' WHERE job_id=?",
        (job_id,),
    )
    connection.commit()
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(isolated_db / "controller.db"))
    monkeypatch.setenv("MCP_RESPONSE_RESOURCE_DIR", str(isolated_db / "resources"))

    job = db.get_job(job_id)
    first = failure_pack.build_failure_pack(
        job,
        log_tail="FAILED tests/test_step.py::test_failure - AssertionError\n",
    )
    second = failure_pack.build_failure_pack(
        job,
        log_tail="FAILED tests/test_step.py::test_failure - AssertionError\n",
    )
    continuation = first["log_continuation"]
    assert first["failure_pack_id"] == second["failure_pack_id"]
    assert first["failed_step"]["step_id"] == step_id
    assert continuation["job_id"] == job_id
    assert continuation["method"] == "get_private_ci_logs"
    assert continuation["failed_step"]["step_id"] == step_id
    assert continuation["step_selector"] == {"step_id": step_id}
    assert continuation["step_log_range"]["end_offset"] == end
    assert continuation["step_log_range"]["end_exclusive"] is True
    assert continuation["cursor"] is None
    assert continuation["next_cursor"]
    assert continuation["has_more"] is True

    located = await _get_logs(get_mcp, cursor=continuation["next_cursor"])
    assert located["step_selector"] == {"step_id": step_id}
    assert "".join(item["content"] for item in located["chunks"]) == step_content

    resource_uri = first["resource_uri"]
    assert resource_uri
    resource_meta = Path(isolated_db / "resources" / f"{resource_uri.rsplit('/', 1)[-1]}.meta.json")
    expired = json.loads(resource_meta.read_text(encoding="utf-8"))
    expired["expires_at"] = 0
    resource_meta.write_text(json.dumps(expired), encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        mcp_response.read_response_resource_text(resource_uri)

    monkeypatch.setattr(
        failure_pack,
        "get_log_tail",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("resource rematerialization must not read or rerun CI")
        ),
    )
    rematerialized = failure_pack.materialize_failure_pack(first["failure_pack_id"])
    assert rematerialized["failure_pack_id"] == first["failure_pack_id"]
    assert rematerialized["log_continuation"] == continuation
    assert rematerialized["rematerialize"]["rerun_ci"] is False


def test_failure_pack_uses_unique_status_match_for_duplicate_step_names(
    isolated_db, monkeypatch
):
    job = _new_job()
    first_id = db.add_step(job["job_id"], "duplicate", status="passed")
    db.append_log_chunk(job["job_id"], "passed\n")
    db.finish_step(first_id, "passed", exit_code=0)
    second_id = db.add_step(job["job_id"], "duplicate", status="running")
    end = db.append_log_chunk(job["job_id"], "failed\n")
    db.finish_step(second_id, "failed", exit_code=1, log_end_offset=end)
    connection = db._get_db()
    connection.execute(
        "UPDATE ci_jobs SET status='failed', exit_code=1 WHERE job_id=?",
        (job["job_id"],),
    )
    connection.commit()
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(isolated_db / "controller.db"))
    monkeypatch.setenv("MCP_RESPONSE_RESOURCE_DIR", str(isolated_db / "resources"))

    failure = failure_pack.build_failure_pack(
        db.get_job(job["job_id"]),
        log_tail="FAILED tests/test_duplicate.py::test_failed - AssertionError\n",
    )

    assert failure["failed_step"]["step_id"] == second_id
    assert failure["log_continuation"]["step_selector"] == {"step_id": second_id}


@pytest.mark.asyncio
async def test_step_log_read_is_bounded_read_only_and_has_no_lifecycle_calls(get_mcp):
    job_id, step_id, _, _, _, _, _, _ = _seed_bounded_job()
    connection = db._get_db()
    before_steps = connection.execute(
        "SELECT COUNT(*) FROM ci_job_steps WHERE job_id=?", (job_id,)
    ).fetchone()[0]
    before_chunks = connection.execute(
        "SELECT COUNT(*) FROM ci_job_log_chunks WHERE job_id=?", (job_id,)
    ).fetchone()[0]

    result = await _get_logs(get_mcp, job_id=job_id, step_id=step_id, limit=100000)
    assert result["ok"] is True
    assert result["page_limit_bytes"] == ci_mcp._MAX_PRIVATE_CI_STEP_PAGE_BYTES
    assert connection.execute(
        "SELECT COUNT(*) FROM ci_job_steps WHERE job_id=?", (job_id,)
    ).fetchone()[0] == before_steps
    assert connection.execute(
        "SELECT COUNT(*) FROM ci_job_log_chunks WHERE job_id=?", (job_id,)
    ).fetchone()[0] == before_chunks

    tool = get_mcp._tool_manager.get_tool("get_private_ci_logs")
    source = inspect.getsource(tool.fn)
    for forbidden in ("start_private_ci_job", "wait_for_job_change", "sleep(", "request_cancel_job"):
        assert forbidden not in source
