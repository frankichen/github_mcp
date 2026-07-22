"""CI repository configuration module.

Loads and validates repository allowlists from config/ci_repositories.yml.
"""

import logging
import os
import yaml
from typing import Optional

logger = logging.getLogger(__name__)

CONFIG_PATH = os.environ.get("CI_REPOS_CONFIG_PATH", "/app/config/ci_repositories.yml")

_DEFAULT_CONFIG = {
    "repositories": {
        "frankichen/github_mcp": {
            "enabled": True,
            "allowed_profiles": ["repo-auto-check"],
            "max_timeout_seconds": 900,
        },
        "frankichen/ai_war": {
            "enabled": True,
            "allowed_profiles": ["repo-auto-check", "python-check"],
            "max_timeout_seconds": 900,
        },
        "frankichen/sxt": {
            "enabled": True,
            "allowed_profiles": ["repo-auto-check", "python-check"],
            "max_timeout_seconds": 900,
        },
    }
}

_config_cache = None


def _load_config() -> dict:
    global _config_cache
    if _config_cache is not None:
        return _config_cache

    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                _config_cache = yaml.safe_load(f) or {}
            logger.info(f"Loaded CI repository config from {CONFIG_PATH}")
            return _config_cache
        except Exception as e:
            logger.warning(f"Failed to load {CONFIG_PATH}: {e}")

    _config_cache = _DEFAULT_CONFIG
    logger.info("Using default CI repository config")
    return _config_cache


def reload_config():
    global _config_cache
    _config_cache = None
    return _load_config()


def is_repository_allowed(repository: str) -> bool:
    config = _load_config()
    repos = config.get("repositories", {})
    if repository not in repos:
        return False
    return repos[repository].get("enabled", False)


def is_profile_allowed(repository: str, profile: str) -> bool:
    config = _load_config()
    repos = config.get("repositories", {})
    if repository not in repos:
        return False
    repo_config = repos[repository]
    if not repo_config.get("enabled", False):
        return False
    allowed = repo_config.get("allowed_profiles", [])
    return profile in allowed


def get_max_timeout(repository: str) -> int:
    config = _load_config()
    repos = config.get("repositories", {})
    if repository not in repos:
        return 900
    return repos[repository].get("max_timeout_seconds", 900)


def get_allowed_repositories() -> list[str]:
    config = _load_config()
    return list(config.get("repositories", {}).keys())


def get_allowed_profiles(repository: Optional[str] = None) -> list[str]:
    """Return profiles allowed for one repository, or the global union."""
    config = _load_config()
    if repository:
        return list(config.get("repositories", {}).get(repository, {}).get("allowed_profiles", []))
    profiles = set()
    for repo_cfg in config.get("repositories", {}).values():
        for p in repo_cfg.get("allowed_profiles", []):
            profiles.add(p)
    return sorted(profiles)
