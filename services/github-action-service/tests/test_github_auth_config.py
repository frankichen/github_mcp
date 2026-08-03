import pytest
from pydantic import SecretStr

from app.config import Settings


def _settings(**values):
    values.setdefault("_env_file", None)
    return Settings(**values)


def test_token_file_has_priority_and_is_trimmed(tmp_path, monkeypatch):
    token_file = tmp_path / "github_classic_pat"
    token_file.write_text("ghp_test-token\n", encoding="utf-8")
    token_file.chmod(0o600)
    monkeypatch.setenv("GITHUB_TOKEN", "legacy-value")

    settings = _settings(
        GITHUB_AUTH_MODE="classic_pat",
        GITHUB_TOKEN_FILE=str(token_file),
    )

    assert settings.GITHUB_TOKEN.get_secret_value() == "ghp_test-token"


def test_missing_token_file_rejected(tmp_path):
    with pytest.raises(ValueError, match="not readable"):
        _settings(GITHUB_AUTH_MODE="classic_pat", GITHUB_TOKEN_FILE=str(tmp_path / "missing"))


def test_token_file_permissions_must_be_0600_or_stricter(tmp_path):
    token_file = tmp_path / "github_classic_pat"
    token_file.write_text("ghp_test-token", encoding="utf-8")
    token_file.chmod(0o640)

    with pytest.raises(ValueError, match="permissions"):
        _settings(GITHUB_AUTH_MODE="classic_pat", GITHUB_TOKEN_FILE=str(token_file))


def test_empty_token_file_rejected(tmp_path):
    token_file = tmp_path / "github_classic_pat"
    token_file.write_text("\n", encoding="utf-8")
    token_file.chmod(0o600)

    with pytest.raises(ValueError, match="empty"):
        _settings(GITHUB_AUTH_MODE="classic_pat", GITHUB_TOKEN_FILE=str(token_file))


def test_classic_pat_without_any_secret_is_rejected(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN_FILE", "")
    with pytest.raises(ValueError, match="requires"):
        _settings(GITHUB_AUTH_MODE="classic_pat")


def test_github_api_request_limits_are_bounded():
    configured = _settings(GITHUB_TOKEN="test-token")

    assert configured.GITHUB_API_TIMEOUT_SECONDS == 10
    assert configured.GITHUB_API_RETRY_TOTAL == 1


def test_github_api_request_limits_reject_unbounded_values():
    with pytest.raises(ValueError):
        _settings(GITHUB_API_TIMEOUT_SECONDS=0)
    with pytest.raises(ValueError):
        _settings(GITHUB_API_RETRY_TOTAL=4)


def test_github_client_uses_bounded_request_policy(monkeypatch):
    from app.github_auth import GitHubCredentialProvider

    monkeypatch.setattr("app.github_auth.settings.GITHUB_AUTH_MODE", "legacy")
    monkeypatch.setattr("app.github_auth.settings.GITHUB_TOKEN", SecretStr("test-token"))
    captured = {}

    def fake_github(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("app.github_auth.Github", fake_github)

    GitHubCredentialProvider().github()

    assert captured["timeout"] == 10
    assert captured["retry"].total == 1
    assert captured["retry"].connect == 1
    assert captured["retry"].read == 1
