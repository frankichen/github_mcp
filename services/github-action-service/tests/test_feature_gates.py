import json

import pytest


@pytest.mark.asyncio
async def test_artifact_write_gates_are_hard_disabled(monkeypatch):
    monkeypatch.delenv("MYGITHUB10_ARTIFACT_BUILD_ENABLED", raising=False)
    monkeypatch.delenv("MYGITHUB10_ARTIFACT_DEPLOY_ENABLED", raising=False)
    from app.mcp_server import build_release_artifact, plan_test_deployment, start_test_deployment
    assert json.loads(await build_release_artifact("frankichen/sxt", "c" * 40, "job", "att"))["error_code"] == "FEATURE_DISABLED"
    assert json.loads(await plan_test_deployment("frankichen/sxt", "gongshi-test", "c" * 40, "job", artifact_id="art"))["error_code"] == "FEATURE_DISABLED"
    assert json.loads(await start_test_deployment("frankichen/sxt", "gongshi-test", "c" * 40, "job", artifact_id="art"))["error_code"] == "FEATURE_DISABLED"


@pytest.mark.asyncio
async def test_repository_policy_is_enforced_at_real_mcp_entries(monkeypatch):
    from app.mcp_server import create_github_branch, apply_github_patch, build_release_artifact, plan_test_deployment
    denied = json.loads(await create_github_branch("frankichen/unknown", "feature"))
    assert denied["error_code"] == "REPOSITORY_OPERATION_DENIED"
    allowed = json.loads(await apply_github_patch("frankichen/github_mcp", "main", "0" * 40, "{}", "", "test"))
    assert allowed.get("error", {}).get("code") in {"PATCH_EMPTY", "PATCH_HEAD_CHANGED", "PATCH_INVALID_FORMAT"}
    denied = json.loads(await plan_test_deployment("frankichen/github_mcp", "gongshi-test", "c" * 40, "job"))
    assert denied["error_code"] == "REPOSITORY_OPERATION_DENIED"
    denied = json.loads(await build_release_artifact("frankichen/github_mcp", "c" * 40, "job", "att"))
    assert denied["error_code"] == "REPOSITORY_OPERATION_DENIED"
    from app.mcp_server import mcp
    from app import ci_mcp
    monkeypatch.setattr(ci_mcp, "is_repository_allowed", lambda *_: True)
    monkeypatch.setattr(ci_mcp, "is_profile_allowed", lambda *_: False)
    result = await mcp.call_tool("start_private_ci_job", {"repository": "frankichen/github_mcp", "branch": "main", "commit_sha": "c" * 40})
    payload = result[0][0].text if isinstance(result[0], (list, tuple)) else result[0].text
    assert json.loads(payload)["error"]["code"] == "PRIVATE_CI_PROFILE_NOT_ALLOWED"
    result = await mcp.call_tool("start_private_ci_job", {"repository": "unknown/repo", "branch": "main", "commit_sha": "c" * 40})
    payload = result[0][0].text if isinstance(result[0], (list, tuple)) else result[0].text
    assert json.loads(payload)["error_code"] == "REPOSITORY_OPERATION_DENIED"
