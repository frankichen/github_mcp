import json

import pytest

from app import infrastructure_deployment_service as service
from app import infrastructure_deployment_store as store

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40


def _spec():
    return {
        "environment": service.ENVIRONMENT,
        "scope": service.SCOPE,
        "profile": service.PROFILE,
        "executor_id": service.DEFAULT_EXECUTOR_ID,
        "heartbeat_ttl_seconds": 30,
    }


def _job():
    return {
        "job_id": "job-1",
        "repository": service.REPOSITORY,
        "branch": "main",
        "commit_sha": SHA_B,
        "tree_sha": SHA_C,
        "profile": service.PROFILE,
        "status": "passed",
        "exit_code": 0,
        "superseded_by_job_id": None,
    }


@pytest.fixture
def isolated_store(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "INFRASTRUCTURE_DEPLOYMENT_DB_PATH",
        str(tmp_path / "infrastructure-deployments.db"),
    )
    store._local.db = None
    monkeypatch.setattr(service, "_spec", lambda repository: _spec() if repository == service.REPOSITORY else {})
    yield
    if getattr(store._local, "db", None) is not None:
        store._local.db.close()
    store._local.db = None


def _ready_plan(monkeypatch):
    monkeypatch.setattr(service, "_repo_state", lambda repository, commit_sha: (SHA_B, SHA_C))
    monkeypatch.setattr(service, "runtime_build_sha", lambda: SHA_A)
    monkeypatch.setattr(service, "_ci_gate", lambda *args: (None, _job()))
    store.write_executor_heartbeat(service.DEFAULT_EXECUTOR_ID, "idle", None)


def test_plan_requires_exact_main_ci_current_build_and_executor(monkeypatch, isolated_store):
    _ready_plan(monkeypatch)

    result = service.plan_infrastructure_deployment(
        service.REPOSITORY,
        service.ENVIRONMENT,
        SHA_B,
        "job-1",
        SHA_A,
    )

    assert result["ready"] is True
    assert result["commit_sha"] == SHA_B
    assert result["tree_sha"] == SHA_C
    assert result["exact_main_sha"] == SHA_B
    assert result["current_build_sha"] == SHA_A
    assert result["private_ci"]["status"] == "passed"
    assert result["executor"]["online"] is True
    assert result["execution_contract"] == "fixed-executor/fail-stop/no-auto-rollback"


def test_plan_rejects_stale_current_build(monkeypatch, isolated_store):
    _ready_plan(monkeypatch)
    monkeypatch.setattr(service, "runtime_build_sha", lambda: SHA_C)

    result = service.plan_infrastructure_deployment(
        service.REPOSITORY,
        service.ENVIRONMENT,
        SHA_B,
        "job-1",
        SHA_A,
    )

    assert result["ready"] is False
    assert "CURRENT_BUILD_SHA_MISMATCH" in result["reasons"]


def test_start_requires_confirm(monkeypatch, isolated_store):
    _ready_plan(monkeypatch)
    result = service.start_infrastructure_deployment(
        service.REPOSITORY,
        service.ENVIRONMENT,
        SHA_B,
        "job-1",
        SHA_A,
    )
    assert result["error"]["code"] == "CONFIRM_REQUIRED"


def test_claim_revalidates_main_ci_and_current_build(monkeypatch, isolated_store):
    _ready_plan(monkeypatch)
    started = service.start_infrastructure_deployment(
        service.REPOSITORY,
        service.ENVIRONMENT,
        SHA_B,
        "job-1",
        SHA_A,
        confirm=True,
    )
    assert started["status"] == "queued"

    monkeypatch.setattr(service, "_repo_state", lambda repository, commit_sha: (SHA_C, SHA_C))
    claimed = service.claim_infrastructure_deployment(service.DEFAULT_EXECUTOR_ID)

    assert claimed["deployment"] is None
    assert claimed["failed_deployment_id"] == started["deployment_id"]
    assert claimed["error_code"] == "COMMIT_NOT_CURRENT_MAIN"
    status = service.get_infrastructure_deployment(started["deployment_id"])
    assert status["deployment"]["status"] == "failed"


def test_complete_requires_runtime_target_sha_and_health(monkeypatch, isolated_store):
    _ready_plan(monkeypatch)
    started = service.start_infrastructure_deployment(
        service.REPOSITORY,
        service.ENVIRONMENT,
        SHA_B,
        "job-1",
        SHA_A,
        confirm=True,
    )
    claimed = service.claim_infrastructure_deployment(service.DEFAULT_EXECUTOR_ID)
    assert claimed["deployment"]["status"] == "claimed"

    unhealthy = service.complete_infrastructure_deployment(
        started["deployment_id"],
        0,
        False,
        True,
        "done",
    )
    assert unhealthy["error"]["code"] == "INFRASTRUCTURE_HEALTH_EVIDENCE_REQUIRED"

    monkeypatch.setattr(service, "runtime_build_sha", lambda: SHA_C)
    mismatch = service.complete_infrastructure_deployment(
        started["deployment_id"],
        0,
        True,
        True,
        "done",
    )
    assert mismatch["error"]["code"] == "RUNTIME_BUILD_SHA_MISMATCH"

    monkeypatch.setattr(service, "runtime_build_sha", lambda: SHA_B)
    completed = service.complete_infrastructure_deployment(
        started["deployment_id"],
        0,
        True,
        True,
        "done",
    )
    assert completed["deployment"]["status"] == "passed"
    assert completed["deployment"]["commit_sha"] == SHA_B


def test_redaction_removes_secret_like_log_values(monkeypatch, isolated_store):
    _ready_plan(monkeypatch)
    started = service.start_infrastructure_deployment(
        service.REPOSITORY,
        service.ENVIRONMENT,
        SHA_B,
        "job-1",
        SHA_A,
        confirm=True,
    )
    service.claim_infrastructure_deployment(service.DEFAULT_EXECUTOR_ID)
    service.update_infrastructure_deployment_progress(
        started["deployment_id"],
        "running",
        "token=abc password=hunter2 safe=text",
    )
    raw = store.get_db().execute(
        "SELECT log_text FROM infrastructure_deployments WHERE deployment_id=?",
        (started["deployment_id"],),
    ).fetchone()[0]
    assert "abc" not in raw
    assert "hunter2" not in raw
    assert "token=***" in raw
    assert "password=***" in raw



def test_get_legacy_shape_and_revision_aware_diagnostics(monkeypatch, isolated_store):
    _ready_plan(monkeypatch)
    started = service.start_infrastructure_deployment(
        service.REPOSITORY,
        service.ENVIRONMENT,
        SHA_B,
        "job-1",
        SHA_A,
        confirm=True,
    )
    legacy = service.get_infrastructure_deployment(started["deployment_id"])
    assert set(legacy) == {"ok", "deployment", "executor"}
    assert legacy["deployment"]["status"] == "queued"
    assert legacy["deployment"]["log_revision"] == 0

    claimed = service.claim_infrastructure_deployment(service.DEFAULT_EXECUTOR_ID)
    assert claimed["deployment"]["status"] == "claimed"
    assert claimed["deployment"]["log_revision"] == 1

    waited = service.get_infrastructure_deployment(
        started["deployment_id"],
        wait_seconds=55,
        last_known_revision=0,
        last_known_status="queued",
        last_known_step="queued",
    )
    diagnostics = waited["diagnostics"]
    assert diagnostics["changed"] is True
    assert diagnostics["timed_out"] is False
    assert diagnostics["terminal"] is False
    assert diagnostics["revision"] == 1
    assert diagnostics["status"] == "claimed"
    assert diagnostics["current_step"] == "claimed"
    assert diagnostics["phase"] == "source_prepare"
    assert diagnostics["elapsed_seconds"] < 1
    assert "log_tail" not in diagnostics


def test_get_wait_timeout_is_bounded_without_logs(monkeypatch, isolated_store):
    _ready_plan(monkeypatch)
    started = service.start_infrastructure_deployment(
        service.REPOSITORY,
        service.ENVIRONMENT,
        SHA_B,
        "job-1",
        SHA_A,
        confirm=True,
    )
    clock = {"value": 0.0}

    def fake_monotonic():
        clock["value"] += 30.0
        return clock["value"]

    monkeypatch.setattr(service.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(service.time, "sleep", lambda seconds: None)
    waited = service.get_infrastructure_deployment(
        started["deployment_id"],
        wait_seconds=999,
        last_known_status="queued",
        last_known_step="queued",
    )
    diagnostics = waited["diagnostics"]
    assert diagnostics["changed"] is False
    assert diagnostics["timed_out"] is True
    assert diagnostics["terminal"] is False
    assert diagnostics["max_wait_seconds"] == 55
    assert diagnostics["phase"] == "validation"
    assert "log_tail" not in diagnostics


def test_terminal_diagnostics_and_redacted_log_tail(monkeypatch, isolated_store):
    _ready_plan(monkeypatch)
    started = service.start_infrastructure_deployment(
        service.REPOSITORY,
        service.ENVIRONMENT,
        SHA_B,
        "job-1",
        SHA_A,
        confirm=True,
    )
    service.claim_infrastructure_deployment(service.DEFAULT_EXECUTOR_ID)
    auth_value = "opaque-auth-" + "example"
    cookie_value = "opaque-cookie-" + "example"
    db_value = "opaque-db-" + "example"
    cache_value = "opaque-cache-" + "example"
    token_value = "opaque-token-" + "example"
    password_value = "opaque-password-" + "example"
    message = (
        "Authorization" + ": Bearer " + auth_value + "\n"
        + "Cookie" + ": session=" + cookie_value + "\n"
        + "DATABASE" + "_URL=" + "postgres" + "://user:" + db_value + "@db.example.invalid/app\n"
        + "redis" + "://:" + cache_value + "@cache.example.invalid/0 "
        + "token=" + token_value + " password=" + password_value + " safe=text"
    )
    service.update_infrastructure_deployment_progress(
        started["deployment_id"],
        "controller_build",
        message,
    )
    monkeypatch.setattr(service, "runtime_build_sha", lambda: SHA_B)
    service.complete_infrastructure_deployment(
        started["deployment_id"],
        0,
        True,
        True,
        "post verify completed",
    )

    result = service.get_infrastructure_deployment(
        started["deployment_id"],
        wait_seconds=55,
        last_known_revision=0,
        include_log_tail=True,
        log_tail_lines=20,
    )
    deployment = result["deployment"]
    diagnostics = result["diagnostics"]
    assert deployment["status"] == "passed"
    assert deployment["exit_code"] == 0
    assert deployment["error_code"] is None
    assert diagnostics["terminal"] is True
    assert diagnostics["phase"] == "completed"
    tail = diagnostics["log_tail"]
    for secret in (
        auth_value, cookie_value, db_value, cache_value, token_value, password_value
    ):
        assert secret not in tail
    assert "Authorization=***" in tail
    assert "Cookie=***" in tail
    assert "DATABASE_URL=***" in tail
    assert "<redacted-connection-url>" in tail
    assert "token=***" in tail
    assert "password=***" in tail
    assert "safe=text" in tail
