import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from app import ci_database, deployment_service, github_utils


def test_changed_files_compare_is_structured_and_truncated(monkeypatch):
    comparison = SimpleNamespace(changed_files=101, files=[SimpleNamespace(filename=f"file-{i}.go") for i in range(101)])
    repo = MagicMock()
    repo.compare.return_value = comparison
    monkeypatch.setattr(github_utils, "_get_gh", lambda: SimpleNamespace(get_repo=lambda _: repo))
    result = github_utils.get_github_changed_files_result("frankichen/sxt", "a" * 40, "b" * 40)
    assert result["ok"] is True
    assert len(result["changed_files"]) == 100
    assert result["total_count"] == 101
    assert result["truncated"] is True
    assert result["warning_code"] == "CHANGED_FILES_TRUNCATED"


def test_changed_files_compare_failure_is_not_empty(monkeypatch):
    class Failure:
        def compare(self, *_):
            raise RuntimeError("compare unavailable")
    monkeypatch.setattr(github_utils, "_get_gh", lambda: SimpleNamespace(get_repo=lambda _: Failure()))
    result = github_utils.get_github_changed_files_result("frankichen/sxt", "a" * 40, "b" * 40)
    assert result["ok"] is False
    assert result["error_code"] == "CHANGED_FILES_COMPARE_FAILED"


def test_deployment_log_batch_is_idempotent_and_wait_is_redacted(monkeypatch, tmp_path):
    monkeypatch.setenv("DEPLOYMENT_DB_PATH", str(tmp_path / "deployments.db"))
    deployment_service._local.db = None
    deployment_service.init_deployment_db()
    db = deployment_service._get_deploy_db()
    db.execute("INSERT INTO deployments(deployment_id,repository,environment,commit_sha,private_ci_job_id,requested_scope,target_release,status,created_at,updated_at,lease_token) VALUES(?,?,?,?,?,?,?,?,?,?,?)", ("dep", "frankichen/sxt", "gongshi-test", "a" * 40, "job", "fullstack", "rel", "running", 1, 1, "secret-lease"))
    db.commit()
    first = deployment_service.append_deployment_log_batch("dep", "batch-1", "hello\n")
    second = deployment_service.append_deployment_log_batch("dep", "batch-1", "hello\n")
    assert first["ok"] and second["idempotent"] is True
    result = deployment_service.get_test_deployment("dep")
    assert "lease_token" not in result["deployment"]
    waited = deployment_service.wait_test_deployment("dep", timeout_seconds=1)
    assert waited["ok"] is True
    assert "lease_token" not in json.dumps(waited)
