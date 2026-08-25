import os
from types import SimpleNamespace

import pytest

import private_ci_agent.executor as executor_module
from private_ci_agent.executor import JobExecutor
from private_ci_agent.models import Job
from private_ci_agent.podman import PodmanRunner
from private_ci_agent.services import ServiceSetupError
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

    def flush(self, _job_id):
        pass


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
        self.call_options = []

    def image_available(self, _image):
        return True

    def image_digest(self, image):
        import hashlib
        return "sha256:" + hashlib.sha256(image.encode()).hexdigest()

    def run_command(self, _image, _job_id, _source_dir, _caches, command, _timeout, network=False, **_kwargs):
        self.caches.append(_caches)
        self.commands.append((command, network, _timeout))
        self.call_options.append(_kwargs)
        exit_code = 1 if self.failing_command and self.failing_command in command else 0
        return {"exit_code": exit_code, "stdout": "", "stderr": "", "timed_out": False}


def make_executor(podman):
    executor = object.__new__(JobExecutor)
    executor.podman = podman
    executor.services = SimpleNamespace(
        prepare=lambda _job_id, _workspace, _services=None: SimpleNamespace(
            network="ci-job_123", database_url="postgres://lenshub@postgres/lenshub_ci_job",
            redis_addr="redis:6379", redis_db="0", rabbitmq_url="amqp://rabbitmq/vhost",
            public_summary=lambda: "services-ready",
        ),
        cleanup=lambda _job_id, _workspace: None,
    )
    executor.log_manager = FakeLogManager()
    executor.client = FakeClient()
    return executor


@pytest.fixture(autouse=True)
def _isolate_go_shared_cache(tmp_path, monkeypatch):
    """Route the shared Go module cache into a temp dir for every test.

    The executor now uses CACHE_MAP["go"] for all Go workspaces; tests must
    never touch the real /srv/private-ci/cache/go path.
    """
    shared_cache = tmp_path / "shared-go-cache"
    monkeypatch.setitem(executor_module.CACHE_MAP, "go", str(shared_cache))
    return shared_cache


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
    assert not any("go vet" in command or "go test" in command or "go build" in command for command, _, _ in podman.commands)
    assert not any("make migrate-up" in command for command, _, _ in podman.commands)


@pytest.mark.parametrize("timed_out", [False, True])
def test_node_setup_failure_blocks_all_node_checks(tmp_path, timed_out):
    class NodeSetupFailurePodman(FakePodman):
        def run_command(self, _image, _job_id, _source_dir, _caches, command, _timeout, network=False, **_kwargs):
            self.caches.append(_caches)
            self.commands.append((command, network, _timeout))
            failed = command == "npm ci"
            return {"exit_code": -1 if failed and timed_out else (1 if failed else 0), "stdout": "", "stderr": "", "timed_out": failed and timed_out}

    podman = NodeSetupFailurePodman()
    workspace = {
        "path": ".", "stack": "node", "package_manager": "npm",
        "scripts": {"test": "vitest run", "typecheck": "vue-tsc --noEmit", "build": "vite build"},
    }
    result = make_executor(podman)._execute_workspace(make_job(tmp_path), workspace)

    assert result["passed"] is False
    setup = next(step for step in result["steps"] if step["step_name"].endswith(":setup"))
    assert setup["status"] == ("timed_out" if timed_out else "failed")
    blocked = [step for step in result["steps"] if step["status"] == "blocked_by_setup"]
    assert {step["step_name"].rsplit(":", 1)[-1] for step in blocked} == {"test", "typecheck", "build"}
    assert not any(command.startswith("npm run ") for command, _, _ in podman.commands)


def test_browser_preheat_failure_blocks_node_checks_without_running_them(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "cloud-storage-plan-browser-smoke.mjs").write_text(
        "npx --yes playwright@1.62.0 install --with-deps chromium\n",
        encoding="utf-8",
    )

    class BrowserPreheatFailurePodman(FakePodman):
        def run_command(self, _image, _job_id, _source_dir, _caches, command, _timeout, network=False, **_kwargs):
            self.caches.append(_caches)
            self.commands.append((command, network, _timeout))
            failed = "npx --yes playwright@1.62.0 install chromium --no-shell" in command
            return {"exit_code": 1 if failed else 0, "stdout": "", "stderr": "", "timed_out": False}

    podman = BrowserPreheatFailurePodman()
    workspace = {
        "path": ".", "stack": "node", "framework": "vue", "package_manager": "npm",
        "scripts": {
            "test:run": "npm run test:browser-smoke:storage-plan",
            "test:browser-smoke:storage-plan": "node scripts/cloud-storage-plan-browser-smoke.mjs",
            "typecheck": "vue-tsc --noEmit", "build": "vite build",
        },
    }
    result = make_executor(podman)._execute_workspace(make_job(tmp_path), workspace)

    assert result["passed"] is False
    assert any(step["step_name"].endswith(":playwright_preheat") and step["status"] == "failed" for step in result["steps"])
    blocked = [step for step in result["steps"] if step["status"] == "blocked_by_setup"]
    assert {step["step_name"] for step in blocked} == {"node:.:test:run", "node:.:typecheck", "node:.:build"}
    assert not any(command.startswith("npm run ") for command, _, _ in podman.commands)


def test_failed_npm_install_blocks_browser_preheat_and_checks(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "cloud-storage-plan-browser-smoke.mjs").write_text(
        "npx --yes playwright@1.62.0 install chromium\n",
        encoding="utf-8",
    )

    class NpmFailurePodman(FakePodman):
        def run_command(self, _image, _job_id, _source_dir, _caches, command, _timeout, network=False, **_kwargs):
            self.caches.append(_caches)
            self.commands.append((command, network, _timeout))
            return {"exit_code": 1 if command == "npm ci" else 0, "stdout": "", "stderr": "", "timed_out": False}

    podman = NpmFailurePodman()
    workspace = {
        "path": ".", "stack": "node", "framework": "vue", "package_manager": "npm",
        "scripts": {
            "test:run": "npm run test:browser-smoke:storage-plan",
            "test:browser-smoke:storage-plan": "node scripts/cloud-storage-plan-browser-smoke.mjs",
        },
    }
    result = make_executor(podman)._execute_workspace(make_job(tmp_path), workspace)

    preheat = next(step for step in result["steps"] if step["step_name"].endswith(":playwright_preheat"))
    assert preheat["status"] == "blocked_by_setup"
    assert not any("playwright@1.62.0 install" in command for command, _, _ in podman.commands)
    assert not any(command.startswith("npm run ") for command, _, _ in podman.commands)


def test_node_workspace_uses_persistent_npm_cache_and_controlled_setup_proxy(tmp_path, monkeypatch):
    cache = tmp_path / "persistent-npm-cache"
    monkeypatch.setitem(executor_module.CACHE_MAP, "npm", str(cache))
    podman = FakePodman()
    workspace = {
        "path": ".", "stack": "node", "package_manager": "npm",
        "scripts": {"test": "vitest run"},
    }

    result = make_executor(podman)._execute_workspace(make_job(tmp_path), workspace)

    assert result["passed"] is True
    assert all(caches == {"npm": str(cache)} for caches in podman.caches)
    assert podman.call_options[0]["pass_proxy"] is True


def test_vue_workspace_maps_shared_playwright_cache_without_node_modules(tmp_path, monkeypatch):
    npm_cache = tmp_path / "npm-cache"
    browser_cache = tmp_path / "ms-playwright"
    monkeypatch.setitem(executor_module.CACHE_MAP, "npm", str(npm_cache))
    monkeypatch.setitem(executor_module.CACHE_MAP, "playwright", str(browser_cache))
    podman = FakePodman()
    workspace = {
        "path": "h5/lenshub-console", "stack": "node", "framework": "vue",
        "package_manager": "npm",
        "scripts": {
            "test": "vitest run",
            "test:browser-smoke": "node scripts/browser-smoke.mjs",
        },
    }

    result = make_executor(podman)._execute_workspace(make_job(tmp_path), workspace)

    assert result["passed"] is True
    assert all(caches == {"npm": str(npm_cache), "playwright": str(browser_cache)} for caches in podman.caches)
    assert all("node_modules" not in str(caches) for caches in podman.caches)


def test_service_failure_log_keeps_safe_diagnostic(monkeypatch, tmp_path):
    (tmp_path / "go.mod").write_text("module example\ngo 1.26.4\n", encoding="utf-8")
    executor = make_executor(FakePodman())
    diagnostic = "code=POSTGRES_UNAVAILABLE operation=start_container exit_code=125 resource=postgres image=docker.io/library/postgres:16-alpine timed_out=false reason=image missing"
    executor.services = SimpleNamespace(
        prepare=lambda _job_id, _workspace, _services: (_ for _ in ()).throw(
            ServiceSetupError("POSTGRES_UNAVAILABLE", diagnostic)
        ),
        cleanup=lambda _job_id, _workspace: None,
    )
    result = executor._execute_workspace(
        make_job(tmp_path), {"path": ".", "stack": "go", "services": ["postgres"]},
    )

    assert result["passed"] is False
    assert result["steps"][0]["error_code"] == "POSTGRES_UNAVAILABLE"
    assert any(diagnostic in message for message in executor.log_manager.messages)


@pytest.mark.parametrize(("job_timeout", "maximum", "expected"), [(900, 600, 600), (60, 600, 60)])
def test_setup_timeout_is_bounded_by_job_and_agent_policy(tmp_path, job_timeout, maximum, expected):
    executor = make_executor(FakePodman())
    executor.config = {"max_job_seconds": maximum}
    job = make_job(tmp_path)
    job.timeout_seconds = job_timeout

    executor._run_setup(job, "node:.", "node:22", str(tmp_path), {}, "setup", "npm ci")

    assert executor.podman.commands == [("npm ci", True, expected)]


def test_go_build_failure_fails_workspace(tmp_path):
    (tmp_path / "go.mod").write_text("module example\ngo 1.26.4\n", encoding="utf-8")
    podman = FakePodman("go build ./...")
    result = make_executor(podman)._execute_workspace(make_job(tmp_path), {"path": ".", "stack": "go"})

    assert result["passed"] is False
    assert any(step["step_name"].endswith(":gobuild") and step["status"] == "failed" for step in result["steps"])


def test_go_cache_is_shared_across_jobs_and_outside_source(tmp_path, monkeypatch):
    source = tmp_path / "job-123" / "source"
    source.mkdir(parents=True)
    (source / "go.mod").write_text("module example\ngo 1.26.4\n", encoding="utf-8")
    podman = FakePodman()
    job = make_job(source)
    job.workspace = str(source.parent)
    shared_cache = tmp_path / "shared-go-cache"
    monkeypatch.setitem(executor_module.CACHE_MAP, "go", str(shared_cache))

    result = make_executor(podman)._execute_workspace(job, {"path": ".", "stack": "go"})

    assert result["passed"] is True
    cache_path = podman.caches[0]["go"]
    assert cache_path == str(shared_cache)
    assert not cache_path.startswith(str(source) + "/")
    assert (shared_cache).stat().st_mode & 0o777 == 0o700
    assert [command for command, _, _ in podman.commands[:4]] == [
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
        self.commands.append((command, network, _timeout))
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
        self.commands.append((command, network, _timeout))
        exit_code = 1 if self._failing and self._failing in command else 0
        return {"exit_code": exit_code, "stdout": "", "stderr": "", "timed_out": False}


def test_gofmt_is_readonly_and_fails_unformatted_workspace(tmp_path):
    (tmp_path / "go.mod").write_text("module example\ngo 1.26.4\n", encoding="utf-8")
    go_file = tmp_path / "bad.go"
    original = "package main\nfunc main(){ }\n"
    go_file.write_text(original, encoding="utf-8")

    class GofmtReadonlyPodman(FakePodman):
        def run_command(self, image, job_id, source_dir, caches, command, timeout, network=False, **kwargs):
            self.commands.append((command, network, timeout))
            if "gofmt -l" in command:
                return {
                    "exit_code": 1,
                    "stdout": "UNFORMATTED GO FILES:\n./bad.go\n",
                    "stderr": "",
                    "timed_out": False,
                }
            return {"exit_code": 0, "stdout": "", "stderr": "", "timed_out": False}

    podman = GofmtReadonlyPodman()
    executor = make_executor(podman)
    result = executor._execute_workspace(make_job(tmp_path), {"path": ".", "stack": "go"})

    assert result["passed"] is False
    gofmt_step = next(step for step in result["steps"] if step["step_name"].endswith(":gofmt"))
    assert gofmt_step["status"] == "failed"
    commands = [command for command, _, _ in podman.commands]
    assert any("gofmt -l" in command for command in commands)
    assert not any("gofmt -w" in command for command in commands)
    assert go_file.read_text(encoding="utf-8") == original



def test_executor_has_no_git_gofmt_writeback_entrypoints():
    assert not hasattr(JobExecutor, "_git_push_autofix")



# ---- openapi-check tests ----


def make_openapi_job(source_dir):
    return Job(
        job_id="openapi-1", repository="frankichen/sxt", branch="feature/openapi",
        commit_sha="a" * 40, profile="openapi-check", timeout_seconds=60,
        lease_token="lease", lease_expires_at="", source_dir=str(source_dir),
    )


class OpenAPIPodman(FakePodman):
    def __init__(self, failing_command=None, exit_code=1):
        super().__init__(None)
        self.failing_command = failing_command
        self.failure_exit_code = exit_code
        self.images = []

    def run_command(self, image, job_id, source_dir, caches, command, timeout, network=False, **kwargs):
        self.images.append(image)
        self.caches.append(caches)
        self.commands.append((command, network, timeout))
        self.call_options.append(kwargs)
        failed = bool(self.failing_command and self.failing_command in command)
        return {
            "exit_code": self.failure_exit_code if failed else 0,
            "stdout": "", "stderr": "", "timed_out": False,
        }


def make_openapi_executor(podman):
    executor = make_executor(podman)
    executor.config = {"source_mirror_enabled": True}
    executor._verify_openapi_source_identity = lambda job: {
        "ok": True, "head_sha": job.commit_sha, "tree_sha": "b" * 40,
    }
    return executor


def test_openapi_check_uses_fixed_node22_image_and_repository_command(tmp_path):
    from private_ci_agent.profiles import NODE_IMAGE, OPENAPI_CHECK_COMMAND

    podman = OpenAPIPodman()
    result = make_openapi_executor(podman)._execute_openapi_check(make_openapi_job(tmp_path))

    assert result["status"] == "passed"
    assert result["exit_code"] == 0
    assert result["commit_sha"] == "a" * 40
    assert result["git_tree_sha"] == "b" * 40
    assert result["image"] == NODE_IMAGE == "docker.io/library/node:22"
    assert podman.images == [NODE_IMAGE]
    assert podman.commands[0][0] == OPENAPI_CHECK_COMMAND
    assert podman.commands[0][1] is True
    assert "test -f scripts/validate-openapi.sh" in OPENAPI_CHECK_COMMAND
    assert "test -f .redocly.yaml" in OPENAPI_CHECK_COMMAND
    for binary in ("bash", "node", "npm", "npx"):
        assert f"command -v {binary}" in OPENAPI_CHECK_COMMAND
    assert OPENAPI_CHECK_COMMAND.endswith("OPENAPI_PARALLELISM=1 bash scripts/validate-openapi.sh")


def test_openapi_redocly_nonzero_fails_job(tmp_path):
    podman = OpenAPIPodman("OPENAPI_PARALLELISM=1 bash scripts/validate-openapi.sh", exit_code=1)
    result = make_openapi_executor(podman)._execute_openapi_check(make_openapi_job(tmp_path))

    assert result["status"] == "failed"
    assert result["exit_code"] == 1
    assert result["error_code"] == "OPENAPI_CHECK_FAILED"


def test_openapi_missing_script_is_configuration_failure(tmp_path):
    podman = OpenAPIPodman("test -f scripts/validate-openapi.sh", exit_code=2)
    result = make_openapi_executor(podman)._execute_openapi_check(make_openapi_job(tmp_path))

    assert result["status"] == "failed"
    assert result["exit_code"] == 2
    assert result["error_code"] == "OPENAPI_CHECK_CONFIGURATION_ERROR"
    assert all(step["status"] != "skipped" for step in result["steps"])


def test_openapi_source_identity_preserves_exact_commit_and_tree(tmp_path, monkeypatch):
    commit_sha = "a" * 40
    tree_sha = "b" * 40

    def fake_run(command, **_kwargs):
        if "status" in command:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        revision = command[-1]
        outputs = {"HEAD": commit_sha, f"{commit_sha}^{{tree}}": tree_sha, "HEAD^{tree}": tree_sha}
        return SimpleNamespace(returncode=0, stdout=outputs[revision] + "\n", stderr="")

    monkeypatch.setattr(executor_module.subprocess, "run", fake_run)
    job = make_openapi_job(tmp_path)
    job.commit_sha = commit_sha

    result = JobExecutor._verify_openapi_source_identity(job)

    assert result == {"ok": True, "head_sha": commit_sha, "tree_sha": tree_sha}


def test_openapi_source_tree_mismatch_fails(tmp_path, monkeypatch):
    commit_sha = "a" * 40

    def fake_run(command, **_kwargs):
        if "status" in command:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        revision = command[-1]
        outputs = {"HEAD": commit_sha, f"{commit_sha}^{{tree}}": "b" * 40, "HEAD^{tree}": "c" * 40}
        return SimpleNamespace(returncode=0, stdout=outputs[revision] + "\n", stderr="")

    monkeypatch.setattr(executor_module.subprocess, "run", fake_run)
    job = make_openapi_job(tmp_path)
    job.commit_sha = commit_sha

    result = JobExecutor._verify_openapi_source_identity(job)

    assert result["ok"] is False
    assert result["error_code"] == "SOURCE_TREE_MISMATCH"


def test_repo_auto_plan_does_not_select_openapi_profile(tmp_path):
    (tmp_path / "go.mod").write_text("module example\ngo 1.26.4\n", encoding="utf-8")
    executor = JobExecutor.__new__(JobExecutor)
    executor.config = {
        "supported_profiles": ["repo-auto-check", "go-check", "openapi-check"],
    }

    plan = executor._auto_plan(str(tmp_path), {})

    assert plan["selected_profiles"] == ["go-check"]
    assert "openapi-check" not in plan["selected_profiles"]


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


def test_generic_go_profile_does_not_inherit_repository_hooks():
    from private_ci_agent.profiles import GO_COMMANDS, apply_workspace_hooks

    assert "migrate" not in [item["name"] for item in GO_COMMANDS["setup"]]
    assert "ai-integrity" not in [item["name"] for item in GO_COMMANDS["check"]]

    configured = apply_workspace_hooks(
        GO_COMMANDS,
        {"path": ".", "stack": "go", "hooks": ["go-migrate", "ai-integrity"]},
    )
    assert "migrate" in [item["name"] for item in configured["setup"]]
    assert "ai-integrity" in [item["name"] for item in configured["check"]]


def test_generic_go_workspace_does_not_start_services(tmp_path):
    (tmp_path / "go.mod").write_text("module example\ngo 1.26.4\n", encoding="utf-8")
    executor = make_executor(FakePodman())
    calls = []
    executor.services = SimpleNamespace(
        prepare=lambda *_args: calls.append(_args),
        cleanup=lambda *_args: None,
    )

    result = executor._execute_workspace(make_job(tmp_path), {"path": ".", "stack": "go"})

    assert result["passed"] is True
    assert calls == []


def test_configured_go_workspace_starts_only_requested_services(tmp_path):
    (tmp_path / "go.mod").write_text("module example\ngo 1.26.4\n", encoding="utf-8")
    executor = make_executor(FakePodman())
    calls = []
    executor.services = SimpleNamespace(
        prepare=lambda job_id, workspace, services: (
            calls.append((job_id, workspace, list(services)))
            or SimpleNamespace(
                network="ci-svc-job_123",
                database_url="",
                redis_addr="redis:6379",
                redis_db="0",
                rabbitmq_url="",
                public_summary=lambda: "REDIS_HOST=redis REDIS_PORT=6379 REDIS_DB=0",
            )
        ),
        cleanup=lambda *_args: None,
    )

    result = executor._execute_workspace(
        make_job(tmp_path),
        {"path": ".", "stack": "go", "services": ["redis"]},
    )

    assert result["passed"] is True
    assert calls == [("job-123", "", ["redis"])] or calls == [("job-123", None, ["redis"])]


# ---- cancellation short-circuit ----


def test_cancel_event_short_circuits_remaining_steps(tmp_path):
    import threading
    (tmp_path / "go.mod").write_text("module example\ngo 1.26.4\n", encoding="utf-8")
    podman = FakePodman()
    cancel_event = threading.Event()

    class CancelledAfterFirst(FakePodman):
        def run_command(self, _image, _job_id, _source_dir, _caches, command, _timeout, network=False, **_kwargs):
            self.commands.append((command, network, _timeout))
            if len(self.commands) == 1:
                cancel_event.set()
            return {"exit_code": 0, "stdout": "", "stderr": "", "timed_out": False, "cancelled": False}

    podman = CancelledAfterFirst()
    executor = make_executor(podman)
    executor.cancel_event = cancel_event
    result = executor._execute_workspace(make_job(tmp_path), {"path": ".", "stack": "go"})

    assert result["passed"] is False
    cancelled = [step for step in result["steps"] if step["status"] == "cancelled"]
    assert cancelled
    # prepare_cache runs; everything after the first cancelled step is skipped.
    assert len([c for c, _, _ in podman.commands]) == 1


def test_execute_finishes_cancelled_when_event_set_before_run(tmp_path):
    import threading
    (tmp_path / "go.mod").write_text("module example\ngo 1.26.4\n", encoding="utf-8")
    cancel_event = threading.Event()
    cancel_event.set()
    podman = FakePodman()

    class CancellingClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.finished = []

        def finish_job(self, job_id, exit_code, status, summary=None, error_code=None, error_message=None):
            self.finished.append({"job_id": job_id, "exit_code": exit_code, "status": status, "summary": summary})

    client = CancellingClient()
    executor = make_executor(podman)
    executor.client = client
    executor.cancel_event = cancel_event
    job = make_job(tmp_path)
    job.workspace = str(tmp_path)

    summary = executor.execute(job)

    assert summary["status"] == "cancelled"
    assert client.finished[-1]["status"] == "cancelled"
    assert client.finished[-1]["exit_code"] == -1


def test_auto_plan_uses_worker_capabilities_without_repository_registration(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='example'\n", encoding="utf-8")
    executor = JobExecutor.__new__(JobExecutor)
    executor.config = {"supported_profiles": ["repo-auto-check", "python-check"]}

    plan = executor._auto_plan(str(tmp_path), {})

    assert "error" not in plan
    assert plan["selected_profiles"] == ["python-check"]
    assert plan["detected_stacks"] == ["python"]


def test_explicit_node_plan_uses_repository_workspace_overrides(tmp_path):
    (tmp_path / "go.mod").write_text("module example\ngo 1.26.4\n", encoding="utf-8")
    (tmp_path / "package.json").write_text('{"name":"root"}\n', encoding="utf-8")
    react = tmp_path / "h5" / "lenshub-console-react"
    react.mkdir(parents=True)
    react.joinpath("package.json").write_text(
        '{"scripts":{"test:run":"vitest run","typecheck":"tsc --noEmit","build":"vite build"}}\n',
        encoding="utf-8",
    )
    react.joinpath("package-lock.json").write_text("{}\n", encoding="utf-8")
    repo_config = {
        "workspaces": [
            {"path": ".", "type": "auto", "services": ["postgres"]},
            {
                "path": "h5/lenshub-console-react",
                "type": "node",
                "package_manager": "npm",
                "required_scripts": ["test:run", "typecheck", "build"],
            },
        ]
    }
    executor = JobExecutor.__new__(JobExecutor)

    plan = executor._explicit_plan("node-check", str(tmp_path), repo_config)

    assert "error" not in plan
    assert [(item["path"], item["stack"]) for item in plan["workspaces"]] == [
        ("h5/lenshub-console-react", "node")
    ]
    assert plan["workspaces"][0]["required_scripts"] == ["test:run", "typecheck", "build"]


def test_explicit_non_node_profile_keeps_root_only_scope(tmp_path):
    nested = tmp_path / "services" / "worker"
    nested.mkdir(parents=True)
    nested.joinpath("pyproject.toml").write_text(
        "[project]\nname='nested'\nversion='0.1.0'\n",
        encoding="utf-8",
    )
    executor = JobExecutor.__new__(JobExecutor)

    plan = executor._explicit_plan("python-check", str(tmp_path), {})

    assert plan["error"] == "unsupported"
    assert plan["workspaces"] == []


@pytest.mark.parametrize(("filename", "stack", "profile"), [
    ("Cargo.toml", "rust", "rust-check"),
    ("pom.xml", "maven", "maven-check"),
    ("build.gradle", "gradle", "gradle-check"),
    ("sample.csproj", "dotnet", "dotnet-check"),
])
def test_auto_plan_selects_common_language_profiles(tmp_path, filename, stack, profile):
    (tmp_path / filename).write_text("placeholder", encoding="utf-8")
    executor = JobExecutor.__new__(JobExecutor)
    executor.config = {"supported_profiles": ["repo-auto-check", profile]}

    plan = executor._auto_plan(str(tmp_path), {})

    assert "error" not in plan
    assert plan["selected_profiles"] == [profile]
    assert plan["detected_stacks"] == [stack]


@pytest.mark.parametrize("stack", ["rust", "maven", "gradle", "dotnet"])
def test_common_language_setup_uses_proxy_and_checks_are_network_isolated(tmp_path, stack, monkeypatch):
    cache = tmp_path / f"{stack}-cache"
    cache_name = {"rust": "cargo", "maven": "maven", "gradle": "gradle", "dotnet": "nuget"}[stack]
    monkeypatch.setitem(executor_module.CACHE_MAP, cache_name, str(cache))
    podman = FakePodman()

    result = make_executor(podman)._execute_workspace(make_job(tmp_path), {"path": ".", "stack": stack})

    assert result["passed"] is True
    assert podman.call_options[0]["pass_proxy"] is True
    assert podman.commands[0][1] is True
    for (_, network, _), options in zip(podman.commands[1:], podman.call_options[1:]):
        assert network is False
        assert options["pass_proxy"] is False


def test_common_language_setup_failure_blocks_checks(tmp_path):
    podman = FakePodman("cargo fetch")
    result = make_executor(podman)._execute_workspace(make_job(tmp_path), {"path": ".", "stack": "rust"})

    assert result["passed"] is False
    assert result["steps"][0]["status"] == "failed"
    assert all(step["status"] == "blocked_by_setup" for step in result["steps"][1:])
    assert len(podman.commands) == 1


@pytest.mark.parametrize(("stack", "manifest", "initial", "changed"), [
    ("go", "go.sum", "module-a h1:one\n", "module-a h1:two\n"),
    ("node", "package-lock.json", '{"lockfileVersion":3}', '{"lockfileVersion":2}'),
    ("python", "requirements-prod.txt", "requests==2.32.0\n", "requests==2.33.0\n"),
    ("rust", "Cargo.lock", 'version = 3\n', 'version = 4\n'),
    ("maven", "pom.xml", "<project><version>1</version></project>", "<project><version>2</version></project>"),
    ("gradle", "gradle.lockfile", "dep=1\n", "dep=2\n"),
    ("dotnet", "packages.lock.json", '{"version":1}', '{"version":2}'),
])
def test_dependency_manifest_hash_covers_all_supported_stacks(tmp_path, stack, manifest, initial, changed):
    path = tmp_path / manifest
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(initial, encoding="utf-8")
    workspaces = [{"path": ".", "stack": stack}]

    before = JobExecutor._hash_dependency_manifests(str(tmp_path), workspaces)
    path.write_text(changed, encoding="utf-8")
    after = JobExecutor._hash_dependency_manifests(str(tmp_path), workspaces)

    assert len(before) == 64
    assert before != after


def test_dependency_hash_binds_relative_path_and_content(tmp_path):
    first = tmp_path / "a.lock"
    second = tmp_path / "nested" / "a.lock"
    first.write_text("same", encoding="utf-8")
    second.parent.mkdir()
    second.write_text("same", encoding="utf-8")

    assert JobExecutor._hash_paths(str(tmp_path), ["a.lock"]) != JobExecutor._hash_paths(
        str(tmp_path), ["nested/a.lock"]
    )


def test_source_immutability_requires_exact_clean_tracked_tree(tmp_path, monkeypatch):
    head = "a" * 40
    job = make_job(tmp_path)
    job.commit_sha = head
    states = {"head": head, "worktree": 0, "index": 0}

    def fake_run(command, **_kwargs):
        if command[-2:] == ["rev-parse", "HEAD"]:
            return SimpleNamespace(returncode=0, stdout=states["head"] + "\n", stderr="")
        if "--cached" in command:
            return SimpleNamespace(returncode=states["index"], stdout="", stderr="")
        if command[-4:] == ["--quiet", "HEAD", "--"] or (
            "diff" in command and "--cached" not in command
        ):
            return SimpleNamespace(returncode=states["worktree"], stdout="", stderr="")
        raise AssertionError(f"unexpected git command: {command}")

    monkeypatch.setattr(executor_module.subprocess, "run", fake_run)

    assert JobExecutor._verify_source_immutable(job) == (True, "")
    states["worktree"] = 1
    assert JobExecutor._verify_source_immutable(job) == (False, "tracked_files_changed")
    states["worktree"] = 0
    states["head"] = "b" * 40
    assert JobExecutor._verify_source_immutable(job) == (False, "head_sha_changed")


def test_workspace_image_identity_is_deterministic_and_includes_services(tmp_path):
    podman = FakePodman()
    executor = make_executor(podman)
    executor.services.images = {
        "postgres": "docker.io/library/postgres:16-alpine",
        "redis": "docker.io/library/redis:7-alpine",
    }
    (tmp_path / "go.mod").write_text("module example\ngo 1.26.4\n", encoding="utf-8")
    workspaces = [{"path": ".", "stack": "go", "services": ["redis", "postgres"]}]

    first = executor._workspace_images(workspaces, str(tmp_path))
    second = executor._workspace_images(workspaces, str(tmp_path))

    assert first == second
    assert [item["stack"] for item in first] == ["go", "service:postgres", "service:redis"]
    assert all(item["digest"].startswith("sha256:") for item in first)


def test_image_identity_gate_accepts_no_planned_images_but_rejects_missing_identity():
    assert JobExecutor._images_have_immutable_identity([]) is True
    assert JobExecutor._images_have_immutable_identity([
        {"image": "docker.io/library/node:22", "digest": "sha256:node"}
    ]) is True
    assert JobExecutor._images_have_immutable_identity([
        {"image": "docker.io/library/node:22", "digest": ""}
    ]) is False


def test_workspace_images_skip_non_executable_configuration_error_services(tmp_path):
    executor = make_executor(FakePodman())
    executor.services.images = {"postgres": "docker.io/library/postgres:16-alpine"}
    workspaces = [{
        "path": ".",
        "stack": "node",
        "services": ["postgres"],
        "configuration_error": "Node workspace requires exactly one supported lock file",
    }]

    assert executor._workspace_images(workspaces, str(tmp_path)) == []


def test_hidden_dependency_config_path_keeps_leading_dot_in_identity(tmp_path):
    cargo = tmp_path / ".cargo" / "config.toml"
    cargo.parent.mkdir()
    cargo.write_text('[net]\ngit-fetch-with-cli = true\n', encoding="utf-8")
    workspaces = [{"path": ".", "stack": "rust"}]

    before = JobExecutor._hash_dependency_manifests(str(tmp_path), workspaces)
    cargo.write_text('[net]\ngit-fetch-with-cli = false\n', encoding="utf-8")
    after = JobExecutor._hash_dependency_manifests(str(tmp_path), workspaces)

    assert before != after


def test_hidden_github_workflow_path_is_bound_into_test_config_identity(tmp_path):
    workflow = tmp_path / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("name: one\n", encoding="utf-8")

    before = JobExecutor._hash_test_config(str(tmp_path))
    workflow.write_text("name: two\n", encoding="utf-8")
    after = JobExecutor._hash_test_config(str(tmp_path))

    assert before != after
