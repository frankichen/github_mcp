from app import deployment_service as deployment


REPOSITORY = "frankichen/sxt"
ENVIRONMENT = "gongshi-test"
TARGET_SHA = "b" * 40
CURRENT_RELEASE = {
    "release_id": "rel_old",
    "git_sha": "a" * 40,
    "current_release_path": "/home/dly/releases/rel_old",
}


def _plan(monkeypatch, changed_files, *, compare_state="complete", allow_infra=False, expected_release_id=""):
    monkeypatch.setattr(deployment, "_repo_state", lambda *_: (TARGET_SHA, ["internal/modules/example/service.go"]))
    monkeypatch.setattr(deployment, "_ci_gate", lambda *_: (None, {"status": "passed", "profile": "repo-auto-check"}))
    monkeypatch.setattr(deployment, "_status_snapshot", lambda _repository: {"current_release_id": "rel_old"})
    monkeypatch.setattr(deployment, "_release_snapshot", lambda _snapshot: (CURRENT_RELEASE, []))
    monkeypatch.setattr(deployment, "_compare_changed_files", lambda *_: (changed_files, compare_state))
    return deployment.plan_test_deployment(
        REPOSITORY,
        ENVIRONMENT,
        TARGET_SHA,
        "ci-job-12345678",
        allow_deploy_infrastructure_changes=allow_infra,
        expected_current_release_id=expected_release_id,
    )


def test_backend_only_plan_omits_frontend_and_icon_checks(monkeypatch):
    plan = _plan(monkeypatch, ["internal/modules/device/service.go"])
    assert plan["ready"] is True
    assert plan["change_classification"] == "backend_only"
    assert plan["frontend_included"] is False
    assert plan["icon_checks_required"] is False
    assert plan["frontend_components"] == []
    assert plan["restart_services"] == ["api", "worker", "scheduler"]
    assert plan["health_endpoints"] == ["/public/v1/health"]


def test_frontend_change_includes_frontend_without_icon_smoke(monkeypatch):
    plan = _plan(monkeypatch, ["h5/lenshub-console-react/src/App.tsx"])
    assert plan["frontend_included"] is True
    assert plan["icon_checks_required"] is False
    assert plan["change_classification"] == "frontend"
    assert "web" in plan["restart_services"]


def test_icon_asset_change_includes_frontend_and_icon_smoke(monkeypatch):
    plan = _plan(monkeypatch, ["deploy/icon-assets/issue312/manifest.json"])
    assert plan["frontend_included"] is True
    assert plan["icon_checks_required"] is True
    assert plan["change_classification"] == "frontend_and_icons"


def test_openapi_contract_change_does_not_rebuild_unmodified_frontend(monkeypatch):
    plan = _plan(monkeypatch, ["api/openapi/app.yaml", "internal/modules/storage/service.go"])
    assert plan["ready"] is True
    assert plan["frontend_included"] is False
    assert plan["icon_checks_required"] is False
    assert plan["change_classification"] == "backend_only"


def test_icon_api_only_change_runs_icon_smoke_without_frontend_build(monkeypatch):
    plan = _plan(monkeypatch, ["internal/modules/appicons/catalog.go"])
    assert plan["frontend_included"] is False
    assert plan["icon_checks_required"] is True
    assert plan["change_classification"] == "icon_api"
    assert plan["frontend_components"] == []
    assert plan["restart_services"] == ["api", "worker", "scheduler"]


def test_incomplete_comparison_falls_back_to_fullstack_and_infra_gate(monkeypatch):
    plan = _plan(monkeypatch, None, compare_state="comparison_unavailable")
    assert plan["ready"] is False
    assert plan["frontend_included"] is True
    assert plan["icon_checks_required"] is True
    assert plan["migrations_changed"] is True
    assert plan["deploy_infrastructure_changed"] is True
    assert "DEPLOY_INFRASTRUCTURE_CHANGE_REQUIRES_EXPLICIT_ALLOW" in plan["reasons"]


def test_compare_changed_files_uses_complete_ancestor_comparison(monkeypatch):
    class Comparison:
        status = "ahead"
        ahead_by = 1
        behind_by = 0
        total_commits = 1
        files = [type("File", (), {"filename": "internal/modules/device/service.go"})()]

    class Repo:
        def compare(self, base, head):
            assert base == "a" * 40
            assert head == TARGET_SHA
            return Comparison()

    class GitHub:
        def get_repo(self, repository):
            assert repository == REPOSITORY
            return Repo()

    monkeypatch.setattr(deployment, "_get_gh", lambda: GitHub())
    files, state = deployment._compare_changed_files(REPOSITORY, "a" * 40, TARGET_SHA)
    assert state == "complete"
    assert files == ["internal/modules/device/service.go"]


def test_start_persists_backend_only_component_selection(monkeypatch, tmp_path):
    monkeypatch.setenv("DEPLOYMENT_DB_PATH", str(tmp_path / "deployments.db"))
    monkeypatch.setenv("DEPLOY_STATUS_FILE", str(tmp_path / "status.json"))
    deployment._local.db = None
    deployment.init_deployment_db()
    monkeypatch.setattr(
        deployment,
        "plan_test_deployment",
        lambda *args, **kwargs: {
            "ready": True,
            "current_release_id": "rel_old",
            "target_release_id": "20261120-test-" + TARGET_SHA[:12],
            "frontend_included": False,
            "icon_checks_required": False,
            "change_classification": "backend_only",
        },
    )
    result = deployment.start_test_deployment(
        REPOSITORY,
        ENVIRONMENT,
        TARGET_SHA,
        "ci-job-12345678",
        confirm=True,
    )
    assert result["ok"] is True
    assert result["frontend_included"] is False
    assert result["icon_checks_required"] is False
    row = deployment._get_deploy_db().execute(
        "SELECT frontend_included, icon_checks_required FROM deployments WHERE deployment_id=?",
        (result["deployment_id"],),
    ).fetchone()
    assert tuple(row) == (0, 0)


def test_renamed_frontend_path_is_not_misclassified_as_backend(monkeypatch):
    class Comparison:
        status = "ahead"
        ahead_by = 1
        behind_by = 0
        total_commits = 1
        files = [type("File", (), {
            "filename": "internal/modules/device/renamed.go",
            "previous_filename": "h5/lenshub-console/src/old.ts",
        })()]

    class Repo:
        def compare(self, base, head):
            return Comparison()

    class GitHub:
        def get_repo(self, repository):
            return Repo()

    monkeypatch.setattr(deployment, "_get_gh", lambda: GitHub())
    files, state = deployment._compare_changed_files(REPOSITORY, "a" * 40, TARGET_SHA)
    assert state == "complete"
    assert "h5/lenshub-console/src/old.ts" in files
    assert "internal/modules/device/renamed.go" in files
    assert deployment._is_frontend_change(files[0]) or deployment._is_frontend_change(files[1])


def test_deployment_plan_rejects_changed_current_release(monkeypatch):
    plan = _plan(
        monkeypatch,
        ["internal/modules/device/service.go"],
        expected_release_id="rel_changed_after_review",
    )
    assert plan["ready"] is False
    assert "CURRENT_RELEASE_CHANGED" in plan["reasons"]


def test_claimed_deployment_blocks_concurrent_queue(monkeypatch, tmp_path):
    monkeypatch.setenv("DEPLOYMENT_DB_PATH", str(tmp_path / "claimed-deployments.db"))
    monkeypatch.setenv("DEPLOY_STATUS_FILE", str(tmp_path / "claimed-status.json"))
    deployment._local.db = None
    deployment.init_deployment_db()
    monkeypatch.setattr(
        deployment,
        "plan_test_deployment",
        lambda *args, **kwargs: {
            "ready": True,
            "current_release_id": "rel_old",
            "target_release_id": "rel_new",
            "frontend_included": False,
            "icon_checks_required": False,
            "change_classification": "backend_only",
        },
    )
    first = deployment.start_test_deployment(
        REPOSITORY, ENVIRONMENT, TARGET_SHA, "ci-job-12345678", confirm=True
    )
    assert first["ok"] is True
    db = deployment._get_deploy_db()
    db.execute(
        "UPDATE deployments SET status='claimed' WHERE deployment_id=?",
        (first["deployment_id"],),
    )
    db.commit()
    second = deployment.start_test_deployment(
        REPOSITORY, ENVIRONMENT, TARGET_SHA, "ci-job-12345678", confirm=True
    )
    assert second["error"]["code"] == "DEPLOYMENT_ALREADY_ACTIVE"
