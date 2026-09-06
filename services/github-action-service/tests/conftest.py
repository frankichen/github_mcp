import os
from dataclasses import dataclass, field

import pytest


_TEST_ENV = {
    "GITHUB_TOKEN": "test_token_value",
    "ACTION_API_KEY": "test_api_key_32_bytes_long",
    "ALLOWED_REPOSITORIES": "owner/allowed-repo",
    "ALLOW_DEFAULT_BRANCH_WRITE": "false",
    "MAX_FILE_CHARACTERS": "5000",
    "MAX_TOTAL_CHARACTERS": "10000",
    "MAX_FILES_PER_COMMIT": "5",
    "IDEMPOTENCY_DB_PATH": "/tmp/github-action-service-tests-idempotency.db",
    "CI_DB_PATH": "/tmp/github-action-service-tests-ci.db",
    "DEPLOYMENT_DB_PATH": "/tmp/github-action-service-tests-deployments.db",
    "INFRASTRUCTURE_DEPLOYMENT_DB_PATH": "/tmp/github-action-service-tests-infrastructure-deployments.db",
    "MYGITHUB12_DB_PATH": "/tmp/github-action-service-tests-mygithub12.db",
}

for _name, _value in _TEST_ENV.items():
    os.environ.setdefault(_name, _value)


@dataclass
class FakeLongRunningCI:
    """Deterministic logical-time CI lifecycle for Web-safe orchestration tests."""

    logical_time_seconds: float = 0.0
    history: list[dict] = field(
        default_factory=lambda: [{"status": "queued", "logical_time_seconds": 0.0}]
    )

    @property
    def status(self) -> str:
        return str(self.history[-1]["status"])

    def transition(self, status: str, at_seconds: float) -> dict:
        at_seconds = float(at_seconds)
        if at_seconds < self.logical_time_seconds:
            raise ValueError("logical CI time cannot move backwards")
        self.logical_time_seconds = at_seconds
        snapshot = {"status": status, "logical_time_seconds": at_seconds}
        self.history.append(snapshot)
        return dict(snapshot)

    def snapshot(self) -> dict:
        return dict(self.history[-1])


@pytest.fixture
def fake_long_running_ci():
    """Reusable fake CI clock; callers advance logical time without sleeping."""
    return FakeLongRunningCI()
