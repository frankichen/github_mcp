import asyncio
import inspect
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import ci_database as db
from app import ci_mcp
from app import ci_request_dispatch as dispatch
from app import ci_request_store as requests
from app.mcp_response import StructuredFastMCP


REPOSITORY = "frankichen/github_mcp"
BRANCH = "ai/web-ci-dev-003-test"
COMMIT = "a" * 40
TREE = "b" * 40
BASE = "c" * 40


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
    path = tmp_path / "ci-dev003.db"
    monkeypatch.setattr(db, "DB_PATH", str(path))
    _reset_current_db_connection()
    db.init_db()
    yield path
    _reset_current_db_connection()


@pytest.fixture
def start_mcp(isolated_db, monkeypatch):
    monkeypatch.setattr(ci_mcp, "schedule_ci_request_preparation", lambda request_id: True)
    mcp = StructuredFastMCP("web-ci-dev003-start")
    ci_mcp.register_private_ci_mcp_tools(mcp)
    return mcp


@pytest.fixture
def fake_long_running_ci():
    return {
        "logical_duration_seconds": 10 * 60 + 1,
        "worker_running": False,
        "terminal": False,
    }


async def _start(mcp, **overrides):
    args = {
        "repository": REPOSITORY,
        "branch": BRANCH,
        "commit_sha": COMMIT,
        "profile": "repo-auto-check",
        "timeout_seconds": 900,
        "priority": "normal",
        "force_rerun": False,
        "supersede_previous": False,
        "base_sha": "",
        "idempotency_key": "dev003-key",
    }
    args.update(overrides)
    return _structured_result(await mcp.call_tool("start_private_ci_job", args))


def _durable_request(key="dispatch-key", *, commit_sha=COMMIT):
    payload = {
        "schema": "private-ci-start-v2",
        "repository": "owner/repo",
        "branch": "main",
        "commit_sha": commit_sha,
        "tree_sha": "derived_from_exact_commit_during_preflight",
        "profile": "repo-auto-check",
        "timeout_seconds": 900,
        "requested_priority": "normal",
        "priority": 100,
        "base_sha": "",
        "force_rerun": False,
        "supersede_previous": False,
        "effective_config_digest": "config-v1",
    }
    request = requests.create_or_get_ci_request(
        repository=payload["repository"],
        branch=payload["branch"],
        commit_sha=payload["commit_sha"],
        tree_sha=None,
        profile=payload["profile"],
        effective_config_digest=payload["effective_config_digest"],
        idempotency_key=key,
        normalized_request_hash=requests.compute_normalized_request_hash(payload),
        request_payload=payload,
    )
    return request, payload


def _preflight_success():
    return {
        "tree_sha": TREE,
        "changed_files": ["app/example.py"],
        "changed_files_total": 1,
        "changed_files_truncated": False,
        "event_data": {"policy_source": "test"},
    }


def _prepare_request(key="dispatch-key"):
    request, payload = _durable_request(key)
    request = requests.transition_ci_request(
        request["request_id"], request["revision"], "preparing", "preparing"
    )
    return request, payload


def _counts():
    connection = db._get_db()
    return {
        "requests": connection.execute("SELECT COUNT(*) FROM ci_requests").fetchone()[0],
        "jobs": connection.execute("SELECT COUNT(*) FROM ci_jobs").fetchone()[0],
    }


@pytest.mark.asyncio
async def test_canonical_start_returns_durable_request_without_wait_network_or_sleep(
    start_mcp, monkeypatch
):
    def forbidden(*args, **kwargs):
        raise AssertionError("canonical start must not wait, compare GitHub, or sleep")

    from app import github_utils

    monkeypatch.setattr(ci_mcp, "wait_for_job_change", forbidden)
    monkeypatch.setattr(github_utils, "get_github_changed_files_result", forbidden)
    monkeypatch.setattr(time, "sleep", forbidden)

    result = await _start(start_mcp)

    assert result["ok"] is True
    assert result["phase"] == result["status"] == "accepted"
    assert result["revision"] == 0
    assert result["terminal"] is False
    assert result["continuation_required"] is True
    assert result["worker_job_id"] is None
    assert result["job_id"] is None
    assert result["tree_sha"] is None
    assert result["tree_pending"] is True
    assert _counts() == {"requests": 1, "jobs": 0}


@pytest.mark.asyncio
async def test_start_returns_before_fake_ten_minute_lifecycle(
    start_mcp, fake_long_running_ci
):
    result = await _start(start_mcp, idempotency_key="long-running-key")

    assert fake_long_running_ci["logical_duration_seconds"] > 600
    assert fake_long_running_ci["worker_running"] is False
    assert fake_long_running_ci["terminal"] is False
    assert result["phase"] == "accepted"
    assert result["worker_job_id"] is None
    assert result["continuation_required"] is True


@pytest.mark.asyncio
async def test_same_key_same_request_reuses_one_request(start_mcp):
    first = await _start(start_mcp, idempotency_key="same-key")
    second = await _start(start_mcp, idempotency_key="same-key")

    assert first["request_id"] == second["request_id"]
    assert first["normalized_request_hash"] == second["normalized_request_hash"]
    assert first["deduplicated"] is False
    assert second["deduplicated"] is True
    assert _counts() == {"requests": 1, "jobs": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        {"timeout_seconds": 899},
        {"priority": "high"},
        {"base_sha": BASE},
        {"force_rerun": True},
        {"supersede_previous": True},
    ],
)
async def test_same_key_execution_semantic_change_is_idempotency_conflict(
    start_mcp, mutation
):
    first = await _start(start_mcp, idempotency_key="conflict-key")
    conflict = await _start(start_mcp, idempotency_key="conflict-key", **mutation)

    assert first["ok"] is True
    assert conflict["ok"] is False
    assert conflict["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert _counts() == {"requests": 1, "jobs": 0}
    persisted = requests.get_ci_request(first["request_id"])
    assert persisted["revision"] == 0


@pytest.mark.asyncio
async def test_normalized_request_persists_execution_semantics(start_mcp):
    result = await _start(
        start_mcp,
        idempotency_key="payload-key",
        timeout_seconds=899,
        priority="high",
        base_sha=BASE,
        supersede_previous=True,
    )
    payload = requests.get_ci_request_payload(result["request_id"])

    assert payload["repository"] == REPOSITORY
    assert payload["branch"] == BRANCH
    assert payload["commit_sha"] == COMMIT
    assert payload["profile"] == "repo-auto-check"
    assert payload["timeout_seconds"] == 899
    assert payload["requested_priority"] == "high"
    assert isinstance(payload["priority"], int)
    assert payload["base_sha"] == BASE
    assert payload["force_rerun"] is False
    assert payload["supersede_previous"] is True
    assert payload["effective_config_digest"] == result["effective_config_digest"]


@pytest.mark.asyncio
async def test_legacy_no_key_behavior_keeps_dedup_and_force_rerun(start_mcp):
    first = await _start(start_mcp, idempotency_key="")
    second = await _start(start_mcp, idempotency_key="")
    forced_a = await _start(start_mcp, idempotency_key="", force_rerun=True)
    forced_b = await _start(start_mcp, idempotency_key="", force_rerun=True)

    assert first["request_id"] == second["request_id"]
    assert forced_a["request_id"] != forced_b["request_id"]
    assert forced_a["request_id"] != first["request_id"]
    assert _counts() == {"requests": 3, "jobs": 0}


@pytest.mark.asyncio
async def test_concurrent_same_key_starts_have_one_request_and_no_integrity_error(start_mcp):
    results = await asyncio.gather(
        *[_start(start_mcp, idempotency_key="concurrent-start") for _ in range(8)]
    )

    assert all(result["ok"] is True for result in results)
    assert len({result["request_id"] for result in results}) == 1
    assert _counts() == {"requests": 1, "jobs": 0}


def test_preflight_success_creates_and_binds_one_worker_job(isolated_db, monkeypatch):
    request, _ = _durable_request("preflight-success")
    monkeypatch.setattr(dispatch, "_perform_ci_request_preflight", lambda current: _preflight_success())

    queued = dispatch.process_ci_request(request["request_id"])

    assert queued["phase"] == queued["status"] == "queued"
    assert queued["tree_sha"] == TREE
    assert queued["worker_job_id"]
    assert requests.get_ci_request_by_worker_job_id(queued["worker_job_id"])["request_id"] == request["request_id"]
    assert db.get_job(queued["worker_job_id"])["job_id"] == queued["worker_job_id"]
    assert _counts() == {"requests": 1, "jobs": 1}
    assert [event["event_type"] for event in requests.get_ci_request_events(request["request_id"])] == [
        "accepted",
        "transition",
        "worker_queued",
    ]


def test_concurrent_preparing_dispatch_creates_at_most_one_worker(isolated_db):
    request, _ = _prepare_request("concurrent-dispatch")

    def run_dispatch():
        return requests.dispatch_ci_request(
            request["request_id"],
            expected_revision=request["revision"],
            tree_sha=TREE,
            changed_files=[],
            changed_files_total=0,
            changed_files_truncated=False,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run_dispatch(), range(2)))

    assert len({result["worker_job_id"] for result in results}) == 1
    assert _counts() == {"requests": 1, "jobs": 1}
    events = requests.get_ci_request_events(request["request_id"])
    assert sum(event["event_type"] == "worker_queued" for event in events) == 1


def test_crash_before_worker_create_recovers_same_request_and_one_job(isolated_db, monkeypatch):
    request, _ = _durable_request("crash-before-worker")
    request_id = request["request_id"]

    _reopen_db()
    reopened = requests.get_ci_request(request_id)
    assert reopened["phase"] == "accepted"
    assert reopened["worker_job_id"] is None
    assert _counts() == {"requests": 1, "jobs": 0}

    monkeypatch.setattr(dispatch, "_perform_ci_request_preflight", lambda current: _preflight_success())
    queued = dispatch.process_ci_request(request_id)

    assert queued["request_id"] == request_id
    assert queued["phase"] == "queued"
    assert _counts() == {"requests": 1, "jobs": 1}


def test_worker_create_bind_boundary_rolls_back_before_reopen_and_legacy_backfill(
    isolated_db, monkeypatch
):
    request, _ = _prepare_request("crash-bind-boundary")
    original_insert_event = requests._insert_event

    def crash_after_worker_insert(*args, **kwargs):
        if kwargs.get("event_type") == "worker_queued":
            raise RuntimeError("simulated crash after Worker INSERT before Request bind")
        return original_insert_event(*args, **kwargs)

    monkeypatch.setattr(requests, "_insert_event", crash_after_worker_insert)
    with pytest.raises(RuntimeError, match="simulated crash"):
        requests.dispatch_ci_request(
            request["request_id"],
            expected_revision=request["revision"],
            tree_sha=TREE,
            changed_files=[],
            changed_files_total=0,
            changed_files_truncated=False,
        )

    assert _counts() == {"requests": 1, "jobs": 0}
    assert requests.get_ci_request(request["request_id"])["worker_job_id"] is None

    _reopen_db()
    assert _counts() == {"requests": 1, "jobs": 0}
    assert requests.get_ci_request(request["request_id"])["request_id"] == request["request_id"]

    monkeypatch.setattr(requests, "_insert_event", original_insert_event)
    queued = requests.dispatch_ci_request(
        request["request_id"],
        expected_revision=request["revision"],
        tree_sha=TREE,
        changed_files=[],
        changed_files_total=0,
        changed_files_truncated=False,
    )
    assert queued["worker_job_id"]
    assert _counts() == {"requests": 1, "jobs": 1}


def test_crash_after_queued_reopen_does_not_requeue_or_legacy_project(isolated_db):
    request, _ = _prepare_request("crash-after-queued")
    queued = requests.dispatch_ci_request(
        request["request_id"],
        expected_revision=request["revision"],
        tree_sha=TREE,
        changed_files=[],
        changed_files_total=0,
        changed_files_truncated=False,
    )
    worker_job_id = queued["worker_job_id"]

    _reopen_db()
    reopened = requests.get_ci_request(request["request_id"])
    assert reopened["worker_job_id"] == worker_job_id
    assert reopened["phase"] == "queued"
    assert _counts() == {"requests": 1, "jobs": 1}

    replay = requests.dispatch_ci_request(
        request["request_id"],
        expected_revision=reopened["revision"],
        tree_sha=TREE,
        changed_files=[],
        changed_files_total=0,
        changed_files_truncated=False,
    )
    assert replay["worker_job_id"] == worker_job_id
    assert replay["deduplicated"] is True
    assert _counts() == {"requests": 1, "jobs": 1}
    events = requests.get_ci_request_events(request["request_id"])
    assert sum(event["event_type"] == "worker_queued" for event in events) == 1


def test_restart_recovery_reschedules_pending_request_identity(isolated_db, monkeypatch):
    request, _ = _durable_request("recovery-schedule")
    request_id = request["request_id"]
    _reopen_db()
    scheduled = []
    monkeypatch.setattr(dispatch, "schedule_ci_request_preparation", lambda value: scheduled.append(value) or True)

    result = dispatch.recover_pending_ci_requests()

    assert result == {"pending": 1, "scheduled": 1}
    assert scheduled == [request_id]
    assert requests.get_ci_request(request_id)["request_id"] == request_id
    assert _counts() == {"requests": 1, "jobs": 0}


def test_invalid_exact_tree_becomes_structured_preflight_failure(isolated_db, monkeypatch):
    request, _ = _durable_request("bad-tree")
    monkeypatch.setattr(dispatch, "_get_github_service", lambda: object())
    monkeypatch.setattr(
        dispatch.mygithub12,
        "plan_private_ci_job",
        lambda *args, **kwargs: {
            "applicable": True,
            "commit_sha": request["commit_sha"],
            "tree_sha": "not-a-sha",
        },
    )

    with pytest.raises(dispatch.CIPreflightFailure) as exc:
        dispatch._perform_ci_request_preflight(request)
    assert exc.value.code == "CI_PREFLIGHT_TREE_UNAVAILABLE"
    assert _counts() == {"requests": 1, "jobs": 0}


def test_changed_config_identity_fails_before_worker_queue(isolated_db, monkeypatch):
    request, _ = _durable_request("config-changed")
    monkeypatch.setattr(dispatch, "_get_github_service", lambda: object())
    monkeypatch.setattr(
        dispatch.mygithub12,
        "plan_private_ci_job",
        lambda *args, **kwargs: {
            "applicable": True,
            "commit_sha": request["commit_sha"],
            "tree_sha": TREE,
            "policy_source": "test",
            "detected_stacks": [],
            "selected_profiles": [request["profile"]],
            "workspaces": [],
        },
    )
    monkeypatch.setattr(
        dispatch.mygithub12,
        "resolve_identity",
        lambda *args, **kwargs: {"commit_sha": request["commit_sha"], "tree_sha": TREE},
    )
    monkeypatch.setattr(dispatch, "effective_ci_config_digest", lambda repository: "config-v2")

    with pytest.raises(dispatch.CIPreflightFailure) as exc:
        dispatch._perform_ci_request_preflight(request)
    assert exc.value.code == "CI_PREFLIGHT_CONFIG_CHANGED"
    assert _counts() == {"requests": 1, "jobs": 0}


@pytest.mark.asyncio
async def test_preflight_failed_replay_and_restart_preserve_terminal_error(
    start_mcp, monkeypatch
):
    started = await _start(start_mcp, idempotency_key="preflight-failure-key")

    def fail_preflight(current):
        raise dispatch.CIPreflightFailure(
            "CI_PREFLIGHT_BRANCH_HEAD_MISMATCH",
            "branch_no_longer_points_to_exact_commit",
        )

    monkeypatch.setattr(dispatch, "_perform_ci_request_preflight", fail_preflight)
    terminal = dispatch.process_ci_request(started["request_id"])
    assert terminal["phase"] == "terminal"
    assert terminal["status"] == "preflight_failed"
    assert terminal["worker_job_id"] is None
    assert terminal["preflight_error_id"]
    assert terminal["preflight_error_code"] == "CI_PREFLIGHT_BRANCH_HEAD_MISMATCH"
    assert _counts() == {"requests": 1, "jobs": 0}

    replay = await _start(start_mcp, idempotency_key="preflight-failure-key")
    assert replay["request_id"] == started["request_id"]
    assert replay["terminal"] is True
    assert replay["continuation_required"] is False
    assert replay["worker_job_id"] is None
    assert replay["preflight_error"]["code"] == "CI_PREFLIGHT_BRANCH_HEAD_MISMATCH"

    _reopen_db()
    reopened = requests.get_ci_request(started["request_id"])
    assert reopened["status"] == "preflight_failed"
    assert reopened["preflight_error_code"] == "CI_PREFLIGHT_BRANCH_HEAD_MISMATCH"
    assert reopened["worker_job_id"] is None
    assert _counts() == {"requests": 1, "jobs": 0}


def test_start_schema_has_first_class_idempotency_key_and_no_wait_source(isolated_db):
    mcp = StructuredFastMCP("web-ci-dev003-schema")
    ci_mcp.register_private_ci_mcp_tools(mcp)
    tool = mcp._tool_manager.get_tool("start_private_ci_job")
    schema = tool.parameters

    assert "idempotency_key" in schema["properties"]
    source = inspect.getsource(tool.fn)
    assert "wait_for_job_change" not in source
    assert "wait_private_ci_job" not in source
    assert "sleep(" not in source
    assert "get_github_changed_files_result" not in source
