import pytest
import json
from fastapi.testclient import TestClient

import os
os.environ["GITHUB_TOKEN"] = "test_token_value"
os.environ["ACTION_API_KEY"] = "test_api_key_32_bytes_long"
os.environ["ALLOWED_REPOSITORIES"] = "owner/allowed-repo"
os.environ["ALLOW_DEFAULT_BRANCH_WRITE"] = "false"
os.environ["MAX_FILE_CHARACTERS"] = "5000"
os.environ["MAX_TOTAL_CHARACTERS"] = "10000"
os.environ["MAX_FILES_PER_COMMIT"] = "5"
os.environ["IDEMPOTENCY_DB_PATH"] = "/tmp/test_idempotency.db"

from app.main import app
from app.config import settings

VALID_API_KEY = settings.ACTION_API_KEY.get_secret_value()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c

    import os
    if os.path.exists("/tmp/test_idempotency.db"):
        os.remove("/tmp/test_idempotency.db")


def auth_headers():
    return {"Authorization": f"Bearer {VALID_API_KEY}"}


class TestIdempotencyMiddleware:
    def test_missing_key_proceeds_normally(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"

    def test_invalid_key_characters_returns_400(self, client):
        response = client.post(
            "/api/v1/github/branches",
            json={
                "repository": "owner/repo",
                "branch": "test",
            },
            headers={
                **auth_headers(),
                "Idempotency-Key": "key\x00with\x00nulls",
            },
        )
        assert response.status_code == 400

    def test_key_too_long_returns_400(self, client):
        long_key = "a" * 300
        response = client.post(
            "/api/v1/github/branches",
            json={
                "repository": "owner/repo",
                "branch": "test",
            },
            headers={
                **auth_headers(),
                "Idempotency-Key": long_key,
            },
        )
        assert response.status_code == 400

    def test_same_key_same_request_returns_cached(self, client):
        response1 = client.get("/health")
        assert response1.status_code == 200

        response2 = client.get(
            "/health",
            headers={"Idempotency-Key": "test-key-123"},
        )
        assert response2.status_code == 200

    def test_different_method_skips_idempotency(self, client):
        response = client.get(
            "/health",
            headers={"Idempotency-Key": "get-key-1"},
        )
        assert response.status_code == 200
