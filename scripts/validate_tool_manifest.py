#!/usr/bin/env python3
"""Validate that the checked-in manifest matches actual MCP registration."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path


async def main() -> int:
    root = Path(__file__).parents[1]
    os.environ.setdefault("GITHUB_TOKEN", "test_token_value")
    os.environ.setdefault("ACTION_API_KEY", "test_action_key")
    os.environ.setdefault("IDEMPOTENCY_DB_PATH", "/tmp/mygithub10-idempotency.db")
    os.environ.setdefault("DEPLOYMENT_DB_PATH", "/tmp/mygithub10-deployment.db")
    os.environ.setdefault("CI_DB_PATH", "/tmp/mygithub10-ci.db")
    sys.path.insert(0, str(root / "services/github-action-service"))
    from app.mcp_server import mcp  # noqa: PLC0415

    actual = await mcp.list_tools()
    manifest = json.loads((root / "docs/MYGITHUB10_TOOL_MANIFEST.json").read_text(encoding="utf-8"))
    actual_names = [tool.name for tool in actual]
    manifest_names = [tool["name"] for tool in manifest["tools"]]
    if len(actual_names) != len(set(actual_names)):
        raise SystemExit("actual MCP registration contains duplicate names")
    if actual_names != manifest_names:
        raise SystemExit(f"manifest mismatch: actual={actual_names!r} manifest={manifest_names!r}")
    if manifest["tool_count"] != len(actual_names):
        raise SystemExit("manifest tool_count mismatch")
    from app.version import SERVICE_NAME, SERVICE_VERSION  # noqa: PLC0415
    if manifest.get("service_name") != SERVICE_NAME or manifest.get("service_version") != SERVICE_VERSION:
        raise SystemExit("manifest service name/version mismatch")
    required = {
        "get_github_file",
        "commit_github_files",
        "start_private_ci_job",
        "wait_test_deployment",
        "apply_github_patch",
        "edit_github_file_ranges",
        "build_github_patch",
        "get_mygithub_capabilities",
    }
    missing = required - set(actual_names)
    if missing:
        raise SystemExit(f"required tools missing: {sorted(missing)}")
    manifest_by_name = {tool["name"]: tool for tool in manifest["tools"]}
    builder = manifest_by_name["build_github_patch"]
    if not builder["read_only"] or builder["consequential"]:
        raise SystemExit("build_github_patch must be read-only and non-consequential")
    print(f"manifest matches {len(actual_names)} registered tools")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
