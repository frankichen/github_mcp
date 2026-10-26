import json

from app import deployment_service


def _setup(monkeypatch, tmp_path):
    monkeypatch.setenv("DEPLOYMENT_DB_PATH", str(tmp_path / "deployments.db"))
    monkeypatch.setenv("DEPLOY_STATUS_FILE", str(tmp_path / "status.json"))
    deployment_service._local.db = None
    deployment_service.init_deployment_db()
    db = deployment_service._get_deploy_db()
    db.execute(
        "INSERT INTO deployments(deployment_id,repository,environment,commit_sha,private_ci_job_id,requested_scope,target_release,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("dep_test", "frankichen/sxt", "gongshi-test", "a" * 40, "job", "fullstack", "rel_test", "queued", 1, 1),
    )
    db.commit()


def _proof():
    return {"release_id": "rel_test", "repository": "frankichen/sxt", "environment": "gongshi-test", "git_sha": "a" * 40,
            "current_release_path": "/home/dly/releases/rel_test", "manifest_verified": True,
            "checksum_verified": True, "health_verified": True, "services_healthy": True,
            "frontend_included": True, "status": "passed"}


def test_progress_complete_is_idempotent_and_terminal(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    progress = deployment_service.update_test_deployment_progress("dep_test", "testing", "go test started", status="running")
    assert progress["ok"] is True
    assert progress["deployment"]["status"] == "running"
    complete = deployment_service.complete_test_deployment("dep_test", 0, "complete", _proof())
    assert complete["deployment"]["status"] == "passed"
    assert complete["deployment"]["target_release"] == "rel_test"
    duplicate = deployment_service.complete_test_deployment("dep_test", 0, "duplicate", _proof())
    assert duplicate["idempotent"] is True
    assert duplicate["deployment"]["status"] == "passed"
    failed_after_pass = deployment_service.fail_test_deployment("dep_test", 1, "LATE_FAIL", "late")
    assert failed_after_pass["idempotent"] is True
    assert failed_after_pass["deployment"]["status"] == "passed"


def test_complete_requires_release_health_evidence(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    deployment_service.update_test_deployment_progress("dep_test", "running", "started", status="running")
    result = deployment_service.complete_test_deployment("dep_test", 0, "missing proof", None)
    assert result["error"]["code"] == "RECONCILIATION_EVIDENCE_REQUIRED"


def test_release_registry_is_shared_by_status_and_list(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    status_path = tmp_path / "status.json"
    status_path.write_text(json.dumps({"current_release_id": "rel_test", "previous_release_id": "rel_old",
                                       "current_git_sha": "a" * 40, "manifest_verified": True,
                                       "checksum_verified": True, "health_verified": True,
                                       "releases": [_proof()]}, ensure_ascii=False))
    monkeypatch.setenv("DEPLOY_STATUS_FILE", str(status_path))
    env = deployment_service.get_test_environment_status("frankichen/sxt", "gongshi-test")
    releases = deployment_service.list_test_releases("frankichen/sxt", "gongshi-test")
    assert env["status"]["current_release_id"] == "rel_test"
    assert releases["items"][0]["is_current"] is True


def test_get_deployment_uses_verified_current_release(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    status_path = tmp_path / "status.json"
    status_path.write_text(json.dumps({
        "current_release_id": "rel_test",
        "previous_release_id": "rel_old",
        "releases": [{**_proof(), "is_current": True, "is_previous": False}],
    }))
    monkeypatch.setenv("DEPLOY_STATUS_FILE", str(status_path))
    result = deployment_service.get_test_deployment("dep_test")
    deployment = result["deployment"]
    assert deployment["current_release_id"] == "rel_test"
    assert deployment["current_git_sha"] == "a" * 40
    assert deployment["current_release_path"].endswith("/rel_test")
    assert deployment["previous_release_id"] == "rel_old"


def test_phantom_release_with_mismatched_path_is_rejected(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    status_path = tmp_path / "status.json"
    status_path.write_text(json.dumps({
        "current_release_id": "new-target",
        "current_git_sha": "a" * 40,
        "manifest_verified": True,
        "checksum_verified": True,
        "health_verified": True,
        "releases": [{**_proof(), "release_id": "new-target", "current_release_path": "/home/dly/releases/old"}],
    }))
    monkeypatch.setenv("DEPLOY_STATUS_FILE", str(status_path))
    env = deployment_service.get_test_environment_status("frankichen/sxt", "gongshi-test")
    releases = deployment_service.list_test_releases("frankichen/sxt", "gongshi-test")
    assert env["status"]["current_release_id"] is None
    assert releases["items"] == []


def test_complete_rejects_release_path_sha_mismatch(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    deployment_service.update_test_deployment_progress("dep_test", "testing", "started", status="running")
    proof = {**_proof(), "release_id": "new-target", "current_release_path": "/home/dly/releases/old"}
    result = deployment_service.complete_test_deployment("dep_test", 0, "bad proof", proof)
    assert result["error"]["code"] == "RELEASE_EVIDENCE_INVALID"


def test_fail_callback_does_not_write_release_registry(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setenv("DEPLOY_STATUS_FILE", str(tmp_path / "status.json"))
    deployment_service.update_test_deployment_progress("dep_test", "building", "started", status="running")
    deployment_service.fail_test_deployment("dep_test", 1, "DEPLOY_SOURCE_FETCH_FAILED", "fetch failed")
    assert not (tmp_path / "status.json").exists()


def _prepare_claimed_cancel(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    (tmp_path / "status.json").write_text(json.dumps({
        "environment": "gongshi-test", "current_release_id": "rel_old",
        "current_git_sha": "b" * 40, "manifest_verified": True,
        "checksum_verified": True, "health_verified": True, "services_healthy": True,
        "releases": [{
            "release_id": "rel_old", "repository": "frankichen/sxt",
            "environment": "gongshi-test", "git_sha": "b" * 40,
            "current_release_path": "/home/dly/releases/rel_old",
            "manifest_verified": True, "checksum_verified": True,
            "health_verified": True, "services_healthy": True
        }],
    }), encoding="utf-8")
    db = deployment_service._get_deploy_db()
    db.execute("""UPDATE deployments SET status='running',current_step='claimed',
        current_release_before='rel_old',started_at=1,updated_at=1,log_revision=1,
        log_text='agent claimed deployment dep_test; execution delegated to WSL'
        WHERE deployment_id='dep_test'""")
    db.commit()
    monkeypatch.setattr(deployment_service, "_now", lambda: 1000)
    assert deployment_service.cancel_test_deployment("dep_test")["deployment"]["status"] == "cancel_requested"
    return db


def _cancel_ack(**extra):
    data = {"deployment_id": "dep_test", "expected_log_revision": 1,
            "expected_current_release_id": "rel_old", "expected_commit_sha": "a" * 40,
            "workspace_absent": True}
    data.update(extra)
    return deployment_service.acknowledge_cancelled_deployment(**data)


def test_cancel_ack_frees_original_lock_and_preserves_verified_release(monkeypatch, tmp_path):
    db = _prepare_claimed_cancel(monkeypatch, tmp_path)
    assert [x["deployment_id"] for x in deployment_service.list_delegated_deployments()] == ["dep_test"]
    assert _cancel_ack()["error"]["code"] == "DEPLOYMENT_CANCEL_ACK_GRACE"
    monkeypatch.setattr(deployment_service, "_now", lambda: 1100)
    result = _cancel_ack()
    assert result["ok"] and result["deployment"]["status"] == "cancelled"
    assert result["deployment"]["reconciliation_reason"] == "wslexecutor_unstarted_cancel_ack"
    assert _cancel_ack()["idempotent"] is True
    assert deployment_service.list_delegated_deployments() == []
    assert db.execute("SELECT log_revision FROM deployments WHERE deployment_id='dep_test'").fetchone()[0] == 2
    current = deployment_service.get_test_environment_status("frankichen/sxt", "gongshi-test")["status"]
    assert current["current_release_id"] == "rel_old" and current["current_git_sha"] == "b" * 40


def test_cancel_ack_fails_closed_on_workspace_progress_and_drift(monkeypatch, tmp_path):
    db = _prepare_claimed_cancel(monkeypatch, tmp_path)
    monkeypatch.setattr(deployment_service, "_now", lambda: 1100)
    assert _cancel_ack(workspace_absent=False)["error"]["code"] == "DEPLOYMENT_WORKSPACE_EVIDENCE_REQUIRED"
    assert _cancel_ack(expected_log_revision=2)["error"]["code"] == "DEPLOYMENT_CANCEL_ACK_IDENTITY_MISMATCH"
    assert _cancel_ack(expected_commit_sha="c" * 40)["error"]["code"] == "DEPLOYMENT_CANCEL_ACK_IDENTITY_MISMATCH"
    assert deployment_service.update_test_deployment_progress(
        "dep_test", "preparing_workspace", "started", status="running"
    )["error"]["code"] == "DEPLOYMENT_CANCEL_PENDING"
    status = json.loads((tmp_path / "status.json").read_text())
    status["current_release_id"] = "rel_other"
    (tmp_path / "status.json").write_text(json.dumps(status))
    assert _cancel_ack()["error"]["code"] == "DEPLOYMENT_RELEASE_DRIFT"
    assert db.execute("SELECT status FROM deployments WHERE deployment_id='dep_test'").fetchone()[0] == "cancel_requested"



def _install_independent_successor(monkeypatch, tmp_path, db):
    """Simulate another deployment completing while cancellation stayed unstarted."""
    successor = "rel_successor"
    proof = {**_proof(), "release_id": successor,
             "current_release_path": "/home/dly/releases/" + successor, "status": "passed"}
    status_path = tmp_path / "status.json"
    status_path.write_text(json.dumps({
        "environment": "gongshi-test", "current_release_id": successor,
        "current_git_sha": "a" * 40,
        "releases": [proof],
    }), encoding="utf-8")
    db.execute(
        """INSERT INTO deployments(
           deployment_id,repository,environment,commit_sha,private_ci_job_id,
           requested_scope,current_release_before,target_release,status,
           current_release_path,created_at,started_at,finished_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("dep_successor", "frankichen/sxt", "gongshi-test", "a" * 40,
         "job_successor", "fullstack", "rel_old", successor, "passed",
         proof["current_release_path"], 100, 950, 1050, 1050),
    )
    db.commit()
    return proof


def test_cancel_ack_allows_later_verified_independent_same_sha_release(monkeypatch, tmp_path):
    db = _prepare_claimed_cancel(monkeypatch, tmp_path)
    monkeypatch.setattr(deployment_service, "_now", lambda: 1100)
    proof = _install_independent_successor(monkeypatch, tmp_path, db)
    result = _cancel_ack()
    assert result["ok"] and result["deployment"]["status"] == "cancelled"
    assert result["deployment"]["reconciliation_reason"] == "wslexecutor_unstarted_cancel_ack_after_successor"
    assert result["deployment"]["error_code"] == "CANCELLED_BEFORE_EXECUTION"
    assert _cancel_ack()["idempotent"] is True
    assert deployment_service.list_delegated_deployments() == []
    assert db.execute("SELECT status FROM deployments WHERE deployment_id='dep_successor'").fetchone()[0] == "passed"
    assert db.execute("SELECT log_revision FROM deployments WHERE deployment_id='dep_test'").fetchone()[0] == 2
    env = deployment_service.get_test_environment_status("frankichen/sxt", "gongshi-test")["status"]
    assert env["current_release_id"] == proof["release_id"]
    assert env["current_git_sha"] == "a" * 40


def test_cancel_ack_successor_requires_independent_passed_evidence(monkeypatch, tmp_path):
    db = _prepare_claimed_cancel(monkeypatch, tmp_path)
    monkeypatch.setattr(deployment_service, "_now", lambda: 1100)
    _install_independent_successor(monkeypatch, tmp_path, db)
    blockers = [
        ("status", "running"),
        ("finished_at", None),
        ("finished_at", 900),
        ("commit_sha", "c" * 40),
        ("target_release", "rel_other"),
        ("current_release_path", "/home/dly/releases/rel_other"),
        ("repository", "other/repo"),
        ("environment", "production"),
        ("requested_scope", "backend"),
    ]
    for column, wrong in blockers:
        before = db.execute(f"SELECT {column} FROM deployments WHERE deployment_id='dep_successor'").fetchone()[0]
        db.execute(f"UPDATE deployments SET {column}=? WHERE deployment_id='dep_successor'", (wrong,))
        db.commit()
        assert _cancel_ack()["error"]["code"] == "DEPLOYMENT_RELEASE_DRIFT", column
        db.execute(f"UPDATE deployments SET {column}=? WHERE deployment_id='dep_successor'", (before,))
        db.commit()
    path = tmp_path / "status.json"
    current = json.loads(path.read_text())
    current["releases"][0]["services_healthy"] = False
    path.write_text(json.dumps(current))
    assert _cancel_ack()["error"]["code"] == "DEPLOYMENT_RELEASE_DRIFT"
    current["releases"][0]["services_healthy"] = True
    current["releases"][0]["current_release_path"] = "/home/dly/releases/not-the-release"
    path.write_text(json.dumps(current))
    assert _cancel_ack()["error"]["code"] == "DEPLOYMENT_RELEASE_DRIFT"
    assert tuple(db.execute("SELECT status,log_revision FROM deployments WHERE deployment_id='dep_test'").fetchone()) == ("cancel_requested", 1)

def test_cancel_ack_rejects_progress_active_lease_and_wrong_generation(monkeypatch, tmp_path):
    db = _prepare_claimed_cancel(monkeypatch, tmp_path)
    monkeypatch.setattr(deployment_service, "_now", lambda: 1100)
    db.execute("UPDATE deployments SET current_step='testing',log_revision=2 WHERE deployment_id='dep_test'")
    db.commit()
    assert _cancel_ack(expected_log_revision=2)["error"]["code"] == "DEPLOYMENT_CANCEL_ACK_UNSAFE"
    db.execute("UPDATE deployments SET current_step='claimed',log_revision=1 WHERE deployment_id='dep_test'")
    db.execute("ALTER TABLE deployments ADD COLUMN lease_expires_at REAL")
    db.execute("UPDATE deployments SET lease_expires_at=1200 WHERE deployment_id='dep_test'")
    db.commit()
    assert _cancel_ack()["error"]["code"] == "DEPLOYMENT_LEASE_ACTIVE"
    db.execute("UPDATE deployments SET lease_expires_at=900 WHERE deployment_id='dep_test'")
    db.execute("ALTER TABLE deployments ADD COLUMN claim_generation INTEGER")
    db.execute("UPDATE deployments SET claim_generation=7 WHERE deployment_id='dep_test'")
    db.commit()
    assert _cancel_ack()["error"]["code"] == "DEPLOYMENT_CANCEL_ACK_GENERATION_MISMATCH"
    assert _cancel_ack(expected_claim_generation=7)["ok"] is True


def test_running_cancel_stays_sticky_until_actual_finish(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    deployment_service.update_test_deployment_progress("dep_test", "testing", "started", status="running")
    deployment_service.cancel_test_deployment("dep_test")
    progress = deployment_service.update_test_deployment_progress("dep_test", "building", "build", status="running")
    assert progress["ok"] and progress["deployment"]["status"] == "cancel_requested"
    assert deployment_service.complete_test_deployment("dep_test", 0, "success", _proof())["deployment"]["status"] == "passed"


def test_queued_cancel_is_terminal_and_late_callback_cannot_restart(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    assert deployment_service.cancel_test_deployment("dep_test")["deployment"]["status"] == "cancelled"
    assert deployment_service.update_test_deployment_progress(
        "dep_test", "building", "late", status="running"
    )["deployment"]["status"] == "cancelled"
