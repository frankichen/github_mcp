from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from app import github_utils


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("github_pat_" + "x" * 20, "fine_grained_pat"),
        ("ghp_" + "x" * 20, "classic_pat"),
        ("ghs_" + "x" * 20, "github_app_installation"),
        ("ghu_" + "x" * 20, "github_app_user_access_token"),
        ("gho_" + "x" * 20, "oauth_token"),
    ],
)
def test_github_credential_type_classification_does_not_return_token(token, expected):
    assert github_utils._classify_github_credential_type(token) == expected


@pytest.mark.parametrize(
    ("status", "remaining", "expected"),
    [
        (401, None, "CHECKS_AUTHENTICATION_FAILED"),
        (403, "10", "CHECKS_PERMISSION_DENIED"),
        (403, "0", "CHECKS_RATE_LIMITED"),
        (429, "10", "CHECKS_RATE_LIMITED"),
        (404, "10", "CHECKS_NOT_FOUND"),
        (500, "10", "CHECKS_API_FAILED"),
    ],
)
def test_checks_error_classification(status, remaining, expected):
    headers = {"X-RateLimit-Remaining": remaining} if remaining is not None else {}
    assert github_utils._github_endpoint_error_code("CHECKS", status, headers) == expected


def test_pull_request_checks_distinguishes_empty_checks_and_statuses(monkeypatch):
    class FakeCommit:
        sha = "a" * 40

    class FakePull:
        head = FakeCommit()

    class FakeRepo:
        def get_pull(self, pull_number):
            assert pull_number == 540
            return FakePull()

    monkeypatch.setattr(github_utils, "_get_gh", lambda: SimpleNamespace(get_repo=lambda _: FakeRepo()))
    monkeypatch.setattr(
        github_utils,
        "_github_get_json",
        lambda path: (
            (200, {"check_runs": []}, {"github_request_id": "checks-empty"})
            if path.endswith("check-runs")
            else (200, [], {"github_request_id": "statuses-empty"})
        ),
    )

    result = github_utils.get_github_pull_request_checks("frankichen/sxt", 540)

    assert result["checks_result_code"] == "CHECKS_EMPTY"
    assert result["statuses_result_code"] == "STATUSES_EMPTY"
    assert result["checks_error_code"] is None
    assert result["statuses_error_code"] is None
    assert result["overall_conclusion"] == "neutral"


def test_pull_request_checks_permission_denied_is_not_empty_or_passed(monkeypatch):
    class FakeCommit:
        sha = "b" * 40

    class FakePull:
        head = FakeCommit()

    class FakeRepo:
        def get_pull(self, pull_number):
            return FakePull()

    monkeypatch.setattr(github_utils, "_get_gh", lambda: SimpleNamespace(get_repo=lambda _: FakeRepo()))
    monkeypatch.setattr(
        github_utils,
        "_github_get_json",
        lambda path: (
            (403, {"message": "forbidden"}, {"X-RateLimit-Remaining": "10"})
            if path.endswith("check-runs")
            else (200, [], {})
        ),
    )

    result = github_utils.get_github_pull_request_checks("frankichen/sxt", 540)

    assert result["checks_error_code"] == "CHECKS_PERMISSION_DENIED"
    assert result["checks_result_code"] == "CHECKS_PERMISSION_DENIED"
    assert result["overall_status"] == "unavailable"
    assert result["overall_conclusion"] is None


def test_classic_pat_with_repo_scope_reports_capabilities(monkeypatch):
    class FakeCommit:
        sha = "c" * 40

    class FakePull:
        head = FakeCommit()

    class FakeRepo:
        def get_pull(self, pull_number):
            return FakePull()

    monkeypatch.setattr(github_utils, "_get_gh", lambda: SimpleNamespace(get_repo=lambda _: FakeRepo()))
    monkeypatch.setattr(github_utils.settings, "GITHUB_AUTH_MODE", "classic_pat")
    monkeypatch.setattr(github_utils.settings, "GITHUB_TOKEN", SecretStr("ghp_" + "x" * 20))
    monkeypatch.setattr(
        github_utils,
        "_github_get_json",
        lambda path: (
            (200, {"check_runs": []}, {"oauth_scopes": "repo"})
            if path.endswith("check-runs")
            else (200, [], {"oauth_scopes": "repo"})
        ),
    )

    result = github_utils.get_github_pull_request_checks("frankichen/sxt", 540)

    assert result["checks_result_code"] == "CHECKS_EMPTY"
    assert result["credential_type"] == "classic_pat"
    assert result["auth_capabilities"]["checks_supported"] is True
    assert result["auth_capabilities"]["contents_write_supported"] is True


def test_classic_pat_permission_diagnostic_requires_repo_scope(monkeypatch):
    class FakeCommit:
        sha = "d" * 40

    class FakePull:
        head = FakeCommit()

    class FakeRepo:
        def get_pull(self, pull_number):
            return FakePull()

    monkeypatch.setattr(github_utils, "_get_gh", lambda: SimpleNamespace(get_repo=lambda _: FakeRepo()))
    monkeypatch.setattr(github_utils.settings, "GITHUB_AUTH_MODE", "classic_pat")
    monkeypatch.setattr(github_utils.settings, "GITHUB_TOKEN", SecretStr("ghp_" + "x" * 20))
    monkeypatch.setattr(
        github_utils,
        "_github_get_json",
        lambda path: (
            (403, {"message": "forbidden"}, {"oauth_scopes": "user"})
            if path.endswith("check-runs")
            else (200, [], {"oauth_scopes": "user"})
        ),
    )

    result = github_utils.get_github_pull_request_checks("frankichen/sxt", 540)

    assert result["diagnostic_code"] == "CLASSIC_PAT_REPO_SCOPE_REQUIRED"


@pytest.mark.parametrize(
    ("status", "remaining", "expected"),
    [
        (401, "10", "CHECKS_AUTHENTICATION_FAILED"),
        (403, "0", "CHECKS_RATE_LIMITED"),
        (404, "10", "CHECKS_NOT_FOUND"),
        (429, "10", "CHECKS_RATE_LIMITED"),
        (500, "10", "CHECKS_API_FAILED"),
    ],
)
def test_checks_endpoint_errors_are_not_reported_as_empty(monkeypatch, status, remaining, expected):
    class FakeCommit:
        sha = "e" * 40

    class FakePull:
        head = FakeCommit()

    class FakeRepo:
        def get_pull(self, pull_number):
            return FakePull()

    monkeypatch.setattr(github_utils, "_get_gh", lambda: SimpleNamespace(get_repo=lambda _: FakeRepo()))
    monkeypatch.setattr(github_utils.settings, "GITHUB_TOKEN", SecretStr("ghp_" + "x" * 20))
    monkeypatch.setattr(
        github_utils,
        "_github_get_json",
        lambda path: (
            (status, {"message": "failure"}, {"rate_remaining": remaining})
            if path.endswith("check-runs")
            else (200, [], {"oauth_scopes": "repo"})
        ),
    )

    result = github_utils.get_github_pull_request_checks("frankichen/sxt", 540)

    assert result["checks_result_code"] == expected
    assert result["checks"] == []
    assert result["overall_conclusion"] is None


def test_checks_records_are_available(monkeypatch):
    class FakeCommit:
        sha = "f" * 40

    class FakePull:
        head = FakeCommit()

    class FakeRepo:
        def get_pull(self, pull_number):
            return FakePull()

    monkeypatch.setattr(github_utils, "_get_gh", lambda: SimpleNamespace(get_repo=lambda _: FakeRepo()))
    monkeypatch.setattr(github_utils.settings, "GITHUB_TOKEN", SecretStr("ghp_" + "x" * 20))
    monkeypatch.setattr(
        github_utils,
        "_github_get_json",
        lambda path: (
            (200, {"check_runs": [{"name": "ci", "status": "completed", "conclusion": "success"}]}, {"oauth_scopes": "repo"})
            if path.endswith("check-runs")
            else (200, [], {"oauth_scopes": "repo"})
        ),
    )

    result = github_utils.get_github_pull_request_checks("frankichen/sxt", 540)

    assert result["checks_result_code"] == "CHECKS_AVAILABLE"
    assert result["checks"][0]["name"] == "ci"
    assert result["checks"][0]["classification"] == "PASS"
    assert result["checks"][0]["is_required"] is False
    assert result["overall_conclusion"] == "success"
