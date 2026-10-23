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
    second = manual.reconcile_manual_release({**proof, "expected_current_release_id": RID})
    assert second["ok"] and second["idempotent"] is True
    assert second["deployment_id"] == first["deployment_id"]
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
