import inspect
import json
import time

import pytest

from app import ci_database as db
from app import ci_mcp
from app import ci_request_dispatch as dispatch
from app import ci_request_store as requests
from app.mcp_response import MAX_SAFE_INLINE_BYTES, StructuredFastMCP, read_response_resource_chunk


REPOSITORY = "frankichen/github_mcp"
BRANCH = "ai/web-ci-dev-004-test"
PROFILE = "repo-auto-check"
COMMIT = "a" * 40
TREE = "b" * 40
RESOURCE_FALLBACK_STEP_COUNT = 8
RESOURCE_FALLBACK_ARGUMENT_REPEAT = 20
RESOURCE_FALLBACK_CHANGED_FILE_COUNT = 80
RESOURCE_FALLBACK_EVIDENCE_REPEAT = 5000
RESOURCE_FALLBACK_SAMPLE_COUNT = 80
RESOURCE_CHUNK_BYTES = 8192


def _structured_result(call_result):
    if isinstance(call_result, tuple):
        return call_result[1]
    structured = getattr(call_result, "structured_content", None)
    if structured is None:
        structured = getattr(call_result, "structuredContent", None)
    return structured


def _reset_current_db_connection():
    current = getattr(db._local, "db", None)
    if current is not None:
        current.close()
    db._local.db = None


def _reopen_db():
    _reset_current_db_connection()
    db.init_db()


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    path = tmp_path / "ci-dev004.db"
    monkeypatch.setattr(db, "DB_PATH", str(path))
    _reset_current_db_connection()
    db.init_db()
    yield path
    _reset_current_db_connection()


@pytest.fixture
def get_mcp(isolated_db):
    mcp = StructuredFastMCP("web-ci-dev004-get")
    ci_mcp.register_private_ci_mcp_tools(mcp)
    return mcp


async def _get(mcp, **kwargs):
    return _structured_result(await mcp.call_tool("get_private_ci_job", kwargs))


def _create_request(seed="a", key="dev004-request"):
    commit_sha = seed * 40
    payload = {
        "schema": "private-ci-start-v2",
        "repository": REPOSITORY,
        "branch": BRANCH,
        "commit_sha": commit_sha,
        "tree_sha": "derived_from_exact_commit_during_preflight",
        "profile": PROFILE,
        "timeout_seconds": 900,
        "requested_timeout_seconds": 900,
        "requested_priority": "normal",
        "priority": 100,
        "base_sha": "",
        "force_rerun": False,
        "supersede_previous": False,
        "effective_config_digest": f"config-{seed}",
    }
    return requests.create_or_get_ci_request(
        repository=REPOSITORY,
        branch=BRANCH,
        commit_sha=commit_sha,
        tree_sha=None,
        profile=PROFILE,
        effective_config_digest=payload["effective_config_digest"],
        idempotency_key=key,
        normalized_request_hash=requests.compute_normalized_request_hash(payload),
        request_payload=payload,
    )


def _prepare_request(seed="a", key="dev004-request"):
    request = _create_request(seed, key)
    return requests.transition_ci_request(
        request["request_id"], request["revision"], "preparing", "preparing"
    )


def _link_worker(seed="a", key="dev004-request", tree_sha=TREE):
    request = _prepare_request(seed, key)
    queued = requests.dispatch_ci_request(
        request["request_id"],
        expected_revision=request["revision"],
        tree_sha=tree_sha,
        changed_files=["services/github-action-service/app/ci_mcp.py"],
        changed_files_total=1,
        changed_files_truncated=False,
    )
    return queued, queued["worker_job_id"]


def _set_worker_status(job_id, status, worker_id=None):
    connection = db._get_db()
    connection.execute(
        "UPDATE ci_jobs SET status = ?, worker_id = ? WHERE job_id = ?",
        (status, worker_id, job_id),
    )
    connection.commit()


@pytest.mark.asyncio
async def test_get_schema_accepts_request_or_job_identity(get_mcp):
    tool = get_mcp._tool_manager.get_tool("get_private_ci_job")
    schema = tool.parameters
    assert schema["properties"]["job_id"]["default"] == ""
    assert schema["properties"]["request_id"]["default"] == ""
    assert schema["properties"]["detail_level"]["default"] == "summary"
    assert "job_id" not in schema.get("required", [])
    assert "request_id" not in schema.get("required", [])

    missing = await _get(get_mcp)
    assert missing["error"]["code"] == "INVALID_ARGUMENT"


@pytest.mark.asyncio
async def test_request_only_accepted_snapshot_has_no_fake_worker(get_mcp):
    request = _create_request()
    result = await _get(get_mcp, request_id=request["request_id"])

    assert result["request_id"] == request["request_id"]
    assert result["job_id"] is None
    assert result["worker_job_id"] is None
    assert result["phase"] == result["status"] == "accepted"
    assert result["request_phase"] == result["request_status"] == "accepted"
    assert result["revision"] == result["request_revision"] == 0
    assert result["terminal"] is False
    assert result["continuation_required"] is True
    assert result["tree_sha"] is None
    assert result["tree_pending"] is True
    assert result["current_step"] is None
    assert result["queue_state"] == "not_created"
    assert result["queue_position"] is None
    assert result["worker_id"] is None
    assert result["worker_available"] is None
    assert result["preflight_error"] is None
    assert result["failure_pack_available"] is False
    assert result["attestation_available"] is False
    assert result["next_actions"] == [
        {"tool": "get_private_ci_job", "request_id": request["request_id"]}
    ]


@pytest.mark.asyncio
async def test_request_only_preparing_snapshot_is_queryable_without_worker(get_mcp):
    request = _prepare_request()
    result = await _get(get_mcp, request_id=request["request_id"])

    assert result["phase"] == result["status"] == "preparing"
    assert result["request_stage"] == "preparing"
    assert result["revision"] == 1
    assert result["worker_job_id"] is None
    assert result["current_step"] is None
    assert result["terminal"] is False
    assert result["continuation_required"] is True


@pytest.mark.asyncio
async def test_request_only_preflight_failed_snapshot_is_terminal_not_worker_failed(get_mcp):
    request = _prepare_request()
    request = requests.transition_ci_request(
        request["request_id"],
        request["revision"],
        "terminal",
        "preflight_failed",
        preflight_error_id="ci_preflight_test",
        preflight_error_code="CI_PREFLIGHT_TEST_FAILED",
        terminal_reason="synthetic preflight failure",
    )
    result = await _get(get_mcp, request_id=request["request_id"])

    assert result["phase"] == "terminal"
    assert result["status"] == "preflight_failed"
    assert result["terminal"] is True
    assert result["worker_job_id"] is None
    assert result["worker_status"] is None
    assert result["preflight_error"] == {
        "error_id": "ci_preflight_test",
        "code": "CI_PREFLIGHT_TEST_FAILED",
        "reason": "synthetic preflight failure",
    }
    assert result["failure_pack_available"] is False
    assert result["attestation_available"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "worker_status,effective_phase,terminal",
    [
        ("queued", "queued", False),
        ("running", "running", False),
        ("passed", "terminal", True),
        ("failed", "terminal", True),
        ("timed_out", "terminal", True),
        ("cancelled", "terminal", True),
        ("superseded", "terminal", True),
        ("worker_lost", "terminal", True),
        ("internal_error", "terminal", True),
    ],
)
async def test_worker_execution_truth_overrides_stale_queued_request(
    get_mcp, worker_status, effective_phase, terminal
):
    request, job_id = _link_worker()
    _set_worker_status(job_id, worker_status)

    result = await _get(get_mcp, request_id=request["request_id"])

    persisted = requests.get_ci_request(request["request_id"])
    assert persisted["phase"] == persisted["status"] == "queued"
    assert persisted["revision"] == 2
    assert result["request_phase"] == result["request_status"] == "queued"
    assert result["request_revision"] == result["revision"] == 2
    assert result["worker_status"] == worker_status
    assert result["phase"] == effective_phase
    assert result["status"] == worker_status
    assert result["terminal"] is terminal
    assert result["continuation_required"] is (not terminal)
    assert result["failure_pack_available"] is False
    assert result["attestation_available"] is False


@pytest.mark.asyncio
async def test_summary_returns_current_step_queue_and_durable_worker_availability(get_mcp):
    request, job_id = _link_worker()
    assert db.register_worker("wsl-ci-test", "test-worker-token", [PROFILE], 1)
    connection = db._get_db()
    connection.execute(
        "UPDATE ci_jobs SET status = 'running', worker_id = 'wsl-ci-test' WHERE job_id = ?",
        (job_id,),
    )
    connection.execute(
        "UPDATE ci_workers SET status = 'busy', current_job_id = ? WHERE worker_id = 'wsl-ci-test'",
        (job_id,),
    )
    connection.commit()
    db.add_step(job_id, "pytest", status="running")

    result = await _get(get_mcp, request_id=request["request_id"], job_id=job_id)

    assert result["current_step"] == "pytest"
    assert result["steps_total"] == 1
    assert result["completed_steps_count"] == 0
    assert result["failed_steps_count"] == 0
    assert result["worker_id"] == "wsl-ci-test"
    assert result["worker_assigned"] is True
    assert result["worker_online"] is True
    assert result["worker_available"] is True
    assert result["worker_agent_status"] == "busy"
    assert result["worker_current_job"] == job_id


@pytest.mark.asyncio
async def test_identity_lookup_matching_mismatch_and_unknown_are_deterministic(get_mcp):
    request_a, job_a = _link_worker("a", "request-a", "b" * 40)
    request_b, job_b = _link_worker("c", "request-b", "d" * 40)

    by_request = await _get(get_mcp, request_id=request_a["request_id"])
    by_job = await _get(get_mcp, job_id=job_a)
    both = await _get(get_mcp, request_id=request_a["request_id"], job_id=job_a)
    mismatch = await _get(get_mcp, request_id=request_a["request_id"], job_id=job_b)
    unknown_request = await _get(get_mcp, request_id="ci_req_unknown")
    unknown_worker = await _get(get_mcp, job_id="job-unknown")

    assert by_request["request_id"] == by_job["request_id"] == request_a["request_id"]
    assert by_request["job_id"] == by_job["job_id"] == job_a
    assert both["ok"] is True
    assert mismatch["error"]["code"] == "CI_REQUEST_IDENTITY_MISMATCH"
    assert mismatch["error"]["details"]["field"] == "worker_job_id"
    assert unknown_request["error"]["code"] == "CI_REQUEST_NOT_FOUND"
    assert unknown_worker["error"]["code"] == "PRIVATE_CI_JOB_NOT_FOUND"
    assert request_b["request_id"] != request_a["request_id"]


@pytest.mark.asyncio
async def test_worker_row_identity_corruption_fails_closed_without_combining_rows(get_mcp):
    request, job_id = _link_worker()
    connection = db._get_db()
    connection.execute(
        "UPDATE ci_jobs SET repository = 'other/repository' WHERE job_id = ?", (job_id,)
    )
    connection.commit()

    result = await _get(get_mcp, request_id=request["request_id"])

    assert result["error"]["code"] == "CI_REQUEST_IDENTITY_MISMATCH"
    assert result["error"]["details"]["field"] == "repository"


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_status", ["queued", "running", "passed", "failed"])
async def test_pre_dev002_legacy_job_projection_remains_readable_by_job_id(
    get_mcp, legacy_status
):
    legacy = db.create_or_get_job(
        REPOSITORY, BRANCH, COMMIT, PROFILE, 100, 900, True, False
    )
    _set_worker_status(legacy["job_id"], legacy_status)
    requests.init_ci_request_schema(db._get_db())

    result = await _get(get_mcp, job_id=legacy["job_id"])

    assert result["ok"] is True
    assert result["job_id"] == legacy["job_id"]
    assert result["request_id"] == legacy["job_id"]
    assert result["worker_status"] == legacy_status
    assert result["tree_sha"] is None
    assert result["failure_pack_available"] is False
    assert result["attestation_available"] is False


@pytest.mark.asyncio
async def test_summary_never_waits_sleeps_calls_network_or_schedules_work(get_mcp, monkeypatch):
    request, job_id = _link_worker()
    _set_worker_status(job_id, "running")

    tool = get_mcp._tool_manager.get_tool("get_private_ci_job")
    source = inspect.getsource(tool.fn)
    for forbidden_name in (
        "wait_for_job_change",
        "wait_private_ci_job",
        "sleep(",
        "get_private_ci_logs",
        "get_log_tail",
        "get_log_chunks",
        "get_github_changed_files_result",
    ):
        assert forbidden_name not in source

    def forbidden(*args, **kwargs):
        raise AssertionError("summary snapshot must remain a pure non-blocking local read")

    from app import github_utils

    monkeypatch.setattr(ci_mcp, "wait_for_job_change", forbidden)
    monkeypatch.setattr(db._job_change_condition, "wait", forbidden)
    monkeypatch.setattr(time, "sleep", forbidden)
    monkeypatch.setattr(github_utils, "get_github_changed_files_result", forbidden)
    monkeypatch.setattr(dispatch, "process_ci_request", forbidden)
    monkeypatch.setattr(ci_mcp, "schedule_ci_request_preparation", forbidden)

    result = await _get(get_mcp, request_id=request["request_id"], detail_level="summary")
    assert result["ok"] is True
    assert result["status"] == "running"


@pytest.mark.asyncio
async def test_summary_never_reads_logs_tail_or_chunks(get_mcp, monkeypatch):
    request, job_id = _link_worker()
    _set_worker_status(job_id, "failed")

    def forbidden(*args, **kwargs):
        raise AssertionError("summary must not load CI logs")

    monkeypatch.setattr(ci_mcp, "get_log_chunks", forbidden)
    monkeypatch.setattr(ci_mcp, "get_log_tail", forbidden)

    result = await _get(get_mcp, request_id=request["request_id"], detail_level="summary")
    assert result["ok"] is True
    assert result["status"] == "failed"
    serialized = json.dumps(result, ensure_ascii=False)
    assert "log_start_offset" not in serialized
    assert "log_end_offset" not in serialized
    assert "command" not in serialized


@pytest.mark.asyncio
async def test_get_snapshot_is_read_only_and_does_not_create_failure_or_attestation(get_mcp):
    request, job_id = _link_worker()
    _set_worker_status(job_id, "failed")
    connection = db._get_db()
    before_request = dict(
        connection.execute(
            "SELECT * FROM ci_requests WHERE request_id = ?", (request["request_id"],)
        ).fetchone()
    )
    before_job = dict(
        connection.execute("SELECT * FROM ci_jobs WHERE job_id = ?", (job_id,)).fetchone()
    )
    before_events = len(requests.get_ci_request_events(request["request_id"]))

    result = await _get(get_mcp, request_id=request["request_id"])

    after_request = dict(
        connection.execute(
            "SELECT * FROM ci_requests WHERE request_id = ?", (request["request_id"],)
        ).fetchone()
    )
    after_job = dict(
        connection.execute("SELECT * FROM ci_jobs WHERE job_id = ?", (job_id,)).fetchone()
    )
    assert before_request == after_request
    assert before_job == after_job
    assert before_events == len(requests.get_ci_request_events(request["request_id"]))
    assert result["failure_pack_available"] is False
    assert result["attestation_available"] is False


@pytest.mark.asyncio
async def test_full_keeps_diagnostics_and_oversized_payload_uses_resource_while_summary_stays_inline(
    get_mcp, isolated_db, monkeypatch
):
    monkeypatch.setenv("MCP_RESPONSE_RESOURCE_DIR", str(isolated_db.parent / "resources"))
    request, job_id = _link_worker()
    worker_steps = [
        {
            "step_name": f"step-{index}",
            "command": "python -m pytest "
            + ("very-long-argument " * RESOURCE_FALLBACK_ARGUMENT_REPEAT),
            "status": "passed",
            "exit_code": 0,
            "duration_seconds": index + 0.5,
        }
        for index in range(RESOURCE_FALLBACK_STEP_COUNT)
    ]
    changed_files = [
        f"generated/file_{index:03d}.py"
        for index in range(RESOURCE_FALLBACK_CHANGED_FILE_COUNT)
    ]
    summary = {
        "status": "passed",
        "exit_code": 0,
        "git_tree_sha": TREE,
        "steps": worker_steps,
        "evidence": {"diagnostic_blob": "evidence-" * RESOURCE_FALLBACK_EVIDENCE_REPEAT},
        "performance": {"samples": list(range(RESOURCE_FALLBACK_SAMPLE_COUNT))},
    }
    connection = db._get_db()
    connection.execute(
        "UPDATE ci_jobs SET status = 'passed', summary_json = ?, changed_files_json = ?, "
        "changed_files_total = ? WHERE job_id = ?",
        (json.dumps(summary), json.dumps(changed_files), len(changed_files), job_id),
    )
    connection.commit()

    full = await _get(get_mcp, request_id=request["request_id"], detail_level="full")
    assert full["response_meta"]["mode"] == "resource"
    assert full["response_meta"]["requested_mode"] == "full"
    assert full["response_meta"]["truncated"] is True
    assert full["response_meta"]["resource_uri"].startswith("mygithub12://response/")

    parts = []
    offset = 0
    while True:
        page = read_response_resource_chunk(
            full["response_meta"]["resource_uri"], offset_bytes=offset, limit_bytes=4096
        )
        parts.append(page["content"])
        if not page["has_more"]:
            break
        offset = page["next_offset"]
    restored = json.loads("".join(parts))
    assert restored["request_id"] == request["request_id"]
    assert restored["changed_files"] == changed_files
    assert restored["summary"]["evidence"] == summary["evidence"]
    assert restored["steps"][0]["command"].startswith("python -m pytest")

    compact = await _get(get_mcp, request_id=request["request_id"], detail_level="summary")
    assert compact["response_meta"]["mode"] == "summary"
    assert compact["response_meta"]["resource_uri"] is None
    serialized = json.dumps(compact, ensure_ascii=False)
    assert "changed_files" not in compact
    assert "summary" not in compact
    assert "command" not in serialized
    assert "diagnostic_blob" not in serialized


@pytest.mark.asyncio
async def test_request_only_snapshot_survives_sqlite_reopen(get_mcp):
    request = _create_request()
    request_id = request["request_id"]
    _reopen_db()

    result = await _get(get_mcp, request_id=request_id)
    assert result["request_id"] == request_id
    assert result["phase"] == result["status"] == "accepted"
    assert result["worker_job_id"] is None


@pytest.mark.asyncio
async def test_linked_worker_truth_survives_sqlite_reopen_while_request_stays_queued(get_mcp):
    request, job_id = _link_worker()
    _set_worker_status(job_id, "passed")
    _reopen_db()

    result = await _get(get_mcp, request_id=request["request_id"])
    assert result["request_status"] == "queued"
    assert result["worker_status"] == "passed"
    assert result["phase"] == "terminal"
    assert result["status"] == "passed"
    assert result["terminal"] is True
