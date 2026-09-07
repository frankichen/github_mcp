import json

import pytest

from app import ci_database as db
from app import ci_mcp
from app import development_failure_pack as failure_pack
from app import mcp_response
from app.mcp_response import StructuredFastMCP


TOKEN = "ghp_DEV012SyntheticToken0123456789"
PASSWORD = "dev012-password-secret"
DSN_PASSWORD = "dev012-dsn-password-secret"
AUTH_VALUE = "dev012-authorization-secret"
STEP_NAME = "pytest:secret-redaction"
TEST_FILE = "tests/test_secret_redaction.py"
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


def _assert_redacted(payload):
    serialized = json.dumps(payload, ensure_ascii=False)
    for secret in (TOKEN, PASSWORD, DSN_PASSWORD, AUTH_VALUE):
        assert secret not in serialized
    assert "[REDACTED]" in serialized
    return serialized


def _seed_failed_step():
    job = _new_job()
    step_id = db.add_step(job["job_id"], STEP_NAME, status="running")
    log_end = db.append_log_chunk(job["job_id"], _fixture_log())
    assert db.finish_step(step_id, "failed", exit_code=EXIT_CODE, log_end_offset=log_end)
    connection = db._get_db()
    connection.execute(
        "UPDATE ci_jobs SET status='failed', exit_code=?, error_code='CI_STEP_FAILED' WHERE job_id=?",
        (EXIT_CODE, job["job_id"]),
    )
    connection.commit()
    return db.get_job(job["job_id"]), step_id


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
        f"Authorization='Bearer {AUTH_VALUE}' pytest {TEST_FILE}::{TEST_NAME}"
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
    _assert_redacted(durable)
    resource = json.loads(
        mcp_response.read_response_resource_text(result["resource_uri"])
    )
    _assert_redacted(resource)
    assert resource["failed_step"]["exit_code"] == EXIT_CODE
    assert any(
        item["file"] == TEST_FILE and item["line"] == 37
        for item in resource["failed_tests"]
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
