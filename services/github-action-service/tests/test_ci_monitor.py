import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import ci_database as db
from app.routers import ci_monitor


def isolated_db(tmp_path, monkeypatch):
    path = tmp_path / "ci-monitor.db"
    monkeypatch.setattr(db, "DB_PATH", str(path))
    if getattr(db._local, "db", None) is not None:
        db._local.db.close()
    db._local.db = None
    db.init_db()
    return path


def test_monitor_snapshot_reports_queue_workers_and_step_progress(tmp_path, monkeypatch):
    isolated_db(tmp_path, monkeypatch)
    now = db.now_ts()
    conn = db._get_db()
    conn.execute(
        "INSERT INTO ci_workers(worker_id, token_hash, supported_profiles, max_concurrent, status, current_job_id, last_heartbeat, registered_at, metadata_json) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        ("wsl-ci-test", "hash", json.dumps(["repo-auto-check"]), 1, "busy", "job-running", now, now, "{}"),
    )
    conn.execute(
        "INSERT INTO ci_jobs(job_id,idempotency_key,repository,branch,commit_sha,profile,priority,status,worker_id,created_at,started_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("job-running", "idem-running", "owner/repo", "main", "a" * 40, "repo-auto-check", 100, "running", "wsl-ci-test", now - 80, now - 70),
    )
    conn.execute(
        "INSERT INTO ci_jobs(job_id,idempotency_key,repository,branch,commit_sha,profile,priority,status,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        ("job-queued", "idem-queued", "owner/repo", "feature", "b" * 40, "repo-auto-check", 50, "queued", now - 60),
    )
    conn.execute(
        "INSERT INTO ci_jobs(job_id,idempotency_key,repository,branch,commit_sha,profile,priority,status,created_at,started_at,finished_at,duration_seconds,exit_code) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("job-passed", "idem-passed", "owner/repo", "main", "c" * 40, "repo-auto-check", 100, "passed", now - 200, now - 180, now - 100, 80, 0),
    )
    conn.execute(
        "INSERT INTO ci_job_steps(job_id,step_name,status,started_at,finished_at,duration_seconds) VALUES(?,?,?,?,?,?)",
        ("job-running", "setup", "passed", now - 70, now - 50, 20),
    )
    conn.execute(
        "INSERT INTO ci_job_steps(job_id,step_name,status,started_at) VALUES(?,?,?,?)",
        ("job-running", "test", "running", now - 40),
    )
    conn.commit()

    snapshot = db.get_monitor_snapshot()

    assert snapshot["summary"]["queued"] == 1
    assert snapshot["summary"]["running"] == 1
    assert snapshot["summary"]["workers_online"] == 1
    running = next(job for job in snapshot["active_jobs"] if job["job_id"] == "job-running")
    assert running["current_step_index"] == 2
    assert running["current_step"] == "test"
    assert running["completed_steps"] == 1
    assert running["total_steps"] == 2
    assert running["elapsed_seconds"] >= 0
    assert snapshot["recent_jobs"][0]["job_id"] == "job-passed"


def test_monitor_api_requires_bearer_token(tmp_path, monkeypatch):
    isolated_db(tmp_path, monkeypatch)
    test_app = FastAPI()
    test_app.include_router(ci_monitor.router)
    client = TestClient(test_app)

    assert client.get("/ci/monitor").status_code == 200
    assert client.get("/api/v1/ci/monitor").status_code == 401
    response = client.get(
        "/api/v1/ci/monitor",
        headers={"Authorization": "Bearer test_api_key_32_bytes_long"},
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True
