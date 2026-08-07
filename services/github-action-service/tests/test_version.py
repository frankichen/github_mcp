import pytest

from app import version


def test_runtime_build_sha_requires_full_commit_in_deployed_modes(monkeypatch):
    monkeypatch.setenv("MYGITHUB12_RUNTIME_MODE", "production")
    monkeypatch.setenv("MYGITHUB12_BUILD_SHA", "not-a-commit")
    with pytest.raises(RuntimeError, match="40-character Git commit SHA"):
        version.runtime_build_sha()


def test_runtime_build_sha_accepts_full_lowercase_commit(monkeypatch):
    expected = "a" * 40
    monkeypatch.setenv("MYGITHUB12_RUNTIME_MODE", "production")
    monkeypatch.setenv("MYGITHUB12_BUILD_SHA", expected)
    assert version.runtime_build_sha() == expected


def test_runtime_version_must_match_authoritative_version(monkeypatch):
    monkeypatch.setenv("MYGITHUB12_RUNTIME_MODE", "production")
    monkeypatch.setenv("MYGITHUB12_BUILD_SHA", "a" * 40)
    monkeypatch.setenv("MYGITHUB12_VERSION", "11.9.9")
    with pytest.raises(RuntimeError, match=version.SERVICE_VERSION):
        version.validate_runtime_metadata()


def test_authoritative_version_is_1201():
    assert version.SERVICE_NAME == "MyGithut12"
    assert version.SERVICE_VERSION == "12.0.1"
