import sqlite3

from app import deploy_worker, deployment_store


def test_minimal_store_initializes_worker_columns(tmp_path, monkeypatch):
    path = tmp_path / "deployments.db"
    monkeypatch.setenv("DEPLOYMENT_DB_PATH", str(path))
    deployment_store._local.db = None
    deployment_store.init_deployment_db()
    db = sqlite3.connect(path)
    columns = {
        row[1] for row in db.execute("PRAGMA table_info(deployments)").fetchall()
    }
    assert {"lease_token", "log_revision", "artifact_id", "performance_json"} <= columns
    db.close()


def test_minimal_store_migrates_legacy_schema(tmp_path, monkeypatch):
    path = tmp_path / "deployments.db"
    db = sqlite3.connect(path)
    db.execute(
        """CREATE TABLE deployments (
           deployment_id TEXT PRIMARY KEY, repository TEXT, environment TEXT,
           commit_sha TEXT, private_ci_job_id TEXT, requested_scope TEXT,
           current_release_before TEXT, target_release TEXT, requested_by TEXT,
           status TEXT, current_step TEXT, exit_code INTEGER, error_code TEXT,
           error_message TEXT, rollback_attempted INTEGER, rollback_succeeded INTEGER,
           cancel_requested INTEGER, frontend_included INTEGER, created_at REAL,
           started_at REAL, finished_at REAL, log_text TEXT
        )"""
    )
    db.commit()
    db.close()
    monkeypatch.setenv("DEPLOYMENT_DB_PATH", str(path))
    deployment_store._local.db = None
    deployment_store.init_deployment_db()
    assert deployment_store.get_deploy_db().execute(
        "SELECT log_revision FROM deployments"
    ).fetchall() == []



def test_claim_only_delegates_sxt_but_executes_auto_gupiao_locally(monkeypatch):
    monkeypatch.setenv("DEPLOY_EXECUTION_MODE", "claim_only")

    assert deploy_worker._should_delegate_to_wsl("frankichen/sxt") is True
    assert deploy_worker._should_delegate_to_wsl("frankichen/auto_gupiao") is False


def test_execute_mode_never_delegates(monkeypatch):
    monkeypatch.setenv("DEPLOY_EXECUTION_MODE", "execute")

    assert deploy_worker._should_delegate_to_wsl("frankichen/sxt") is False
    assert deploy_worker._should_delegate_to_wsl("frankichen/auto_gupiao") is False
