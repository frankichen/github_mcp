import threading
from types import SimpleNamespace

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
    ps_output = "ci-job-abc123-aaa111\nci-job-abc123-bbb222\n"

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

    assert any("ci-job-abc123-aaa111" in cmd and cmd[1] == "stop" for cmd in stopped)
    assert any("ci-job-abc123-bbb222" in cmd and cmd[1] == "stop" for cmd in stopped)
    assert any("ci-job-abc123-aaa111" in cmd and cmd[1] == "rm" for cmd in removed)
    assert any("ci-job-abc123-bbb222" in cmd and cmd[1] == "rm" for cmd in removed)
