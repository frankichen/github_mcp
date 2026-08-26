import json
from pathlib import Path

import pytest
import yaml

from app import ci_repository_config as repo_policy


@pytest.mark.asyncio
async def test_artifact_write_gates_are_hard_disabled(monkeypatch):
    monkeypatch.delenv("MYGITHUB10_ARTIFACT_BUILD_ENABLED", raising=False)
    monkeypatch.delenv("MYGITHUB10_ARTIFACT_DEPLOY_ENABLED", raising=False)
    from app.mcp_server import build_release_artifact, plan_test_deployment, start_test_deployment
    assert json.loads(await build_release_artifact("frankichen/sxt", "c" * 40, "job", "att"))["error_code"] == "FEATURE_DISABLED"
    assert json.loads(await plan_test_deployment("frankichen/sxt", "gongshi-test", "c" * 40, "job", artifact_id="art"))["error_code"] == "FEATURE_DISABLED"
    assert json.loads(await start_test_deployment("frankichen/sxt", "gongshi-test", "c" * 40, "job", artifact_id="art"))["error_code"] == "FEATURE_DISABLED"


def _configure_policy(tmp_path, monkeypatch, repositories=None):
    path = tmp_path / "ci_repositories.yml"
    path.write_text(
        yaml.safe_dump(
            {
                "auto_enroll": {
                    "enabled": True,
                    "repository_patterns": ["frankichen/*"],
                    "defaults": {
                        "enabled": True,
                        "private_ci": True,
                        "auto_detect": True,
                        "allowed_profiles": [
                            "repo-auto-check",
                            "python-check",
                            "go-check",
                            "node-check",
                            "rust-check",
                            "maven-check",
                            "gradle-check",
                            "dotnet-check",
                        ],
                        "max_timeout_seconds": 900,
                    },
                },
                "repositories": repositories or {},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(repo_policy, "CONFIG_PATH", str(path))
    monkeypatch.setattr(repo_policy, "_config_cache", None)
    monkeypatch.setattr(repo_policy, "github_repository_is_allowed", lambda repository: repository.startswith("frankichen/"))


def test_auto_enrollment_enables_private_ci_but_not_deployment(tmp_path, monkeypatch):
    _configure_policy(tmp_path, monkeypatch)

    repository = "frankichen/new-project"
    assert repo_policy.get_repository_policy_source(repository) == "auto"
    assert repo_policy.is_repository_allowed(repository) is True
    assert repo_policy.is_private_ci_enabled(repository) is True
    assert set(repo_policy.get_allowed_profiles(repository)) == {
        "repo-auto-check",
        "python-check",
        "go-check",
        "node-check",
        "rust-check",
        "maven-check",
        "gradle-check",
        "dotnet-check",
    }
    assert repo_policy.is_test_deploy_enabled(repository) is False
    assert repo_policy.is_self_deploy_enabled(repository) is False
    assert repo_policy.get_deployment_config(repository) == {}


def test_checked_in_auto_enrollment_includes_common_stacks_but_not_openapi():
    expected = {"rust-check", "maven-check", "gradle-check", "dotnet-check"}
    assert expected.issubset(set(repo_policy._DEFAULT_AUTO_PROFILES))
    assert "openapi-check" not in repo_policy._DEFAULT_AUTO_PROFILES

    path = Path(__file__).parents[1] / "config" / "ci_repositories.yml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    profiles = set(data["auto_enroll"]["defaults"]["allowed_profiles"])
    assert expected.issubset(profiles)
    assert "openapi-check" not in profiles


def test_explicit_repository_entry_overrides_auto_enrollment(tmp_path, monkeypatch):
    _configure_policy(
        tmp_path,
        monkeypatch,
        repositories={
            "frankichen/disabled": {
                "enabled": False,
                "private_ci": False,
                "allowed_profiles": [],
            }
        },
    )

    repository = "frankichen/disabled"
    assert repo_policy.get_repository_policy_source(repository) == "explicit"
    assert repo_policy.is_repository_allowed(repository) is False
    assert repo_policy.is_private_ci_enabled(repository) is False


def test_auto_enrollment_respects_global_github_repository_policy(tmp_path, monkeypatch):
    _configure_policy(tmp_path, monkeypatch)
    monkeypatch.setattr(repo_policy, "github_repository_is_allowed", lambda repository: False)

    assert repo_policy.get_repository_policy_source("frankichen/not-allowed") == "none"
    assert repo_policy.is_repository_allowed("frankichen/not-allowed") is False
    assert repo_policy.is_private_ci_enabled("frankichen/not-allowed") is False


def test_github_mcp_explicit_infrastructure_deploy_contract(tmp_path, monkeypatch):
    _configure_policy(
        tmp_path,
        monkeypatch,
        repositories={
            "frankichen/github_mcp": {
                "enabled": True,
                "private_ci": True,
                "auto_detect": True,
                "allowed_profiles": ["repo-auto-check", "repo-fast-check", "python-check"],
                "max_timeout_seconds": 900,
                "infrastructure_deployment": {
                    "enabled": True,
                    "environment": "mygithub12-production",
                    "scope": "control-plane",
                    "private_ci": True,
                    "profile": "repo-auto-check",
                    "executor_id": "mygithub12-infrastructure-deploy-01",
                    "heartbeat_ttl_seconds": 30,
                },
            }
        },
    )

    repository = "frankichen/github_mcp"
    assert repo_policy.get_repository_policy_source(repository) == "explicit"
    assert repo_policy.is_private_ci_enabled(repository) is True
    assert repo_policy.is_test_deploy_enabled(repository) is False
    assert repo_policy.is_infrastructure_deploy_enabled(repository) is True
    assert repo_policy.is_self_deploy_enabled(repository) is True
    assert repo_policy.get_deployment_config(repository) == {}
    assert repo_policy.get_infrastructure_deployment_config(repository)["scope"] == "control-plane"


@pytest.mark.asyncio
async def test_fail_stop_contract_does_not_expand_private_ci_start_inputs():
    from app.mcp_server import mcp

    tools = {tool.name: tool for tool in await mcp.list_tools()}
    properties = tools["start_private_ci_job"].inputSchema["properties"]
    assert "command" not in properties
    assert "image" not in properties
    assert "services" not in properties
    assert "hooks" not in properties
    assert "failure_mode" not in properties
    assert "deploy_failure_mode" not in properties
    assert "MYGITHUB12_DEPLOY_FAILURE_MODE" not in properties
