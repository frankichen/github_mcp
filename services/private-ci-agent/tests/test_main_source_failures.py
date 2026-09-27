from types import SimpleNamespace

from private_ci_agent import main as main_module
from private_ci_agent.models import Job
from private_ci_agent.source import DownloadError, SourceDownloadTimeout
from private_ci_agent.workspace import WorkspaceManager


class RecordingClient:
    def __init__(self):
        self.finished = []
        self.logs = []
        self.statuses = []

    def update_job_status(self, job_id, status):
        self.statuses.append((job_id, status))

    def upload_log(self, job_id, content):
        self.logs.append((job_id, content))
        return True

    def finish_job(self, job_id, exit_code, status, summary=None,
                   error_code=None, error_message=None):
        self.finished.append({
            "job_id": job_id,
            "exit_code": exit_code,
            "status": status,
            "summary": summary,
            "error_code": error_code,
            "error_message": error_message,
        })


def make_job(job_id):
    return Job(
        job_id=job_id,
        repository="frankichen/example",
        branch="main",
        commit_sha="a" * 40,
        profile="repo-auto-check",
        timeout_seconds=300,
        lease_token="lease",
        lease_expires_at="2099-01-01T00:00:00Z",
    )


def base_config():
    return {
        "source_mirror_enabled": False,
        "controller_url": "http://controller",
        "worker_id": "wsl-ci-01",
        "worker_token": "token",
    }


def proxy_available():
    return {
        "PROXY_AVAILABLE": "1",
        "PROXY_PROTOCOL": "http",
        "PROXY_HOST": "127.0.0.1",
        "PROXY_PORT": "10808",
    }


def run_lifecycle(monkeypatch, tmp_path, job, client):
    manager = WorkspaceManager(str(tmp_path / "workspaces"))
    cleanup_calls = []
    real_cleanup = main_module._cleanup_current_job

    def record_cleanup(job_id, workspace_mgr):
        cleanup_calls.append(job_id)
        return real_cleanup(job_id, workspace_mgr)

    monkeypatch.setattr(main_module, "_cleanup_current_job", record_cleanup)
    main_module._run_job_lifecycle(
        job, client, base_config(), manager, 1024 * 1024, SimpleNamespace()
    )
    return manager, cleanup_calls


def test_download_timeout_reaches_terminal_cleanup(monkeypatch, tmp_path):
    job = make_job("job-download-timeout")
    client = RecordingClient()
    monkeypatch.setattr(main_module, "refresh_proxy_before_external_access", proxy_available)
    monkeypatch.setattr(
        main_module,
        "download_source_archive",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(SourceDownloadTimeout()),
    )

    manager, cleanup_calls = run_lifecycle(monkeypatch, tmp_path, job, client)

    assert client.finished[-1]["status"] == "failed"
    assert client.finished[-1]["error_code"]
    assert cleanup_calls == [job.job_id]
    assert not (tmp_path / "workspaces" / job.job_id).exists()
    assert manager.cleanup(job.job_id) is False


def test_download_failure_reaches_terminal_cleanup(monkeypatch, tmp_path):
    job = make_job("job-download-failed")
    client = RecordingClient()
    monkeypatch.setattr(main_module, "refresh_proxy_before_external_access", proxy_available)
    monkeypatch.setattr(
        main_module,
        "download_source_archive",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(DownloadError("network failed")),
    )

    _, cleanup_calls = run_lifecycle(monkeypatch, tmp_path, job, client)

    assert client.finished[-1]["status"] == "failed"
    assert client.finished[-1]["error_code"]
    assert cleanup_calls == [job.job_id]
    assert not (tmp_path / "workspaces" / job.job_id).exists()


def test_source_extract_failure_reaches_terminal_cleanup(monkeypatch, tmp_path):
    job = make_job("job-extract-failed")
    client = RecordingClient()
    monkeypatch.setattr(main_module, "refresh_proxy_before_external_access", proxy_available)
    monkeypatch.setattr(
        main_module,
        "download_source_archive",
        lambda *_args, **_kwargs: ("b" * 64, 128),
    )
    monkeypatch.setattr(
        main_module,
        "extract_source",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bad archive")),
    )

    _, cleanup_calls = run_lifecycle(monkeypatch, tmp_path, job, client)

    assert client.finished[-1]["status"] == "failed"
    assert client.finished[-1]["error_code"] == "SOURCE_EXTRACT_FAILED"
    assert cleanup_calls == [job.job_id]
    assert not (tmp_path / "workspaces" / job.job_id).exists()
