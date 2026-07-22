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
