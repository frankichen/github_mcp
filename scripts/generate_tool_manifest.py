#!/usr/bin/env python3
"""Generate the MyGithub10 tool manifest from the running MCP registration."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
from pathlib import Path


READ_PREFIXES = ("get_", "list_", "compare_", "search_", "diagnose_", "plan_")
WRITE_PREFIXES = ("create_", "update_", "delete_", "merge_", "mark_", "convert_", "start_", "cancel_", "rollback_")


def category(name: str) -> str:
    if "private_ci" in name or name.startswith("list_ci_") or name.startswith("start_ci_") or name.startswith("get_ci_") or name.startswith("cancel_ci_"):
        return "private_ci"
    if "deployment" in name or "deploy_" in name or name.endswith("_deployment") or "_deployment_" in name:
        return "deployment"
    if "release" in name:
        return "release"
    return "github"


def git_value(path: Path, *args: str) -> str:
    try:
        return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()
    except Exception:
        return "unknown"


async def build_manifest(source: Path, source_commit: str | None = None, service_name: str = "MyGithub10") -> dict:
    os.environ.setdefault("GITHUB_TOKEN", "test_token_value")
    os.environ.setdefault("ACTION_API_KEY", "test_action_key")
    os.environ.setdefault("IDEMPOTENCY_DB_PATH", "/tmp/mygithub10-idempotency.db")
    os.environ.setdefault("DEPLOYMENT_DB_PATH", "/tmp/mygithub10-deployment.db")
    os.environ.setdefault("CI_DB_PATH", "/tmp/mygithub10-ci.db")
    import sys

    sys.path.insert(0, str(source))
    from app.mcp_server import mcp  # noqa: PLC0415

    registered = await mcp.list_tools()
    tools = []
    seen = set()
    for tool in registered:
        if tool.name in seen:
            raise SystemExit(f"duplicate MCP tool: {tool.name}")
        seen.add(tool.name)
        name = tool.name
        read_only = name.startswith(READ_PREFIXES) and not name.startswith(WRITE_PREFIXES)
        tools.append(
            {
                "name": name,
                "read_only": read_only,
                "consequential": not read_only,
                "category": category(name),
                "description": tool.description or "",
                "input_schema": tool.inputSchema,
            }
        )
    return {
        "service_name": service_name,
        "source_commit": source_commit or git_value(source, "rev-parse", "HEAD"),
        "controller_image": os.environ.get("MYGITHUB09_CONTROLLER_IMAGE", "not-read-from-runtime"),
        "pygithub_version": "2.5.0",
        "mcp_sdk_version": "mcp>=1.7.0 (requirements baseline)",
        "tool_count": len(tools),
        "tools": tools,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path(__file__).parents[1])
    parser.add_argument("--source-commit", default=None)
    parser.add_argument("--service-name", default="MyGithub10")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = asyncio.run(build_manifest(args.source.resolve(), args.source_commit, args.service_name))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
