from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.config import settings
from app.github_auth import GitHubCredentialProvider
from app.github_extended import GitHubExtendedService
from app.mcp_server import _github_call
from app import github_utils


class Response:
    def __init__(self, status=200, payload=None, headers=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {"X-GitHub-Request-Id": "request-1"}
        self.text = ""

    def json(self):
        return self._payload


def test_extended_read_enforces_repository_allowlist(monkeypatch):
    monkeypatch.setattr(settings, "ALLOWED_REPOSITORIES", "owner/allowed")
    service = GitHubExtendedService()
    with pytest.raises(Exception) as exc:
        service.list_issues("owner/denied")
    assert exc.value.error == "repository_not_allowed"


def test_get_github_branch_avoids_redundant_base_branch_request(monkeypatch):
    head_sha = "a" * 40
    branch = SimpleNamespace(
        name="feature",
        protected=False,
        commit=SimpleNamespace(sha=head_sha, html_url="https://github.com/owner/allowed/commit/" + head_sha),
    )
    comparison = SimpleNamespace(ahead_by=7, behind_by=2)
    repo = MagicMock()
    repo.get_branch.return_value = branch
    repo.compare.return_value = comparison
    gh = MagicMock()
    gh.get_repo.return_value = repo
    monkeypatch.setattr(github_utils, "_get_gh", lambda: gh)

    result = github_utils.get_github_branch("owner/allowed", "feature", "main")

    assert result["ok"] is True
    assert result["commit_sha"] == head_sha
    assert result["ahead_by"] == 7
    assert result["behind_by"] == 2
    gh.get_repo.assert_called_once_with("owner/allowed", lazy=True)
    repo.get_branch.assert_called_once_with("feature")
    repo.compare.assert_called_once_with("main", head_sha)


@pytest.mark.asyncio
async def test_legacy_advanced_tools_share_allowlist(monkeypatch):
    monkeypatch.setattr(settings, "ALLOWED_REPOSITORIES", "owner/allowed")
    with pytest.raises(Exception) as exc:
        await _github_call(github_utils.get_github_repository, "owner/denied")
    assert exc.value.error == "repository_not_allowed"

    with pytest.raises(Exception) as exc:
        await _github_call(
            github_utils.search_github_pull_request_history,
            ["owner/allowed", "owner/denied"],
            "someone",
        )
    assert exc.value.error == "repository_not_allowed"


def test_consequential_operation_requires_confirmation(monkeypatch):
    monkeypatch.setattr(settings, "ALLOWED_REPOSITORIES", "owner/allowed")
    result = GitHubExtendedService().create_issue("owner/allowed", "Title")
    assert result["ok"] is False
    assert result["error"]["code"] == "CONFIRMATION_REQUIRED"


def test_issue_creation_is_bounded_and_authorized(monkeypatch):
    monkeypatch.setattr(settings, "ALLOWED_REPOSITORIES", "owner/allowed")
    monkeypatch.setattr(
        "app.github_extended.credential_provider.token", lambda: "redacted-token"
    )
    seen = {}

    def request(method, url, **kwargs):
        seen.update({"method": method, "url": url, **kwargs})
        return Response(201, {"number": 7, "title": "Title"})

    monkeypatch.setattr("app.github_extended.requests.request", request)
    result = GitHubExtendedService().create_issue(
        "owner/allowed", "Title", labels=["bug"], confirm=True
    )
    assert result["ok"] is True
    assert seen["url"].endswith("/repos/owner/allowed/issues")
    assert seen["json"]["labels"] == ["bug"]
    assert seen["headers"]["Authorization"] == "Bearer redacted-token"


def test_download_returns_redirect_without_following_it(monkeypatch):
    monkeypatch.setattr(settings, "ALLOWED_REPOSITORIES", "owner/allowed")
    monkeypatch.setattr(
        "app.github_extended.credential_provider.token", lambda: "redacted-token"
    )

    def request(_method, _url, **kwargs):
        assert kwargs["allow_redirects"] is False
        return Response(302, None, {"Location": "https://objects.example/artifact"})

    monkeypatch.setattr("app.github_extended.requests.request", request)
    result = GitHubExtendedService().download_artifact("owner/allowed", 12)
    assert result == {
        "ok": True,
        "download_url": "https://objects.example/artifact",
        "github_request_id": None,
    }


def test_global_notifications_are_filtered_to_allowed_repositories(monkeypatch):
    monkeypatch.setattr(settings, "ALLOWED_REPOSITORIES", "owner/allowed")
    monkeypatch.setattr(
        "app.github_extended.credential_provider.token", lambda: "redacted-token"
    )

    monkeypatch.setattr(
        "app.github_extended.requests.request",
        lambda *_args, **_kwargs: Response(200, [
            {"id": "1", "repository": {"full_name": "owner/allowed"}},
            {"id": "2", "repository": {"full_name": "owner/denied"}},
        ]),
    )
    result = GitHubExtendedService().list_notifications()
    assert [item["id"] for item in result["data"]] == ["1"]
    assert result["filtered_by_repository_policy"] is True


def test_github_app_token_is_cached_and_refreshable(monkeypatch, tmp_path):
    key = tmp_path / "app.pem"
    key.write_text("private-key", encoding="utf-8")
    key.chmod(0o600)
    monkeypatch.setattr(settings, "GITHUB_AUTH_MODE", "github_app")
    monkeypatch.setattr(settings, "GITHUB_APP_ID", 1)
    monkeypatch.setattr(settings, "GITHUB_APP_INSTALLATION_ID", 2)
    monkeypatch.setattr(settings, "GITHUB_APP_PRIVATE_KEY_FILE", str(key))

    issued = []

    class Integration:
        def __init__(self, **_kwargs):
            pass

        def get_access_token(self, installation_id):
            issued.append(installation_id)
            return SimpleNamespace(
                token=f"installation-{len(issued)}",
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )

    monkeypatch.setattr("app.github_auth.GithubIntegration", Integration)
    monkeypatch.setattr("app.github_auth.Auth.AppAuth", lambda *_args: object())
    provider = GitHubCredentialProvider()
    assert provider.token() == "installation-1"
    assert provider.token() == "installation-1"
    assert provider.refresh()["expires_at"]
    assert provider.token() == "installation-2"
    assert issued == [2, 2]
