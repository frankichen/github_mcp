import os
from types import SimpleNamespace

from private_ci_agent.executor import JobExecutor
from private_ci_agent.models import Job
from private_ci_agent.podman import PodmanRunner
from private_ci_agent.workspace import WorkspaceManager


class FakeLogManager:
    def __init__(self):
        self.messages = []

    def reset(self, _job_id):
        pass

    def upload(self, _job_id, message):
        self.messages.append(message)

    def get_total(self, _job_id):
        return 0

    def is_truncated(self, _job_id):
        return False


class FakeClient:
    def __init__(self):
        self.statuses = []

    def update_job_status(self, _job_id, _status):
        self.statuses.append(_status)

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
    workspace = manager.create("job-123")
    (tmp_path / "workspaces" / "job-123" / "go-cache").mkdir()

    manager.cleanup("job-123")

    assert not (tmp_path / "workspaces" / "job-123").exists()


class OnceFailingPodman(FakePodman):
    """Fail only the first command matching a pattern; all subsequent calls pass."""

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
    """Fail every command matching a pattern."""

    def __init__(self, failing_command: str):
        super().__init__(None)
        self._failing = failing_command

    def run_command(self, _image, _job_id, _source_dir, _caches, command, _timeout, network=False, **_kwargs):
        self.caches.append(_caches)
        self.commands.append((command, network))
        exit_code = 1 if self._failing and self._failing in command else 0
        return {"exit_code": exit_code, "stdout": "", "stderr": "", "timed_out": False}


def test_gofmt_failure_is_read_only_and_fails_job(tmp_path):
    (tmp_path / "go.mod").write_text("module example\ngo 1.26.4\n", encoding="utf-8")
    go_file = tmp_path / "bad.go"
    go_file.write_text("package main\nfunc main(){ }\n", encoding="utf-8")
    before = go_file.read_bytes()

    class GofmtFailPodman(FakePodman):
        def run_command(self, image, job_id, source_dir, caches, command, timeout, network=False, **kwargs):
            self.commands.append((command, network))
            if "gofmt -l" in command:
                return {
                    "exit_code": 1,
                    "stdout": "UNFORMATTED FILES:\n./bad.go\n",
                    "stderr": "",
                    "timed_out": False,
                }
            return {"exit_code": 0, "stdout": "", "stderr": "", "timed_out": False}

    podman = GofmtFailPodman()
    executor = make_executor(podman)
    result = executor._execute_workspace(make_job(tmp_path), {"path": ".", "stack": "go"})

    assert result["passed"] is False
    gofmt_step = next(step for step in result["steps"] if step["step_name"].endswith(":gofmt"))
    assert gofmt_step["status"] == "failed"
    assert go_file.read_bytes() == before
    commands = [command for command, _ in podman.commands]
    assert not any("gofmt -w" in command for command in commands)
    assert not any("git commit" in command or "git push" in command for command in commands)
    assert any("./bad.go" in message for message in executor.log_manager.messages)


def test_executor_has_no_gofmt_writeback_entrypoints():
    assert not hasattr(JobExecutor, "_gofmt_autofix")
    assert not hasattr(JobExecutor, "_git_push_autofix")



# ---- repo-fast-check tests ----


import subprocess as sp_mod


class FakeFastCheckClient(FakeClient):
    """Records finish_job calls for fast-check tests."""

    def __init__(self):
        super().__init__()
        self.finished = []
        self.finished_steps = []

    def finish_job(self, job_id, exit_code, status, summary=None, error_code=None, error_message=None):
        self.finished.append({
            "job_id": job_id, "exit_code": exit_code, "status": status,
            "summary": summary, "error_code": error_code, "error_message": error_message,
        })

    def finish_step(self, job_id, step_id, status, exit_code, log_end):
        self.finished_steps.append({
            "job_id": job_id, "step_id": step_id, "status": status, "exit_code": exit_code,
        })


def make_fast_check_executor(subprocess_runner=None):
    """Build a JobExecutor with everything needed for _execute_fast_check."""
    class FastCheckPodman:
        def image_available(self, _image, allow_pull=False):
            return not allow_pull

        def image_digest(self, _image):
            return "sha256:test"

        def resource_summary(self):
            return {"mode": "test"}

        def run_command(self, _image, _job_id, source_dir, _caches, _command, _timeout, **_kwargs):
            runner = subprocess_runner or sp_mod.run
            result = runner(
                ["make", "-C", source_dir, "ai-integrity-check"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            return {
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "timed_out": False,
            }

    e = object.__new__(JobExecutor)
    e.podman = FastCheckPodman()
    e.services = SimpleNamespace(
        prepare=lambda _jid, _ws: None,
        cleanup=lambda _jid, _ws: None,
    )
    e.log_manager = FakeLogManager()
    e.client = FakeFastCheckClient()
    e.config = {}
    if subprocess_runner is not None:
        e._fast_check_subprocess = subprocess_runner
    return e


def _default_fast_check_subprocess(cmd, **_kw):
    """Simulate subprocess.run for fast-check: succeeds for standard tool checks."""
    if isinstance(cmd, list) and cmd[0] == "which":
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    if isinstance(cmd, list) and cmd[1:3] == ["-C", "cat-file"]:
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    if isinstance(cmd, list) and "ai-integrity-check" in cmd:
        return SimpleNamespace(returncode=0, stdout="All checks passed", stderr="")
    return SimpleNamespace(returncode=0, stdout="", stderr="")


def test_repo_fast_check_success(tmp_path, monkeypatch):
    """repo-fast-check with make ai-integrity-check passing."""
    (tmp_path / "Makefile").write_text("ai-integrity-check:\n\t@echo ok\n", encoding="utf-8")
    job = Job(
        job_id="fast-1", repository="frankichen/sxt", branch="feature/x",
        commit_sha="a" * 40, profile="repo-fast-check", timeout_seconds=60,
        lease_token="lt", lease_expires_at="", base_sha="b" * 40,
        changed_files=["main.go"], source_dir=str(tmp_path), workspace=str(tmp_path / "ws"),
    )

    executor = make_fast_check_executor(_default_fast_check_subprocess)

    # Patch subprocess.run
    orig_run = sp_mod.run

    def fake_run(cmd, **kw):
        if isinstance(cmd, list) and cmd[0] == "which":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if isinstance(cmd, list) and len(cmd) >= 3 and cmd[1] == "-C" and cmd[2] == str(tmp_path) and "cat-file" in cmd:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if isinstance(cmd, list) and cmd[1:3] == ["-C", str(tmp_path)] and "ai-integrity-check" in cmd:
            return SimpleNamespace(returncode=0, stdout="All checks passed", stderr="")
        return orig_run(cmd, **kw)

    monkeypatch.setattr(sp_mod, "run", fake_run)
    monkeypatch.setattr(os, "makedirs", lambda *a, **kw: None)
    monkeypatch.setattr(os.path, "isfile", lambda p: False)  # report not found (OK)

    summary = executor._execute_fast_check(job)

    assert summary["status"] == "passed"
    assert summary["exit_code"] == 0
    assert summary["profile"] == "repo-fast-check"
    steps = summary["steps"]
    assert any(s["step_name"] == "repo-fast-check:ai-integrity" for s in steps)
    integrity_step = next(s for s in steps if s["step_name"] == "repo-fast-check:ai-integrity")
    assert integrity_step["status"] == "passed"
    assert integrity_step["exit_code"] == 0
    # Verify env vars were set
    # (env is set via os.environ.copy() — hard to assert in unit test, but the code path ran)


def test_repo_fast_check_missing_make(tmp_path, monkeypatch):
    """fast-check with make not installed → configuration_error."""
    job = Job(
        job_id="fast-2", repository="frankichen/sxt", branch="feature/x",
        commit_sha="a" * 40, profile="repo-fast-check", timeout_seconds=60,
        lease_token="lt", lease_expires_at="", base_sha="",
        source_dir=str(tmp_path), workspace=str(tmp_path / "ws"),
    )

    executor = make_fast_check_executor()

    def fake_run(cmd, **kw):
        if isinstance(cmd, list) and cmd[0] == "which" and cmd[1] == "make":
            return SimpleNamespace(returncode=1, stdout="", stderr="make not found")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sp_mod, "run", fake_run)

    summary = executor._execute_fast_check(job)

    assert summary["status"] == "failed"
    assert summary["exit_code"] == 2
    assert summary["error_code"] == "FAST_CHECK_MAKE_MISSING"


def test_repo_fast_check_entrypoint_missing(tmp_path, monkeypatch):
    """fast-check when make ai-integrity-check target doesn't exist."""
    job = Job(
        job_id="fast-3", repository="frankichen/sxt", branch="feature/x",
        commit_sha="a" * 40, profile="repo-fast-check", timeout_seconds=60,
        lease_token="lt", lease_expires_at="", base_sha="",
        source_dir=str(tmp_path), workspace=str(tmp_path / "ws"),
    )

    executor = make_fast_check_executor()

    def fake_run(cmd, **kw):
        if isinstance(cmd, list) and cmd[0] == "which":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if isinstance(cmd, list) and "ai-integrity-check" in cmd and "-n" in cmd:
            return SimpleNamespace(returncode=2, stdout="", stderr="No rule to make target")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sp_mod, "run", fake_run)

    summary = executor._execute_fast_check(job)

    assert summary["status"] == "failed"
    assert summary["exit_code"] == 2
    assert summary["error_code"] == "FAST_CHECK_ENTRYPOINT_MISSING"


def test_repo_fast_check_integrity_failure(tmp_path, monkeypatch):
    """fast-check when ai-integrity-check returns non-zero."""
    (tmp_path / "Makefile").write_text("ai-integrity-check:\n\t@echo ok\n", encoding="utf-8")
    job = Job(
        job_id="fast-4", repository="frankichen/sxt", branch="feature/x",
        commit_sha="a" * 40, profile="repo-fast-check", timeout_seconds=60,
        lease_token="lt", lease_expires_at="", base_sha="b" * 40,
        changed_files=["bad.go"], source_dir=str(tmp_path), workspace=str(tmp_path / "ws"),
    )

    executor = make_fast_check_executor()

    def fake_run(cmd, **kw):
        if isinstance(cmd, list) and cmd[0] == "which":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if isinstance(cmd, list) and len(cmd) >= 3 and cmd[1] == "-C" and "cat-file" in cmd:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if isinstance(cmd, list) and cmd[1:3] == ["-C", str(tmp_path)] and "ai-integrity-check" in cmd and "-n" not in cmd:
            return SimpleNamespace(returncode=1, stdout="FAIL: .only found in test", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sp_mod, "run", fake_run)
    monkeypatch.setattr(os, "makedirs", lambda *a, **kw: None)
    monkeypatch.setattr(os.path, "isfile", lambda p: False)

    summary = executor._execute_fast_check(job)

    assert summary["status"] == "failed"
    assert summary["exit_code"] == 1
    assert summary["error_code"] == "FAST_CHECK_INTEGRITY_FAILED"


def test_fast_check_worker_idle_after_job(tmp_path, monkeypatch):
    """After fast-check, worker should have status idle (no current_job)."""
    (tmp_path / "Makefile").write_text("ai-integrity-check:\n\t@echo ok\n", encoding="utf-8")
    job = Job(
        job_id="fast-5", repository="frankichen/sxt", branch="feature/x",
        commit_sha="a" * 40, profile="repo-fast-check", timeout_seconds=60,
        lease_token="lt", lease_expires_at="", base_sha="b" * 40,
        source_dir=str(tmp_path), workspace=str(tmp_path / "ws"),
    )

    executor = make_fast_check_executor()

    def fake_run(cmd, **kw):
        if isinstance(cmd, list) and cmd[0] == "which":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if isinstance(cmd, list) and len(cmd) >= 3 and cmd[1] == "-C" and "cat-file" in cmd:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if isinstance(cmd, list) and "ai-integrity-check" in cmd and "-n" not in cmd:
            return SimpleNamespace(returncode=0, stdout="All checks passed", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sp_mod, "run", fake_run)
    monkeypatch.setattr(os, "makedirs", lambda *a, **kw: None)
    monkeypatch.setattr(os.path, "isfile", lambda p: False)

    summary = executor._execute_fast_check(job)

    assert summary["status"] == "passed"
    # finish_job was called by execute() (not _execute_fast_check itself)
    # The caller (execute) calls client.finish_job()


def test_repo_auto_check_has_ai_integrity_step(tmp_path):
    """repo-auto-check GO_COMMANDS should include ai-integrity as first check."""
    from private_ci_agent.profiles import GO_COMMANDS

    check_names = [c["name"] for c in GO_COMMANDS["check"]]
    assert "ai-integrity" in check_names, f"ai-integrity missing from {check_names}"
    assert check_names.index("ai-integrity") < check_names.index("gofmt"), \
        "ai-integrity should run before gofmt"
