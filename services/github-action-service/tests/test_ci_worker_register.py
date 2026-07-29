import json
import os
import sqlite3
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("GITHUB_TOKEN", "test_token_value")
os.environ.setdefault("ACTION_API_KEY", "test_api_key_32_bytes_long")

from app.ci_database import _worker_row_to_dict, init_db, register_worker
from app.routers import ci_worker


@pytest.fixture
def client():
    test_app = FastAPI()
    test_app.include_router(ci_worker.router)
    with TestClient(test_app) as test_client:
        yield test_client


def registration_body(**overrides):
    body = {
        "worker_id": "wsl-ci-test",
        "token": "redacted-test-token",
        "profiles": ["repo-auto-check", "go-check", "node-check"],
        "max_concurrent": 1,
    }
    body.update(overrides)
    return body


def patch_successful_registration():
    return patch.multiple(
        ci_worker,
        recover_worker_jobs=lambda _worker_id: None,
        register_worker=lambda *_args: True,
    )


def test_first_registration_returns_200(client):
    with patch_successful_registration():
        response = client.post("/internal/ci/workers/register", json=registration_body())
    assert response.status_code == 200
    assert response.json() == {"status": "registered", "worker_id": "wsl-ci-test"}


def test_repeated_registration_is_idempotent(client):
    with patch_successful_registration():
        assert client.post("/internal/ci/workers/register", json=registration_body()).status_code == 200
        assert client.post("/internal/ci/workers/register", json=registration_body()).status_code == 200


def test_current_job_null_is_serialized_as_null():
    row = {
        "worker_id": "wsl-ci-test",
        "current_job_id": None,
        "last_heartbeat": 1_700_000_000.0,
        "supported_profiles": json.dumps(["go-check"]),
        "max_concurrent": 1,
        "status": "idle",
    }
    result = _worker_row_to_dict(row, 1_700_000_010.0)
    assert result["current_job"] is None
    assert isinstance(result, dict)


def test_profiles_must_be_a_list(client):
    with patch_successful_registration():
        response = client.post("/internal/ci/workers/register", json=registration_body(profiles="go-check"))
    assert response.status_code == 400


def test_max_concurrent_must_be_an_integer(client):
    with patch_successful_registration():
        response = client.post("/internal/ci/workers/register", json=registration_body(max_concurrent="1"))
    assert response.status_code == 400


def test_valid_heartbeat_is_iso_8601(client):
    row = {
        "worker_id": "wsl-ci-test",
        "current_job_id": None,
        "last_heartbeat": 1_700_000_000.0,
        "supported_profiles": json.dumps(["go-check"]),
        "max_concurrent": 1,
        "status": "idle",
    }
    result = _worker_row_to_dict(row, 1_700_000_010.0)
    assert result["last_heartbeat"].endswith("+00:00")


def test_worker_dict_always_returns_dict():
    row = {
        "worker_id": "wsl-ci-test",
        "current_job_id": None,
        "last_heartbeat": 0,
        "supported_profiles": "[]",
        "max_concurrent": 1,
        "status": "idle",
    }
    assert isinstance(_worker_row_to_dict(row, 1_700_000_010.0), dict)


def test_register_worker_upsert_does_not_duplicate(tmp_path, monkeypatch):
    db_path = tmp_path / "ci.db"
    import app.ci_database as database
    monkeypatch.setattr(database, "DB_PATH", str(db_path))
    database._local.db = None
    init_db()
    try:
        assert register_worker("wsl-ci-test", "test-token", ["go-check"], 1)
        assert register_worker("wsl-ci-test", "test-token", ["go-check", "node-check"], 1)
        db = sqlite3.connect(db_path)
        assert db.execute("select count(*) from ci_workers where worker_id='wsl-ci-test'").fetchone()[0] == 1
        assert json.loads(db.execute("select supported_profiles from ci_workers where worker_id='wsl-ci-test'").fetchone()[0]) == ["go-check", "node-check"]
    finally:
        if database._local.db is not None:
            database._local.db.close()
            database._local.db = None


def test_register_storage_failure_is_503(client):
    with patch.object(ci_worker, "recover_worker_jobs"), patch.object(ci_worker, "register_worker", return_value=False):
        response = client.post("/internal/ci/workers/register", json=registration_body())
    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "worker_registration_unavailable"


def test_invalid_json_is_4xx(client):
    with patch_successful_registration():
        response = client.post("/internal/ci/workers/register", content=b"not-json", headers={"Content-Type": "application/json"})
    assert response.status_code == 400


def test_missing_fields_are_4xx(client):
    with patch_successful_registration():
        response = client.post("/internal/ci/workers/register", json={"worker_id": "wsl-ci-test"})
    assert response.status_code == 400


def test_register_response_does_not_expose_token(client):
    with patch_successful_registration():
        response = client.post("/internal/ci/workers/register", json=registration_body())
    assert "token" not in response.text.lower()


def test_job_lease_recovers_expired_leases(client, monkeypatch):
    recovered = []

    async def accept(_request):
        return "wsl-ci-test"

    monkeypatch.setattr(ci_worker, "verify_ci_worker", accept)
    monkeypatch.setattr(
        ci_worker,
        "get_worker",
        lambda worker_id: {
            "worker_id": worker_id,
            "supported_profiles": ["repo-auto-check"],
            "max_concurrent": 1,
        },
    )
    monkeypatch.setattr(ci_worker, "recover_expired_leases", lambda: recovered.append(True))
    monkeypatch.setattr(ci_worker, "lease_job", lambda *_args: None)

    response = client.post("/internal/ci/jobs/lease", headers={"Authorization": "Bearer test", "X-Worker-ID": "wsl-ci-test"})

    assert response.status_code == 200
    assert recovered == [True]


def test_missing_worker_auth_is_401(client):
    response = client.post("/internal/ci/workers/heartbeat", json={})
    assert response.status_code == 401


def test_invalid_worker_auth_is_403(client, monkeypatch):
    from fastapi import HTTPException

    async def reject(_request):
        raise HTTPException(status_code=403, detail={"error": "forbidden"})

    monkeypatch.setattr(ci_worker, "verify_ci_worker", reject)
    response = client.post("/internal/ci/workers/heartbeat", json={}, headers={"Authorization": "Bearer invalid", "X-Worker-ID": "wsl-ci-test"})
    assert response.status_code == 403


def test_list_workers_preserves_history_and_null_job(client, monkeypatch):
    async def accept(_request):
        return "wsl-ci-test"

    monkeypatch.setattr(ci_worker, "verify_ci_worker", accept)
    monkeypatch.setattr(ci_worker, "get_workers", lambda: [{"worker_id": "wsl-ci-test", "online": True, "status": "idle", "current_job": None}])
    response = client.get("/internal/ci/workers", headers={"Authorization": "Bearer test", "X-Worker-ID": "wsl-ci-test"})
    assert response.status_code == 200
    assert response.json()["workers"][0]["current_job"] is None
