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


def test_running_heartbeat_keeps_same_deployment_identity_and_stops(monkeypatch):
    calls = []
    monkeypatch.setattr(executor, "HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(
        executor,
        "_heartbeat",
        lambda state, deployment_id="": calls.append((state, deployment_id)),
    )

    def fake_execute(row):
        deadline = executor.time.monotonic() + 0.5
        while sum(1 for state, _ in calls if state == "running") < 3:
            if executor.time.monotonic() >= deadline:
                raise AssertionError("running heartbeat did not repeat during deployment")
            executor.time.sleep(0.005)

    monkeypatch.setattr(executor, "execute", fake_execute)
    executor._execute_with_heartbeat(_row())

    running = [item for item in calls if item[0] == "running"]
    assert len(running) >= 3
    assert {deployment_id for _, deployment_id in running} == {"infra_dep_1"}
    assert calls[-1] == ("idle", "")
    final_count = len(calls)
    executor.time.sleep(0.03)
    assert len(calls) == final_count


def test_running_heartbeat_recovers_after_temporary_callback_error(monkeypatch):
    calls = []
    completed = []
    monkeypatch.setattr(executor, "HEARTBEAT_SECONDS", 0.01)

    def flaky_heartbeat(state, deployment_id=""):
        calls.append((state, deployment_id))
        running_count = sum(1 for item_state, _ in calls if item_state == "running")
        if state == "running" and running_count == 1:
            raise RuntimeError("temporary controller 502")

    def fake_execute(row):
        deadline = executor.time.monotonic() + 0.5
        while sum(1 for state, _ in calls if state == "running") < 3:
            if executor.time.monotonic() >= deadline:
                raise AssertionError("running heartbeat did not recover")
            executor.time.sleep(0.005)
        completed.append(row["deployment_id"])

    monkeypatch.setattr(executor, "_heartbeat", flaky_heartbeat)
    monkeypatch.setattr(executor, "execute", fake_execute)
    executor._execute_with_heartbeat(_row())

    assert completed == ["infra_dep_1"]
    assert sum(1 for state, _ in calls if state == "running") >= 3
    assert {deployment_id for state, deployment_id in calls if state == "running"} == {"infra_dep_1"}
    assert calls[-1] == ("idle", "")


def test_running_heartbeat_stops_and_reports_idle_when_execute_raises(monkeypatch):
    calls = []
    monkeypatch.setattr(executor, "HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(
        executor,
        "_heartbeat",
        lambda state, deployment_id="": calls.append((state, deployment_id)),
    )
    monkeypatch.setattr(executor, "execute", lambda row: (_ for _ in ()).throw(RuntimeError("boom")))

    try:
        executor._execute_with_heartbeat(_row())
    except RuntimeError as exc:
        assert str(exc) == "boom"
    else:
        raise AssertionError("execute error must propagate after heartbeat cleanup")

    assert calls[0] == ("running", "infra_dep_1")
    assert calls[-1] == ("idle", "")
    final_count = len(calls)
    executor.time.sleep(0.03)
    assert len(calls) == final_count


def test_running_heartbeat_cleans_up_for_deployment_failure_and_timeout(monkeypatch, tmp_path):
    heartbeat_calls = []
    monkeypatch.setattr(executor, "HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(
        executor,
        "_heartbeat",
        lambda state, deployment_id="": heartbeat_calls.append((state, deployment_id)),
    )
    monkeypatch.setattr(executor, "_progress", lambda *args: None)
    monkeypatch.setattr(executor.shutil, "rmtree", lambda *args, **kwargs: None)

    def run_case(name, returncode, fire_timeout):
        workspace = tmp_path / name
        script = workspace / executor.DEPLOY_SCRIPT
        script.parent.mkdir(parents=True)
        script.write_text("#!/bin/sh\n", encoding="utf-8")
        callbacks = []
        monkeypatch.setattr(executor, "prepare_workspace", lambda value: (str(workspace), []))

        class FakeProcess:
            stdout = []

            def __init__(self):
                self.returncode = None if fire_timeout else returncode

            def poll(self):
                return self.returncode

            def wait(self):
                return self.returncode

            def terminate(self):
                self.returncode = -15

        process = FakeProcess()
        monkeypatch.setattr(executor.subprocess, "Popen", lambda *args, **kwargs: process)

        class FakeTimer:
            def __init__(self, seconds, callback):
                self.callback = callback
                self.daemon = False

            def start(self):
                if fire_timeout:
                    self.callback()

            def cancel(self):
                return None

        monkeypatch.setattr(executor.threading, "Timer", FakeTimer)
        monkeypatch.setattr(
            executor,
            "_request",
            lambda method, path, payload=None, **kwargs: callbacks.append((path, payload)) or {"ok": True},
        )

        before = len(heartbeat_calls)
        executor._execute_with_heartbeat(_row())
        case_heartbeats = heartbeat_calls[before:]
        assert case_heartbeats[0] == ("running", "infra_dep_1")
        assert case_heartbeats[-1] == ("idle", "")
        fail_callbacks = [(path, payload) for path, payload in callbacks if path.endswith("/fail")]
        assert len(fail_callbacks) == 1
        return fail_callbacks[0][1]

    failure = run_case("failure", 17, False)
    assert failure["exit_code"] == 17
    assert failure["error_code"] == "INFRASTRUCTURE_DEPLOYMENT_FAILED"

    timeout = run_case("timeout", -15, True)
    assert timeout["exit_code"] == 124
    assert timeout["error_code"] == "INFRASTRUCTURE_DEPLOYMENT_TIMEOUT"



def test_phase_markers_are_fixed_and_unknown_values_do_not_expand_contract():
    assert executor._phase_from_output(
        "[deploy] DX2_PHASE=controller_build", "validation"
    ) == "controller_build"
    assert executor._phase_from_output(
        "[deploy] DX2_PHASE=controller_switch", "controller_build"
    ) == "controller_switch"
    assert executor._phase_from_output(
        "[deploy] DX2_PHASE=arbitrary_shell", "health"
    ) == "health"
    assert executor._phase_from_output("ordinary deployment output", "preheat") == "preheat"


def test_fixed_deploy_script_contains_dx2_phase_markers():
    script = Path(__file__).resolve().parents[2] / "private-ci-agent" / "deploy" / "apply-fixes.sh"
    content = script.read_text(encoding="utf-8")
    for phase in ("controller_build", "controller_switch", "health", "preheat", "post_verify"):
        assert content.count(f'DX2_PHASE={phase}') == 1
    assert "MYGITHUB12_DEPLOY_FAILURE_MODE" in content
    assert content.count("systemctl restart private-ci-agent.service") == 1
    assert content.index("systemctl restart private-ci-agent.service") < content.index("DX2_PHASE=controller_switch")
