"""Manual SXT releases must never silently promote an unverified runtime."""

from datetime import datetime, timezone

from app import manual_release_reconciliation as manual

SHA = "2" * 40
RID = "20261023-123755-" + SHA[:12]


def _evidence():
    return {
        "repository": "frankichen/sxt", "environment": "gongshi-test",
        "git_sha": SHA, "release_id": RID,
        "current_release_path": "/home/dly/releases/" + RID,
        "private_ci_job_id": "pass-main-job", "expected_current_release_id": "old",
        "observed_at_utc": datetime.now(timezone.utc).isoformat(),
        "manifest_verified": True, "checksum_verified": True,
        "health_verified": True, "services_healthy": True,
        "frontend_included": True,
    }


def _setup(monkeypatch, tmp_path):
    monkeypatch.setenv("DEPLOYMENT_DB_PATH", str(tmp_path / "deployments.db"))
    monkeypatch.setenv("DEPLOY_STATUS_FILE", str(tmp_path / "status.json"))
    manual.ds._local.db = None
    monkeypatch.setattr(manual, "_eligible_sha", lambda sha, job_id="": (None, "pass-main-job"))
    manual.ds.init_deployment_db()
    # A verified predecessor must be present for the expected-current CAS.
    current = {
        "current_release_id": "old", "previous_release_id": None,
        "current_release_path": "/home/dly/releases/old", "current_git_sha": "1" * 40,
        "manifest_verified": True, "checksum_verified": True, "health_verified": True,
        "releases": [{
            "release_id": "old", "repository": "frankichen/sxt",
            "environment": "gongshi-test", "git_sha": "1" * 40,
            "current_release_path": "/home/dly/releases/old",
            "manifest_verified": True, "checksum_verified": True,
            "health_verified": True, "services_healthy": True,
        }],
    }
    import json
    (tmp_path / "status.json").write_text(json.dumps(current), encoding="utf-8")


def test_preflight_requires_matching_scope_and_ci(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    assert manual.preflight_manual_release({
        "repository": "frankichen/sxt", "environment": "gongshi-test",
        "git_sha": SHA,
    })["expected_current_release_id"] == "old"
    assert manual.preflight_manual_release({
        "repository": "frankichen/devhub", "environment": "gongshi-test",
        "git_sha": SHA,
    })["error"]["code"] == "MANUAL_RELEASE_SCOPE_INVALID"


def test_registration_is_durable_idempotent_and_audited(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    proof = _evidence()
    first = manual.reconcile_manual_release(proof)
    assert first["ok"] and first["idempotent"] is False
    assert manual._registered_current() == RID
    # Retry with the SAME ticket if the success HTTP response was lost.
    second = manual.reconcile_manual_release(proof)
    assert second["ok"] and second["idempotent"] is True
    assert second["deployment_id"] == first["deployment_id"]
    snapshot = manual.ds._status_snapshot("frankichen/sxt")
    assert snapshot["previous_release_id"] == "old"
    assert snapshot["current_release_id"] == RID
    # A newer ticket is also idempotent without mutating history.
    third = manual.reconcile_manual_release({**proof, "expected_current_release_id": RID})
    assert third["ok"] and third["idempotent"] is True
    assert manual.ds._status_snapshot("frankichen/sxt")["previous_release_id"] == "old"
    row = manual.ds._get_deploy_db().execute(
        "SELECT requested_by, status, private_ci_job_id FROM deployments "
        "WHERE deployment_id=?", (first["deployment_id"],),
    ).fetchone()
    assert tuple(row) == ("manual-script", "passed", "pass-main-job")


def test_bad_evidence_does_not_change_registry(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    for field, value in [
        ("git_sha", "f" * 40), ("release_id", "fake"),
        ("current_release_path", "/tmp/bad"), ("health_verified", False),
        ("manifest_verified", "true"), ("frontend_included", "yes"),
        ("observed_at_utc", "2020-01-01T00:00:00Z"),
    ]:
        bad = {**_evidence(), field: value}
        assert manual.reconcile_manual_release(bad)["ok"] is False
    assert manual._registered_current() == "old"
    assert manual.ds._get_deploy_db().execute("SELECT COUNT(*) FROM deployments").fetchone()[0] == 0


def test_wrong_previous_release_cas_fails_closed(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    res = manual.reconcile_manual_release({**_evidence(), "expected_current_release_id": "other"})
    assert res["error"]["code"] == "DEPLOYMENT_RELEASE_DRIFT"
    assert manual._registered_current() == "old"


def test_registry_write_failure_keeps_previous_and_failed_audit(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    status_path = tmp_path / "status.json"
    previous = status_path.read_bytes()
    def fail_atomic_replace(*_args):
        raise OSError("simulated disk write failure")
    monkeypatch.setattr(manual.ds.os, "replace", fail_atomic_replace)
    res = manual.reconcile_manual_release(_evidence())
    assert res["error"]["code"] == "MANUAL_RELEASE_REGISTRY_WRITE_FAILED"
    assert status_path.read_bytes() == previous
    assert manual._registered_current() == "old"
    row = manual.ds._get_deploy_db().execute(
        "SELECT status, error_code FROM deployments ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    assert tuple(row) == ("failed", "MANUAL_RELEASE_REGISTRY_WRITE_FAILED")


def test_active_managed_deployment_blocks_manual_adoption(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    db = manual.ds._get_deploy_db()
    db.execute(
        "INSERT INTO deployments (deployment_id,repository,environment,commit_sha,"
        "private_ci_job_id,requested_scope,status,created_at,updated_at) "
        "VALUES ('dep_active','frankichen/sxt','gongshi-test',?,"
        "'job','fullstack','running',1,1)", (SHA,),
    )
    db.commit()
    assert manual.preflight_manual_release({
        "repository": "frankichen/sxt", "environment": "gongshi-test",
        "git_sha": SHA,
    })["error"]["code"] == "DEPLOYMENT_ALREADY_ACTIVE"
    assert manual.reconcile_manual_release(_evidence())["error"]["code"] == "DEPLOYMENT_ALREADY_ACTIVE"


def test_selected_private_ci_job_ignores_list_pagination(monkeypatch):
    calls = []
    monkeypatch.setattr(manual.ds, "_repo_state", lambda repo, sha: (sha, []))
    monkeypatch.setattr(manual.ds, "_ci_gate", lambda repo, job_id, sha: (None, {"job_id": job_id}))
    monkeypatch.setattr(manual, "list_jobs", lambda **kwargs: calls.append(kwargs) or [])
    reason, job_id = manual._eligible_sha(SHA, "old-but-still-valid-main-ci")
    assert reason is None and job_id == "old-but-still-valid-main-ci"
    assert calls == []
