import json
import time

import pytest
from pydantic import SecretStr


@pytest.mark.asyncio
async def test_history_tools_fail_closed_before_query(monkeypatch):
    from app import github_utils
    from app.mcp_server import (
        get_github_development_history_tool,
        get_github_weekly_report_data_tool,
        list_github_issue_history_tool,
        list_github_review_history_tool,
        search_github_pull_request_history_tool,
    )

    def should_not_query(*_args, **_kwargs):
        raise AssertionError("query must not run before policy validation")

    for name in (
        "search_github_pull_request_history", "list_github_review_history",
        "list_github_issue_history", "get_github_development_history",
        "get_github_weekly_report_data",
    ):
        monkeypatch.setattr(github_utils, name, should_not_query)

    assert json.loads(await search_github_pull_request_history_tool("[]", "alice"))["error_code"] == "INVALID_ARGUMENT"
    assert json.loads(await list_github_review_history_tool("[\"unknown/repo\"]", "alice"))["error_code"] == "REPOSITORY_OPERATION_DENIED"
    assert json.loads(await list_github_issue_history_tool("[\"frankichen/sxt\",\"unknown/repo\"]", "alice"))["error_code"] == "REPOSITORY_OPERATION_DENIED"
    assert json.loads(await get_github_development_history_tool("alice", "[]"))["error_code"] == "INVALID_ARGUMENT"
    assert json.loads(await get_github_weekly_report_data_tool("alice", "[\"unknown/repo\"]"))["error_code"] == "REPOSITORY_OPERATION_DENIED"


@pytest.mark.asyncio
async def test_private_ci_list_tools_require_repository():
    from app.mcp_server import mcp

    jobs = mcp._tool_manager._tools["list_private_ci_jobs"].fn
    profiles = mcp._tool_manager._tools["list_private_ci_profiles"].fn
    assert json.loads(await jobs(repository=""))["error"]["code"] == "INVALID_ARGUMENT"
    assert json.loads(await profiles(repository=""))["error"]["code"] == "INVALID_ARGUMENT"


@pytest.mark.asyncio
async def test_resource_capability_and_hmac_guards(monkeypatch):
    from app import mygithub10
    from app import mcp_server

    assert mygithub10.capabilities("a" * 40, "")["supports_mcp_resources"] is False
    assert mygithub10.capabilities("a" * 40, "short")["supports_mcp_resources"] is False
    assert mygithub10.capabilities("a" * 40, "x" * 32)["supports_mcp_resources"] is True

    monkeypatch.setattr(mcp_server.app_settings, "MYGITHUB10_RESOURCE_TOKEN_SECRET", SecretStr("x" * 32))
    monkeypatch.setattr(mcp_server, "_service", object())
    monkeypatch.setattr(mygithub10, "file_manifest", lambda *_args: {
        "repository": "frankichen/sxt", "path": "README.md", "resolved_commit_sha": "a" * 40,
        "blob_sha": "b" * 40, "size_bytes": 3, "content_sha256": "c" * 64,
    })
    monkeypatch.setattr(mygithub10, "file_chunk", lambda *_args: {"ok": True, "content": "ok", "eof": True})

    opened = json.loads(await mcp_server.open_github_file_resource("frankichen/sxt", "README.md", "main"))
    assert opened["resource_uri"].startswith("mygithub10://blob/")
    assert json.loads(await mcp_server.read_github_file_resource(opened["resource_uri"]))["ok"] is True
    tampered = opened["resource_uri"][:-1] + ("0" if opened["resource_uri"][-1] != "0" else "1")
    assert json.loads(await mcp_server.read_github_file_resource(tampered))["error"]["code"] == "RESOURCE_TOKEN_INVALID"

    expired = mcp_server._resource_token({"repository": "frankichen/sxt", "path": "README.md", "commit": "a" * 40, "expires_at": int(time.time()) - 1})
    assert json.loads(await mcp_server.read_github_file_resource(f"mygithub10://blob/{expired}"))["error"]["code"] == "RESOURCE_TOKEN_EXPIRED"
    assert json.loads(await mcp_server.open_github_file_resource("unknown/repo", "README.md", "main"))["error_code"] == "REPOSITORY_OPERATION_DENIED"
