import logging
import threading
from types import SimpleNamespace

import pytest

import private_ci_agent.main as main_module


def test_request_cancel_sets_event_and_reclaims_containers(monkeypatch):
    cancel_event = threading.Event()
    killed = []

    monkeypatch.setattr(main_module, "_cancel_event", cancel_event)
    monkeypatch.setattr(main_module, "_current_job_id", "job-abc123")
    monkeypatch.setattr(main_module, "_kill_current_job", lambda *_: killed.append("job-abc123"))

    assert main_module._request_cancel("job-abc123") is True

    assert cancel_event.is_set()
    assert killed == ["job-abc123"]


def test_request_cancel_ignores_stale_response_for_previous_job(monkeypatch):
    """A cancel response for a finished job must not kill the fresh lease."""
    cancel_event = threading.Event()
    killed = []

    monkeypatch.setattr(main_module, "_cancel_event", cancel_event)
    monkeypatch.setattr(main_module, "_current_job_id", "job-new456")
    monkeypatch.setattr(main_module, "_kill_current_job", lambda *_: killed.append(main_module._current_job_id))

    assert main_module._request_cancel("job-old789") is False

    assert not cancel_event.is_set()
    assert killed == []


def test_request_cancel_is_noop_without_active_job(monkeypatch):
    cancel_event = threading.Event()
    killed = []

    monkeypatch.setattr(main_module, "_cancel_event", cancel_event)
    monkeypatch.setattr(main_module, "_current_job_id", None)
    monkeypatch.setattr(main_module, "_kill_current_job", lambda *_: killed.append("x"))

    assert main_module._request_cancel() is False
    assert not cancel_event.is_set()
    assert killed == []


def test_kill_current_job_reclaims_all_job_containers_by_prefix(monkeypatch):
    stopped = []
    removed = []
    ps_output = "ci-wsl-ci-01-job-abc123-aaa111\nci-wsl-ci-01-job-abc123-bbb222\n"

    def fake_run(cmd, **_kwargs):
        if cmd[:2] == ["podman", "ps"]:
            return SimpleNamespace(returncode=0, stdout=ps_output, stderr="")
        if cmd[1] == "stop":
            stopped.append(cmd)
        elif cmd[1] == "rm":
            removed.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    import private_ci_agent.podman as podman_module
    monkeypatch.setattr(podman_module.subprocess, "run", fake_run)
    monkeypatch.setattr(main_module, "_current_job_id", "job-abc123")
    monkeypatch.setattr(main_module, "_podman_binary", "podman")

    main_module._kill_current_job()

    assert any("ci-wsl-ci-01-job-abc123-aaa111" in cmd and cmd[1] == "stop" for cmd in stopped)
    assert any("ci-wsl-ci-01-job-abc123-bbb222" in cmd and cmd[1] == "stop" for cmd in stopped)
    assert any("ci-wsl-ci-01-job-abc123-aaa111" in cmd and cmd[1] == "rm" for cmd in removed)
    assert any("ci-wsl-ci-01-job-abc123-bbb222" in cmd and cmd[1] == "rm" for cmd in removed)


def test_controller_client_sends_attempt_lease_on_job_callbacks(monkeypatch):
    import private_ci_agent.controller_client as client_module

    requests = []
    payloads = [
        b'{"job_id":"job-lease","lease_token":"attempt-secret"}',
        b'{}',
        b'{}',
    ]

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return self.payload

    def fake_urlopen(request, timeout=30):
        requests.append(request)
        return FakeResponse(payloads.pop(0))

    monkeypatch.setattr(client_module.urllib.request, "urlopen", fake_urlopen)
    client = client_module.ControllerClient("http://controller", "worker-a", "worker-token")

    leased = client.lease_job()
    assert leased["lease_token"] == "attempt-secret"
    assert client.upload_log("job-lease", "hello\n") is True
    callback_headers = {key.lower(): value for key, value in requests[1].header_items()}
    assert callback_headers["x-ci-lease-token"] == "attempt-secret"

    client.finish_job("job-lease", 0, "passed")
    assert "job-lease" not in client._job_leases


def test_sigterm_marks_active_job_for_cancellation(monkeypatch):
    cancel_event = threading.Event()
    monkeypatch.setattr(main_module, "_running", True)
    monkeypatch.setattr(main_module, "_current_job_id", "job-active")
    monkeypatch.setattr(main_module, "_cancel_event", cancel_event)

    main_module.signal_handler(15, None)

    assert main_module._running is False
    assert cancel_event.is_set()


def test_cleanup_job_runs_service_container_source_workspace_order(monkeypatch, tmp_path, caplog):
    events = []
    workspace = tmp_path / "job-ordered"
    workspace.mkdir()
    manager = SimpleNamespace(
        workspace_root=str(tmp_path),
        cleanup=lambda _job_id: (events.append("workspace"), workspace.rmdir(), True)[-1],
    )
    monkeypatch.setattr(main_module, "cleanup_job_services", lambda *_args: events.append("services"))

    class FakeRunner:
        def __init__(self, *_args, **_kwargs):
            pass

        def kill_job(self, _job_id):
            events.append("containers")

    monkeypatch.setattr(main_module, "PodmanRunner", FakeRunner)
    monkeypatch.setattr(main_module, "remove_source_worktree", lambda *_args: events.append("source_worktree"))

    with caplog.at_level(logging.INFO, logger="ci-agent"):
        main_module._cleanup_job("job-ordered", manager)

    assert events == ["services", "containers", "source_worktree", "workspace"]
    assert not workspace.exists()
    assert (
        "Job cleanup verified: job=job-ordered cleanup_status=passed "
        "workspace_absent=true"
    ) in caplog.text


def test_cleanup_job_attempts_all_phases_and_fails_without_success_log(monkeypatch, tmp_path, caplog):
    events = []
    workspace = tmp_path / "job-failed"
    workspace.mkdir()

    def fail_workspace(_job_id):
        events.append("workspace")
        raise PermissionError("simulated UID-mapped tree")

    manager = SimpleNamespace(workspace_root=str(tmp_path), cleanup=fail_workspace)
    monkeypatch.setattr(main_module, "cleanup_job_services", lambda *_args: events.append("services"))

    class FakeRunner:
        def __init__(self, *_args, **_kwargs):
            pass

        def kill_job(self, _job_id):
            events.append("containers")

    monkeypatch.setattr(main_module, "PodmanRunner", FakeRunner)
    monkeypatch.setattr(main_module, "remove_source_worktree", lambda *_args: events.append("source_worktree"))

    with caplog.at_level(logging.ERROR, logger="ci-agent"):
        with pytest.raises(RuntimeError, match="workspace"):
            main_module._cleanup_job("job-failed", manager)

    assert events == ["services", "containers", "source_worktree", "workspace"]
    assert workspace.exists()
    assert "phase=workspace" in caplog.text
    assert "cleanup_status=failed" in caplog.text
    assert "simulated UID-mapped tree" in caplog.text
    assert "Job cleanup verified" not in caplog.text


def test_cleanup_current_job_resets_worker_state_even_when_cleanup_fails(monkeypatch, tmp_path, caplog):
    manager = SimpleNamespace(workspace_root=str(tmp_path))
    monkeypatch.setattr(main_module, "_current_job_id", "job-reset")
    monkeypatch.setattr(main_module, "_current_lease_token", "lease")
    monkeypatch.setattr(
        main_module,
        "_cleanup_job",
        lambda *_args: (_ for _ in ()).throw(PermissionError("cleanup failed")),
    )

    with caplog.at_level(logging.ERROR, logger="ci-agent"):
        main_module._cleanup_current_job("job-reset", manager)

    assert main_module._current_job_id is None
    assert main_module._current_lease_token is None
    assert "Job cleanup failed: job=job-reset cleanup_status=failed" in caplog.text
    assert "cleanup failed" in caplog.text


def test_run_job_lifecycle_always_cleans_after_normal_return(monkeypatch, tmp_path):
    events = []
    job = SimpleNamespace(job_id="job-normal")
    manager = SimpleNamespace(workspace_root=str(tmp_path))
    monkeypatch.setattr(main_module, "_execute_job", lambda *_args: events.append("execute"))
    monkeypatch.setattr(
        main_module,
        "_cleanup_current_job",
        lambda job_id, _manager: events.append(f"cleanup:{job_id}"),
    )

    main_module._run_job_lifecycle(job, SimpleNamespace(), {}, manager, 1024, SimpleNamespace())

    assert events == ["execute", "cleanup:job-normal"]


def test_run_job_lifecycle_internal_exception_finishes_and_cleans(monkeypatch, tmp_path):
    events = []
    finished = []
    job = SimpleNamespace(job_id="job-internal")
    manager = SimpleNamespace(workspace_root=str(tmp_path))
    client = SimpleNamespace(
        finish_job=lambda job_id, exit_code, status, **kwargs: finished.append(
            (job_id, exit_code, status, kwargs)
        )
    )
    monkeypatch.setattr(
        main_module,
        "_execute_job",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr(
        main_module,
        "_cleanup_current_job",
        lambda job_id, _manager: events.append(f"cleanup:{job_id}"),
    )

    main_module._run_job_lifecycle(job, client, {}, manager, 1024, SimpleNamespace())

    assert finished and finished[-1][2] == "internal_error"
    assert events == ["cleanup:job-internal"]
