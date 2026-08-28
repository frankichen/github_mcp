import subprocess
from types import SimpleNamespace

import pytest

from private_ci_agent.services import ServiceManager, ServiceSetupError, safe_job_suffix, sanitize_service_stderr


def test_job_suffix_is_safe_and_bounded():
    assert safe_job_suffix("../../69b9dbdc94094f7b") == "69b9dbdc94094f7b"
    assert len(safe_job_suffix("X" * 100)) <= 48


def test_prepare_uses_isolated_network_and_aliases(monkeypatch, tmp_path):
    commands = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("private_ci_agent.services.subprocess.run", fake_run)
    manager = ServiceManager("podman")
    env = manager.prepare("job-123", str(tmp_path), ["postgres", "redis", "rabbitmq"])
    assert env.network == "ci-svc-wsl-ci-01-job_123"
    assert any("--pod" in command and any("postgres" in item for item in command) for command in commands)
    assert any("--pod" in command and any("redis" in item for item in command) for command in commands)
    assert any("--pod" in command and any("rabbitmq" in item for item in command) for command in commands)
    assert all("--http-proxy=false" in command for command in commands if command[1] == "run")
    assert any("private-ci.job=job_123" in item for command in commands for item in command)
    assert any("private-ci.resource=postgres" in item for command in commands for item in command)
    assert (tmp_path / "runtime" / "services.env").stat().st_mode & 0o777 == 0o600
    manager.cleanup("job-123", str(tmp_path))
    assert not (tmp_path / "runtime" / "services.env").exists()


def test_prepare_requires_explicit_service_list(tmp_path):
    with pytest.raises(ServiceSetupError) as raised:
        ServiceManager("podman").prepare("job-none", str(tmp_path), [])
    assert raised.value.code == "SERVICE_CONFIGURATION_INVALID"


def test_prepare_starts_only_explicitly_requested_services(monkeypatch, tmp_path):
    commands = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("private_ci_agent.services.subprocess.run", fake_run)
    manager = ServiceManager("podman")
    env = manager.prepare("job-redis-only", str(tmp_path), ["redis"])

    assert env.database_url == ""
    assert env.redis_addr == "redis:6379"
    assert env.rabbitmq_url == ""
    flattened = [item for command in commands for item in command]
    assert "docker.io/library/redis:7-alpine" in flattened
    assert "docker.io/library/postgres:16-alpine" not in flattened
    assert "docker.io/library/rabbitmq:3-management-alpine" not in flattened
    assert "postgres:127.0.0.1" not in flattened
    assert "rabbitmq:127.0.0.1" not in flattened
    assert "redis:127.0.0.1" in flattened


def test_cleanup_is_scoped_to_current_job(monkeypatch):
    commands = []
    monkeypatch.setattr("private_ci_agent.services.subprocess.run", lambda command, **_: commands.append(command) or SimpleNamespace(returncode=0, stdout="", stderr=""))
    ServiceManager("podman").cleanup("job-123")
    assert ["podman", "pod", "rm", "-f", "ci-svc-wsl-ci-01-job_123"] in commands
    assert all("lenshub-postgres" not in item for command in commands for item in command)


def test_second_worker_service_names_do_not_overlap_primary(monkeypatch, tmp_path):
    commands = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("private_ci_agent.services.subprocess.run", fake_run)
    manager = ServiceManager("podman", {"worker_id": "wsl-ci-02"})
    env = manager.prepare("job-123", str(tmp_path), ["redis"])

    assert env.network == "ci-svc-wsl-ci-02-job_123"
    flattened = [item for command in commands for item in command]
    assert "ci-wsl-ci-02-job_123-redis" in flattened
    assert not any("ci-wsl-ci-01" in item for item in flattened)


def test_default_service_images_match_preloaded_rootless_images():
    manager = ServiceManager("podman")
    assert manager.images == {
        "postgres": "docker.io/library/postgres:16-alpine",
        "redis": "docker.io/library/redis:7-alpine",
        "rabbitmq": "docker.io/library/rabbitmq:3-management-alpine",
    }


def test_service_run_failure_contains_safe_diagnostic(monkeypatch):
    def fake_run(_command, **_kwargs):
        return SimpleNamespace(
            returncode=125,
            stdout="",
            stderr=(
                "Error: POSTGRES_PASSWORD=unit-password-value "
                "pull https://user:unit-pass@example.invalid/image?token=unit-token-value "
                "secret=unit-secret-value"
            ),
        )

    monkeypatch.setattr("private_ci_agent.services.subprocess.run", fake_run)
    with pytest.raises(ServiceSetupError) as raised:
        ServiceManager("podman")._run(
            ["run", "docker.io/library/postgres:16-alpine"],
            "POSTGRES_UNAVAILABLE",
            resource_type="postgres",
            operation="start_container",
            image="docker.io/library/postgres:16-alpine",
            resource_name="ci-job-postgres",
        )

    message = str(raised.value)
    assert "code=POSTGRES_UNAVAILABLE" in message
    assert "operation=start_container" in message
    assert "exit_code=125" in message
    assert "resource=postgres" in message
    assert "name=ci-job-postgres" in message
    assert "image=docker.io/library/postgres:16-alpine" in message
    assert "unit-password-value" not in message
    assert "user:unit-pass" not in message
    assert "token=unit-token-value" not in message
    assert "secret=unit-secret-value" not in message
    assert len(message.rsplit("reason=", 1)[-1]) <= 500


def test_service_timeout_diagnostic_is_distinct(monkeypatch):
    def fake_run(_command, **_kwargs):
        raise subprocess.TimeoutExpired("podman", 20)

    monkeypatch.setattr("private_ci_agent.services.subprocess.run", fake_run)
    with pytest.raises(ServiceSetupError) as raised:
        ServiceManager("podman")._run(
            ["run", "docker.io/library/redis:7-alpine"],
            "REDIS_UNAVAILABLE",
            resource_type="redis",
            operation="start_container",
            image="docker.io/library/redis:7-alpine",
        )

    assert "code=REDIS_UNAVAILABLE" in str(raised.value)
    assert "timed_out=true" in str(raised.value)
    assert "exit_code=-1" in str(raised.value)


def test_missing_service_image_reports_inspect_operation(monkeypatch, tmp_path):
    def fake_run(command, **_kwargs):
        if command[1:3] == ["image", "exists"] and "postgres" in command[-1]:
            return SimpleNamespace(returncode=125, stdout="", stderr="image not known")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("private_ci_agent.services.subprocess.run", fake_run)
    with pytest.raises(ServiceSetupError) as raised:
        ServiceManager("podman").prepare("job-image-missing", str(tmp_path), ["postgres"])

    assert raised.value.code == "POSTGRES_UNAVAILABLE"
    assert "operation=inspect" in raised.value.diagnostic
    assert "image=docker.io/library/postgres:16-alpine" in raised.value.diagnostic
    assert "image not known" in raised.value.diagnostic


def test_readiness_reports_exited_resource_and_tail(monkeypatch):
    def fake_run(command, **_kwargs):
        if command[1:2] == ["exec"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="not ready")
        if command[1:3] == ["inspect", "--format"]:
            return SimpleNamespace(returncode=0, stdout="exited|17|container failed", stderr="")
        if command[1:3] == ["logs", "--tail"]:
            return SimpleNamespace(returncode=0, stdout="password=unit-hidden", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("private_ci_agent.services.subprocess.run", fake_run)
    manager = ServiceManager("podman")
    manager.timeout = 1
    with pytest.raises(ServiceSetupError) as raised:
        manager._wait_ready({
            "postgres": "ci-postgres",
            "redis": "ci-redis",
            "rabbitmq": "ci-rabbitmq",
        })

    message = str(raised.value)
    assert raised.value.code == "POSTGRES_UNAVAILABLE"
    assert "resource=postgres" in message
    assert "exit_code=17" in message
    assert "container failed" in message
    assert "password=unit-hidden" not in message


def test_readiness_diagnostic_includes_attempts_and_health(monkeypatch):
    def fake_run(command, **_kwargs):
        if command[1:2] == ["exec"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="not ready")
        if command[1:3] == ["inspect", "--format"]:
            return SimpleNamespace(returncode=0, stdout="running|0||starting", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("private_ci_agent.services.subprocess.run", fake_run)
    manager = ServiceManager("podman")
    manager.timeout = 0
    with pytest.raises(ServiceSetupError) as raised:
        manager._wait_ready({"postgres": "ci-postgres", "redis": "ci-redis", "rabbitmq": "ci-rabbitmq"})

    assert raised.value.code == "SERVICE_SETUP_TIMEOUT"
    assert "operation=readiness" in raised.value.diagnostic
    assert "attempts=0" in raised.value.diagnostic or "attempts=" in raised.value.diagnostic


def test_stderr_sanitizer_bounds_and_removes_url_credentials():
    value = "x " * 1000 + " https://user:unit-pass@example.invalid/path?token=unit-secret"
    sanitized = sanitize_service_stderr(value)
    assert len(sanitized) == 500
    assert "unit-pass" not in sanitized
    assert "token=unit-secret" not in sanitized
