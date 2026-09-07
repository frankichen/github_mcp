import json
from types import SimpleNamespace

import pytest

from app import ci_database as db
from app import ci_request_dispatch as dispatch
from app import ci_request_store as requests
from app import development_orchestrator as dx
from app import development_session_store as sessions
from app import mygithub12
from app import mygithub12_dx_mcp as dx_mcp
from app.mcp_response import StructuredFastMCP


SHA_A = "a" * 40
SHA_B = "b" * 40
TREE_B = "c" * 40


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


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    path = tmp_path / "ci-validate-dev007.db"
    monkeypatch.setattr(db, "DB_PATH", str(path))
    _reset_current_db_connection()
    db.init_db()
    yield path
    _reset_current_db_connection()


def _session(**overrides):
    value = {
        "session_id": "dev_validate_007",
        "workspace_id": "ws_validate_007",
        "repository": "owner/repo",
        "branch": "ai/validate-007",
        "base_branch": "main",
        "base_commit_sha": SHA_A,
        "head_commit_sha": SHA_B,
        "tree_sha": TREE_B,
        "session_revision": 7,
        "workspace_revision": 4,
        "status": "active",
        "pull_number": None,
        "metadata": {},
    }
    value.update(overrides)
    return value


def _prepared(profile="repo-fast-check"):
    return {
        "profile": profile,
        "base_sha": SHA_A,
        "selection": {"complete": True, "changed_paths": ["app/example.py"]},
        "priority": 100,
        "timeout_seconds": 900,
    }


def _start_request(monkeypatch, *, session=None, mode="fast", key="validate-key"):
    monkeypatch.setattr(dx, "effective_ci_config_digest", lambda repository: "config-v1")
    monkeypatch.setattr(dx, "schedule_ci_request_preparation", lambda request_id: True)
    session = session or _session()
    return dx.start_validation_request(
        SimpleNamespace(),
        session,
        mode,
        SHA_A,
        False,
        True,
        key,
        _prepared("repo-fast-check" if mode == "fast" else "repo-auto-check"),
    )


def _preflight_success():
    return {
        "tree_sha": TREE_B,
        "changed_files": ["app/example.py"],
        "changed_files_total": 1,
        "changed_files_truncated": False,
        "event_data": {"policy_source": "test"},
    }


def _queue_request(request, monkeypatch):
    preparing = requests.transition_ci_request(
        request["request_id"], request["revision"], "preparing", "preparing"
    )
    monkeypatch.setattr(
        dispatch, "_perform_ci_request_preflight", lambda current: _preflight_success()
    )
    queued = dispatch.process_ci_request(preparing["request_id"])
    assert queued["worker_job_id"]
    return queued


def _set_job_status(job_id, status, *, exit_code=None, current_step=None):
    connection = db._get_db()
    summary = {
        "steps": [
            {
                "step_name": current_step or "tests",
                "status": "failed" if status == "failed" else status,
                "exit_code": exit_code,
            }
        ],
        "git_tree_sha": TREE_B,
    }
    connection.execute(
        "UPDATE ci_jobs SET status=?, exit_code=?, current_step=?, summary_json=? WHERE job_id=?",
        (status, exit_code, current_step, json.dumps(summary), job_id),
    )
    connection.commit()


def test_start_validation_request_is_idempotent_and_revision_scoped(isolated_db, monkeypatch):
    session = _session(session_revision=7)
    first, selection = _start_request(monkeypatch, session=session, key="same-key")
    second, _ = _start_request(monkeypatch, session=session, key="same-key")

    assert first["request_id"] == second["request_id"]
    assert first["tree_sha"] == TREE_B
    assert second["deduplicated"] is True
    assert selection["complete"] is True
    connection = db._get_db()
    assert connection.execute("SELECT COUNT(*) FROM ci_requests").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM ci_jobs").fetchone()[0] == 0

    with pytest.raises(mygithub12.MyGithub12Error) as exc:
        _start_request(monkeypatch, session=_session(session_revision=8), key="same-key")
    assert exc.value.code == "IDEMPOTENCY_CONFLICT"


def test_accepted_request_without_worker_is_truthful_and_nonterminal(
    isolated_db, monkeypatch
):
    request, selection = _start_request(monkeypatch)
    result = dx.validation_observation(
        "dev_validate_007", 7, "fast", request, selection, include_failure_pack=True
    )

    assert result["request_id"] == request["request_id"]
    assert result["job"]["job_id"] is None
    assert result["phase"] == "accepted"
    assert result["status"] == "accepted"
    assert result["durable_status"]["queue_state"] == "not_created"
    assert result["continuation_required"] is True
    assert result["terminal"] is False
    assert result["next_actions"][0]["request_id"] == request["request_id"]


def test_queued_and_running_worker_snapshots_return_immediately(isolated_db, monkeypatch):
    request, selection = _start_request(monkeypatch)
    queued = _queue_request(request, monkeypatch)

    queued_result = dx.validation_observation(
        "dev_validate_007", 7, "fast", queued, selection
    )
    assert queued_result["job"]["job_id"] == queued["worker_job_id"]
    assert queued_result["status"] == "queued"
    assert queued_result["continuation_required"] is True

    _set_job_status(queued["worker_job_id"], "running", current_step="tests")
    running_result = dx.validation_observation(
        "dev_validate_007", 7, "fast", queued, selection
    )
    assert running_result["status"] == "running"
    assert running_result["phase"] == "running"
    assert running_result["job"]["current_step"] == "tests"
    assert running_result["continuation_required"] is True


def test_terminal_passed_returns_attestation_identity(isolated_db, monkeypatch):
    request, selection = _start_request(monkeypatch, mode="full", key="passed-key")
    queued = _queue_request(request, monkeypatch)
    _set_job_status(queued["worker_job_id"], "passed", exit_code=0)
    monkeypatch.setattr(
        dx.attestation_registry,
        "create_attestation_for_passed_job",
        lambda job_id: {"attestation_id": "att-validate", "private_ci_job_id": job_id},
    )
    monkeypatch.setattr(sessions, "record_validation", lambda *args, **kwargs: 1)

    result = dx.validation_observation("dev_validate_007", 7, "full", queued, selection)

    assert result["terminal"] is True
    assert result["merge_eligible"] is True
    assert result["attestation_id"] == "att-validate"
    assert result["attestation_available"] is True
    assert result["continuation_required"] is False


def test_terminal_failed_returns_failure_pack_identity(isolated_db, monkeypatch):
    request, selection = _start_request(monkeypatch, mode="full", key="failed-key")
    queued = _queue_request(request, monkeypatch)
    _set_job_status(queued["worker_job_id"], "failed", exit_code=1)
    monkeypatch.setattr(sessions, "record_validation", lambda *args, **kwargs: 1)
    monkeypatch.setattr(
        dx,
        "build_failure_pack",
        lambda job, affected=None: {
            "summary": {"job_id": job["job_id"], "status": "failed"},
            "content_sha256": "f" * 64,
            "resource_uri": "mygithub12://response/failure-pack",
        },
    )

    result = dx.validation_observation("dev_validate_007", 7, "full", queued, selection)

    assert result["terminal"] is True
    assert result["merge_eligible"] is False
    assert result["failure_pack_id"] == "f" * 64
    assert result["failure_pack_available"] is True
    assert result["continuation_required"] is False


@pytest.mark.asyncio
async def test_stale_session_revision_fail_stops_before_request_creation(monkeypatch):
    mcp = StructuredFastMCP("web-ci-dev007-stale-session")
    session = _session(session_revision=7)
    monkeypatch.setattr(sessions, "get_session", lambda session_id: dict(session))
    monkeypatch.setattr(dx, "validation_preflight", lambda *args, **kwargs: _prepared())
    monkeypatch.setattr(
        dx,
        "maybe_auto_renew_session_workspace",
        lambda *args, **kwargs: {
            "renewed": False,
            "session": dict(session),
            "workspace": {"revision": session["workspace_revision"]},
            "remaining_seconds": 3600.0,
            "audit": None,
            "recovery": None,
        },
    )
    monkeypatch.setattr(
        mygithub12, "workspace_write_preflight", lambda *args, **kwargs: {"ok": True}
    )

    def stale_transition(*args, **kwargs):
        raise mygithub12.MyGithub12Error(
            "DEVELOPMENT_SESSION_REVISION_MISMATCH", "development session changed"
        )

    monkeypatch.setattr(sessions, "transition", stale_transition)
    monkeypatch.setattr(
        dx,
        "start_validation_request",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("stale session must not start CI")
        ),
    )

    async def github_call(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def finalize_write(*args, **kwargs):
        return {}

    dx_mcp.register_dx_tools(mcp, github_call, SimpleNamespace(), finalize_write)
    result = _structured_result(
        await mcp.call_tool(
            "validate_development_task",
            {
                "development_session_id": session["session_id"],
                "expected_session_revision": 6,
                "mode": "fast",
            },
        )
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "DEVELOPMENT_SESSION_REVISION_MISMATCH"


def test_fake_ten_minute_validation_does_not_wait(
    isolated_db, monkeypatch, fake_long_running_ci
):
    fake_long_running_ci.transition("running", at_seconds=1)
    fake_long_running_ci.transition("running", at_seconds=601)
    request, selection = _start_request(monkeypatch, key="long-ci-key")

    result = dx.validation_observation("dev_validate_007", 7, "fast", request, selection)

    assert fake_long_running_ci.snapshot()["status"] == "running"
    assert fake_long_running_ci.snapshot()["logical_time_seconds"] == 601.0
    assert result["continuation_required"] is True
    assert result["terminal"] is False
