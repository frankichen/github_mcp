"""Private CI repository policy.

Explicit repository entries remain authoritative for exceptions and deployment.
Repositories matching the configured auto-enrollment patterns receive a synthetic,
CI-only policy so new projects do not need per-repository registration.
"""

from fnmatch import fnmatchcase
import logging
import os
from typing import Optional

import yaml

from app.github_policy import repository_is_allowed as github_repository_is_allowed

logger = logging.getLogger(__name__)

CONFIG_PATH = os.environ.get("CI_REPOS_CONFIG_PATH", "/app/config/ci_repositories.yml")
_DEFAULT_AUTO_PROFILES = ["repo-auto-check", "python-check", "go-check", "node-check"]
_DEFAULT_AUTO_MERGE_POLICY = {
    "private_ci_authoritative": True,
    "required_private_ci_profile": "repo-auto-check",
    "github_checks_mode": "required_only",
    "allow_non_required_check_failures": True,
    "allow_quota_or_infrastructure_failures": True,
    "required_workflows": [],
}
_DEFAULT_AUTO_ENROLLMENT = {
    "enabled": True,
    "repository_patterns": ["frankichen/*"],
    "defaults": {
        "enabled": True,
        "private_ci": True,
        "auto_detect": True,
        "allowed_profiles": list(_DEFAULT_AUTO_PROFILES),
        "max_timeout_seconds": 900,
        "merge_policy": dict(_DEFAULT_AUTO_MERGE_POLICY),
    },
}

_DEFAULT_CONFIG = {
    "auto_enroll": _DEFAULT_AUTO_ENROLLMENT,
    "repositories": {
        "frankichen/ai_war": {
            "enabled": True,
            "allowed_profiles": ["repo-auto-check", "python-check"],
            "max_timeout_seconds": 900,
        },
        "frankichen/sxt": {
            "enabled": True,
            "allowed_profiles": ["repo-auto-check", "repo-fast-check", "python-check"],
            "max_timeout_seconds": 900,
            "deployment": {
                "enabled": True,
                "self_deploy": False,
                "environment": "gongshi-test",
                "scope": "fullstack",
                "private_ci": True,
                "profile": "repo-auto-check",
                "script": "scripts/deploy_gongshi_test.sh",
                "status_file_env": "DEPLOY_STATUS_FILE",
            },
        },
        "frankichen/auto_gupiao": {
            "enabled": True,
            "private_ci": True,
            "auto_detect": True,
            "allowed_profiles": ["repo-auto-check", "go-check"],
            "max_timeout_seconds": 900,
            "merge_policy": {
                "private_ci_authoritative": True,
                "required_private_ci_profile": "repo-auto-check",
            },
            "deployment": {
                "enabled": True,
                "self_deploy": True,
                "environment": "auto-gupiao-test",
                "scope": "reports",
                "private_ci": False,
                "script": "scripts/deploy_auto_gupiao.sh",
                "workspace_env": "AUTO_GUPIAO_DEPLOY_WORKSPACE",
                "status_file_env": "AUTO_GUPIAO_DEPLOY_STATUS_FILE",
            },
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
            with open(CONFIG_PATH, encoding="utf-8") as handle:
                loaded = yaml.safe_load(handle) or {}
            if not isinstance(loaded, dict):
                raise ValueError("CI repository config must be a mapping")
            _config_cache = loaded
            logger.info("Loaded CI repository config from %s", CONFIG_PATH)
            return _config_cache
        except Exception as exc:
            logger.warning("Failed to load %s: %s", CONFIG_PATH, exc)

    _config_cache = _DEFAULT_CONFIG
    logger.info("Using default CI repository config")
    return _config_cache


def reload_config():
    global _config_cache
    _config_cache = None
    return _load_config()


def _auto_enrollment_policy() -> dict:
    policy = _load_config().get("auto_enroll") or {}
    return policy if isinstance(policy, dict) else {}


def _auto_enrolled_config(repository: str) -> dict:
    """Build a CI-only policy for one repository without persisting registration."""
    policy = _auto_enrollment_policy()
    if policy.get("enabled") is not True:
        return {}
    if not github_repository_is_allowed(repository):
        return {}

    patterns = policy.get("repository_patterns") or []
    if not isinstance(patterns, list):
        logger.warning("auto_enroll.repository_patterns must be a list")
        return {}
    if not any(isinstance(pattern, str) and fnmatchcase(repository, pattern) for pattern in patterns):
        return {}

    defaults = policy.get("defaults") or {}
    if not isinstance(defaults, dict):
        logger.warning("auto_enroll.defaults must be a mapping")
        return {}

    entry = {
        "enabled": True,
        "private_ci": True,
        "auto_detect": True,
        "allowed_profiles": list(_DEFAULT_AUTO_PROFILES),
        "max_timeout_seconds": 900,
        "merge_policy": dict(_DEFAULT_AUTO_MERGE_POLICY),
    }
    for key in (
        "enabled",
        "private_ci",
        "auto_detect",
        "allowed_profiles",
        "max_timeout_seconds",
        "merge_policy",
        "workspaces",
    ):
        if key in defaults:
            entry[key] = defaults[key]

    entry.pop("deployment", None)
    return entry


def get_repository_policy_source(repository: str) -> str:
    repositories = _load_config().get("repositories", {}) or {}
    if repository in repositories:
        return "explicit"
    if _auto_enrolled_config(repository):
        return "auto"
    return "none"


def get_repository_config(repository: str) -> dict:
    """Resolve explicit policy first, then safe CI-only auto-enrollment."""
    repositories = _load_config().get("repositories", {}) or {}
    if repository in repositories:
        return dict(repositories.get(repository) or {})
    return _auto_enrolled_config(repository)


def is_repository_allowed(repository: str) -> bool:
    return get_repository_config(repository).get("enabled") is True


def is_profile_allowed(repository: str, profile: str) -> bool:
    entry = get_repository_config(repository)
    return bool(entry.get("enabled") is True and profile in (entry.get("allowed_profiles") or []))


def get_max_timeout(repository: str) -> int:
    return int(get_repository_config(repository).get("max_timeout_seconds", 900))


def get_allowed_repositories() -> list[str]:
    """Return explicit repository entries; auto-enrollment is pattern based."""
    return list((_load_config().get("repositories", {}) or {}).keys())


def get_allowed_profiles(repository: Optional[str] = None) -> list[str]:
    """Return profiles allowed for one repository, or the global effective union."""
    if repository:
        return list(get_repository_config(repository).get("allowed_profiles", []) or [])

    profiles = set()
    for repo_cfg in (_load_config().get("repositories", {}) or {}).values():
        for profile in (repo_cfg or {}).get("allowed_profiles", []):
            profiles.add(profile)
    auto_policy = _auto_enrollment_policy()
    if auto_policy.get("enabled") is True:
        defaults = auto_policy.get("defaults") or {}
        auto_profiles = defaults.get("allowed_profiles", _DEFAULT_AUTO_PROFILES)
        if isinstance(auto_profiles, list):
            profiles.update(profile for profile in auto_profiles if isinstance(profile, str))
    return sorted(profiles)


def is_private_ci_enabled(repository: str) -> bool:
    """Return whether private CI is an enabled policy gate for a repository."""
    entry = get_repository_config(repository)
    if "private_ci" in entry:
        return entry.get("private_ci") is True
    return bool(entry.get("enabled", False) and entry.get("allowed_profiles"))


def get_deployment_config(repository: str) -> dict:
    """Return only explicit, fixed repository deployment contracts."""
    if get_repository_policy_source(repository) != "explicit":
        return {}
    entry = get_repository_config(repository)
    return dict(entry.get("deployment") or {})


def is_test_deploy_enabled(repository: str) -> bool:
    """Return whether repository has an explicit enabled deployment contract."""
    if get_repository_policy_source(repository) != "explicit":
        return False
    entry = get_repository_config(repository)
    deployment = entry.get("deployment") or {}
    return bool(entry.get("enabled", False) and deployment.get("enabled", False))


def is_self_deploy_enabled(repository: str) -> bool:
    """Return whether an explicit fixed deployment contract allows self deployment."""
    if get_repository_policy_source(repository) != "explicit":
        return False
    entry = get_repository_config(repository)
    deployment = entry.get("deployment") or {}
    return bool(
        entry.get("enabled", False)
        and deployment.get("enabled", False)
        and deployment.get("self_deploy", False)
    )
