#!/usr/bin/env python3
"""MyGithub10 read-only acceptance using the official MCP ClientSession."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
from pathlib import Path

import httpx
from mcp import ClientSession
try:
    from mcp.client.streamable_http import streamable_http_client
except ImportError:  # MCP >= 1.12 renamed the helper.
    from mcp.client.streamable_http import streamablehttp_client as streamable_http_client


def report(name: str, ok: bool, detail: str = "") -> None:
    print(json.dumps({"check": name, "ok": bool(ok), "detail": detail}, ensure_ascii=False))
    if not ok:
        raise RuntimeError(name)


def load_simulation(manifest_path: Path) -> tuple[dict, dict, dict, dict, list[str]]:
    service_root = manifest_path.parents[1] / "services" / "github-action-service"
    sys.path.insert(0, str(service_root))
    os.environ.setdefault("CI_DB_PATH", "/tmp/mygithub10-live-ci.db")
    os.environ.setdefault("IDEMPOTENCY_DB_PATH", "/tmp/mygithub10-live-idem.db")
    os.environ.setdefault("DEPLOYMENT_DB_PATH", "/tmp/mygithub10-live-deploy.db")
    from app import mygithub10
    from app.feature_flags import ARTIFACT_BUILD, ARTIFACT_DEPLOY, ATTESTATION_REUSE, enabled
    from app.mcp_server import mcp
    from app.repository_policy import get_policy

    async def names():
        return [tool.name for tool in await mcp.list_tools()]

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    capabilities = mygithub10.capabilities(manifest.get("source_commit", ""))
    flags = {"artifact_build": enabled(ARTIFACT_BUILD), "artifact_deploy": enabled(ARTIFACT_DEPLOY), "attestation_reuse": enabled(ATTESTATION_REUSE)}
    policy = get_policy("frankichen/github_mcp")
    return manifest, capabilities, flags, policy, asyncio.run(names())


def simulated(manifest_path: Path) -> None:
    manifest, capabilities, flags, policy, names = load_simulation(manifest_path)
    report("manifest_tool_count", manifest["tool_count"] == len(manifest["tools"]))
    report("tool_names", names == [tool["name"] for tool in manifest["tools"]])
    report("capabilities_build_sha", bool(re.fullmatch(r"[0-9a-f]{40}", capabilities.get("build_sha", ""))))
    report("legacy_compatibility", {"get_github_file", "commit_github_files", "get_test_deployment_logs"} <= set(names))
    report("github_policy", policy.get("github") is True and policy.get("private_ci") is True)
    report("github_mcp_deploy_denied", policy.get("test_deploy") is False and policy.get("self_deploy") is False)
    report("feature_flags_loaded", flags == {"artifact_build": False, "artifact_deploy": False, "attestation_reuse": False})


async def live(base_url: str, api_key: str, manifest_path: Path, strict: bool = False) -> None:
    required = ("MYGITHUB10_TEST_JOB_ID", "MYGITHUB10_TEST_ATTESTATION_ID", "MYGITHUB10_TEST_LARGE_FILE_PATH")
    if strict:
        missing = [name for name in required if not os.environ.get(name, "").strip()]
        if missing:
            raise RuntimeError("strict mode requires: " + ", ".join(missing))
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    async with httpx.AsyncClient(base_url=base_url.rstrip("/"), headers=headers, trust_env=False, timeout=45) as client:
        health = await client.get("/health")
        report("health", health.is_success, str(health.status_code))
        async with streamable_http_client(base_url.rstrip("/"), http_client=client) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                names = [tool.name for tool in tools]

                async def call(name: str, arguments: dict) -> dict:
                    result = await session.call_tool(name, arguments)
                    return json.loads(result.content[0].text)

                capabilities = await call("get_mygithub_capabilities", {})
                report("capabilities_build_sha", bool(re.fullmatch(r"[0-9a-f]{40}", capabilities.get("build_sha", ""))))
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                report("manifest_tool_count", manifest["tool_count"] == len(names))
                report("tool_names", set(names) == {tool["name"] for tool in manifest["tools"]})
                report("legacy_compatibility", {"get_github_file", "commit_github_files", "get_test_deployment_logs"} <= set(names))
                policy = await call("get_repository_operation_policy", {"repository": "frankichen/github_mcp"})
                values = policy.get("policy", {})
                report("github_policy", values.get("github") is True and values.get("private_ci") is True)
                report("github_mcp_deploy_denied", values.get("test_deploy") is False and values.get("self_deploy") is False)
                artifact = await call("build_release_artifact", {"repository": "frankichen/sxt", "commit_sha": "0" * 40, "private_ci_job_id": "probe", "source_attestation_id": "probe"})
                report("artifact_build_disabled", artifact.get("error_code") == "FEATURE_DISABLED")
                deploy = await call("start_test_deployment", {"repository": "frankichen/sxt", "environment": "gongshi-test", "commit_sha": "0" * 40, "private_ci_job_id": "probe", "artifact_id": "probe"})
                report("artifact_deploy_disabled", deploy.get("error_code") == "FEATURE_DISABLED")

                repository = os.environ.get("MYGITHUB10_TEST_REPOSITORY", "frankichen/sxt")
                path = os.environ.get("MYGITHUB10_TEST_FILE_PATH", "go.sum")
                branch = await call("get_github_branch", {"repository": repository, "branch": "main"})
                head = branch.get("commit_sha")
                report("real_main_head", bool(re.fullmatch(r"[0-9a-f]{40}", head or "")))
                file_manifest = await call("get_github_file_manifest", {"repository": repository, "path": path, "ref": "main"})
                report("test_file_manifest", file_manifest.get("ok", True) and file_manifest.get("size_bytes", 0) > 0)
                blob = file_manifest["blob_sha"]
                file_data = await call("get_github_file", {"repository": repository, "path": path, "ref": "main"})
                original = file_data.get("content", "")
                first_line = original.splitlines(keepends=True)[0] if original.splitlines(keepends=True) else "\n"
                first_line_text = first_line.rstrip("\r\n")
                patch = f"--- a/{path}\n+++ b/{path}\n@@ -1,1 +1,1 @@\n-{first_line_text}\n+{first_line_text}\n"
                offset, chunks = 0, []
                while True:
                    chunk = await call("read_github_file_chunk", {"repository": repository, "path": path, "ref": "main", "offset_bytes": offset, "limit_bytes": 64 * 1024, "expected_blob_sha": blob})
                    chunks.append(chunk["content"]); offset = chunk["next_offset"]
                    if chunk["eof"]: break
                rebuilt = "".join(chunks).encode("utf-8")
                report("chunk_reassembly_sha", hashlib.sha256(rebuilt).hexdigest() == file_manifest["content_sha256"])
                report("utf8_boundary", all(part.encode("utf-8").decode("utf-8") == part for part in chunks))
                dry = await call("apply_github_patch", {"repository": repository, "branch": "main", "expected_head_sha": head, "expected_blob_shas_json": json.dumps({path: blob}), "patch": patch, "commit_message": "acceptance", "dry_run": True})
                report("patch_dry_run", dry.get("dry_run") is True and (dry.get("applied") is True or dry.get("planned") is True or bool(dry.get("changed_files"))))
                stale = await call("apply_github_patch", {"repository": repository, "branch": "main", "expected_head_sha": "f" * 40, "expected_blob_shas_json": "{}", "patch": patch, "commit_message": "acceptance", "dry_run": True})
                report("stale_head_rejected", stale.get("error", {}).get("code") == "PATCH_HEAD_CHANGED")
                range_ops = [{"path": path, "operation": "replace", "start_line": 1, "end_line": 1, "expected_old_text_sha256": hashlib.sha256(first_line.encode()).hexdigest(), "replacement": first_line}]
                range_result = await call("edit_github_file_ranges", {"repository": repository, "branch": "main", "expected_head_sha": head, "operations_json": json.dumps(range_ops), "commit_message": "acceptance", "dry_run": True})
                report("range_edit_dry_run", range_result.get("dry_run") is True and bool(range_result.get("changed_files")))
                head_after = await call("get_github_branch", {"repository": repository, "branch": "main"})
                report("dry_run_head_unchanged", head_after.get("commit_sha") == head)
                upload = await call("begin_github_file_upload", {})
                report("upload_lifecycle_begin", bool(upload.get("upload_id")))
                if upload.get("upload_id"):
                    upload_id = upload["upload_id"]; content = "验收边界✅".encode("utf-8")
                    appended = await call("append_github_file_upload_chunk", {"upload_id": upload_id, "offset": 0, "text": content.decode("utf-8"), "chunk_sha256": hashlib.sha256(content).hexdigest()})
                    report("upload_lifecycle_append", appended.get("next_offset") == len(content))
                    finalized = await call("finalize_github_file_upload", {"upload_id": upload_id, "expected_size_bytes": len(content), "expected_sha256": hashlib.sha256(content).hexdigest()})
                    report("upload_lifecycle_finalize", finalized.get("finalized") is True)
                    aborted = await call("abort_github_file_upload", {"upload_id": upload_id})
                    report("upload_lifecycle_abort", aborted.get("aborted") is True)
                job_id = os.environ.get("MYGITHUB10_TEST_JOB_ID", "")
                logs = await call("get_private_ci_log_tail", {"job_id": job_id or "invalid", "lines": 20})
                if job_id and logs.get("ok", True):
                    redacted = json.dumps(logs, ensure_ascii=False)
                    report("logs_secret_redaction", re.search(r"(?i)(token|password|secret|authorization)=([^*\s]+)", redacted) is None)
                else:
                    report("logs_invalid_id_safe", logs.get("error_code") in {"PRIVATE_CI_JOB_NOT_FOUND", "INVALID_ARGUMENT"} or logs.get("error", {}).get("code") in {"PRIVATE_CI_JOB_NOT_FOUND", "INVALID_ARGUMENT"})
                attestation = await call("get_attestation", {"attestation_id": "invalid"})
                report("attestation_invalid_id", attestation.get("ok") is False)
                valid_attestation = os.environ.get("MYGITHUB10_TEST_ATTESTATION_ID", "")
                if valid_attestation:
                    valid = await call("get_attestation", {"attestation_id": valid_attestation})
                    report("attestation_read", valid.get("ok") is True and bool(valid.get("attestation")))
                large_path = os.environ.get("MYGITHUB10_TEST_LARGE_FILE_PATH", "")
                if strict:
                    large_manifest = await call("get_github_file_manifest", {"repository": repository, "path": large_path, "ref": "main"})
                    report("large_file_exceeds_inline", large_manifest.get("size_bytes", 0) > capabilities.get("max_inline_response_bytes", 0))
                    large_chunks, large_offset = [], 0
                    while True:
                        chunk = await call("read_github_file_chunk", {"repository": repository, "path": large_path, "ref": "main", "offset_bytes": large_offset, "limit_bytes": 64 * 1024, "expected_blob_sha": large_manifest["blob_sha"]})
                        large_chunks.append(chunk["content"]); large_offset = chunk["next_offset"]
                        if chunk["eof"]: break
                    report("large_file_multiple_chunks", len(large_chunks) > 1)
                    report("large_file_sha", hashlib.sha256("".join(large_chunks).encode()).hexdigest() == large_manifest["content_sha256"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", default="docs/MYGITHUB10_TOOL_MANIFEST.json")
    parser.add_argument("--base-url", default=os.environ.get("CONTROLLER_URL", ""))
    parser.add_argument("--simulate", action="store_true")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args(); path = Path(args.manifest)
    try:
        if args.simulate or not args.base_url: simulated(path)
        else: asyncio.run(live(args.base_url, os.environ.get("ACTION_API_KEY", ""), path, args.strict))
        print(json.dumps({"ok": True, "manifest_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}))
        return 0
    except (OSError, ValueError, KeyError, RuntimeError, httpx.HTTPError) as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__ + ": " + str(exc)}, ensure_ascii=False)); return 1


if __name__ == "__main__": sys.exit(main())
