import inspect
from types import SimpleNamespace

import pytest

from app import ci_database
from app import ci_mcp
from app import development_converge as converge
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


async def _direct_call(fn, *args, **kwargs):
    result = fn(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


def _session(**overrides):
    value = {
        "session_id": "dev_web_ci_baseline",
        "workspace_id": "ws_web_ci_baseline",
        "repository": "owner/repo",
        "branch": "ai/web-ci-baseline",
        "base_branch": "main",
        "base_commit_sha": SHA_A,
        "head_commit_sha": SHA_B,
        "tree_sha": TREE_B,
        "session_revision": 7,
        "workspace_revision": 4,
        "status": "active",
        "pull_number": None,
        "metadata": {"task_name": "WEB-CI-DEV-001"},
    }
    value.update(overrides)
    return value


@pytest.mark.asyncio
async def test_wait_private_ci_job_defaults_to_55_and_delegates_to_wait_for_job_change(monkeypatch):
    mcp = StructuredFastMCP("web-ci-wait-baseline")
    calls = []

    def fake_wait(job_id, timeout_seconds, last_known_status, last_known_step, last_known_revision):
        calls.append(
            (job_id, timeout_seconds, last_known_status, last_known_step, last_known_revision)
        )
        return {
            "ok": True,
            "job_id": job_id,
            "status": "running",
            "changed": False,
            "timed_out": True,
            "terminal": False,
        }

    monkeypatch.setattr(ci_mcp, "wait_for_job_change", fake_wait)
    ci_mcp.register_private_ci_mcp_tools(mcp)

    result = _structured_result(
        await mcp.call_tool("wait_private_ci_job", {"job_id": "job-web-ci-baseline"})
    )

    assert calls == [("job-web-ci-baseline", 55, "", "", 0)]
    assert result["status"] == "running"
    assert result["terminal"] is False


def test_wait_for_job_change_caps_long_poll_at_55_without_real_sleep(monkeypatch):
    now = [1000.0]
    waits = []

    class FakeCondition:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def wait(self, timeout):
            waits.append(timeout)
            now[0] += timeout

    snapshot = {"status": "running", "current_step": "tests", "revision": 9}
    job = {"job_id": "job-bounded", "status": "running"}
    monkeypatch.setattr(ci_database, "_job_change_condition", FakeCondition())
    monkeypatch.setattr(ci_database.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(ci_database, "_job_snapshot", lambda job_id: dict(snapshot))
    monkeypatch.setattr(ci_database, "get_job", lambda job_id: dict(job))
    monkeypatch.setattr(ci_database, "get_steps", lambda job_id: [])
    monkeypatch.setattr(ci_database, "_newly_completed_steps", lambda job_id, revision: [])

    result = ci_database.wait_for_job_change(
        "job-bounded",
        timeout_seconds=999,
        last_known_status="running",
        last_known_step="tests",
        last_known_revision=9,
    )

    assert waits == [55]
    assert result["timed_out"] is True
    assert result["terminal"] is False
    assert result["elapsed_seconds"] == 55.0


@pytest.mark.asyncio
async def test_validate_development_task_defaults_to_55_and_enters_wait_path(monkeypatch):
    mcp = StructuredFastMCP("web-ci-validate-baseline")
    service = SimpleNamespace()
    session = _session(session_revision=1, workspace_revision=3)
    events = []

    monkeypatch.setattr(sessions, "get_session", lambda session_id: dict(session))
    monkeypatch.setattr(
        dx,
        "validation_preflight",
        lambda *args, **kwargs: {
            "profile": "repo-fast-check",
            "selection": {"complete": True},
        },
    )
    monkeypatch.setattr(
        dx,
        "maybe_auto_renew_session_workspace",
        lambda *args, **kwargs: {
            "renewed": False,
            "session": dict(session),
            "workspace": {"revision": 3},
            "remaining_seconds": 3600.0,
            "audit": None,
            "recovery": None,
        },
    )
    monkeypatch.setattr(mygithub12, "workspace_write_preflight", lambda *args, **kwargs: {"ok": True})

    def transition(session_id, expected_revision, status, **kwargs):
        events.append(("transition", status))
        return {**session, "status": status, "session_revision": expected_revision + 1}

    monkeypatch.setattr(sessions, "transition", transition)
    job = {
        "job_id": "job-validate",
        "status": "running",
        "profile": "repo-fast-check",
        "commit_sha": SHA_B,
    }

    def start_validation(*args, **kwargs):
        events.append(("start_ci", "job-validate"))
        return dict(job), {"complete": True}

    monkeypatch.setattr(dx, "start_validation_job", start_validation)
    monkeypatch.setattr(sessions, "record_validation", lambda *args, **kwargs: None)

    def wait_validation(job_id, wait_seconds):
        events.append(("wait_ci", wait_seconds))
        return dict(job)

    monkeypatch.setattr(dx, "wait_validation", wait_validation)
    monkeypatch.setattr(
        dx,
        "validation_result",
        lambda *args, **kwargs: {
            "job": {
                "job_id": "job-validate",
                "status": "running",
                "profile": "repo-fast-check",
                "commit_sha": SHA_B,
            },
            "affected": {"complete": True},
            "merge_eligible": False,
            "attestation": None,
            "failure_pack": None,
            "terminal": False,
        },
    )

    async def github_call(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def finalize_write(*args, **kwargs):
        return {}

    dx_mcp.register_dx_tools(mcp, github_call, service, finalize_write)
    result = _structured_result(
        await mcp.call_tool(
            "validate_development_task",
            {
                "development_session_id": session["session_id"],
                "expected_session_revision": 1,
                "mode": "fast",
            },
        )
    )

    assert ("start_ci", "job-validate") in events
    assert ("wait_ci", 55) in events
    assert events.index(("start_ci", "job-validate")) < events.index(("wait_ci", 55))
    assert result["job"]["status"] == "running"


@pytest.mark.asyncio
async def test_converge_development_task_defaults_to_55_plus_55(monkeypatch):
    mcp = StructuredFastMCP("web-ci-converge-defaults")
    captured = []

    async def fake_converge_task(
        github_call,
        service,
        development_session_id,
        expected_session_revision,
        mode,
        base_sha,
        index_wait_seconds,
        wait_seconds,
        force_rerun,
        supersede_previous,
        include_failure_pack,
        idempotency_key,
    ):
        captured.append((index_wait_seconds, wait_seconds))
        return {"ok": True, "converged": False, "terminal": False}

    monkeypatch.setattr(converge, "converge_task", fake_converge_task)

    async def github_call(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def finalize_write(*args, **kwargs):
        return {}

    dx_mcp.register_dx_tools(mcp, github_call, SimpleNamespace(), finalize_write)
    result = _structured_result(
        await mcp.call_tool(
            "converge_development_task",
            {"development_session_id": "dev-defaults", "expected_session_revision": 3},
        )
    )

    assert captured == [(55, 55)]
    assert result["terminal"] is False


@pytest.mark.asyncio
async def test_converge_development_task_waits_index_then_ci_in_order(monkeypatch):
    session = _session()
    wait_order = []
    status_reads = []

    monkeypatch.setattr(sessions, "_require_revision", lambda *args, **kwargs: session)
    monkeypatch.setattr(sessions, "get_session", lambda session_id: dict(session))
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
    monkeypatch.setattr(mygithub12, "workspace_write_preflight", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(
        mygithub12,
        "resolve_identity",
        lambda service, repository, commit_sha="": {
            "repository": repository,
            "commit_sha": commit_sha,
            "tree_sha": TREE_B,
        },
    )

    def index_status(service, repository, commit_sha="", ref=""):
        status_reads.append(commit_sha)
        if len(status_reads) == 1:
            return {"status": "missing", "commit_sha": commit_sha, "tree_sha": TREE_B}
        return {
            "status": "ready",
            "commit_sha": commit_sha,
            "tree_sha": TREE_B,
            "index_version": "12.0.0-1",
        }

    monkeypatch.setattr(mygithub12, "get_index_status", index_status)
    monkeypatch.setattr(
        mygithub12,
        "request_index_build",
        lambda *args, **kwargs: {
            "job_id": "index-job",
            "status": "running",
            "revision": 4,
            "step": "snapshot",
        },
    )

    def wait_index_job(job_id, timeout_seconds, last_known_revision, last_known_status, last_known_step):
        wait_order.append(("index", timeout_seconds))
        return {"status": "completed"}

    monkeypatch.setattr(mygithub12, "wait_index_job", wait_index_job)
    monkeypatch.setattr(mygithub12, "change_context_pack", lambda *args, **kwargs: {"ok": True, "items": []})
    monkeypatch.setattr(
        mygithub12,
        "change_impact",
        lambda *args, **kwargs: {
            "ok": True,
            "complete": True,
            "changed_paths": ["tests/test_web_ci_wait_baseline.py"],
            "affected_modules": ["app"],
            "affected_tests": ["tests/test_web_ci_wait_baseline.py"],
            "contract_changes": [],
        },
    )
    monkeypatch.setattr(
        mygithub12,
        "contract_changes",
        lambda *args, **kwargs: {"ok": True, "summary": {}, "changes": []},
    )
    monkeypatch.setattr(
        mygithub12,
        "affected_tests",
        lambda *args, **kwargs: {"ok": True, "authoritative": False, "tests": []},
    )
    monkeypatch.setattr(
        converge,
        "store_response_resource",
        lambda value: {
            "resource_uri": "mygithub12://response/web-ci-baseline",
            "total_bytes": 1,
            "sha256": "d" * 64,
        },
    )
    monkeypatch.setattr(
        dx,
        "validation_preflight",
        lambda *args, **kwargs: {"profile": "repo-auto-check", "selection": {"complete": True}},
    )

    def transition(session_id, expected_revision, status, **kwargs):
        return {**session, "status": status, "session_revision": expected_revision + 1}

    monkeypatch.setattr(sessions, "transition", transition)
    job = {
        "job_id": "ci-job",
        "status": "running",
        "profile": "repo-auto-check",
        "commit_sha": SHA_B,
        "worker_id": "wsl-ci-01",
    }
    monkeypatch.setattr(dx, "start_validation_job", lambda *args, **kwargs: (dict(job), {"complete": True}))
    monkeypatch.setattr(sessions, "record_validation", lambda *args, **kwargs: None)

    def wait_validation(job_id, wait_seconds):
        wait_order.append(("ci", wait_seconds))
        return dict(job)

    monkeypatch.setattr(dx, "wait_validation", wait_validation)
    monkeypatch.setattr(
        dx,
        "validation_result",
        lambda *args, **kwargs: {
            "job": {
                "job_id": "ci-job",
                "status": "running",
                "profile": "repo-auto-check",
                "commit_sha": SHA_B,
            },
            "affected": {"complete": True},
            "merge_eligible": False,
            "attestation": None,
            "failure_pack": None,
            "terminal": False,
        },
    )
    monkeypatch.setattr(
        converge,
        "wait_worker_final_state",
        lambda job_id, wait_seconds=5: {
            "worker_id": "wsl-ci-01",
            "terminal": False,
            "released": False,
            "idle": False,
            "status": "running",
            "current_job": job_id,
        },
    )

    result = await converge.converge_task(
        _direct_call,
        object(),
        session["session_id"],
        session["session_revision"],
        mode="full",
    )

    assert wait_order == [("index", 55), ("ci", 55)]
    assert result["validation"]["job"]["status"] == "running"
    assert result["converged"] is False


def test_fake_long_running_ci_fixture_models_ten_plus_minutes_without_sleep(fake_long_running_ci):
    fake_long_running_ci.transition("running", at_seconds=1)
    fake_long_running_ci.transition("running", at_seconds=601)
    fake_long_running_ci.transition("passed", at_seconds=602)

    assert [entry["status"] for entry in fake_long_running_ci.history] == [
        "queued",
        "running",
        "running",
        "passed",
    ]
    assert (
        fake_long_running_ci.history[2]["logical_time_seconds"]
        - fake_long_running_ci.history[1]["logical_time_seconds"]
    ) >= 600
    assert fake_long_running_ci.snapshot() == {
        "status": "passed",
        "logical_time_seconds": 602.0,
    }
