import json
import re

import pytest

from app import (
    ci_database,
    ci_mcp,
    development_converge,
    development_failure_pack as failure_pack,
    observability,
)
from app.mcp_response import (
    StructuredFastMCP,
    json_bytes,
    prepare_tool_response,
    read_response_resource_text,
    response_size_bytes,
)


@pytest.fixture(autouse=True)
def reset_observability_metrics():
    observability._reset_metrics_for_tests()
    yield
    observability._reset_metrics_for_tests()


def _structured(call_result):
    if isinstance(call_result, tuple):
        return call_result[1]
    structured = getattr(call_result, "structured_content", None)
    if structured is None:
        structured = getattr(call_result, "structuredContent", None)
    return structured


@pytest.mark.asyncio
async def test_canonical_mcp_tool_count_and_duration_use_common_wrapper(monkeypatch):
    mcp = StructuredFastMCP("observability-duration-test")

    @mcp.tool(name="canonical_probe")
    async def canonical_probe() -> str:
        return json.dumps({"ok": True, "status": "ready"})

    clock = iter([10.0, 12.5])
    monkeypatch.setattr(observability, "monotonic", lambda: next(clock))

    structured = _structured(await mcp.call_tool("canonical_probe", {}))

    assert structured["ok"] is True
    metrics = observability.prometheus_metrics()
    assert (
        'mygithub_mcp_tool_requests_total{tool="canonical_probe",result="ok",mode="ordinary"} 1'
        in metrics
    )
    assert (
        'mygithub_mcp_tool_duration_seconds_total{tool="canonical_probe",result="ok",mode="ordinary"} 2.500000'
        in metrics
    )


@pytest.mark.asyncio
async def test_snapshot_and_explicit_private_ci_wait_paths_are_distinct(monkeypatch):
    mcp = StructuredFastMCP("observability-wait-test")
    ci_mcp.register_private_ci_mcp_tools(mcp)
    job = {
        "job_id": "job-observe-1",
        "repository": "owner/repo",
        "branch": "ai/example",
        "commit_sha": "a" * 40,
        "profile": "repo-auto-check",
        "status": "queued",
        "priority": 100,
        "created_at": "2026-09-08T10:00:00+00:00",
        "queued_at": "2026-09-08T10:00:01+00:00",
        "worker_id": None,
        "summary": {},
    }
    monkeypatch.setattr(ci_mcp, "get_job", lambda job_id: dict(job))
    monkeypatch.setattr(ci_mcp, "get_steps", lambda job_id: [])
    monkeypatch.setattr(
        ci_mcp,
        "wait_for_job_change",
        lambda *args, **kwargs: {"ok": True, "job_id": "job-observe-1", "status": "queued"},
    )
    clock = iter([100.0, 101.0, 200.0, 201.0, 204.0, 205.0])
    monkeypatch.setattr(observability, "monotonic", lambda: next(clock))

    snapshot = _structured(
        await mcp.call_tool("get_private_ci_job", {"job_id": "job-observe-1"})
    )
    waited = _structured(
        await mcp.call_tool(
            "wait_private_ci_job",
            {"job_id": "job-observe-1", "timeout_seconds": 1},
        )
    )

    assert snapshot["job_id"] == "job-observe-1"
    assert waited["job_id"] == "job-observe-1"
    metrics = observability.prometheus_metrics()
    assert (
        'mygithub_mcp_tool_requests_total{tool="get_private_ci_job",result="ok",mode="ordinary"} 1'
        in metrics
    )
    assert (
        'mygithub_mcp_tool_duration_seconds_total{tool="get_private_ci_job",result="ok",mode="ordinary"} 1.000000'
        in metrics
    )
    assert (
        'mygithub_mcp_tool_requests_total{tool="wait_private_ci_job",result="ok",mode="explicit_wait"} 1'
        in metrics
    )
    assert (
        'mygithub_mcp_tool_duration_seconds_total{tool="wait_private_ci_job",result="ok",mode="explicit_wait"} 5.000000'
        in metrics
    )
    assert 'mygithub_mcp_explicit_waits_total{kind="private_ci_job"} 1' in metrics
    assert (
        'mygithub_mcp_explicit_wait_duration_seconds_total{kind="private_ci_job"} 3.000000'
        in metrics
    )


def test_private_ci_lifecycle_durations_come_from_durable_timestamps():
    observability.observe_private_ci_lifecycle(
        {
            "created_at": "2026-09-08T10:00:00+00:00",
            "queued_at": "2026-09-08T10:00:02+00:00",
            "started_at": "2026-09-08T10:00:07+00:00",
            "finished_at": "2026-09-08T10:00:20+00:00",
            "duration_seconds": 13.0,
        },
    )

    metrics = observability.prometheus_metrics()
    assert (
        'mygithub_private_ci_lifecycle_duration_observations_total{kind="queue"} 1'
        in metrics
    )
    assert (
        'mygithub_private_ci_lifecycle_duration_observations_total{kind="execution"} 1'
        in metrics
    )
    assert (
        'mygithub_private_ci_lifecycle_duration_observations_total{kind="total"} 1'
        in metrics
    )
    assert (
        'mygithub_private_ci_lifecycle_duration_seconds_total{kind="queue"} 5.000000'
        in metrics
    )
    assert (
        'mygithub_private_ci_lifecycle_duration_seconds_total{kind="execution"} 13.000000'
        in metrics
    )
    assert (
        'mygithub_private_ci_lifecycle_duration_seconds_total{kind="total"} 20.000000'
        in metrics
    )


def test_private_ci_completion_metrics_are_durable_and_snapshots_do_not_duplicate(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(ci_database, "DB_PATH", str(tmp_path / "ci.db"))
    previous = getattr(ci_database._local, "db", None)
    if previous is not None:
        previous.close()
    ci_database._local.db = None
    try:
        ci_database.init_db()
        monkeypatch.setattr(ci_database, "now_ts", lambda: 100.0)
        job = ci_database.create_or_get_job(
            repository="owner/repo",
            branch="main",
            commit_sha="c" * 40,
            profile="repo-auto-check",
            priority=100,
            timeout_seconds=900,
            force_rerun=False,
            supersede_previous=False,
        )
        row = ci_database._get_db().execute(
            "SELECT created_at,queued_at FROM ci_jobs WHERE job_id=?",
            (job["job_id"],),
        ).fetchone()
        assert row["created_at"] == 100.0
        assert row["queued_at"] == 100.0

        assert ci_database.register_worker(
            "worker-observe", "token-observe", ["repo-auto-check"], 1
        )
        monkeypatch.setattr(ci_database, "now_ts", lambda: 105.0)
        lease = ci_database.lease_job("worker-observe", ["repo-auto-check"], 1)
        assert lease is not None
        assert lease["job_id"] == job["job_id"]

        monkeypatch.setattr(ci_database, "now_ts", lambda: 120.0)
        assert ci_database.complete_job(
            job["job_id"],
            0,
            "passed",
            {"status": "passed"},
            worker_id="worker-observe",
            lease_token=lease["lease_token"],
        ) is True

        metrics = observability.prometheus_metrics()
        assert (
            'mygithub_private_ci_lifecycle_duration_observations_total{kind="queue"} 1'
            in metrics
        )
        assert (
            'mygithub_private_ci_lifecycle_duration_seconds_total{kind="queue"} 5.000000'
            in metrics
        )
        assert (
            'mygithub_private_ci_lifecycle_duration_seconds_total{kind="execution"} 15.000000'
            in metrics
        )
        assert (
            'mygithub_private_ci_lifecycle_duration_seconds_total{kind="total"} 20.000000'
            in metrics
        )

        finished = ci_database.get_job(job["job_id"])
        ci_mcp.build_private_ci_snapshot_response(None, finished, [], "summary")
        ci_mcp.build_private_ci_snapshot_response(None, finished, [], "summary")
        after_snapshots = observability.prometheus_metrics()
        assert (
            'mygithub_private_ci_lifecycle_duration_observations_total{kind="total"} 1'
            in after_snapshots
        )
        assert 'mygithub_private_ci_phase_observations_total{phase="terminal"} 2' in after_snapshots
    finally:
        current = getattr(ci_database._local, "db", None)
        if current is not None:
            current.close()
        ci_database._local.db = None


@pytest.mark.parametrize(
    ("phase", "next_phase", "started_at", "ended_at", "expected_seconds"),
    [
        ("accepted", "index_requested", "2026-09-08T10:00:00+00:00", "2026-09-08T10:00:01+00:00", 1.0),
        ("index_requested", "analysis_pending", "2026-09-08T10:00:01+00:00", "2026-09-08T10:00:03+00:00", 2.0),
        ("analysis_pending", "ci_requested", "2026-09-08T10:00:03+00:00", "2026-09-08T10:00:06+00:00", 3.0),
        ("ci_requested", "ci_running", "2026-09-08T10:00:06+00:00", "2026-09-08T10:00:10+00:00", 4.0),
        ("ci_running", "post_ci_finalize", "2026-09-08T10:00:10+00:00", "2026-09-08T10:00:15+00:00", 5.0),
        ("post_ci_finalize", "passed", "2026-09-08T10:00:15+00:00", "2026-09-08T10:00:21+00:00", 6.0),
    ],
)
def test_convergence_phase_timing_is_table_driven(
    phase, next_phase, started_at, ended_at, expected_seconds
):
    observability.observe_convergence_transition(
        phase,
        next_phase,
        started_at,
        ended_at,
    )

    metrics = observability.prometheus_metrics()
    assert f'mygithub_convergence_phase_entries_total{{phase="{next_phase}"}} 1' in metrics
    assert (
        f'mygithub_convergence_phase_duration_observations_total{{phase="{phase}"}} 1'
        in metrics
    )
    assert (
        f'mygithub_convergence_phase_duration_seconds_total{{phase="{phase}"}} '
        f"{expected_seconds:.6f}"
        in metrics
    )


def test_convergence_uses_last_durable_phase_entry_not_same_phase_refresh(monkeypatch):
    snapshot = {
        "convergence_id": "conv-observe-1",
        "phase": "analysis_pending",
        "created_at": "2026-09-08T10:00:00+00:00",
        "updated_at": "2026-09-08T10:00:30+00:00",
    }
    events = [
        {
            "from_phase": "index_requested",
            "to_phase": "analysis_pending",
            "created_at": "2026-09-08T10:00:10+00:00",
        },
        {
            "from_phase": "analysis_pending",
            "to_phase": "analysis_pending",
            "created_at": "2026-09-08T10:00:25+00:00",
        },
    ]
    monkeypatch.setattr(
        development_converge.convergence_store,
        "list_convergence_events",
        lambda convergence_id, limit=500: events,
    )

    assert (
        development_converge._durable_phase_started_at(snapshot)
        == "2026-09-08T10:00:10+00:00"
    )


def test_idempotent_reuse_and_conflict_are_counted_without_identity_labels():
    for operation in ("private_ci", "validation", "convergence"):
        observability.observe_idempotency(operation, "reuse")
        observability.observe_idempotency(operation, "conflict")

    metrics = observability.prometheus_metrics()
    for operation in ("private_ci", "validation", "convergence"):
        assert (
            f'mygithub_idempotency_events_total{{operation="{operation}",outcome="reuse"}} 1'
            in metrics
        )
        assert (
            f'mygithub_idempotency_events_total{{operation="{operation}",outcome="conflict"}} 1'
            in metrics
        )
    assert "idempotency_key" not in metrics
    assert "request_hash" not in metrics


def test_failure_pack_build_metrics_keep_redaction_and_payload_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "controller.db"))
    monkeypatch.setenv("MCP_RESPONSE_RESOURCE_DIR", str(tmp_path / "resources"))
    monkeypatch.setattr(failure_pack, "get_steps", lambda job_id: [])
    monkeypatch.setattr(failure_pack, "get_log_tail", lambda *args, **kwargs: {"lines": []})
    clock = iter([40.0, 42.5])
    monkeypatch.setattr(observability, "monotonic", lambda: next(clock))
    job = {
        "job_id": "job-failure-observe",
        "repository": "owner/repo",
        "branch": "ai/failure-observe",
        "commit_sha": "a" * 40,
        "base_sha": "b" * 40,
        "profile": "repo-auto-check",
        "status": "failed",
        "exit_code": 1,
        "error_code": "CI_STEP_FAILED",
        "error_message": "test command failed",
        "changed_files": [{"path": "tests/test_observe.py", "operation": "modified"}],
        "changed_files_total": 1,
        "summary": {
            "steps": [
                {
                    "step_name": "pytest",
                    "status": "failed",
                    "exit_code": 1,
                    "command": "pytest --token=top-secret tests/test_observe.py",
                }
            ]
        },
    }

    result = failure_pack.build_failure_pack(
        job,
        affected={"complete": True, "selected_tests": ["tests/test_observe.py::test_x"]},
        log_tail="FAILED tests/test_observe.py::test_x - token=ghp_not-for-output",
    )

    rendered = json.dumps(result)
    assert "top-secret" not in rendered
    assert "ghp_not-for-output" not in rendered
    assert result["failure_pack_id"]
    assert result["total_bytes"] > 0
    metrics = observability.prometheus_metrics()
    assert 'mygithub_failure_pack_builds_total{operation="build"} 1' in metrics
    assert (
        'mygithub_failure_pack_build_duration_seconds_total{operation="build"} 2.500000'
        in metrics
    )
    assert (
        f'mygithub_failure_pack_payload_bytes_total{{operation="build"}} {result["total_bytes"]}'
        in metrics
    )


def test_inline_and_resource_fallback_metrics_use_existing_serialization_bytes(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MCP_RESPONSE_RESOURCE_DIR", str(tmp_path / "resources"))
    inline_payload = {"ok": True, "status": "ready"}
    inline = prepare_tool_response(inline_payload)
    inline_bytes = response_size_bytes(inline)

    resource_payload = {
        "ok": True,
        "repository": "owner/repo",
        "blob": "resource-content-" * 6000,
    }
    resource_raw = json_bytes(resource_payload)
    resource = prepare_tool_response(resource_payload)
    resource_bytes = response_size_bytes(resource)

    assert resource["response_meta"]["mode"] == "resource"
    assert json.loads(read_response_resource_text(resource["response_meta"]["resource_uri"])) == resource_payload
    metrics = observability.prometheus_metrics()
    assert 'mygithub_mcp_responses_total{mode="inline"} 1' in metrics
    assert 'mygithub_mcp_responses_total{mode="resource"} 1' in metrics
    assert f'mygithub_mcp_response_bytes_total{{mode="inline"}} {inline_bytes}' in metrics
    assert f'mygithub_mcp_response_bytes_total{{mode="resource"}} {resource_bytes}' in metrics
    assert (
        f'mygithub_mcp_resource_bytes_total{{mode="resource"}} {len(resource_raw)}'
        in metrics
    )


def test_prometheus_exposition_has_only_bounded_label_names_and_no_runtime_identities(monkeypatch):
    fake_values = {
        "repository": "owner/high-cardinality-repo",
        "branch": "ai/high-cardinality-branch",
        "job_id": "job-high-cardinality",
        "request_id": "request-high-cardinality",
        "resource_uri": "mygithub12://response/high-cardinality",
        "commit": "f" * 40,
    }
    clock = iter([1.0, 2.0, 3.0, 4.0])
    monkeypatch.setattr(observability, "monotonic", lambda: next(clock))
    started, state, token = observability.begin_mcp_tool("bounded_tool")
    observability.finish_mcp_tool("bounded_tool", started, state, token, "ok")
    with observability.explicit_wait("validation"):
        pass
    observability.observe_private_ci_lifecycle(
        {
            "created_at": "2026-09-08T10:00:00+00:00",
            "queued_at": "2026-09-08T10:00:01+00:00",
            "started_at": "2026-09-08T10:00:02+00:00",
            "finished_at": "2026-09-08T10:00:03+00:00",
            "duration_seconds": 1,
        },
    )
    observability.observe_private_ci_phase("terminal")
    observability.observe_convergence_transition(
        "accepted",
        "index_requested",
        "2026-09-08T10:00:00+00:00",
        "2026-09-08T10:00:01+00:00",
    )
    observability.observe_idempotency("private_ci", "reuse")
    observability.observe_failure_pack_build(0.25, 128)
    observability.observe_mcp_response("resource", 64, 128)

    metrics = observability.prometheus_metrics()
    allowed_labels = {
        "method",
        "route",
        "status",
        "tool",
        "result",
        "mode",
        "kind",
        "phase",
        "operation",
        "outcome",
    }
    forbidden_labels = {
        "repository",
        "job",
        "job_id",
        "branch",
        "commit",
        "tree",
        "request_id",
        "worker_id",
        "workspace_id",
        "session_id",
        "attestation_id",
        "failure_pack_id",
        "resource_uri",
        "idempotency_key",
        "request_hash",
        "path",
        "error",
        "error_message",
    }
    emitted_labels = set()
    for line in metrics.splitlines():
        if line.startswith("#") or "{" not in line:
            continue
        label_block = line.split("{", 1)[1].split("}", 1)[0]
        emitted_labels.update(
            match.group(1)
            for match in re.finditer(r'(?:^|,)([A-Za-z_][A-Za-z0-9_]*)="', label_block)
        )

    assert emitted_labels <= allowed_labels
    assert emitted_labels.isdisjoint(forbidden_labels)
    for value in fake_values.values():
        assert value not in metrics
