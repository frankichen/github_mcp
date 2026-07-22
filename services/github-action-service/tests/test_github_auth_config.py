import pytest

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
