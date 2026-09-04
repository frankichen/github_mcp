import json
import threading
import time

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


def test_wait_returns_when_progress_changes_after_blocking(monkeypatch, isolated_store):
    _ready_plan(monkeypatch)
    started = service.start_infrastructure_deployment(
        service.REPOSITORY,
        service.ENVIRONMENT,
        SHA_B,
        "job-1",
        SHA_A,
        confirm=True,
    )
    claimed = service.claim_infrastructure_deployment(service.DEFAULT_EXECUTOR_ID)["deployment"]
    monkeypatch.setattr(service, "WAIT_POLL_SECONDS", 0.01)

    writer_finished = threading.Event()

    def update_progress():
        time.sleep(0.05)
        service.update_infrastructure_deployment_progress(
            started["deployment_id"],
            "health",
            "health phase reached",
        )
        writer_finished.set()

    writer = threading.Thread(target=update_progress, daemon=True)
    writer.start()
    wait_started = time.monotonic()
    waited = service.get_infrastructure_deployment(
        started["deployment_id"],
        wait_seconds=2,
        last_known_revision=claimed["log_revision"],
        last_known_status=claimed["status"],
        last_known_step=claimed["current_step"],
    )
    elapsed = time.monotonic() - wait_started
    writer.join(timeout=1)

    assert writer_finished.is_set() is True
    assert writer.is_alive() is False
    assert elapsed < 1
    diagnostics = waited["diagnostics"]
    assert diagnostics["changed"] is True
    assert diagnostics["timed_out"] is False
    assert diagnostics["terminal"] is False
    assert diagnostics["revision"] > claimed["log_revision"]
    assert diagnostics["status"] == "running"
    assert diagnostics["current_step"] == "health"
    assert diagnostics["phase"] == "health"


def test_failed_terminal_returns_exit_and_error_code(monkeypatch, isolated_store):
    _ready_plan(monkeypatch)
    started = service.start_infrastructure_deployment(
        service.REPOSITORY,
        service.ENVIRONMENT,
        SHA_B,
        "job-1",
        SHA_A,
        confirm=True,
    )
    claimed = service.claim_infrastructure_deployment(service.DEFAULT_EXECUTOR_ID)["deployment"]
    failed = service.fail_infrastructure_deployment(
        started["deployment_id"],
        17,
        "DX2_TEST_FAILURE",
        "fixed executor failure",
    )
    assert failed["deployment"]["status"] == "failed"

    waited = service.get_infrastructure_deployment(
        started["deployment_id"],
        wait_seconds=55,
        last_known_revision=claimed["log_revision"],
        last_known_status=claimed["status"],
        last_known_step=claimed["current_step"],
    )
    deployment = waited["deployment"]
    diagnostics = waited["diagnostics"]
    assert deployment["status"] == "failed"
    assert deployment["exit_code"] == 17
    assert deployment["error_code"] == "DX2_TEST_FAILURE"
    assert diagnostics["changed"] is True
    assert diagnostics["timed_out"] is False
    assert diagnostics["terminal"] is True
    assert diagnostics["phase"] == "failed"


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


def _failed_post_switch_source(monkeypatch):
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
        "controller_switch",
        "DX2_PHASE=controller_switch",
    )
    service.fail_infrastructure_deployment(
        started["deployment_id"],
        1,
        "INFRASTRUCTURE_DEPLOYMENT_FAILED",
        "post-switch preheat failed",
    )
    return started["deployment_id"]


def test_normal_plan_still_rejects_already_deployed(monkeypatch, isolated_store):
    _ready_plan(monkeypatch)
    monkeypatch.setattr(service, "runtime_build_sha", lambda: SHA_B)
    result = service.plan_infrastructure_deployment(
        service.REPOSITORY, service.ENVIRONMENT, SHA_B, "job-1", SHA_B
    )
    assert result["ready"] is False
    assert "ALREADY_DEPLOYED" in result["reasons"]
    assert result["execution_mode"] == service.EXECUTION_MODE_FULL


def test_post_switch_recovery_creates_new_auditable_deployment(monkeypatch, isolated_store):
    source_id = _failed_post_switch_source(monkeypatch)
    source_before = service.get_infrastructure_deployment(source_id)["deployment"]
    monkeypatch.setattr(service, "runtime_build_sha", lambda: SHA_B)

    planned = service.plan_infrastructure_deployment(
        service.REPOSITORY,
        service.ENVIRONMENT,
        SHA_B,
        "job-1",
        SHA_B,
        recovery_of_deployment_id=source_id,
    )
    assert planned["ready"] is True
    assert "ALREADY_DEPLOYED" not in planned["reasons"]
    assert planned["execution_mode"] == service.EXECUTION_MODE_POST_SWITCH_RECOVERY
    assert planned["recovery_source"]["deployment_id"] == source_id
    assert planned["execution_contract"] == "fixed-executor/post-switch-recovery/fail-stop/no-auto-rollback"

    recovered = service.start_infrastructure_deployment(
        service.REPOSITORY,
        service.ENVIRONMENT,
        SHA_B,
        "job-1",
        SHA_B,
        confirm=True,
        recovery_of_deployment_id=source_id,
    )
    assert recovered["deployment_id"] != source_id
    assert recovered["execution_mode"] == service.EXECUTION_MODE_POST_SWITCH_RECOVERY
    assert recovered["recovery_of_deployment_id"] == source_id

    source_after = service.get_infrastructure_deployment(source_id)["deployment"]
    assert source_after["status"] == "failed"
    assert source_after["error_code"] == source_before["error_code"]
    claimed = service.claim_infrastructure_deployment(service.DEFAULT_EXECUTOR_ID)
    assert claimed["deployment"]["deployment_id"] == recovered["deployment_id"]
    assert claimed["deployment"]["execution_mode"] == service.EXECUTION_MODE_POST_SWITCH_RECOVERY


def test_recovery_requires_failed_post_switch_exact_target(monkeypatch, isolated_store):
    _ready_plan(monkeypatch)
    started = service.start_infrastructure_deployment(
        service.REPOSITORY, service.ENVIRONMENT, SHA_B, "job-1", SHA_A, confirm=True
    )
    service.claim_infrastructure_deployment(service.DEFAULT_EXECUTOR_ID)
    service.fail_infrastructure_deployment(
        started["deployment_id"], 1, "EARLY_FAILURE", "failed before controller switch"
    )
    monkeypatch.setattr(service, "runtime_build_sha", lambda: SHA_B)
    planned = service.plan_infrastructure_deployment(
        service.REPOSITORY,
        service.ENVIRONMENT,
        SHA_B,
        "job-1",
        SHA_B,
        recovery_of_deployment_id=started["deployment_id"],
    )
    assert planned["ready"] is False
    assert "INFRASTRUCTURE_RECOVERY_SOURCE_NOT_POST_SWITCH" in planned["reasons"]


def test_recovery_requires_live_runtime_to_match_target(monkeypatch, isolated_store):
    source_id = _failed_post_switch_source(monkeypatch)
    planned = service.plan_infrastructure_deployment(
        service.REPOSITORY,
        service.ENVIRONMENT,
        SHA_B,
        "job-1",
        SHA_A,
        recovery_of_deployment_id=source_id,
    )
    assert planned["ready"] is False
    assert "INFRASTRUCTURE_RECOVERY_RUNTIME_NOT_TARGET" in planned["reasons"]


def test_store_upgrades_legacy_deployment_table_for_recovery(isolated_store):
    db = store.get_db()
    db.execute("DROP TABLE IF EXISTS infrastructure_deployments")
    db.executescript(
        """
        CREATE TABLE infrastructure_deployments (
          deployment_id TEXT PRIMARY KEY, repository TEXT NOT NULL, environment TEXT NOT NULL,
          requested_scope TEXT NOT NULL, commit_sha TEXT NOT NULL, tree_sha TEXT NOT NULL,
          private_ci_job_id TEXT NOT NULL, expected_current_build_sha TEXT NOT NULL,
          requested_by TEXT NOT NULL DEFAULT 'mcp', status TEXT NOT NULL, current_step TEXT,
          exit_code INTEGER, error_code TEXT, error_message TEXT, created_at REAL NOT NULL,
          started_at REAL, finished_at REAL, updated_at REAL NOT NULL,
          log_revision INTEGER NOT NULL DEFAULT 0, log_text TEXT NOT NULL DEFAULT ''
        );
        """
    )
    db.commit()
    store.init_db()
    columns = {row[1] for row in db.execute("PRAGMA table_info(infrastructure_deployments)")}
    assert {"execution_mode", "recovery_of_deployment_id"} <= columns
