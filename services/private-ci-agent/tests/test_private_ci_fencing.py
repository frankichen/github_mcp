from private_ci_agent.controller_client import ControllerClient
from private_ci_agent.executor import JobExecutor


def test_controller_client_sends_lease_token_on_job_writes(monkeypatch):
    client = ControllerClient("http://controller", "wsl-ci-test", "worker-token")
    calls = []

    def fake_request(method, path, body=None, timeout=30):
        calls.append((method, path, body))
        if path == "/internal/ci/jobs/lease":
            return {"job_id": "job-1", "lease_token": "lease-1"}
        if path.endswith("/steps") and body.get("action") == "start":
            return {"step_id": 7}
        return {}

    monkeypatch.setattr(client, "_request", fake_request)
    assert client.lease_job()["job_id"] == "job-1"
    assert client.upload_log("job-1", "hello") is True
    assert client.start_step("job-1", "node:build") == 7

    writes = [body for _method, path, body in calls if path != "/internal/ci/jobs/lease"]
    assert writes
    assert all(body["lease_token"] == "lease-1" for body in writes)


def test_node_check_respects_mixed_repo_workspace_config(tmp_path):
    (tmp_path / "go.mod").write_text("module example\ngo 1.26.4\n", encoding="utf-8")
    (tmp_path / "package.json").write_text("{\"name\":\"orchestrator\"}", encoding="utf-8")

    admin = tmp_path / "h5" / "lenshub-admin"
    admin.mkdir(parents=True)
    (admin / "package.json").write_text(
        "{\"name\":\"admin\",\"scripts\":{\"build\":\"vite build\"}}",
        encoding="utf-8",
    )
    (admin / "package-lock.json").write_text("{}", encoding="utf-8")

    repo_config = {"workspaces": [
        {"path": ".", "type": "auto"},
        {"path": "h5/lenshub-admin", "type": "node", "package_manager": "npm", "required_scripts": ["build"]},
    ]}
    executor = object.__new__(JobExecutor)
    plan = executor._explicit_plan("node-check", str(tmp_path), repo_config)

    node_paths = [item["path"] for item in plan["workspaces"]]
    assert "." not in node_paths
    assert "h5/lenshub-admin" in node_paths
