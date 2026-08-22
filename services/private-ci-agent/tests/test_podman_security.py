import logging
import subprocess
import threading
from types import SimpleNamespace

import pytest

from private_ci_agent.podman import PodmanRunner, ROOTLESS_OUTBOUND_NETWORK


def test_rootless_command_does_not_use_env_host_or_forward_tokens(monkeypatch, tmp_path):
    captured = []

    def fake_run(command, **_kwargs):
        captured.append(command)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    for name, value in {
        "HTTP_PROXY": "http://127.0.0.1:10808",
        "HTTPS_PROXY": "http://127.0.0.1:10808",
        "ALL_PROXY": "socks5h://127.0.0.1:10808",
        "http_proxy": "http://127.0.0.1:10808",
        "https_proxy": "http://127.0.0.1:10808",
        "all_proxy": "socks5h://127.0.0.1:10808",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("private_ci_agent.podman.subprocess.run", fake_run)
    result = PodmanRunner("podman").run_command(
        "docker.io/library/golang:1.26.4",
        "job-123",
        str(tmp_path),
        {},
        "go version",
        30,
        env={
            "CI_WORKER_TOKEN": "must-not-pass",
            "CI_COMMIT_SHA": "a" * 40,
            "CI_REPOSITORY_ROOT": "/repo",
        },
        network=False,
    )

    assert result["exit_code"] == 0
    command = captured[0]
    assert "--env-host" not in command
    assert "--http-proxy=false" in command
    assert all("TOKEN" not in item and "must-not-pass" not in item for item in command)
    assert "CI_COMMIT_SHA=" + "a" * 40 in command
    assert "CI_REPOSITORY_ROOT=/repo" in command
    assert not any(item.startswith(("HTTP_PROXY=", "HTTPS_PROXY=", "ALL_PROXY=", "http_proxy=", "https_proxy=", "all_proxy=")) for item in command)
    assert "127.0.0.1:10808" not in command
    assert "--network=none" in command



def test_pip_cache_uses_operator_controlled_index(monkeypatch, tmp_path):
    captured = []

    def fake_run(command, **_kwargs):
        captured.append(command)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setenv("PIP_INDEX_URL", "https://pypi.tuna.tsinghua.edu.cn/simple")
    monkeypatch.setenv("PIP_TRUSTED_HOST", "pypi.tuna.tsinghua.edu.cn")
    monkeypatch.setattr("private_ci_agent.podman.subprocess.run", fake_run)
    cache = tmp_path / "pip-cache"
    cache.mkdir()

    result = PodmanRunner("podman").run_command(
        "docker.io/library/python:3.12-slim", "job-123", str(tmp_path),
        {"pip": str(cache)}, "python -m pip --version", 30,
        env={"PIP_INDEX_URL": "https://untrusted.example/simple"}, network=True,
    )

    assert result["exit_code"] == 0
    command = captured[0]
    assert "PIP_CACHE_DIR=/ci-cache/pip" in command
    assert "PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple" in command
    assert "PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn" in command
    no_proxy = next(item for item in command if item.startswith("NO_PROXY="))
    assert "pypi.tuna.tsinghua.edu.cn" in no_proxy
    assert not any("untrusted.example" in item for item in command)


def test_credentialed_pip_index_is_not_forwarded(monkeypatch):
    monkeypatch.setenv("PIP_INDEX_URL", "https://user:secret@example.com/simple")
    monkeypatch.setenv("PIP_TRUSTED_HOST", "bad/host")

    assert PodmanRunner._controlled_pip_env() == {}


def test_local_shared_node_image_never_falls_back_to_pull(monkeypatch):
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=1, stdout="", stderr="missing")

    monkeypatch.setattr("private_ci_agent.podman.subprocess.run", fake_run)
    assert not PodmanRunner("podman").image_available("localhost/node-chromium:22")
    assert len(calls) == 1
    assert calls[0][:3] == ["podman", "image", "exists"]


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
    assert "--http-proxy=false" in command
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
    assert "--network" in command
    assert command[command.index("--network") + 1] == ROOTLESS_OUTBOUND_NETWORK
    assert "--network=none" not in command


def test_npm_cache_mounts_to_controlled_shared_directory(monkeypatch, tmp_path):
    captured = []

    def fake_run(command, **_kwargs):
        captured.append(command)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("private_ci_agent.podman.subprocess.run", fake_run)
    cache = tmp_path / "npm-cache"
    cache.mkdir()
    result = PodmanRunner("podman").run_command(
        "docker.io/library/node:22", "job-123", str(tmp_path / "source"),
        {"npm": str(cache)}, "npm ci", 30, network=True,
    )

    assert result["exit_code"] == 0
    command = captured[0]
    mount = command[command.index("-v", command.index("--workdir")) + 1]
    assert mount == f"{cache}:/ci-cache/npm:rw,z"
    assert "NPM_CONFIG_CACHE=/ci-cache/npm" in command
    assert not any("node_modules" in item for item in command)


def test_playwright_cache_mount_and_environment_are_explicit(monkeypatch, tmp_path):
    captured = []

    def fake_run(command, **_kwargs):
        captured.append(command)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("private_ci_agent.podman.subprocess.run", fake_run)
    cache = tmp_path / "ms-playwright"
    result = PodmanRunner("podman").run_command(
        "docker.io/library/node:22", "job-123", str(tmp_path / "source"),
        {"npm": str(tmp_path / "npm"), "playwright": str(cache)},
        "node -e 'console.log(process.env.PLAYWRIGHT_BROWSERS_PATH)'", 30, network=True,
    )

    assert result["exit_code"] == 0
    command = captured[0]
    assert f"{cache}:/ci-cache/ms-playwright:rw,z" in command
    assert "PLAYWRIGHT_BROWSERS_PATH=/ci-cache/ms-playwright" in command
    assert not any("node_modules" in item for item in command)
    assert "--http-proxy=false" in command


def test_shared_browser_cache_uses_nonexclusive_relabel(monkeypatch, tmp_path):
    captured = []

    def fake_run(command, **_kwargs):
        captured.append(command)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("private_ci_agent.podman.subprocess.run", fake_run)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    cache = tmp_path / "ms-playwright"
    runner = PodmanRunner("podman")
    runner.run_command("docker.io/library/node:22", "job-a", str(first), {"playwright": str(cache)}, "true", 30, network=False)
    runner.run_command("docker.io/library/node:22", "job-b", str(second), {"playwright": str(cache)}, "true", 30, network=False)

    assert all(f"{cache}:/ci-cache/ms-playwright:rw,z" in command for command in captured)
    assert all(":Z" not in item for command in captured for item in command if "ms-playwright" in item)


def test_proxy_is_explicitly_rewritten_and_redacted_from_logs(monkeypatch, tmp_path, caplog):
    captured = []

    def fake_run(command, **_kwargs):
        captured.append(command)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setenv("HTTP_PROXY", "http://proxy-user:proxy-password@127.0.0.1:10808")
    monkeypatch.setenv("HTTPS_PROXY", "http://localhost:10808")
    monkeypatch.setenv("ALL_PROXY", "socks5h://127.0.0.1:10808")
    monkeypatch.setenv("NO_PROXY", "allowed.internal")
    monkeypatch.setenv("PRIVATE_CI_CONTAINER_PROXY_HOST", "host.containers.internal")
    monkeypatch.setattr("private_ci_agent.podman.subprocess.run", fake_run)
    caplog.set_level(logging.DEBUG, logger="private_ci_agent.podman")

    PodmanRunner("podman").run_command(
        "docker.io/library/node:22", "job-123", str(tmp_path), {}, "npm ci", 30,
        network=True, pass_proxy=True,
    )

    assert len(captured) == 2
    validation, command = captured
    assert "--http-proxy=false" in validation
    assert any("curl -4 --proxy \"$proxy\" --connect-timeout 5 --max-time 15 -fsS -o /dev/null https://api.github.com" in item for item in validation)
    assert any("https://api.github.com" in item for item in validation)
    assert validation[validation.index("--network") + 1] == ROOTLESS_OUTBOUND_NETWORK
    assert "--http-proxy=false" in command
    assert command[command.index("--network") + 1] == ROOTLESS_OUTBOUND_NETWORK
    assert "HTTP_PROXY=http://proxy-user:proxy-password@host.containers.internal:10808" in command
    assert "HTTPS_PROXY=http://host.containers.internal:10808" in command
    assert "ALL_PROXY=socks5h://host.containers.internal:10808" in command
    assert not any("127.0.0.1:10808" in item or "localhost:10808" in item for item in command)
    no_proxy = next(item for item in command if item.startswith("NO_PROXY="))
    for host in ("allowed.internal", "postgres", "redis", "rabbitmq", "localhost", "127.0.0.1"):
        assert host in no_proxy
    assert next(item for item in command if item.startswith("no_proxy=")) == no_proxy.replace("NO_PROXY", "no_proxy", 1)
    assert "proxy-password" not in caplog.text


def test_proxy_rewrite_only_changes_loopback_hostname():
    assert PodmanRunner._rewrite_loopback_proxy_url(
        "http://notlocalhost.example:8080/path", "host.containers.internal"
    ) == "http://notlocalhost.example:8080/path"
    assert PodmanRunner._rewrite_loopback_proxy_url(
        "http://user:password@127.0.0.1:10808/path", "host.containers.internal"
    ) == "http://user:password@host.containers.internal:10808/path"


def test_proxy_validation_fails_closed_when_container_probe_fails(monkeypatch, tmp_path):
    def fake_run(_command, **_kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:10808")
    monkeypatch.setenv("PRIVATE_CI_CONTAINER_PROXY_HOST", "host.containers.internal")
    monkeypatch.setattr("private_ci_agent.podman.subprocess.run", fake_run)

    result = PodmanRunner("podman").run_command(
        "docker.io/library/node:22", "job-123", str(tmp_path), {}, "npm ci", 30,
        network=True, pass_proxy=True,
    )

    assert result["stderr"] == "PROXY_VALIDATION_FAILED"


def test_parallel_node_workspaces_share_only_controlled_download_cache(monkeypatch, tmp_path):
    captured = []

    def fake_run(command, **_kwargs):
        captured.append(command)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("private_ci_agent.podman.subprocess.run", fake_run)
    cache = tmp_path / "npm-cache"
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    runner = PodmanRunner("podman")
    runner.run_command("docker.io/library/node:22", "job-a", str(first), {"npm": str(cache)}, "npm ci", 30, network=True)
    runner.run_command("docker.io/library/node:22", "job-b", str(second), {"npm": str(cache)}, "npm ci", 30, network=True)

    assert all(f"{cache}:/ci-cache/npm:rw,z" in command for command in captured)
    assert any(f"{first}:/workspace:Z" in item for item in captured[0])
    assert any(f"{second}:/workspace:Z" in item for item in captured[1])
    assert all(not any("node_modules" in item for item in command) for command in captured)


def test_legacy_run_also_disables_automatic_proxy_inheritance(monkeypatch, tmp_path):
    captured = []

    def fake_run(command, **_kwargs):
        captured.append(command)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:10808")
    monkeypatch.setattr("private_ci_agent.podman.subprocess.run", fake_run)
    PodmanRunner("podman").run("docker.io/library/node:22", "job-123", str(tmp_path), {}, ["node --version"], 30)

    command = captured[0]
    assert "--http-proxy=false" in command
    assert not any(item.startswith("HTTP_PROXY=") for item in command)
    assert "127.0.0.1:10808" not in command


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


def test_proxy_probe_reuses_named_service_pod(monkeypatch, tmp_path):
    captured = []

    def fake_run(command, **_kwargs):
        captured.append(command)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:10808")
    monkeypatch.setenv("PRIVATE_CI_CONTAINER_PROXY_HOST", "10.0.2.2")
    monkeypatch.setattr("private_ci_agent.podman.subprocess.run", fake_run)

    PodmanRunner("podman").run_command(
        "docker.io/library/golang:1.26.4", "job-123", str(tmp_path), {}, "go env", 30,
        network=True, network_name="ci-job_123", pass_proxy=True,
    )

    assert len(captured) == 2
    for command in captured:
        assert command[command.index("--pod") + 1] == "ci-job_123"
        assert ROOTLESS_OUTBOUND_NETWORK not in command


# ---- cancellation / forced reclaim ----


def test_run_command_with_cancel_event_stops_container_and_reports_cancelled(monkeypatch, tmp_path):
    """A cancel request must terminate a running container instead of waiting
    for the network request inside it to finish."""
    import time
    import private_ci_agent.podman as podman_module

    killed = []

    class FakePopen:
        def __init__(self, cmd, **_kwargs):
            self.returncode = None

        def terminate(self):
            self.returncode = -1

        def kill(self):
            self.returncode = -1

        def communicate(self, timeout=None):
            if self.returncode is None:
                # The real communicate keeps blocking on a live process; only
                # termination lets the reaping call return.
                raise subprocess.TimeoutExpired("podman", timeout)
            return "out", "CANCELLED"

    def fake_stop(cmd, **_kwargs):
        killed.append(cmd)

    monkeypatch.setattr(podman_module.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(podman_module.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""))
    cancel_event = threading.Event()
    runner = PodmanRunner("podman")
    monkeypatch.setattr(runner, "_kill_container", fake_stop)

    def request_cancel():
        time.sleep(0.05)
        cancel_event.set()

    cancel_thread = threading.Thread(target=request_cancel)
    cancel_thread.start()
    result = runner.run_command(
        "docker.io/library/node:22", "job-123", str(tmp_path), {}, "npm ci", 900,
        network=True, pass_proxy=False, cancel_event=cancel_event,
    )
    cancel_thread.join()

    assert result["cancelled"] is True
    assert result["exit_code"] == -1
    assert any("ci-job-123" in str(cmd) for cmd in killed)


def test_kill_job_stops_every_container_with_job_prefix(monkeypatch):
    import private_ci_agent.podman as podman_module

    ps_output = "ci-job-123-abc123\nci-job-123-def456\nci-job-999-other\nci-svc-job_123\n"
    stopped = []

    def fake_run(cmd, **_kwargs):
        if cmd[:2] == ["podman", "ps"]:
            return SimpleNamespace(returncode=0, stdout=ps_output, stderr="")
        stopped.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(podman_module.subprocess, "run", fake_run)
    runner = PodmanRunner("podman")
    count = runner.kill_job("job-123")

    assert count == 2
    assert any("ci-job-123-abc123" in cmd and cmd[1] == "stop" for cmd in stopped)
    assert any("ci-job-123-def456" in cmd and cmd[1] == "stop" for cmd in stopped)
    assert not any("ci-job-999" in cmd or "ci-svc-job_123" in cmd for cmd in stopped)


def test_cleanup_stale_keeps_active_job_containers(monkeypatch):
    import private_ci_agent.podman as podman_module

    ps_output = "ci-job-123-abc123\nci-job-456-stale\n"
    removed = []

    def fake_run(cmd, **_kwargs):
        if cmd[:2] == ["podman", "ps"]:
            return SimpleNamespace(returncode=0, stdout=ps_output, stderr="")
        if cmd[1] == "rm":
            removed.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(podman_module.subprocess, "run", fake_run)
    PodmanRunner("podman").cleanup_stale(["job-123"])

    assert removed == [["podman", "rm", "-f", "ci-job-456-stale"]]


def test_candidate_build_uses_fixed_rootless_context_and_controlled_proxy(monkeypatch, tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM python:3.12-slim AS runtime\n", encoding="utf-8")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:10808")
    captured = []
    runner = PodmanRunner("podman")
    monkeypatch.setattr(runner, "_validate_container_proxy", lambda *args, **kwargs: True)

    def fake_process(cmd, _name, _timeout, _cancel):
        captured.append(cmd)
        return {"exit_code": 0, "stdout": "ok", "stderr": "", "timed_out": False}

    monkeypatch.setattr(runner, "_run_process", fake_process)
    result = runner.build_candidate(
        "job/unsafe", str(tmp_path), 60,
        probe_image="docker.io/library/python:3.12-slim",
        build_policy="xianyu-radar-python-v1",
    )

    command = captured[0]
    assert command[:3] == ["podman", "build", "--pull=never"]
    assert command[command.index("--from") + 1] == "docker.io/library/python:3.12-slim"
    assert "--network" in command
    assert "slirp4netns:allow_host_loopback=true" in command
    assert "localhost/private-ci-job-unsafe:candidate" in command
    assert "HTTP_PROXY=http://host.containers.internal:10808" in command
    assert command[-1] == str(tmp_path.resolve())
    assert "-c" not in command
    assert result["exit_code"] == 0
    assert result["image"] == "localhost/private-ci-job-unsafe:candidate"


def test_candidate_build_accepts_canonical_base_and_stage_to_stage_from(monkeypatch, tmp_path):
    (tmp_path / "Dockerfile").write_text(
        "FROM docker.io/library/python:3.12-slim AS runtime\nFROM runtime AS final\n",
        encoding="utf-8",
    )
    captured = []
    runner = PodmanRunner("podman")
    monkeypatch.setattr(runner, "_validate_container_proxy", lambda *args, **kwargs: True)

    def fake_process(cmd, _name, _timeout, _cancel):
        captured.append(cmd)
        return {"exit_code": 0, "stdout": "ok", "stderr": "", "timed_out": False}

    monkeypatch.setattr(runner, "_run_process", fake_process)
    result = runner.build_candidate(
        "job-123", str(tmp_path), 60,
        probe_image="docker.io/library/python:3.12-slim",
        build_policy="xianyu-radar-python-v1",
    )

    assert result["exit_code"] == 0
    assert captured[0][captured[0].index("--from") + 1] == "docker.io/library/python:3.12-slim"


@pytest.mark.parametrize(
    ("dockerfile", "message"),
    [
        ("FROM evil.example/image:latest\n", "unapproved external Dockerfile base"),
        ("FROM ${BASE_IMAGE}\n", "dynamic Dockerfile base is not allowed"),
        (
            "FROM python:3.12-slim AS runtime\nFROM evil.example/image:latest AS helper\n",
            "additional external Dockerfile FROM is not allowed",
        ),
    ],
    ids=["unapproved-base", "dynamic-base", "second-external-base"],
)
def test_candidate_build_fails_closed_before_subprocess(monkeypatch, tmp_path, dockerfile, message):
    (tmp_path / "Dockerfile").write_text(dockerfile, encoding="utf-8")
    runner = PodmanRunner("podman")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("controlled build must fail before proxy probe or build subprocess")

    monkeypatch.setattr(runner, "_validate_container_proxy", forbidden)
    monkeypatch.setattr(runner, "_run_process", forbidden)
    result = runner.build_candidate(
        "job-123", str(tmp_path), 60,
        probe_image="docker.io/library/python:3.12-slim",
        build_policy="xianyu-radar-python-v1",
    )

    assert result["exit_code"] == 2
    assert result["stderr"].startswith("CONFIGURATION_ERROR:")
    assert message in result["stderr"]


def test_candidate_build_rejects_arbitrary_image_as_policy_before_subprocess(monkeypatch, tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM python:3.12-slim\n", encoding="utf-8")
    runner = PodmanRunner("podman")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("arbitrary caller image must not reach a build subprocess")

    monkeypatch.setattr(runner, "_validate_container_proxy", forbidden)
    monkeypatch.setattr(runner, "_run_process", forbidden)
    result = runner.build_candidate(
        "job-123", str(tmp_path), 60,
        probe_image="docker.io/library/python:3.12-slim",
        build_policy="evil.example/image:latest",
    )

    assert result["exit_code"] == 2
    assert "unknown controlled build policy" in result["stderr"]
