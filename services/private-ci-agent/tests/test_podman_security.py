from types import SimpleNamespace

from private_ci_agent.podman import PodmanRunner


def test_rootless_command_does_not_use_env_host_or_forward_tokens(monkeypatch, tmp_path):
    captured = []

    def fake_run(command, **_kwargs):
        captured.append(command)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("private_ci_agent.podman.subprocess.run", fake_run)
    result = PodmanRunner("podman").run_command(
        "docker.io/library/golang:1.26.4",
        "job-123",
        str(tmp_path),
        {},
        "go version",
        30,
        env={"CI_WORKER_TOKEN": "must-not-pass", "HTTP_PROXY": "http://proxy.invalid"},
        network=False,
    )

    assert result["exit_code"] == 0
    command = captured[0]
    assert "--env-host" not in command
    assert all("TOKEN" not in item and "must-not-pass" not in item for item in command)
    assert "--network=none" in command


def test_go_uses_read_write_job_cache_and_controlled_environment(monkeypatch, tmp_path):
    captured = []

    def fake_run(command, **_kwargs):
        captured.append(command)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("private_ci_agent.podman.subprocess.run", fake_run)
    cache = tmp_path / "go-cache"
    cache.mkdir()
    result = PodmanRunner("podman").run_command(
        "100.118.124.97:5555/library/golang:1.26.4", "job-123", str(tmp_path / "source"),
        {"go": str(cache)}, "go env", 30, env={"GITHUB_TOKEN": "must-not-pass", "UNCONTROLLED": "must-not-pass"}, network=True,
    )

    assert result["exit_code"] == 0
    command = captured[0]
    assert command.count("--read-only") == 1
    mount = command[command.index("--mount") + 1]
    assert "dst=/ci-cache,rw" in mount
    assert "GOPATH=/ci-cache/gopath" in command
    assert "GOMODCACHE=/ci-cache/gomod" in command
    assert "GOCACHE=/ci-cache/gobuild" in command
    assert "GOTMPDIR=/ci-cache/tmp" in command
    assert "--privileged" not in command
    assert "--env-host" not in command
    assert all("GITHUB_TOKEN" not in item for item in command)
    assert all("UNCONTROLLED" not in item for item in command)


def test_service_environment_is_forwarded_without_host_env(monkeypatch, tmp_path):
    captured = []

    def fake_run(command, **_kwargs):
        captured.append(command)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("private_ci_agent.podman.subprocess.run", fake_run)
    cache = tmp_path / "go-cache"
    cache.mkdir()
    PodmanRunner("podman").run_command(
        "100.118.124.97:5555/library/golang:1.26.4", "job-123", str(tmp_path / "source"),
        {"go": str(cache)}, "go test ./...", 30,
        env={"DATABASE_URL": "postgres://lenshub:REDACTED@postgres:5432/lenshub_ci_job", "CI_WORKER_TOKEN": "no"},
        network_name="ci-job_123",
    )
    command = captured[0]
    assert "--pod" in command and "ci-job_123" in command
    assert any(item.startswith("DATABASE_URL=") for item in command)
    assert "CI_WORKER_TOKEN" not in " ".join(command)
    assert "postgres" in next(item for item in command if item.startswith("NO_PROXY="))
