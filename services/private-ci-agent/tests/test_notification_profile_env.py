"""Regression: managed CI identity survives the final Podman environment allowlist."""

from types import SimpleNamespace

import pytest

from private_ci_agent.executor import JobExecutor
from private_ci_agent.podman import PodmanRunner


@pytest.mark.parametrize(
    ("repository", "profile", "marker_expected"),
    [
        ("frankichen/sxt", "repo-auto-check", False),
        ("frankichen/sxt", "go-check", False),
        ("frankichen/sxt", "repo-fast-check", False),
        ("other/repository", "repo-auto-check", False),
    ],
)
def test_profile_and_dedicated_stage_reach_real_podman_arguments(
    monkeypatch, tmp_path, repository, profile, marker_expected
):
    """Mock only Podman execution; inspect the *actual* process argv after filtering."""
    calls = []

    def capture_subprocess(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("private_ci_agent.podman.subprocess.run", capture_subprocess)
    executor = object.__new__(JobExecutor)
    executor.podman = PodmanRunner("podman")
    executor.client = SimpleNamespace(start_step=lambda *_args: None)
    executor.log_manager = SimpleNamespace(upload=lambda *_args: None)
    job = SimpleNamespace(
        job_id="integration-check", repository=repository, profile=profile,
        commit_sha="a" * 40, timeout_seconds=30,
    )

    result = executor._run_check(
        job, "go:.", "docker.io/library/golang:1.26.4", str(tmp_path),
        {}, "gotest", "true",
    )

    assert result["status"] == "passed"
    assert calls
    args = calls[-1]
    assert "CI_COMMIT_SHA=" + "a" * 40 in args
    assert "CI_PROFILE=" + profile in args
    assert ("SXT_NOTIFICATION_NODE_CI_ENABLED=1" in args) == marker_expected
    assert "--env-host" not in args


def test_dedicated_notification_phase_forwards_marker_through_podman(
    monkeypatch, tmp_path
):
    """The Worker alone marks the real Go+Node stage after the Go checks pass."""
    calls = []

    def capture_subprocess(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("private_ci_agent.podman.subprocess.run", capture_subprocess)
    executor = object.__new__(JobExecutor)
    executor.podman = PodmanRunner("podman")
    monkeypatch.setattr(executor.podman, "image_available", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(executor.podman, "image_digest", lambda *_args, **_kwargs: "sha256:prewarmed")
    executor.cache_map = {"go": str(tmp_path)}
    executor.client = SimpleNamespace(start_step=lambda *_args: None)
    executor.log_manager = SimpleNamespace(upload=lambda *_args: None)
    job = SimpleNamespace(
        job_id="integration-check", repository="frankichen/sxt",
        profile="repo-auto-check", commit_sha="a" * 40,
        timeout_seconds=30, source_dir=str(tmp_path),
    )
    service = SimpleNamespace(
        network="ci-network", database_url="postgres://ci@postgres/test",
        redis_addr="", redis_db="", rabbitmq_url="",
    )

    result = executor._run_notification_node_integration(job, service)

    assert result["status"] == "passed"
    args = calls[-1]
    assert "CI_PROFILE=repo-auto-check" in args
    assert "SXT_NOTIFICATION_NODE_CI_ENABLED=1" in args
    assert "DATABASE_URL=postgres://ci@postgres/test" in args


def test_unapproved_or_secret_keys_never_enter_container_argv(monkeypatch, tmp_path):
    calls = []

    def capture_subprocess(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("private_ci_agent.podman.subprocess.run", capture_subprocess)
    result = PodmanRunner("podman").run_command(
        "docker.io/library/golang:1.26.4", "test-job", str(tmp_path),
        {}, "true", 30,
        env={
            "CI_PROFILE": "repo-auto-check",
            "SXT_NOTIFICATION_NODE_CI_ENABLED": "1",
            "CI_COMMIT_SHA": "b" * 40,
            "CI_WORKER_TOKEN": "never-forward",
            "GITHUB_TOKEN": "never-forward",
            "UNAPPROVED_MARKER": "never-forward",
        },
    )
    assert result["exit_code"] == 0
    args = calls[-1]
    assert "CI_PROFILE=repo-auto-check" in args
    assert "SXT_NOTIFICATION_NODE_CI_ENABLED=1" in args
    assert all("never-forward" not in arg for arg in args)
    assert all(not arg.startswith(("CI_WORKER_TOKEN=", "GITHUB_TOKEN=", "UNAPPROVED_MARKER=")) for arg in args)
