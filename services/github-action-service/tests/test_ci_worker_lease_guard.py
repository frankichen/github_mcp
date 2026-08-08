import os

from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("GITHUB_TOKEN", "test_token_value")
os.environ.setdefault("ACTION_API_KEY", "test_api_key_32_bytes_long")

from app.routers import ci_worker


def test_stale_lease_cannot_append_logs(monkeypatch):
    app = FastAPI()
    app.include_router(ci_worker.router)

    async def accept(_request):
        return "wsl-ci-test"

    appended = []
    monkeypatch.setattr(ci_worker, "verify_ci_worker", accept)
    monkeypatch.setattr(ci_worker, "get_current_lease_attempt", lambda *_args: None)
    monkeypatch.setattr(ci_worker, "append_log_chunk", lambda *args: appended.append(args))

    with TestClient(app) as client:
        response = client.post(
            "/internal/ci/jobs/job-1/logs",
            json={"content": "stale", "lease_token": "old-attempt"},
            headers={"Authorization": "Bearer worker", "X-Worker-ID": "wsl-ci-test"},
        )

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "stale_lease"
    assert appended == []
