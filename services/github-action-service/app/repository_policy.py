"""Server-owned repository operation policy; request parameters cannot widen it."""
from __future__ import annotations

import os
from pathlib import Path

import yaml

_DEFAULT = Path(__file__).resolve().parents[1] / "config" / "repository_policies.yml"

GITHUB_OPERATIONS = frozenset({
    "read", "chunk_read", "search", "create_branch", "patch", "range_edit",
    "upload", "create_pr", "update_pr", "comment", "reviewers", "ready", "draft",
    "update_branch", "merge", "delete_branch", "ci_read", "ci", "ci_cancel",
})
SPECIAL_OPERATIONS = frozenset({"private_ci", "test_deploy", "self_deploy"})


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
    if not policy:
        return False
    if operation in SPECIAL_OPERATIONS:
        return bool(policy.get(operation, False))
    if operation not in GITHUB_OPERATIONS or policy.get("github") is not True:
        return False
    if operation in set(policy.get("denied_operations") or []):
        return False
    allowed = policy.get("allowed_operations")
    return operation in GITHUB_OPERATIONS if allowed is None else operation in set(allowed)


def require_operation(repository: str, operation: str) -> dict | None:
    """Return a stable MCP error payload when the server policy denies access."""
    if is_operation_allowed(repository, operation):
        return None
    return {
        "ok": False,
        "error_code": "REPOSITORY_OPERATION_DENIED",
        "repository": repository,
        "operation": operation,
    }
