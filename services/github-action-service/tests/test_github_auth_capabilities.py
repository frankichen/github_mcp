from types import SimpleNamespace

import pytest

from app import github_utils
from app.github_utils import _github_auth_capabilities


@pytest.mark.parametrize(
    "credential_type",
    [
        "fine_grained_pat",
        "github_app_installation",
        "github_app_user_access_token",
        "other_or_unknown",
    ],
)
def test_non_scope_credentials_do_not_report_missing_repo_scope_as_write_denial(credential_type):
    capabilities = _github_auth_capabilities(credential_type, [], 403, 200)

    assert capabilities["checks_supported"] is False
    assert capabilities["statuses_supported"] is True
    assert capabilities["contents_write_supported"] is None
    assert capabilities["pull_requests_write_supported"] is None
    assert capabilities["write_capability_source"] == "not_inferred_from_oauth_scopes"


def test_scope_based_credentials_report_repo_scope_result():
    with_repo_scope = _github_auth_capabilities("classic_pat", ["repo"], 200, 200)
    without_repo_scope = _github_auth_capabilities("classic_pat", [], 200, 200)

    assert with_repo_scope["contents_write_supported"] is True
    assert with_repo_scope["pull_requests_write_supported"] is True
    assert with_repo_scope["write_capability_source"] == "oauth_scopes"
    assert without_repo_scope["contents_write_supported"] is False
    assert without_repo_scope["pull_requests_write_supported"] is False


def test_fine_grained_checks_403_does_not_claim_contents_or_pr_write_is_denied(monkeypatch):
    commit = SimpleNamespace(sha="a" * 40)
    pull = SimpleNamespace(head=commit, base=SimpleNamespace(ref="main"))
    repo = SimpleNamespace(get_pull=lambda _: pull)
    monkeypatch.setattr(
        github_utils,
        "_get_gh",
        lambda: SimpleNamespace(get_repo=lambda _: repo),
    )
    monkeypatch.setattr(github_utils.credential_provider, "token", lambda: "")
    monkeypatch.setattr(
        github_utils, "_classify_github_credential_type", lambda _: "fine_grained_pat"
    )
    monkeypatch.setattr(github_utils.settings, "GITHUB_AUTH_MODE", "fine_grained_pat")
    monkeypatch.setattr(
        github_utils,
        "_github_get_json",
        lambda path: (
            (403, {}, {"accepted_github_permissions": "checks=read"})
            if path.endswith("check-runs")
            else (200, [], {"accepted_github_permissions": "statuses=read"})
        ),
    )
    monkeypatch.setattr(github_utils, "_repository_merge_policy", lambda _: {})
    monkeypatch.setattr(
        github_utils,
        "_required_check_sources",
        lambda *args: {"contexts": [], "checks": [], "sources": []},
    )

    result = github_utils.get_github_pull_request_checks("owner/repo", 1)

    assert result["checks_error_code"] == "CHECKS_PERMISSION_DENIED"
    assert result["diagnostic_code"] == "FINE_GRAINED_PAT_CHECKS_UNAVAILABLE"
    assert result["auth_capabilities"]["contents_write_supported"] is None
    assert result["auth_capabilities"]["pull_requests_write_supported"] is None
