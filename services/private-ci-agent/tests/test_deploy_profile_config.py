from pathlib import Path

import yaml

from private_ci_agent.config import DEFAULT_CONFIG
from private_ci_agent.profiles import PROFILE_COMMANDS


DEPLOY_DIR = Path(__file__).parents[1] / "deploy"


def test_worker_registers_every_builtin_profile():
    assert set(PROFILE_COMMANDS).issubset(set(DEFAULT_CONFIG["supported_profiles"]))


def test_deploy_profiles_include_repo_fast_check():
    data = yaml.safe_load((DEPLOY_DIR / "profiles.yml").read_text(encoding="utf-8"))

    assert "repo-fast-check" in data["profiles"]
    assert data["profiles"]["repo-fast-check"]["merge_eligible"] is False


def test_sxt_allows_repo_fast_check_on_agent_side():
    data = yaml.safe_load((DEPLOY_DIR / "repositories.yml").read_text(encoding="utf-8"))
    allowed = data["repositories"]["frankichen/sxt"]["allowed_profiles"]

    assert "repo-fast-check" in allowed


def test_deploy_repositories_keep_runtime_allowed_repos():
    data = yaml.safe_load((DEPLOY_DIR / "repositories.yml").read_text(encoding="utf-8"))

    assert {"frankichen/ai_war", "frankichen/lenshub-diag-mcp", "frankichen/sxt", "frankichen/github_mcp", "frankichen/auto_gupiao"}.issubset(
        set(data["repositories"])
    )


def test_auto_gupiao_is_agent_source_allowed():
    data = yaml.safe_load((DEPLOY_DIR / "repositories.yml").read_text(encoding="utf-8"))
    allowed = data["repositories"]["frankichen/auto_gupiao"]["allowed_profiles"]

    assert data["repositories"]["frankichen/auto_gupiao"]["enabled"] is True
    assert "repo-auto-check" in allowed
    assert "go-check" in allowed
