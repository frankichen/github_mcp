from types import SimpleNamespace

from private_ci_agent.executor import JobExecutor
from private_ci_agent.models import Job
from private_ci_agent.workspace import WorkspaceManager


class FakeLogManager:
    def reset(self, _job_id):
        pass

    def upload(self, _job_id, _message):
        pass

    def get_total(self, _job_id):
        return 0


class FakeClient:
    def start_step(self, _job_id, _step_name):
        return None


class FakePodman:
    def __init__(self, failing_command=None):
        self.failing_command = failing_command
        self.commands = []
        self.caches = []

    def image_available(self, _image):
        return True

    def run_command(self, _image, _job_id, _source_dir, _caches, command, _timeout, network=False, **_kwargs):
        self.caches.append(_caches)
        self.commands.append((command, network))
        exit_code = 1 if self.failing_command and self.failing_command in command else 0
        return {"exit_code": exit_code, "stdout": "", "stderr": "", "timed_out": False}


def make_executor(podman):
    executor = object.__new__(JobExecutor)
    executor.podman = podman
    executor.services = SimpleNamespace(
        prepare=lambda _job_id, _workspace: SimpleNamespace(
            network="ci-job_123", database_url="postgres://lenshub@postgres/lenshub_ci_job",
            redis_addr="redis:6379", redis_db="0", rabbitmq_url="amqp://rabbitmq/vhost",
            public_summary=lambda: "services-ready",
        ),
        cleanup=lambda _job_id, _workspace: None,
    )
    executor.log_manager = FakeLogManager()
    executor.client = FakeClient()
    return executor


def make_job(source_dir):
    return Job(
        job_id="job-123",
        repository="example/repo",
        branch="main",
        commit_sha="a" * 40,
        profile="go-check",
        timeout_seconds=30,
        lease_token="lease",
        lease_expires_at="",
        source_dir=str(source_dir),
    )


def test_setup_failure_blocks_go_checks_without_mislabeling_code_failure(tmp_path):
    (tmp_path / "go.mod").write_text("module example\ngo 1.26.4\n", encoding="utf-8")
    podman = FakePodman("go mod download")
    result = make_executor(podman)._execute_workspace(make_job(tmp_path), {"path": ".", "stack": "go"})

    assert result["passed"] is False
    assert any(step["status"] == "failed" and step["step_name"].endswith(":mod_download") for step in result["steps"])
    blocked = [step for step in result["steps"] if step["status"] == "blocked_by_setup"]
    assert {step["step_name"].rsplit(":", 1)[-1] for step in blocked} == {"govet", "gotest", "gobuild"}
    assert not any("go vet" in command or "go test" in command or "go build" in command for command, _ in podman.commands)


def test_go_build_failure_fails_workspace(tmp_path):
    (tmp_path / "go.mod").write_text("module example\ngo 1.26.4\n", encoding="utf-8")
    podman = FakePodman("go build ./...")
    result = make_executor(podman)._execute_workspace(make_job(tmp_path), {"path": ".", "stack": "go"})

    assert result["passed"] is False
    assert any(step["step_name"].endswith(":gobuild") and step["status"] == "failed" for step in result["steps"])


def test_go_cache_is_job_scoped_and_outside_source(tmp_path):
    source = tmp_path / "job-123" / "source"
    source.mkdir(parents=True)
    (source / "go.mod").write_text("module example\ngo 1.26.4\n", encoding="utf-8")
    podman = FakePodman()
    job = make_job(source)
    job.workspace = str(source.parent)

    result = make_executor(podman)._execute_workspace(job, {"path": ".", "stack": "go"})

    assert result["passed"] is True
    cache_path = podman.caches[0]["go"]
    assert cache_path == str(source.parent / "go-cache")
    assert not cache_path.startswith(str(source) + "/")
    assert (source.parent / "go-cache").stat().st_mode & 0o777 == 0o700
    assert [command for command, _ in podman.commands[:4]] == [
        "echo '[go:.:setup] preparing writable cache'; mkdir -p \"$HOME\" \"$GOPATH\" \"$GOMODCACHE\" \"$GOCACHE\" \"$(dirname \"$GOENV\")\" \"$GOTMPDIR\" \"$XDG_CACHE_HOME\" \"$XDG_CONFIG_HOME\"; test -w /ci-cache; test -w \"$GOMODCACHE\"; test -w \"$GOCACHE\"; test -w \"$GOTMPDIR\"",
        "go version",
        "go env",
        "go mod download 2>&1",
    ]


def test_job_cleanup_removes_go_cache_with_workspace(tmp_path):
    manager = WorkspaceManager(str(tmp_path / "workspaces"))
    manager.create("job-123")
    (tmp_path / "workspaces" / "job-123" / "go-cache").mkdir()

    manager.cleanup("job-123")

    assert not (tmp_path / "workspaces" / "job-123").exists()


class OnceFailingPodman(FakePodman):
    def __init__(self, failing_command: str):
        super().__init__(None)
        self._failing = failing_command
        self._already_failed = False

    def run_command(self, _image, _job_id, _source_dir, _caches, command, _timeout, network=False, **_kwargs):
        self.caches.append(_caches)
        self.commands.append((command, network))
        exit_code = 0
        if self._failing and self._failing in command and not self._already_failed:
            self._already_failed = True
            exit_code = 1
        return {"exit_code": exit_code, "stdout": "", "stderr": "", "timed_out": False}


class AlwaysFailPodman(FakePodman):
    def __init__(self, failing_command: str):
        super().__init__(None)
        self._failing = failing_command

    def run_command(self, _image, _job_id, _source_dir, _caches, command, _timeout, network=False, **_kwargs):
        self.caches.append(_caches)
        self.commands.append((command, network))
        exit_code = 1 if self._failing and self._failing in command else 0
        return {"exit_code": exit_code, "stdout": "", "stderr": "", "timed_out": False}


def test_gofmt_autofix_success_makes_workspace_pass(tmp_path):
    (tmp_path / "go.mod").write_text("module example\ngo 1.26.4\n", encoding="utf-8")
    podman = OnceFailingPodman("gofmt -l")
    result = make_executor(podman)._execute_workspace(make_job(tmp_path), {"path": ".", "stack": "go"})

    assert result["passed"] is True
    gofmt_step = next(step for step in result["steps"] if step["step_name"].endswith(":gofmt"))
    assert gofmt_step["status"] == "autofixed"
    assert gofmt_step.get("autofix", {}).get("formatted") is True
    assert any("gofmt -w" in cmd for cmd, _ in podman.commands)


def test_gofmt_autofix_failure_still_fails_workspace(tmp_path):
    (tmp_path / "go.mod").write_text("module example\ngo 1.26.4\n", encoding="utf-8")
    podman = AlwaysFailPodman("gofmt -l")
    result = make_executor(podman)._execute_workspace(make_job(tmp_path), {"path": ".", "stack": "go"})

    assert result["passed"] is False
    gofmt_step = next(step for step in result["steps"] if step["step_name"].endswith(":gofmt"))
    assert gofmt_step["status"] in ("failed", "timed_out")
    assert gofmt_step.get("autofix") is None


def test_setup_failure_blocks_gofmt_autofix(tmp_path):
    (tmp_path / "go.mod").write_text("module example\ngo 1.26.4\n", encoding="utf-8")
    podman = FakePodman("go mod download")
    result = make_executor(podman)._execute_workspace(make_job(tmp_path), {"path": ".", "stack": "go"})

    assert result["passed"] is False
    gofmt_step = next(step for step in result["steps"] if step["step_name"].endswith(":gofmt"))
    assert gofmt_step["status"] == "passed"
    assert gofmt_step.get("autofix") is None
    assert not any("gofmt -w" in command for command, _ in podman.commands)
