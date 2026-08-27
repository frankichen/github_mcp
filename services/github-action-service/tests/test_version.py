from pathlib import Path

import pytest

from app import version


def test_runtime_build_sha_requires_full_commit_in_deployed_modes(monkeypatch):
    monkeypatch.setenv("MYGITHUB12_RUNTIME_MODE", "production")
    monkeypatch.setenv("MYGITHUB12_BUILD_SHA", "not-a-commit")
    monkeypatch.setenv("CI_COMMIT_SHA", "b" * 40)
    with pytest.raises(RuntimeError, match="40-character Git commit SHA"):
        version.runtime_build_sha()


def test_runtime_build_sha_accepts_full_lowercase_commit(monkeypatch):
    expected = "a" * 40
    monkeypatch.setenv("MYGITHUB12_RUNTIME_MODE", "production")
    monkeypatch.setenv("MYGITHUB12_BUILD_SHA", expected)
    assert version.runtime_build_sha() == expected


def test_runtime_build_sha_accepts_ci_commit_in_development(monkeypatch):
    expected = "b" * 40
    monkeypatch.delenv("MYGITHUB12_BUILD_SHA", raising=False)
    monkeypatch.delenv("MYGITHUB10_BUILD_SHA", raising=False)
    monkeypatch.setenv("MYGITHUB12_RUNTIME_MODE", "development")
    monkeypatch.setenv("CI_COMMIT_SHA", expected)
    assert version.runtime_build_sha() == expected


def test_runtime_version_must_match_authoritative_version(monkeypatch):
    monkeypatch.setenv("MYGITHUB12_RUNTIME_MODE", "production")
    monkeypatch.setenv("MYGITHUB12_BUILD_SHA", "a" * 40)
    monkeypatch.setenv("MYGITHUB12_VERSION", "11.9.9")
    with pytest.raises(RuntimeError, match=version.SERVICE_VERSION):
        version.validate_runtime_metadata()


def test_authoritative_version_is_1234():
    assert version.SERVICE_NAME == "MyGithut12"
    assert version.SERVICE_VERSION == "12.3.4"


def test_controller_dockerfile_routes_apt_through_inherited_proxy():
    dockerfile = (Path(__file__).parents[1] / "Dockerfile").read_text(encoding="utf-8")

    assert "Acquire::http::Proxy=${http_apt_proxy}" in dockerfile
    assert "Acquire::https::Proxy=${https_apt_proxy}" in dockerfile
    assert "127.0.0.1:10808" not in dockerfile


def test_controller_dockerfile_keeps_dependency_layers_before_commit_metadata():
    dockerfile = (Path(__file__).parents[1] / "Dockerfile").read_text(encoding="utf-8")

    validation = dockerfile.index("RUN python -c")
    assert dockerfile.index("apt-get") < validation
    assert dockerfile.index("pip install") < validation
    assert dockerfile.index("LABEL org.opencontainers.image") > validation
