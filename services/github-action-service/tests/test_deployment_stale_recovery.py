import json
import sqlite3
import threading
import time

from app import deployment_service as service


SHA = "a" * 40


def _status(path, current="rel_current", sha="b" * 40, deployment_id="dep_current"):
    release = {
        "release_id": current,
        "repository": service.REPOSITORY,
        "environment": service.ENVIRONMENT,
        "git_sha": sha,
        "current_release_path": f"/home/dly/releases/{current}",
        "manifest_verified": True,
        "checksum_verified": True,
        "health_verified": True,
        "services_healthy": True,
        "deployment_id": deployment_id,
        "status": "passed",
    }
    path.write_text(json.dumps({
        "current_release_id": current,
        "current_git_sha": sha,
        "current_release_path": release["current_release_path"],
        "manifest_verified": True,
        "checksum_verified": True,
        "health_verified": True,
        "releases": [release],
    }))


def _setup(monkeypatch, tmp_path):
    db_path = tmp_path / "deployments.db"
    status_path = tmp_path / "status.json"
    monkeypatch.setenv("DEPLOYMENT_DB_PATH", str(db_path))
    monkeypatch.setenv("DEPLOY_STATUS_FILE", str(status_path))
    monkeypatch.setenv("DEPLOYMENT_CLAIM_LEASE_SECONDS", "10")
    service._local.db = None
    service.init_deployment_db()
    _status(status_path)
    return service._get_deploy_db(), status_path


def _insert(db, deployment_id="dep_test", status="claimed", step="claimed", owner=service.DELEGATED_HANDOFF_OWNER,
            lease=None, cancel=False, target="rel_target", state_revision=0):
    now = time.time()
    if lease is None:
        lease = now + 30
    db.execute(
        """INSERT INTO deployments(
             deployment_id,repository,environment,commit_sha,private_ci_job_id,requested_scope,
             current_release_before,target_release,status,current_step,cancel_requested,created_at,
             started_at,updated_at,claim_owner,claim_started_at,heartbeat_at,lease_expires_at,
             claim_generation,state_revision,log_text
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            deployment_id, service.REPOSITORY, service.ENVIRONMENT, SHA, "ci", "fullstack",
            "rel_current", target, status, step, int(cancel), now - 60, now - 60, now - 60,
            owner, now - 60, now - 60, lease, 1, state_revision, "",
        ),
    )


def _claim(db, executor="executor-a"):
    result = service.claim_delegated_test_deployment(executor)
    assert result["ok"] is True and result["deployment"]
    return result["deployment"]


def _proof(release_id="rel_target"):
    return {
        "release_id": release_id,
        "repository": service.REPOSITORY,
        "environment": service.ENVIRONMENT,
        "git_sha": SHA,
        "current_release_path": f"/home/dly/releases/{release_id}",
        "manifest_verified": True,
        "checksum_verified": True,
        "health_verified": True,
        "services_healthy": True,
    }


def _credentials(claim):
    return claim["claim_owner"], claim["claim_token"], claim["claim_generation"]


def test_queued_handoff_claim_and_heartbeat(monkeypatch, tmp_path):
    db, _ = _setup(monkeypatch, tmp_path)
    _insert(db)
    claim = _claim(db)
    assert claim["status"] == "claimed"
    assert claim["claim_owner"] == "executor-a"
    first_expiry = claim["lease_expires_at"]
    result = service.heartbeat_test_deployment("dep_test", *_credentials(claim))
    assert result["ok"] is True and result["terminal"] is False
    assert result["lease_expires_at"] >= first_expiry


def test_claimed_executor_completes_with_fenced_callback(monkeypatch, tmp_path):
    db, _ = _setup(monkeypatch, tmp_path)
    _insert(db)
    claim = _claim(db)
    progress = service.update_test_deployment_progress("dep_test", "building", "started", "running", None, *_credentials(claim))
    assert progress["deployment"]["status"] == "running"
    completed = service.complete_test_deployment("dep_test", 0, "done", _proof(), *_credentials(claim))
    assert completed["deployment"]["status"] == "passed"
    assert completed["deployment"]["finished_at"] is not None


def test_claimed_executor_crash_and_lease_expiry_fail_closed(monkeypatch, tmp_path):
    db, _ = _setup(monkeypatch, tmp_path)
    _insert(db, lease=time.time() - 1, owner="executor-dead")
    result = service.reconcile_stale_test_deployment("dep_test")
    assert result["reconciled"] is True
    assert result["deployment"]["status"] == "failed"
    assert result["reason"] == "DEPLOYMENT_EXECUTOR_LOST"


def test_cancel_requested_executor_gone_becomes_cancelled(monkeypatch, tmp_path):
    db, _ = _setup(monkeypatch, tmp_path)
    _insert(db, status="cancel_requested", cancel=True, lease=time.time() - 1, owner="executor-dead")
    result = service.reconcile_stale_test_deployment("dep_test")
    assert result["deployment"]["status"] == "cancelled"
    assert result["deployment"]["finished_at"] is not None
    assert result["reason"] == "DEPLOYMENT_CANCELED_STALE_CLAIM"


def test_running_callback_wins_before_reaper(monkeypatch, tmp_path):
    db, _ = _setup(monkeypatch, tmp_path)
    _insert(db)
    claim = _claim(db)
    db.execute("UPDATE deployments SET lease_expires_at=? WHERE deployment_id='dep_test'", (time.time() - 1,))
    progress = service.update_test_deployment_progress("dep_test", "testing", "still alive", "running", None, *_credentials(claim))
    assert progress["ok"] is True
    reconcile = service.reconcile_stale_test_deployment("dep_test")
    assert reconcile["reconciled"] is False
    assert reconcile["reason"] == "CLAIM_NOT_STALE"


def test_verified_target_current_is_never_cancelled_or_marked_passed(monkeypatch, tmp_path):
    db, status_path = _setup(monkeypatch, tmp_path)
    _status(status_path, current="rel_target", sha=SHA, deployment_id="dep_test")
    _insert(db, status="cancel_requested", cancel=True, lease=time.time() - 1, owner="executor-dead")
    result = service.reconcile_stale_test_deployment("dep_test")
    assert result["deployment"]["status"] == "failed"
    assert result["reason"] == "DEPLOYMENT_CALLBACK_LOST_AFTER_RELEASE_SWITCH"
    assert result["evidence"]["target_is_current"] is True


def test_current_release_mismatch_allows_safe_cancellation(monkeypatch, tmp_path):
    db, status_path = _setup(monkeypatch, tmp_path)
    _status(status_path, current="rel_current", sha=SHA, deployment_id="dep_newer")
    _insert(db, status="cancel_requested", cancel=True, lease=time.time() - 1, owner="executor-dead")
    result = service.reconcile_stale_test_deployment("dep_test")
    assert result["evidence"]["current_release_id"] == "rel_current"
    assert result["evidence"]["target_is_current"] is False
    assert result["deployment"]["status"] == "cancelled"


def test_cancel_with_live_executor_remains_cancel_requested(monkeypatch, tmp_path):
    db, _ = _setup(monkeypatch, tmp_path)
    _insert(db)
    claim = _claim(db)
    result = service.cancel_test_deployment("dep_test")
    assert result["deployment"]["status"] == "cancel_requested"
    assert result["deployment"]["finished_at"] is None
    heartbeat = service.heartbeat_test_deployment("dep_test", *_credentials(claim))
    assert heartbeat["cancel_requested"] is True


def test_duplicate_cancel_and_reconcile_are_idempotent(monkeypatch, tmp_path):
    db, _ = _setup(monkeypatch, tmp_path)
    _insert(db, lease=time.time() - 1, owner="executor-dead")
    first = service.cancel_test_deployment("dep_test")
    second = service.cancel_test_deployment("dep_test")
    third = service.reconcile_stale_test_deployment("dep_test")
    assert first["deployment"]["status"] == "cancelled"
    assert second["idempotent"] is True
    assert third["idempotent"] is True


def test_two_executors_cannot_reclaim_same_generation(monkeypatch, tmp_path):
    db, _ = _setup(monkeypatch, tmp_path)
    _insert(db, lease=time.time() - 1, owner="executor-dead")
    results = []
    threads = [threading.Thread(target=lambda name=name: results.append(service.claim_delegated_test_deployment(name)))
               for name in ("executor-a", "executor-b")]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    claims = [item["deployment"] for item in results if item.get("deployment")]
    assert len(claims) == 1
    assert claims[0]["claim_generation"] == 2


def test_stale_generation_cannot_double_terminal(monkeypatch, tmp_path):
    db, _ = _setup(monkeypatch, tmp_path)
    _insert(db)
    claim = _claim(db)
    owner, token, generation = _credentials(claim)
    failed = service.fail_test_deployment("dep_test", 1, "TEST_FAILURE", "failed", owner, token, generation)
    late = service.complete_test_deployment("dep_test", 0, "late", _proof(), owner, token, generation)
    assert failed["deployment"]["status"] == "failed"
    assert late["idempotent"] is True
    assert late["deployment"]["status"] == "failed"


def test_executor_restart_reclaims_only_pre_execution_claim(monkeypatch, tmp_path):
    db, _ = _setup(monkeypatch, tmp_path)
    _insert(db)
    first = _claim(db, "executor-a")
    db.execute("UPDATE deployments SET lease_expires_at=? WHERE deployment_id='dep_test'", (time.time() - 1,))
    second = _claim(db, "executor-b")
    assert second["claim_generation"] == first["claim_generation"] + 1
    stale_heartbeat = service.heartbeat_test_deployment("dep_test", *_credentials(first))
    assert stale_heartbeat["error"]["code"] == "DEPLOYMENT_CLAIM_MISMATCH"


def test_stale_cleanup_allows_new_deployment_but_live_claim_blocks(monkeypatch, tmp_path):
    db, _ = _setup(monkeypatch, tmp_path)
    _insert(db, deployment_id="dep_old", status="cancel_requested", cancel=True, lease=time.time() - 1, owner="executor-dead")
    plan = {"ok": True, "ready": True, "current_release_id": "rel_current", "target_release_id": "rel_new"}
    monkeypatch.setattr(service, "plan_test_deployment", lambda *args, **kwargs: plan)
    created = service.start_test_deployment(service.REPOSITORY, service.ENVIRONMENT, SHA, "ci", confirm=True)
    assert created["ok"] is True
    db.execute("UPDATE deployments SET status='claimed',current_step='claimed',claim_owner='executor-live',claim_token_hash='x',heartbeat_at=?,lease_expires_at=? WHERE deployment_id=?",
               (time.time(), time.time() + 60, created["deployment_id"]))
    blocked = service.start_test_deployment(service.REPOSITORY, service.ENVIRONMENT, "c" * 40, "ci2", confirm=True)
    assert blocked["error"]["code"] == "DEPLOYMENT_ALREADY_ACTIVE"


def test_audit_records_reconciliation_reason_without_claim_token(monkeypatch, tmp_path):
    db, _ = _setup(monkeypatch, tmp_path)
    _insert(db, status="cancel_requested", cancel=True, lease=time.time() - 1, owner="executor-dead")
    service.reconcile_stale_test_deployment("dep_test")
    audit = service.get_test_deployment_audit("dep_test")
    assert audit["items"][0]["reason_code"] == "DEPLOYMENT_CANCELED_STALE_CLAIM"
    assert "token" not in json.dumps(audit).lower()


def test_sqlite_wal_busy_timeout_and_short_autocommit(monkeypatch, tmp_path):
    db, _ = _setup(monkeypatch, tmp_path)
    _insert(db)
    assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 2500
    assert db.isolation_level is None
    lock_db = sqlite3.connect(tmp_path / "deployments.db", isolation_level=None, check_same_thread=False)
    lock_db.execute("PRAGMA busy_timeout=100")
    lock_db.execute("BEGIN IMMEDIATE")
    releaser = threading.Thread(target=lambda: (time.sleep(0.1), lock_db.rollback()))
    releaser.start()
    claimed = service.claim_delegated_test_deployment("executor-after-lock")
    releaser.join()
    assert claimed["deployment"]["claim_owner"] == "executor-after-lock"
    assert db.in_transaction is False
    lock_db.close()
