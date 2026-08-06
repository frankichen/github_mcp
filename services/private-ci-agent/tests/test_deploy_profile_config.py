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


def test_playwright_cache_maintenance_is_pinned_and_not_a_job_step():
    script = (DEPLOY_DIR / "prepare-playwright-cache").read_text(encoding="utf-8")

    assert "/srv/private-ci/cache/ms-playwright" in script
    assert "/ci-cache/ms-playwright" in script
    assert "playwright@1.62.0 install chromium --no-shell" in script
    assert "pass_proxy=True" in script
    assert "run as ciworker" in script


def test_apply_fixes_runs_go_cache_preheat_with_bash():
    script = (DEPLOY_DIR / "apply-fixes.sh").read_text(encoding="utf-8")

    expected = '/bin/bash "${AGENT_DIR}/deploy/prepare-go-cache"'
    assert expected in script
    assert '/usr/bin/python3 "${AGENT_DIR}/deploy/prepare-go-cache"' not in script
