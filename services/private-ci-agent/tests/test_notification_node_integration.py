"""SXT dedicated Go+Node integration fail-closed unit checks."""
from types import SimpleNamespace
from private_ci_agent.executor import JobExecutor
from private_ci_agent.notification_node_integration import (
    GO_ONLY_TEST_COMMAND, NODE_INTEGRATION_COMMAND, NODE_INTEGRATION_IMAGE,
    SIMULATOR_TEST, notification_node_integration_required,
    sxt_go_only_image_allowed)

def test_exact_sxt_full_ci_only():
    assert notification_node_integration_required("frankichen/sxt", "repo-auto-check")
    assert not notification_node_integration_required("frankichen/sxt", "go-check")
    assert not notification_node_integration_required("other/repo", "repo-auto-check")

def test_one_test_delegated_not_removed(tmp_path):
    (tmp_path / "go.mod").write_text("module example\ngo 1.26.4\n")
    executor = object.__new__(JobExecutor)
    job = SimpleNamespace(repository="frankichen/sxt", profile="repo-auto-check")
    checks = executor._workspace_commands({"path": ".", "stack": "go"}, str(tmp_path), job=job)["check"]
    assert next(c["command"] for c in checks if c["name"] == "gotest") == GO_ONLY_TEST_COMMAND
    job.profile = "go-check"
    checks = executor._workspace_commands({"path": ".", "stack": "go"}, str(tmp_path), job=job)["check"]
    assert next(c["command"] for c in checks if c["name"] == "gotest") != GO_ONLY_TEST_COMMAND
    assert f"--- PASS: {SIMULATOR_TEST}" in NODE_INTEGRATION_COMMAND
    assert f"--- SKIP: {SIMULATOR_TEST}" in NODE_INTEGRATION_COMMAND

class Podman:
    def __init__(self, available=True):
        self.available, self.lookups, self.runs = available, [], []
    def image_available(self, image, allow_pull=True):
        self.lookups.append((image, allow_pull))
        return self.available
    def image_digest(self, image):
        return "sha256:unit-test"
    def run_command(self, image, job_id, src, cache, command, timeout, **kwargs):
        self.runs.append((image, command, kwargs))
        return {"exit_code": 0, "stdout": "", "stderr": "", "timed_out": False}

def test_stage_fails_closed_and_uses_same_real_service(tmp_path):
    worker = Podman()
    e = object.__new__(JobExecutor)
    e.podman, e.cache_map, e.cancel_event = worker, {"go":str(tmp_path)}, None
    e.client = SimpleNamespace(start_step=lambda *args: None)
    e.log_manager = SimpleNamespace(upload=lambda *a: None, flush=lambda *a: None, get_total=lambda *a: 0)
    job = SimpleNamespace(job_id="unit", repository="frankichen/sxt", profile="repo-auto-check",
                          source_dir=str(tmp_path), timeout_seconds=30, commit_sha="a"*40)
    assert e._run_notification_node_integration(job, None)["exit_code"] != 0
    worker.available = False
    assert e._run_notification_node_integration(job, SimpleNamespace(network="net"))["exit_code"] != 0
    assert worker.lookups[-1] == (NODE_INTEGRATION_IMAGE, False)
    worker.available = True
    service = SimpleNamespace(network="net", database_url="postgres://fake@postgres/ci",
                              redis_addr="", redis_db="", rabbitmq_url="")
    assert e._run_notification_node_integration(job, service)["status"] == "passed"
    assert worker.runs[-1][0] == NODE_INTEGRATION_IMAGE
    assert worker.runs[-1][2]["network_name"] == "net"
    assert worker.runs[-1][2]["env"]["DATABASE_URL"] == service.database_url
    assert worker.runs[-1][2]["env"]["CI_PROFILE"] == "repo-auto-check"
    assert worker.runs[-1][2]["env"]["SXT_NOTIFICATION_NODE_CI_ENABLED"] == "1"

def test_profile_is_explicit_and_only_full_ci_enables_node_gate(tmp_path):
    worker = Podman()
    e = object.__new__(JobExecutor)
    e.podman, e.cache_map, e.cancel_event = worker, {"go": str(tmp_path)}, None
    e.client = SimpleNamespace(start_step=lambda *args: None)
    e.log_manager = SimpleNamespace(upload=lambda *args: None)
    for profile in ("go-check", "repo-fast-check", "repo-auto-check"):
        job = SimpleNamespace(job_id="unit", repository="frankichen/sxt",
                              profile=profile, commit_sha="a"*40, timeout_seconds=30)
        result = e._run_check(job, "go:.", "golang:1.26.4", str(tmp_path),
                              {"go":str(tmp_path)}, "gotest", "true")
        assert result["status"] == "passed"
        env = worker.runs[-1][2]["env"]
        assert env["CI_PROFILE"] == profile
        assert (env.get("SXT_NOTIFICATION_NODE_CI_ENABLED") == "1") == (profile == "repo-auto-check")

def test_sxt_go_only_image_contract_blocks_mixed_worker_configuration(tmp_path, monkeypatch):
    (tmp_path / "go.mod").write_text("module example\ngo 1.26.4\n")
    executor = object.__new__(JobExecutor)
    job = SimpleNamespace(repository="frankichen/sxt", profile="repo-auto-check")
    workspace = {"path": ".", "stack": "go"}
    original = executor._workspace_commands(workspace, str(tmp_path), job=job)
    assert not original.get("error")
    assert sxt_go_only_image_allowed(job.repository, "go", original["image"])
    assert original["image"] != NODE_INTEGRATION_IMAGE
    assert "node" not in GO_ONLY_TEST_COMMAND.lower()
    for invalid in (NODE_INTEGRATION_IMAGE, "docker.io/library/golang:1.26.4-node",
                    "localhost/custom-go-node:latest", "node:22"):
        assert not sxt_go_only_image_allowed(job.repository, "go", invalid)
    assert sxt_go_only_image_allowed("another/repo", "go", NODE_INTEGRATION_IMAGE)
    assert sxt_go_only_image_allowed(job.repository, "node", NODE_INTEGRATION_IMAGE)
    monkeypatch.setattr(
        "private_ci_agent.executor.go_commands_for_workspace",
        lambda _source: {**original, "image": NODE_INTEGRATION_IMAGE},
    )
    rejected = executor._workspace_commands(workspace, str(tmp_path), job=job)
    assert rejected["error"] == "configuration_error"
    assert rejected["message"] == "CI_GO_ONLY_IMAGE_CONTRACT_VIOLATION"


def test_podman_runtime_forwards_controlled_ci_profile_and_node_marker(tmp_path, monkeypatch):
    """Regression: Executor markers must survive PodmanRunner's env allowlist."""
    from private_ci_agent.podman import PodmanRunner
    import inspect

    source = inspect.getsource(PodmanRunner.run_command)
    assert '"CI_PROFILE"' in source
    assert '"SXT_NOTIFICATION_NODE_CI_ENABLED"' in source
    assert '"CI_COMMIT_SHA"' in source
    assert '"CI_REPOSITORY_ROOT"' in source
