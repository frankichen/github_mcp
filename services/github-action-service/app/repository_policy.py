"""Server-owned repository operation policy; request parameters cannot widen it."""
from __future__ import annotations

import os
from pathlib import Path

import yaml

_DEFAULT = Path(__file__).resolve().parents[1] / "config" / "repository_policies.yml"


def _policies() -> dict:
    path = Path(os.environ.get("REPOSITORY_POLICY_FILE", str(_DEFAULT)))
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}


def get_policy(repository: str) -> dict:
    return dict((_policies().get("repositories") or {}).get(repository) or {})


def is_operation_allowed(repository: str, operation: str) -> bool:
    policy = get_policy(repository)
    if operation in {"github", "private_ci", "test_deploy", "self_deploy"}:
        return bool(policy.get(operation, False))
    return operation in set(policy.get("allowed_operations") or [])
