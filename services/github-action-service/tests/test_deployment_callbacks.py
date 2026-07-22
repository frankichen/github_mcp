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
