import pytest

import app.ci_database as database


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    if getattr(database._local, "db", None) is not None:
        database._local.db.close()
    database._local.db = None
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "ci.db"))
    database.init_db()
    try:
        yield
    finally:
        if getattr(database._local, "db", None) is not None:
            database._local.db.close()
        database._local.db = None


def _create_job():
    return database.create_or_get_job(
        "owner/repo", "feature", "a" * 40, "node-check",
        50, 120, True, False,
    )


def test_reregister_preserves_live_job_and_old_attempt_is_fenced(isolated_db):
    assert database.register_worker("wsl-ci-test", "worker-token", ["node-check"], 1)
    job_id = _create_job()["job_id"]

    first = database.lease_job("wsl-ci-test", ["node-check"], 1)
    assert first["job_id"] == job_id
    assert first["attempt_number"] == 1
    database.set_job_status(job_id, "running")
    first_started_at = database.get_job(job_id)["started_at"]

    assert database.register_worker("wsl-ci-test", "worker-token", ["node-check"], 1)
    assert database.get_job(job_id)["status"] == "running"
    assert database.get_worker("wsl-ci-test")["current_job"] == job_id
    assert database.get_current_lease_attempt(job_id, "wsl-ci-test", first["lease_token"]) == 1
    assert database.get_current_lease_attempt(job_id, "wsl-ci-test", "wrong-token") is None
    assert database.renew_lease(job_id, "wrong-token") is False
    assert database.renew_lease(job_id, first["lease_token"]) is True
    assert database.get_controller_drain_status()["safe_to_restart"] is False

    old_step = database.add_step(job_id, "node:first:build", "running", attempt_number=1)
    assert database.finish_step(old_step, "passed", 0, job_id=job_id, attempt_number=1) is True
    database.append_log_chunk(job_id, "old-attempt\n", attempt_number=1)

    assert database.release_job(job_id, expected_worker_id="wsl-ci-test", expected_lease_token=first["lease_token"], expected_attempt_number=1) is True
    second = database.lease_job("wsl-ci-test", ["node-check"], 1)
    assert second["attempt_number"] == 2
    assert database.get_job(job_id)["started_at"] == first_started_at

    second_step = database.add_step(job_id, "node:second:build", "running", attempt_number=2)
    assert database.finish_step(old_step, "failed", 1, job_id=job_id, attempt_number=2) is False
    database.append_log_chunk(job_id, "new-attempt\n", attempt_number=2)
    current_steps = database.get_steps(job_id, 2)
    assert [item["step_name"] for item in current_steps] == ["node:second:build"]
    assert current_steps[0]["attempt_number"] == 2
    assert second_step > old_step
    assert database.get_log_tail(job_id, lines=10)["lines"] == ["new-attempt"]
    current_logs = database.get_log_chunks(job_id)
    assert current_logs["attempt_number"] == 2
    assert [item["content"] for item in current_logs["chunks"]] == ["new-attempt\n"]

    assert database.complete_job(job_id, 0, "passed", expected_worker_id="wsl-ci-test", expected_lease_token=first["lease_token"], expected_attempt_number=1) is False
    assert database.get_job(job_id)["status"] == "leased"

    db = database._get_db()
    db.execute("UPDATE ci_jobs SET lease_expires_at = ? WHERE job_id = ?", (database.now_ts() - 1, job_id))
    db.commit()
    assert database.get_current_lease_attempt(job_id, "wsl-ci-test", second["lease_token"]) is None
    assert database.renew_lease(job_id, second["lease_token"]) is False
    database.recover_expired_leases()
    assert database.get_controller_drain_status()["safe_to_restart"] is True


def test_controller_drain_blocks_new_leases_and_reports_live_job_identity(isolated_db):
    assert database.register_worker("wsl-ci-test", "worker-token", ["node-check"], 1)
    job_id = _create_job()["job_id"]

    status = database.set_controller_draining(True)
    assert status["draining"] is True
    assert status["safe_to_restart"] is True
    assert database.lease_job("wsl-ci-test", ["node-check"], 1) is None

    database.set_controller_draining(False)
    leased = database.lease_job("wsl-ci-test", ["node-check"], 1)
    status = database.set_controller_draining(True)
    assert status["draining"] is True
    assert status["safe_to_restart"] is False
    assert status["active_job_count"] == 1
    assert status["active_jobs"][0]["job_id"] == job_id
    assert status["active_jobs"][0]["attempt_number"] == leased["attempt_number"]
