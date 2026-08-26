from pathlib import Path
from types import SimpleNamespace

import scripts.infrastructure_deploy_executor as executor

SHA = "a" * 40


def _row():
    return {
        "deployment_id": "infra_dep_1",
        "repository": executor.EXPECTED_REPOSITORY,
        "environment": executor.EXPECTED_ENVIRONMENT,
        "requested_scope": executor.EXPECTED_SCOPE,
        "commit_sha": SHA,
        "tree_sha": "b" * 40,
        "expected_current_build_sha": "c" * 40,
    }


def test_prepare_workspace_requires_exact_authoritative_main(monkeypatch, tmp_path):
    mirror = tmp_path / "frankichen-github-mcp.git"
    mirror.mkdir()
    workspace_root = tmp_path / "workspaces"
    calls = []

    def fake_git(*args, cwd=None):
        calls.append((args, cwd))
        if args[:3] == ("remote", "get-url", "origin"):
            return executor.AUTHORITATIVE_REPOSITORY_URL
        if args[:2] in {("rev-parse", "refs/heads/main"), ("rev-parse", "refs/remotes/origin/main"), ("rev-parse", "HEAD")}:
            return SHA
        if args[:2] == ("branch", "--show-current"):
            return "main"
        if args[:2] == ("status", "--porcelain"):
            return ""
        return ""

    monkeypatch.setattr(executor, "DEPLOY_MIRROR", str(mirror))
    monkeypatch.setattr(executor, "DEPLOY_WORKSPACES", str(workspace_root))
    monkeypatch.setattr(executor, "_git", fake_git)
    monkeypatch.setattr(executor.os.path, "exists", lambda path: False)
    monkeypatch.setattr(executor.os.path, "isfile", lambda path: path.endswith(executor.DEPLOY_SCRIPT))

    workspace, evidence = executor.prepare_workspace(_row())

    assert workspace.endswith("/workspaces/infra_dep_1")
    assert "mirror_main_sha=" + SHA in evidence
    assert "deploy_failure_mode=fail-stop" in evidence
    assert "automatic_rollback=false" in evidence
    assert any(call[0][:3] == ("remote", "update", "--prune") for call in calls)


def test_prepare_workspace_rejects_non_fixed_contract(monkeypatch, tmp_path):
    row = _row()
    row["repository"] = "evil/repo"
    try:
        executor.prepare_workspace(row)
    except executor.InfrastructureDeploymentSourceError as exc:
        assert exc.code == "INFRASTRUCTURE_CONTRACT_MISMATCH"
    else:
        raise AssertionError("non-fixed repository must be rejected")


def test_execute_forces_fail_stop_and_never_uses_queue_failure_mode(monkeypatch, tmp_path):
    row = _row()
    row["failure_mode"] = "auto-rollback"
    workspace = tmp_path / "checkout"
    script = workspace / executor.DEPLOY_SCRIPT
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    captured = {}
    callbacks = []

    monkeypatch.setattr(executor, "prepare_workspace", lambda value: (str(workspace), []))
    monkeypatch.setattr(executor, "_progress", lambda *args: None)
    monkeypatch.setattr(executor, "_fixed_health_evidence", lambda: (True, True))
    monkeypatch.setattr(executor.shutil, "rmtree", lambda *args, **kwargs: None)

    class FakeProcess:
        returncode = 0
        stdout = []

        def poll(self):
            return 0

        def wait(self):
            return 0

        def terminate(self):
            raise AssertionError("timeout not expected")

    def fake_popen(args, cwd, stdout, stderr, text, env):
        captured["args"] = args
        captured["cwd"] = cwd
        captured["env"] = env
        return FakeProcess()

    monkeypatch.setattr(executor.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        executor,
        "_request",
        lambda method, path, payload=None, **kwargs: callbacks.append((path, payload)) or {"ok": True},
    )

    executor.execute(row)

    assert captured["args"] == ["bash", str(script)]
    assert captured["env"]["MYGITHUB12_DEPLOY_FAILURE_MODE"] == "fail-stop"
    assert captured["env"]["MYGITHUB12_EXPECTED_PARENT_BUILD_SHA"] == row["expected_current_build_sha"]
    assert any(path.endswith("/complete") for path, _ in callbacks)
    assert not any("rollback" in str(payload).lower() for _, payload in callbacks if payload)


def test_callback_header_uses_dedicated_secret_file(monkeypatch, tmp_path):
    key_file = tmp_path / "callback.key"
    key_file.write_text("dedicated-test-key\n", encoding="utf-8")
    seen = {}

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True}

    def fake_request(method, url, headers, json, timeout):
        seen["headers"] = headers
        return Response()

    monkeypatch.setattr(executor, "CALLBACK_KEY_FILE", str(key_file))
    monkeypatch.setattr(executor.requests, "request", fake_request)
    result = executor._request("POST", "/x", {"a": 1}, required=True, attempts=1)
    assert result == {"ok": True}
    assert seen["headers"]["X-Infrastructure-Deployment-Callback-Key"] == "dedicated-test-key"
    assert "X-Deployment-Callback-Key" not in seen["headers"]
