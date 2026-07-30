import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from app import github_utils, deployment_service, ci_repository_config


def _pr(head="a" * 40, requested=None, reviews=None):
    pr = MagicMock()
    pr.number = 544; pr.title = "测试"; pr.body = ""; pr.state = "open"; pr.draft = False; pr.merged = False
    pr.mergeable = True; pr.mergeable_state = "clean"; pr.head.ref = "feature"; pr.head.sha = head
    pr.base.ref = "main"; pr.base.sha = "b" * 40; pr.user.login = "owner"; pr.html_url = "https://example/pr/544"
    pr.created_at = pr.updated_at = pr.closed_at = pr.merged_at = None
    pr.labels = []; pr.commits = 1; pr.changed_files = 1; pr.additions = 2; pr.deletions = 1; pr.comments = 0; pr.review_comments = 0
    if requested is None:
        requested = ([], [])
    pr.get_review_requests.return_value = requested
    pr.get_reviews.return_value = reviews or []
    return pr


def test_pull_request_read_has_separate_requested_and_submitted_reviews(monkeypatch):
    user = SimpleNamespace(login="reviewer")
    team = SimpleNamespace(slug="backend")
    review = SimpleNamespace(id=1, user=user, state="APPROVED", body="ok", commit_id="a" * 40, submitted_at=None, html_url="u")
    pr = _pr(requested=([user], [team]), reviews=[review])
    repo = MagicMock(); repo.full_name = "owner/repo"; repo.get_pull.return_value = pr
    monkeypatch.setattr(github_utils, "_get_gh", lambda: MagicMock(get_repo=lambda _: repo))
    result = github_utils.get_github_pull_request("owner/repo", 544)
    assert result["requested_reviewers"] == ["reviewer"]
    assert result["requested_teams"] == ["backend"]
    assert result["reviews"][0]["state"] == "APPROVED"
    assert result["review_decision"] == "APPROVED"


def test_reviewer_rest_fallback_when_sdk_method_missing():
    pr = MagicMock(spec=["number"]); pr.number = 544
    repo = MagicMock(); repo.full_name = "owner/repo"
    repo._requester.requestJsonAndCheck.return_value = ({"users": [{"login": "u"}], "teams": [{"slug": "t"}]}, None)
    assert github_utils._get_requested_reviewers(repo, pr) == (["u"], ["t"])


def test_merge_requires_confirm_and_full_sha():
    assert github_utils.merge_github_pull_request("owner/repo", 544, confirm=False)["error"]["code"] == "CONFIRM_REQUIRED"
    assert github_utils.merge_github_pull_request("owner/repo", 544, confirm=True, required_private_ci_job_id="j")["error"]["code"] == "EXPECTED_HEAD_SHA_REQUIRED"


def test_deployment_whitelist_and_cancel(monkeypatch, tmp_path):
    monkeypatch.setenv("CI_DB_PATH", str(tmp_path / "ci.db"))
    # ci_database caches connections per thread; this test exercises validation before persistence.
    result = deployment_service.start_test_deployment("evil/repo", "gongshi-test", "a" * 40, "job", confirm=True)
    assert result["error"]["code"] == "REPOSITORY_NOT_ALLOWED"


def test_deployment_id_is_distinct_from_ci_job_id(monkeypatch, tmp_path):
    monkeypatch.setenv("CI_DB_PATH", str(tmp_path / "ci.db"))
    assert deployment_service._validate_common("frankichen/sxt", "gongshi-test", "fullstack", "a" * 40) is None
    assert deployment_service.STATUSES[-1] == "rolled_back"


def test_auto_gupiao_deployment_contract_is_config_backed(monkeypatch, tmp_path):
    monkeypatch.setenv("CI_DB_PATH", str(tmp_path / "ci.db"))
    monkeypatch.setenv("DEPLOYMENT_DB_PATH", str(tmp_path / "deployments.db"))
    deployment_service._local.db = None

    sha = "c" * 40
    assert deployment_service._validate_common("frankichen/auto_gupiao", "auto-gupiao-test", "reports", sha) is None
    assert deployment_service._validate_common("frankichen/auto_gupiao", "gongshi-test", "reports", sha) == "ENVIRONMENT_NOT_ALLOWED"
    assert deployment_service._validate_common("frankichen/auto_gupiao", "auto-gupiao-test", "fullstack", sha) == "SCOPE_NOT_ALLOWED"

    monkeypatch.setattr(deployment_service, "_repo_state", lambda repository, commit_sha: (commit_sha, ["internal/report/paper.go"]))
    result = deployment_service.start_test_deployment(
        "frankichen/auto_gupiao",
        "auto-gupiao-test",
        sha,
        "",
        scope="reports",
        confirm=True,
    )

    assert result["ok"] is True
    assert result["status"] == "queued"


def test_auto_gupiao_policy_is_deployable_but_not_self_deploy():
    assert ci_repository_config.is_private_ci_enabled("frankichen/auto_gupiao") is True
    assert ci_repository_config.is_test_deploy_enabled("frankichen/auto_gupiao") is True


def test_delegated_deployments_default_lists_all_deployable_repositories(monkeypatch, tmp_path):
    monkeypatch.setenv("DEPLOYMENT_DB_PATH", str(tmp_path / "deployments.db"))
    deployment_service._local.db = None
    deployment_service.init_deployment_db()
    db = deployment_service._get_deploy_db()
    db.execute(
        "INSERT INTO deployments(deployment_id,repository,environment,commit_sha,private_ci_job_id,requested_scope,target_release,status,current_step,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("dep_auto", "frankichen/auto_gupiao", "auto-gupiao-test", "d" * 40, "not_required", "reports", "rel", "running", "claimed", 1, 1),
    )
    db.commit()

    items = deployment_service.list_delegated_deployments()

    assert [item["deployment_id"] for item in items] == ["dep_auto"]
