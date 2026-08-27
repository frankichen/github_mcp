#!/usr/bin/env python3
"""Validate checked-in compatibility and canonical-production MCP manifests."""

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

    previous_expose = os.environ.get("MYGITHUB12_EXPOSE_DEPRECATED_TOOLS")
    try:
        os.environ["MYGITHUB12_EXPOSE_DEPRECATED_TOOLS"] = "true"
        registered = await mcp.list_tools()
        registered_names = [tool.name for tool in registered]
        if len(registered_names) != len(set(registered_names)):
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
            "put_github_file",
            "put_github_files",
            "put_github_file_from_local_candidate",
            "get_mygithub_capabilities",
            "plan_private_ci_job",
        }
        missing = required - set(registered_names)
        if missing:
            raise SystemExit(f"required tools missing: {sorted(missing)}")

        builder = next(tool for tool in registered if tool.name == "build_github_patch")
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
            manifest_registered_names = legacy_names + new_names
            if registered_names != manifest_registered_names:
                raise SystemExit(
                    f"MyGithut12 compatibility manifest mismatch: actual={registered_names!r} manifest={manifest_registered_names!r}"
                )
            if manifest12["legacy_tool_count"] != len(legacy_names):
                raise SystemExit("MyGithut12 legacy_tool_count mismatch")
            if manifest12["new_tool_count"] != len(new_names):
                raise SystemExit("MyGithut12 new_tool_count mismatch")
            if manifest12.get("compatibility_tool_count") != len(registered_names):
                raise SystemExit("MyGithut12 compatibility_tool_count mismatch")

            hidden = manifest12.get("hidden_deprecated_tools") or []
            if not isinstance(hidden, list) or len(hidden) != len(set(hidden)):
                raise SystemExit("MyGithut12 hidden_deprecated_tools must be a unique list")
            if not set(hidden) <= set(registered_names):
                raise SystemExit("MyGithut12 hidden deprecated tool is not registered")
            expected_canonical_names = [name for name in registered_names if name not in set(hidden)]
            os.environ["MYGITHUB12_EXPOSE_DEPRECATED_TOOLS"] = "false"
            canonical = await mcp.list_tools()
            canonical_names = [tool.name for tool in canonical]
            if canonical_names != expected_canonical_names:
                raise SystemExit(
                    f"MyGithut12 canonical schema mismatch: actual={canonical_names!r} expected={expected_canonical_names!r}"
                )
            if manifest12["tool_count"] != len(canonical_names):
                raise SystemExit("MyGithut12 canonical tool_count mismatch")
            if manifest12.get("schema_identity_algorithm") != "sha256-canonical-mcp-tools-v1":
                raise SystemExit("MyGithut12 schema identity algorithm mismatch")
            if manifest12.get("service_name") != SERVICE_NAME or manifest12.get("service_version") != SERVICE_VERSION:
                raise SystemExit("MyGithut12 manifest service name/version mismatch")
            identity = await mcp.tool_schema_identity(canonical)
            if identity["tool_count"] != len(canonical_names):
                raise SystemExit("MyGithut12 runtime schema identity count mismatch")
            if len(identity["tool_schema_sha256"]) != 64:
                raise SystemExit("MyGithut12 runtime schema fingerprint is invalid")
            print(
                f"MyGithut12 manifest matches {len(canonical_names)} canonical / {len(registered_names)} compatibility tools ({len(new_names)} new)"
            )
            return 0

        manifest = json.loads((root / "docs/MYGITHUB10_TOOL_MANIFEST.json").read_text(encoding="utf-8"))
        manifest_names = [tool["name"] for tool in manifest["tools"]]
        if registered_names != manifest_names:
            raise SystemExit(f"manifest mismatch: actual={registered_names!r} manifest={manifest_names!r}")
        if manifest["tool_count"] != len(registered_names):
            raise SystemExit("manifest tool_count mismatch")
        if manifest.get("service_name") != SERVICE_NAME or manifest.get("service_version") != SERVICE_VERSION:
            raise SystemExit("manifest service name/version mismatch")
        manifest_by_name = {tool["name"]: tool for tool in manifest["tools"]}
        builder_manifest = manifest_by_name["build_github_patch"]
        if not builder_manifest["read_only"] or builder_manifest["consequential"]:
            raise SystemExit("build_github_patch must be read-only and non-consequential")
        print(f"manifest matches {len(registered_names)} registered tools")
        return 0
    finally:
        if previous_expose is None:
            os.environ.pop("MYGITHUB12_EXPOSE_DEPRECATED_TOOLS", None)
        else:
            os.environ["MYGITHUB12_EXPOSE_DEPRECATED_TOOLS"] = previous_expose


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
