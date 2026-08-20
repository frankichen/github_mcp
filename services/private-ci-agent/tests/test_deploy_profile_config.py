from pathlib import Path

import yaml

from private_ci_agent.config import DEFAULT_CONFIG
from private_ci_agent.profiles import PROFILE_COMMANDS, get_repository_overrides


DEPLOY_DIR = Path(__file__).parents[1] / "deploy"


def test_worker_registers_every_builtin_profile():
    assert set(PROFILE_COMMANDS).issubset(set(DEFAULT_CONFIG["supported_profiles"]))


def test_deploy_profiles_include_repo_fast_check():
    data = yaml.safe_load((DEPLOY_DIR / "profiles.yml").read_text(encoding="utf-8"))

    assert "repo-fast-check" in data["profiles"]
    assert data["profiles"]["repo-fast-check"]["merge_eligible"] is False


def test_deploy_profiles_include_fixed_openapi_check():
    data = yaml.safe_load((DEPLOY_DIR / "profiles.yml").read_text(encoding="utf-8"))
    profile = data["profiles"]["openapi-check"]

    assert profile["merge_eligible"] is False
    assert profile["language"] == "node"
    assert profile["base_image"] == "docker.io/library/node:22"
    assert profile["command"] == "OPENAPI_PARALLELISM=1 bash scripts/validate-openapi.sh"


def test_only_sxt_allows_openapi_check_on_agent_side():
    data = yaml.safe_load((DEPLOY_DIR / "repositories.yml").read_text(encoding="utf-8"))
    repositories = data["repositories"]

    assert "openapi-check" in repositories["frankichen/sxt"]["allowed_profiles"]
    for repository, config in repositories.items():
        if repository != "frankichen/sxt":
            assert "openapi-check" not in (config.get("allowed_profiles") or [])


def test_sxt_allows_repo_fast_check_on_agent_side():
    data = yaml.safe_load((DEPLOY_DIR / "repositories.yml").read_text(encoding="utf-8"))
    allowed = data["repositories"]["frankichen/sxt"]["allowed_profiles"]

    assert "repo-fast-check" in allowed


def test_deploy_repositories_keep_runtime_overrides():
    data = yaml.safe_load((DEPLOY_DIR / "repositories.yml").read_text(encoding="utf-8"))

    assert {"frankichen/ai_war", "frankichen/lenshub-diag-mcp", "frankichen/sxt", "frankichen/github_mcp", "frankichen/auto_gupiao"}.issubset(
        set(data["repositories"])
    )


def test_auto_gupiao_legacy_entry_is_preserved():
    data = yaml.safe_load((DEPLOY_DIR / "repositories.yml").read_text(encoding="utf-8"))
    allowed = data["repositories"]["frankichen/auto_gupiao"]["allowed_profiles"]

    assert data["repositories"]["frankichen/auto_gupiao"]["enabled"] is True
    assert "repo-auto-check" in allowed
    assert "go-check" in allowed


def test_repository_overrides_expose_only_workspace_configuration(tmp_path, monkeypatch):
    path = tmp_path / "repositories.yml"
    path.write_text(
        """
repositories:
  frankichen/example:
    enabled: false
    private_ci: false
    allowed_profiles:
      - repo-auto-check
    deployment:
      enabled: true
    workspaces:
      - path: web
        type: node
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("PRIVATE_CI_REPOSITORY_OVERRIDES_PATH", str(path))

    assert get_repository_overrides("frankichen/example", "repo-auto-check") == {
        "workspaces": [{"path": "web", "type": "node"}]
    }


def test_apply_fixes_syncs_source_module():
    script = (DEPLOY_DIR / "apply-fixes.sh").read_text(encoding="utf-8")

    assert "profiles.py source.py controller_client.py" in script


def test_playwright_cache_maintenance_is_pinned_and_not_a_job_step():
    script = (DEPLOY_DIR / "prepare-playwright-cache").read_text(encoding="utf-8")

    assert "/srv/private-ci/cache/ms-playwright" in script
    assert "/ci-cache/ms-playwright" in script
    assert "playwright@1.62.0 install chromium --no-shell" in script
    assert "pass_proxy=True" in script
    assert "run as ciworker" in script


def test_controller_build_passes_host_proxy():
    script = (DEPLOY_DIR / "apply-fixes.sh").read_text(encoding="utf-8")

    assert "PRIVATE_CI_DOCKER_BUILD_PROXY:-http://127.0.0.1:10808" in script
    assert "--network host" in script
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        assert f'--build-arg "{name}=${{DOCKER_BUILD_PROXY}}"' in script


def test_node_chromium_dockerfile_uses_inherited_proxy():
    dockerfile = (DEPLOY_DIR / "Dockerfile.node-chromium").read_text(encoding="utf-8")
    preheat = (DEPLOY_DIR / "prepare-node-chromium").read_text(encoding="utf-8")

    assert "Acquire::http::Proxy=${http_apt_proxy}" in dockerfile
    assert "Acquire::https::Proxy=${https_apt_proxy}" in dockerfile
    assert "proxy.runtime.conf" in preheat
    assert "PRIVATE_CI_CONTAINER_PROXY_HOST:-10.0.2.2" in preheat
