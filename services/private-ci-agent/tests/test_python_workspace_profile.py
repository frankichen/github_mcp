from pathlib import Path
from types import SimpleNamespace

import pytest

from private_ci_agent.executor import JobExecutor
from private_ci_agent.models import Job
from private_ci_agent.podman import PodmanRunner
from private_ci_agent.profiles import python_commands_for_workspace


def _mkdir_package(root, name):
    package = root / name
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")


def _commands(plan, section):
    return [item["command"] if isinstance(item, dict) else item for item in plan[section]]


def test_requirements_dev_workspace_does_not_install_requirements_twice(tmp_path):
    (tmp_path / "requirements.txt").write_text("pyyaml\n", encoding="utf-8")
    (tmp_path / "requirements-dev.txt").write_text("-r requirements.txt\npytest\n", encoding="utf-8")
    _mkdir_package(tmp_path, "app")
    (tmp_path / "tests").mkdir()

    plan = python_commands_for_workspace(str(tmp_path))
    setup = _commands(plan, "setup")

    assert any("-r requirements-dev.txt" in command for command in setup)
    assert not any(command.endswith("-r requirements.txt 2>&1") for command in setup)
    assert [item["name"] for item in plan["check"]] == ["ruff", "compileall", "pytest"]
    assert "app tests" in plan["check"][0]["command"]


def test_pyproject_without_dev_extra_uses_plain_editable_install(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='private-ci-agent'\nversion='1.0.0'\n",
        encoding="utf-8",
    )
    _mkdir_package(tmp_path, "private_ci_agent")
    (tmp_path / "tests").mkdir()

    plan = python_commands_for_workspace(str(tmp_path))
    setup = _commands(plan, "setup")

    assert any("install --no-input --quiet -e ." in command for command in setup)
    assert not any("[dev" in command or "--extra" in command for command in setup)


def test_workspace_without_tests_has_structured_skip_and_safe_targets(tmp_path):
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")
    _mkdir_package(tmp_path, "app")

    plan = python_commands_for_workspace(str(tmp_path))

    assert plan["skipped"] == [{"name": "pytest", "status": "skipped", "reason": "no_tests_directory"}]
    assert all(item["name"] != "pytest" for item in plan["check"])
    assert plan["check"][0]["command"].endswith("ruff check app 2>&1")
    assert plan["check"][1]["command"].endswith("compileall -q app 2>&1")
    assert "||" not in " ".join(_commands(plan, "check"))
    assert "; true" not in " ".join(_commands(plan, "check"))


def test_missing_package_directory_is_configuration_error(tmp_path):
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()

    plan = python_commands_for_workspace(str(tmp_path))

    assert plan["error"] == "configuration_error"
    assert "approved package directory" in plan["message"]


def test_pipfile_only_workspace_is_rejected(tmp_path):
    (tmp_path / "Pipfile").write_text("[packages]\n", encoding="utf-8")
    _mkdir_package(tmp_path, "app")

    plan = python_commands_for_workspace(str(tmp_path))

    assert plan["error"] == "configuration_error"
    assert "Pipfile-only" in plan["message"]


class FakeLogManager:
    def upload(self, *_args):
        pass

    def get_total(self, *_args):
        return 0


class FakeClient:
    def __init__(self):
        self.started = []
        self.finished = []

    def start_step(self, _job_id, step_name):
        self.started.append(step_name)
        return len(self.started)

    def finish_step(self, _job_id, step_id, status, exit_code=None, log_end_offset=None):
        self.finished.append((step_id, status, exit_code, log_end_offset))


class SetupFailPodman:
    def __init__(self):
        self.calls = []

    def image_available(self, _image):
        return True

    def run_command(self, _image, _job_id, _source_dir, caches, command, _timeout, **kwargs):
        self.calls.append((command, caches, kwargs))
        return {"exit_code": 1, "stdout": "", "stderr": "setup failed", "timed_out": False}


class SuccessPodman:
    def __init__(self):
        self.calls = []

    def image_available(self, _image):
        return True

    def run_command(self, _image, _job_id, _source_dir, caches, command, _timeout, **kwargs):
        self.calls.append((command, caches, kwargs))
        return {"exit_code": 0, "stdout": "", "stderr": "", "timed_out": False}


def _executor(podman):
    executor = object.__new__(JobExecutor)
    executor.podman = podman
    executor.log_manager = FakeLogManager()
    executor.client = FakeClient()
    executor.services = SimpleNamespace()
    return executor


def _job(source_dir, workspace):
    return Job(
        job_id="job-123", repository="example/repo", branch="feature",
        commit_sha="a" * 40, profile="repo-auto-check", timeout_seconds=30,
        lease_token="lease", lease_expires_at="", source_dir=str(source_dir),
        workspace=str(workspace),
    )


def test_python_setup_failure_blocks_all_checks(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "requirements.txt").write_text("requests\n", encoding="utf-8")
    _mkdir_package(source, "app")
    (source / "tests").mkdir()
    podman = SetupFailPodman()

    executor = _executor(podman)
    result = executor._execute_workspace(
        _job(source, tmp_path),
        {"path": ".", "stack": "python"},
    )

    assert result["passed"] is False
    assert len(podman.calls) == 1
    assert podman.calls[0][2]["pass_proxy"] is True
    assert executor.client.started == ["python:.:bootstrap"]
    assert executor.client.finished[0][1:3] == ("failed", 1)
    blocked = [step for step in result["steps"] if step["status"] == "blocked_by_setup"]
    assert {step["step_name"].rsplit(":", 1)[-1] for step in blocked} == {"ruff", "compileall", "pytest"}


def test_python_checks_stay_network_isolated_without_proxy(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "requirements.txt").write_text("requests\n", encoding="utf-8")
    _mkdir_package(source, "app")
    (source / "tests").mkdir()
    podman = SuccessPodman()

    result = _executor(podman)._execute_workspace(
        _job(source, tmp_path),
        {"path": ".", "stack": "python"},
    )

    assert result["passed"] is True
    assert len(podman.calls) == 4
    assert podman.calls[0][2]["network"] is True
    assert podman.calls[0][2]["pass_proxy"] is True
    for _, _, options in podman.calls[1:]:
        assert options["network"] is False
        assert options["pass_proxy"] is False
        assert options["env"]["CI_COMMIT_SHA"] == "a" * 40
        assert options["env"]["CI_REPOSITORY_ROOT"] == "/repo"


def test_python_venv_is_scoped_by_workspace_identity(tmp_path):
    first_root = tmp_path / "services" / "a"
    second_root = tmp_path / "services" / "b"
    first_root.mkdir(parents=True)
    second_root.mkdir(parents=True)
    (first_root / "requirements.txt").write_text("", encoding="utf-8")
    (second_root / "requirements.txt").write_text("", encoding="utf-8")
    _mkdir_package(first_root, "app")
    _mkdir_package(second_root, "app")

    first = python_commands_for_workspace(str(first_root))
    second = python_commands_for_workspace(str(second_root))

    assert first["workspace_key"] != second["workspace_key"]
    assert f"/ci-venv/{first['workspace_key']}/bin/python" in first["check"][0]["command"]
    assert f"/ci-venv/{second['workspace_key']}/bin/python" in second["check"][0]["command"]
    assert "PIP_CACHE_DIR=/ci-venv/" not in first["setup"][0]["command"]
    assert "--retries 6 --timeout 60 install --no-input --quiet" in first["setup"][0]["command"]


class Completed:
    returncode = 0
    stdout = ""
    stderr = ""


def test_podman_mounts_python_venv_and_pip_cache_and_controls_proxy(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    venv = tmp_path / "venv"
    venv.mkdir()
    pip_cache = tmp_path / "pip"
    pip_cache.mkdir()
    captured = []

    def fake_run(command, **_kwargs):
        captured.append(command)
        return Completed()

    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:10808")
    monkeypatch.setattr("private_ci_agent.podman.subprocess.run", fake_run)
    runner = PodmanRunner("/usr/bin/podman")
    caches = {"python_venv": str(venv), "pip": str(pip_cache)}

    runner.run_command(
        "docker.io/library/python:3.12-slim", "job-123", str(source), caches,
        "python --version", 30, network=True, pass_proxy=True,
    )
    setup_command = captured[-1]
    assert f"{venv}:/ci-venv:Z" in setup_command
    assert f"{pip_cache}:/ci-cache/pip:rw,Z" in setup_command
    assert "HTTP_PROXY=http://host.containers.internal:10808" in setup_command
    assert "PIP_CACHE_DIR=/ci-cache/pip" in setup_command
    assert "--network=none" not in setup_command

    runner.run_command(
        "docker.io/library/python:3.12-slim", "job-123", str(source), caches,
        "python --version", 30, network=False, pass_proxy=False,
    )
    check_command = captured[-1]
    assert "--network=none" in check_command
    assert not any(item.startswith("HTTP_PROXY=") for item in check_command)
    assert "PIP_CACHE_DIR=/ci-cache/pip" in check_command
