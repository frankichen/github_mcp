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
    actual_names = [tool.name for tool in actual]
    if len(actual_names) != len(set(actual_names)):
        raise SystemExit("actual MCP registration contains duplicate names")

    from app.version import SERVICE_NAME, SERVICE_VERSION  # noqa: PLC0415

    required = {
        "get_github_file",
        "commit_github_files",
        "start_private_ci_job",
        "wait_test_deployment",
        "apply_github_patch",
        "edit_github_file_ranges",
        "replace_github_text_once",
        "build_github_patch",
        "get_mygithub_capabilities",
    }
    missing = required - set(actual_names)
    if missing:
        raise SystemExit(f"required tools missing: {sorted(missing)}")

    builder = next(tool for tool in actual if tool.name == "build_github_patch")
    annotations = builder.annotations
    if annotations is None or annotations.readOnlyHint is not True:
        raise SystemExit("build_github_patch must be read-only")
    if annotations.destructiveHint is True:
        raise SystemExit("build_github_patch must be non-destructive")

    manifest12_path = root / "docs/MYGITHUB12_TOOL_MANIFEST.json"
    if manifest12_path.exists():
        manifest12 = json.loads(manifest12_path.read_text(encoding="utf-8"))
        legacy_manifest_name = manifest12.get("legacy_manifest")
        if not isinstance(legacy_manifest_name, str):
            raise SystemExit("MyGithut12 manifest legacy_manifest must be a filename")
        legacy_path = root / "docs" / legacy_manifest_name
        legacy_manifest = json.loads(legacy_path.read_text(encoding="utf-8"))
        legacy_names = [tool["name"] for tool in legacy_manifest["tools"]]
        new_names = manifest12["new_tools"]
        manifest_names = legacy_names + new_names
        if actual_names != manifest_names:
            raise SystemExit(f"MyGithut12 manifest mismatch: actual={actual_names!r} manifest={manifest_names!r}")
        if manifest12["legacy_tool_count"] != len(legacy_names):
            raise SystemExit("MyGithut12 legacy_tool_count mismatch")
        if manifest12["new_tool_count"] != len(new_names):
            raise SystemExit("MyGithut12 new_tool_count mismatch")
        if manifest12["tool_count"] != len(actual_names):
            raise SystemExit("MyGithut12 tool_count mismatch")
        if manifest12.get("service_name") != SERVICE_NAME or manifest12.get("service_version") != SERVICE_VERSION:
            raise SystemExit("MyGithut12 manifest service name/version mismatch")
        print(f"MyGithut12 manifest matches {len(actual_names)} registered tools ({len(new_names)} new)")
        return 0

    manifest = json.loads((root / "docs/MYGITHUB10_TOOL_MANIFEST.json").read_text(encoding="utf-8"))
    manifest_names = [tool["name"] for tool in manifest["tools"]]
    if actual_names != manifest_names:
        raise SystemExit(f"manifest mismatch: actual={actual_names!r} manifest={manifest_names!r}")
    if manifest["tool_count"] != len(actual_names):
        raise SystemExit("manifest tool_count mismatch")
    if manifest.get("service_name") != SERVICE_NAME or manifest.get("service_version") != SERVICE_VERSION:
        raise SystemExit("manifest service name/version mismatch")
    manifest_by_name = {tool["name"]: tool for tool in manifest["tools"]}
    builder_manifest = manifest_by_name["build_github_patch"]
    if not builder_manifest["read_only"] or builder_manifest["consequential"]:
        raise SystemExit("build_github_patch must be read-only and non-consequential")
    print(f"manifest matches {len(actual_names)} registered tools")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
