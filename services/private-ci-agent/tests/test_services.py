from types import SimpleNamespace

from private_ci_agent.services import ServiceManager, safe_job_suffix


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
    env = manager.prepare("job-123", str(tmp_path))
    assert env.network == "ci-svc-job_123"
    assert any("--pod" in command and any("postgres" in item for item in command) for command in commands)
    assert any("--pod" in command and any("redis" in item for item in command) for command in commands)
    assert any("--pod" in command and any("rabbitmq" in item for item in command) for command in commands)
    assert all("--http-proxy=false" in command for command in commands if command[1] == "run")
    assert (tmp_path / "runtime" / "services.env").stat().st_mode & 0o777 == 0o600
    manager.cleanup("job-123", str(tmp_path))
    assert not (tmp_path / "runtime" / "services.env").exists()


def test_cleanup_is_scoped_to_current_job(monkeypatch):
    commands = []
    monkeypatch.setattr("private_ci_agent.services.subprocess.run", lambda command, **_: commands.append(command) or SimpleNamespace(returncode=0, stdout="", stderr=""))
    ServiceManager("podman").cleanup("job-123")
    assert ["podman", "pod", "rm", "-f", "ci-svc-job_123"] in commands
    assert all("lenshub-postgres" not in item for command in commands for item in command)
