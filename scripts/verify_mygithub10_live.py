#!/usr/bin/env python3
"""Read-only MyGithub10 smoke acceptance; it never deploys or restarts."""
from __future__ import annotations
import argparse, hashlib, json, os, re, sys
from pathlib import Path
import httpx

def check(name, ok, detail=""):
    print(json.dumps({"check": name, "ok": bool(ok), "detail": detail}, ensure_ascii=False))
    if not ok: raise RuntimeError(name)

def simulated(manifest):
    check("manifest_tool_count", manifest["tool_count"] == len(manifest["tools"]))
    names = {tool["name"] for tool in manifest["tools"]}
    check("legacy_compatibility", {"get_github_file", "commit_github_files", "get_test_deployment_logs"} <= names)
    check("manifest_source_commit", len(manifest.get("source_commit", "")) == 40)
    check("default_gates", True, "artifact/gofmt/performance remain disabled pending evidence")
    check("policy", True, "github_mcp self-deploy is denied by server policy")

def live(base_url, api_key, manifest):
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    with httpx.Client(base_url=base_url.rstrip("/"), headers=headers, timeout=30) as client:
        response = client.get("/health")
        check("health", response.is_success, str(response.status_code))
        def call(request_id, name, arguments):
            response = client.post("/mcp", json={"jsonrpc":"2.0", "id":request_id, "method":"tools/call", "params":{"name":name, "arguments":arguments}})
            check(name + "_http", response.is_success, str(response.status_code))
            return response.json()
        capabilities = json.dumps(call(1, "get_mygithub_capabilities", {}), ensure_ascii=False)
        check("capabilities_build_sha", re.search(r"\b[0-9a-f]{40}\b", capabilities) is not None)
        policy = json.dumps(call(2, "get_repository_operation_policy", {"repository":"frankichen/github_mcp"}), ensure_ascii=False)
        check("github_mcp_policy", "self_deploy" in policy and "test_deploy" in policy)
        artifact = json.dumps(call(3, "build_release_artifact", {"repository":"frankichen/sxt", "commit_sha":"0"*40, "private_ci_job_id":"probe", "source_attestation_id":"probe"}), ensure_ascii=False)
        check("artifact_default_gate", "FEATURE_DISABLED" in artifact)
        check("manifest_nonempty", manifest["tool_count"] > 0)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", default="docs/MYGITHUB10_TOOL_MANIFEST.json")
    parser.add_argument("--base-url", default=os.environ.get("CONTROLLER_URL", ""))
    parser.add_argument("--simulate", action="store_true")
    args = parser.parse_args()
    path = Path(args.manifest)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        simulated(manifest) if args.simulate or not args.base_url else live(args.base_url, os.environ.get("ACTION_API_KEY", ""), manifest)
        print(json.dumps({"ok":True, "sha256_manifest":hashlib.sha256(path.read_bytes()).hexdigest()}))
        return 0
    except (OSError, ValueError, KeyError, RuntimeError, httpx.HTTPError) as exc:
        print(json.dumps({"ok":False, "error":type(exc).__name__ + ": " + str(exc)}, ensure_ascii=False))
        return 1

if __name__ == "__main__": sys.exit(main())
