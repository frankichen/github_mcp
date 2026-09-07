import json
from pathlib import Path

import pytest

from app import ci_database as db
from app import ci_mcp
from app import development_failure_pack as failure_pack
from app import development_failure_pack_store as failure_pack_store
from app import mcp_response
from app import mygithub12
from app.mcp_response import StructuredFastMCP


TOKEN = "ghp_DEV012SyntheticToken0123456789"
PASSWORD = "dev012-password-secret"
DSN_PASSWORD = "dev012-dsn-password-secret"
AUTH_VALUE = "dev012-authorization-secret"
STEP_NAME = "pytest:secret-redaction"
TEST_FILE = "tests/test_redaction.py"
TEST_NAME = "test_keeps_diagnostics"
EXIT_CODE = 23


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
    mcp = StructuredFastMCP("web-ci-dev012-secret-redaction")
    ci_mcp.register_private_ci_mcp_tools(mcp)
    return mcp


async def _call(mcp, name, **arguments):
    return _structured_result(await mcp.call_tool(name, arguments))


def _new_job():
    return db.create_or_get_job(
        repository="owner/repo",
        branch="feature/secret-redaction",
        commit_sha="a" * 40,
        profile="repo-auto-check",
        priority=100,
        timeout_seconds=900,
        force_rerun=True,
        supersede_previous=False,
        base_sha="b" * 40,
        changed_files=[],
    )


def _dsn():
    return f"postgresql://dev012_user:{DSN_PASSWORD}@db.example.invalid:5432/app"


def _fixture_log():
    return "\n".join(
        [
            f"FAILED {TEST_FILE}::{TEST_NAME} - AssertionError",
            f"{TEST_FILE}:37:5: AssertionError: expected redaction",
            f"step={STEP_NAME} exit_code={EXIT_CODE}",
            f"token={TOKEN}",
            f"password={PASSWORD}",
            f"DATABASE_URL={_dsn()}",
            f"Authorization: Bearer {AUTH_VALUE}",
        ]
    ) + "\n"


def _assert_no_fixture_secrets(payload):
    serialized = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    for secret in (TOKEN, PASSWORD, DSN_PASSWORD, AUTH_VALUE):
        assert secret not in serialized
    return serialized


def _assert_redacted(payload):
    serialized = _assert_no_fixture_secrets(payload)
    assert "[REDACTED]" in serialized
    return serialized


def _mark_job_failed(job_id):
    connection = db._get_db()
    connection.execute(
        "UPDATE ci_jobs SET status='failed', exit_code=?, error_code='CI_STEP_FAILED' WHERE job_id=?",
        (EXIT_CODE, job_id),
    )
    connection.commit()


def _seed_failed_step():
    job = _new_job()
    step_id = db.add_step(job["job_id"], STEP_NAME, status="running")
    log_end = db.append_log_chunk(job["job_id"], _fixture_log())
    assert db.finish_step(step_id, "failed", exit_code=EXIT_CODE, log_end_offset=log_end)
    _mark_job_failed(job["job_id"])
    return db.get_job(job["job_id"]), step_id


def _seed_paginated_failed_step(page_count=4):
    job = _new_job()
    step_id = db.add_step(job["job_id"], STEP_NAME, status="running")
    raw_pages = []
    for index in range(page_count):
        page = f"page={index}\n" + _fixture_log()
        raw_pages.append(page)
        log_end = db.append_log_chunk(job["job_id"], page)
    assert db.finish_step(step_id, "failed", exit_code=EXIT_CODE, log_end_offset=log_end)
    _mark_job_failed(job["job_id"])
    return db.get_job(job["job_id"]), step_id, raw_pages


@pytest.mark.asyncio
async def test_tail_job_and_step_log_redact_secret_fixtures_without_losing_step_evidence(
    get_mcp,
):
    job, step_id = _seed_failed_step()

    tail = await _call(
        get_mcp, "get_private_ci_log_tail", job_id=job["job_id"], lines=100
    )
    job_log = await _call(get_mcp, "get_private_ci_logs", job_id=job["job_id"])
    step_log = await _call(
        get_mcp, "get_private_ci_logs", job_id=job["job_id"], step_id=step_id
    )

    for payload in (tail, job_log, step_log):
        serialized = _assert_redacted(payload)
        assert f"{TEST_FILE}:37:5" in serialized

    assert step_log["step"]["step_name"] == STEP_NAME
    assert step_log["step"]["status"] == "failed"
    assert step_log["step"]["exit_code"] == EXIT_CODE
    assert step_log["step_selector"] == {"step_id": step_id}


@pytest.mark.asyncio
async def test_precise_step_log_pagination_redacts_every_page_and_keeps_diagnostics(get_mcp):
    job, step_id, raw_pages = _seed_paginated_failed_step()

    first = await _call(
        get_mcp,
        "get_private_ci_logs",
        job_id=job["job_id"],
        step_id=step_id,
        limit=1,
    )
    assert first["cursor"] is None
    assert first["has_more"] is True
    assert first["next_cursor"]

    continuation = await _call(
        get_mcp,
        "get_private_ci_logs",
        job_id=job["job_id"],
        step_id=step_id,
        cursor=first["next_cursor"],
        limit=1,
    )
    assert continuation["cursor"] == first["next_cursor"]
    assert continuation["has_more"] is True
    assert continuation["next_cursor"]

    cursor_only = await _call(
        get_mcp,
        "get_private_ci_logs",
        cursor=continuation["next_cursor"],
        limit=1,
    )
    assert cursor_only["cursor"] == continuation["next_cursor"]

    pages = [first, continuation, cursor_only]
    while pages[-1]["has_more"]:
        previous = pages[-1]
        page = await _call(
            get_mcp,
            "get_private_ci_logs",
            cursor=previous["next_cursor"],
            limit=1,
        )
        assert page["cursor"] == previous["next_cursor"]
        pages.append(page)

    assert len(pages) == len(raw_pages)
    previous_end = pages[0]["step_log_range"]["start_offset"]
    reconstructed = ""
    for index, page in enumerate(pages):
        serialized = _assert_redacted(page)
        assert page["ok"] is True
        assert page["mode"] == "step"
        assert page["step_selector"] == {"step_id": step_id}
        assert page["step"]["step_id"] == step_id
        assert page["step"]["step_name"] == STEP_NAME
        assert page["step"]["status"] == "failed"
        assert page["step"]["exit_code"] == EXIT_CODE
        assert TEST_FILE in serialized
        assert TEST_NAME in serialized
        assert f"{TEST_FILE}:37:5" in serialized
        assert len(page["chunks"]) == 1
        chunk = page["chunks"][0]
        assert chunk["offset_from"] == previous_end
        assert chunk["offset_to"] > chunk["offset_from"]
        reconstructed += chunk["content"]
        previous_end = chunk["offset_to"]
        if index:
            assert page["cursor"] == pages[index - 1]["next_cursor"]

    expected_redacted = "".join(failure_pack.redact_text(raw) for raw in raw_pages)
    assert reconstructed == expected_redacted
    assert previous_end == pages[-1]["step_log_range"]["end_offset"]
    assert pages[-1]["has_more"] is False
    assert pages[-1]["next_cursor"] is None


@pytest.mark.asyncio
async def test_job_wide_resource_and_chunk_paging_redact_secrets_without_losing_diagnostics(
    get_mcp,
):
    job = _new_job()
    raw_pages = []
    for index in range(80):
        page = (
            f"job-page={index:03d} "
            + ("safe-diagnostic-context " * 24)
            + "\n"
            + _fixture_log()
        )
        raw_pages.append(page)
        db.append_log_chunk(job["job_id"], page)
    _mark_job_failed(job["job_id"])

    inline = await _call(
        get_mcp,
        "get_private_ci_logs",
        job_id=job["job_id"],
        offset=0,
        limit=200,
    )
    _assert_no_fixture_secrets(inline)
    meta = inline["response_meta"]
    assert meta["mode"] == "resource"
    assert meta["truncated"] is True
    assert meta["has_more"] is True

    resource_text = mcp_response.read_response_resource_text(meta["resource_uri"])
    resource = json.loads(resource_text)
    resource_serialized = _assert_redacted(resource)
    assert TEST_FILE in resource_serialized
    assert TEST_NAME in resource_serialized
    assert f"{TEST_FILE}:37:5" in resource_serialized
    assert f"exit_code={EXIT_CODE}" in resource_serialized

    parts = []
    offset = 0
    page_count = 0
    while True:
        page = mcp_response.read_response_resource_chunk(
            meta["resource_uri"], offset_bytes=offset, limit_bytes=1024
        )
        page_count += 1
        chunk_text = _assert_no_fixture_secrets(page["content"])
        parts.append(chunk_text)
        assert page["offset_from"] == offset
        if page["has_more"]:
            assert page["next_offset"] is not None
            assert page["next_offset"] > offset
            offset = page["next_offset"]
            continue
        assert page["next_offset"] is None
        assert page["has_more"] is False
        break

    assert page_count > 1
    assert "".join(parts) == resource_text
    assert json.loads("".join(parts)) == resource


def test_failure_pack_and_its_resource_redact_secrets_but_keep_test_file_line_and_exit_code(
    isolated_db, monkeypatch
):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(isolated_db / "controller.db"))
    monkeypatch.setenv("MCP_RESPONSE_RESOURCE_DIR", str(isolated_db / "resources"))
    monkeypatch.setattr(
        failure_pack,
        "get_steps",
        lambda job_id: [
            {
                "step_id": 12,
                "step_name": STEP_NAME,
                "status": "failed",
                "exit_code": EXIT_CODE,
                "log_start_offset": 0,
                "log_end_offset": len(_fixture_log().encode("utf-8")),
            }
        ],
    )
    command = (
        f"token={TOKEN} password={PASSWORD} DATABASE_URL={_dsn()} "
        f'--header "Authorization: Bearer {AUTH_VALUE}" pytest {TEST_FILE}::{TEST_NAME}'
    )
    job = {
        "job_id": "job-dev012-failure-pack",
        "repository": "owner/repo",
        "branch": "feature/secret-redaction",
        "commit_sha": "a" * 40,
        "base_sha": "b" * 40,
        "profile": "repo-auto-check",
        "status": "failed",
        "exit_code": EXIT_CODE,
        "error_code": "CI_STEP_FAILED",
        "error_message": f"{TEST_FILE}:37:5 token={TOKEN}",
        "changed_files": [{"path": TEST_FILE, "operation": "modified"}],
        "changed_files_total": 1,
        "summary": {
            "steps": [
                {
                    "step_id": 12,
                    "step_name": STEP_NAME,
                    "status": "failed",
                    "exit_code": EXIT_CODE,
                    "command": command,
                }
            ]
        },
    }

    result = failure_pack.build_failure_pack(job, log_tail=_fixture_log())
    serialized = _assert_redacted(result)

    assert result["failed_step"]["step_name"] == STEP_NAME
    assert result["failed_step"]["exit_code"] == EXIT_CODE
    failed_test = next(item for item in result["failed_tests"] if item["file"] == TEST_FILE)
    assert failed_test["name"] == TEST_NAME
    assert failed_test["line"] == 37
    assert failed_test["column"] == 5
    assert TEST_FILE in serialized

    durable = failure_pack.read_failure_pack(result["failure_pack_id"])
    durable_serialized = _assert_redacted(durable)
    assert TEST_FILE in durable_serialized
    assert TEST_NAME in durable_serialized
    assert f"{TEST_FILE}:37:5" in durable_serialized

    failure_pack_store.init_failure_pack_db()
    with mygithub12._db() as controller_db:
        row = controller_db.execute(
            "SELECT payload_json FROM development_failure_packs WHERE failure_pack_id=?",
            (result["failure_pack_id"],),
        ).fetchone()
    assert row is not None
    raw_persisted_json = row["payload_json"]
    _assert_redacted(raw_persisted_json)
    assert TEST_FILE in raw_persisted_json
    assert TEST_NAME in raw_persisted_json
    assert f"{TEST_FILE}:37:5" in raw_persisted_json

    original_resource_uri = result["resource_uri"]
    original_resource_text = mcp_response.read_response_resource_text(original_resource_uri)
    original_resource = json.loads(original_resource_text)
    _assert_redacted(original_resource)
    assert original_resource["failed_step"]["exit_code"] == EXIT_CODE
    assert any(
        item["file"] == TEST_FILE
        and item["name"] == TEST_NAME
        and item["line"] == 37
        and item["column"] == 5
        for item in original_resource["failed_tests"]
    )

    resource_meta = Path(
        isolated_db
        / "resources"
        / f"{original_resource_uri.rsplit('/', 1)[-1]}.meta.json"
    )
    expired = json.loads(resource_meta.read_text(encoding="utf-8"))
    expired["expires_at"] = 0
    resource_meta.write_text(json.dumps(expired), encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        mcp_response.read_response_resource_text(original_resource_uri)

    def _forbid_ci_read(*args, **kwargs):
        raise AssertionError("failure-pack rematerialization must not reread CI/log evidence")

    monkeypatch.setattr(failure_pack, "get_log_tail", _forbid_ci_read)
    monkeypatch.setattr(failure_pack, "get_steps", _forbid_ci_read)

    rematerialized = failure_pack.materialize_failure_pack(result["failure_pack_id"])
    assert rematerialized["failure_pack_id"] == result["failure_pack_id"]
    assert rematerialized["resource_uri"] != original_resource_uri
    assert rematerialized["rematerialize"]["rerun_ci"] is False
    _assert_redacted(rematerialized)

    rematerialized_resource_text = mcp_response.read_response_resource_text(
        rematerialized["resource_uri"]
    )
    rematerialized_resource = json.loads(rematerialized_resource_text)
    rematerialized_serialized = _assert_redacted(rematerialized_resource)
    assert rematerialized_resource == durable
    assert TEST_FILE in rematerialized_serialized
    assert TEST_NAME in rematerialized_serialized
    assert f"{TEST_FILE}:37:5" in rematerialized_serialized
    assert rematerialized_resource["failed_step"]["step_name"] == STEP_NAME
    assert rematerialized_resource["failed_step"]["exit_code"] == EXIT_CODE
    assert any(
        item["file"] == TEST_FILE
        and item["name"] == TEST_NAME
        and item["line"] == 37
        and item["column"] == 5
        for item in rematerialized_resource["failed_tests"]
    )


def test_full_private_ci_resource_is_redacted_recursively_and_preserves_diagnostics(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MCP_RESPONSE_RESOURCE_DIR", str(tmp_path / "resources"))
    large_context = "diagnostic-context-line\n" * 4000
    job = {
        "job_id": "job-dev012-full-resource",
        "repository": "owner/repo",
        "branch": "feature/secret-redaction",
        "commit_sha": "c" * 40,
        "base_sha": "b" * 40,
        "profile": "repo-auto-check",
        "status": "failed",
        "exit_code": EXIT_CODE,
        "changed_files": [{"path": TEST_FILE, "operation": "modified"}],
        "changed_files_total": 1,
        "summary": {
            "git_tree_sha": "d" * 40,
            "steps": [
                {
                    "step_name": STEP_NAME,
                    "status": "failed",
                    "exit_code": EXIT_CODE,
                    "command": f"pytest token={TOKEN} password={PASSWORD}",
                }
            ],
            "error_message": f"{TEST_FILE}:37:5 Authorization: Bearer {AUTH_VALUE}",
            "evidence": {
                "token": TOKEN,
                "password": PASSWORD,
                "database_url": _dsn(),
                "headers": {"Authorization": f"Bearer {AUTH_VALUE}"},
                "diagnostic": f"{TEST_FILE}:37:5",
                "large_context": large_context,
            },
        },
    }
    persisted_steps = [
        {
            "step_name": STEP_NAME,
            "status": "failed",
            "exit_code": EXIT_CODE,
            "log_start_offset": 100,
            "log_end_offset": 200,
        }
    ]

    full = ci_mcp.build_private_ci_snapshot_response(None, job, persisted_steps, "full")
    serialized = _assert_redacted(full)
    assert TEST_FILE in serialized
    assert f"{TEST_FILE}:37:5" in serialized
    assert full["steps"][0]["step_name"] == STEP_NAME
    assert full["steps"][0]["exit_code"] == EXIT_CODE
    assert full["changed_files"] == [{"path": TEST_FILE, "operation": "modified"}]

    prepared = mcp_response.prepare_tool_response(full)
    meta = prepared["response_meta"]
    assert meta["mode"] == "resource"
    assert meta["truncated"] is True
    restored = json.loads(mcp_response.read_response_resource_text(meta["resource_uri"]))
    restored_serialized = _assert_redacted(restored)
    assert f"{TEST_FILE}:37:5" in restored_serialized
    assert restored["steps"][0]["step_name"] == STEP_NAME
    assert restored["steps"][0]["exit_code"] == EXIT_CODE
