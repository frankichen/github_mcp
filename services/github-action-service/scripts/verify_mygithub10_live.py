#!/usr/bin/env python3
"""Read-only MyGithub10 manifest/capability simulation for the controller image."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--simulate", action="store_true")
    args = parser.parse_args()
    if not args.simulate:
        raise SystemExit("image verification supports --simulate only")
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    os.environ.setdefault("CI_DB_PATH", "/tmp/mygithub10-live-ci.db")
    os.environ.setdefault("IDEMPOTENCY_DB_PATH", "/tmp/mygithub10-live-idem.db")
    os.environ.setdefault("DEPLOYMENT_DB_PATH", "/tmp/mygithub10-live-deploy.db")
    from app import mygithub10
    from app.feature_flags import ARTIFACT_BUILD, ARTIFACT_DEPLOY, ATTESTATION_REUSE, enabled
    from app.mcp_server import mcp
    from app.repository_policy import get_policy

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    names = [tool.name for tool in asyncio.run(mcp.list_tools())]
    capabilities = mygithub10.capabilities(os.environ.get("MYGITHUB10_BUILD_SHA", ""))
    assert manifest["tool_count"] == 84 == len(names)
    assert names == [tool["name"] for tool in manifest["tools"]]
    assert re.fullmatch(r"[0-9a-f]{40}", capabilities.get("build_sha", ""))
    assert {"get_github_file", "commit_github_files", "get_test_deployment_logs"} <= set(names)
    policy = get_policy("frankichen/github_mcp")
    assert policy.get("github") is True and policy.get("private_ci") is True
    assert policy.get("test_deploy") is False and policy.get("self_deploy") is False
    assert {enabled(ARTIFACT_BUILD), enabled(ARTIFACT_DEPLOY), enabled(ATTESTATION_REUSE)} == {False}
    print(json.dumps({"ok": True, "tool_count": len(names), "build_sha": capabilities["build_sha"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
