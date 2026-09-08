import json

import pytest

from app import ci_database as db
from app import ci_mcp
from app import ci_request_store as requests
from app.mcp_response import StructuredFastMCP
from app.mcp_server import get_mygithub_capabilities, mcp as service_mcp


REPOSITORY = "owner/repo"
BRANCH = "feature/compatibility-contract"
COMMIT = "a" * 40
TREE = "b" * 40
PROFILE = "repo-auto-check"
WAIT_DEPRECATION_GUIDANCE = (
    "Legacy wait is compatibility-only for existing/debug clients. Canonical Web CI uses "
    "start_private_ci_job then get_private_ci_job snapshots and must not loop wait to terminal. "
    "The 55-second bound is legacy behavior, not an OpenAI/ChatGPT Web timeout SLA. Fast feedback "
    "is not merge-eligible; the formal gate requires full CI plus a reusable attestation."
)
COMPATIBILITY_ONLY_TOOLS = [
    "append_github_file_upload_chunk",
    "begin_github_file_upload",
    "commit_github_files",
    "commit_github_uploaded_files",
    "finalize_github_file_upload",
    "get_github_file",
    "get_test_deployment_logs",
    "put_github_file",
    "put_github_file_from_local_candidate",
    "put_github_files",
    "wait_private_ci_job",
]


def _structured_result(call_result):
    if isinstance(call_result, tuple):
        return call_result[1]
    structured = getattr(call_result, "structured_content", None)
    if structured is None:
        structured = getattr(call_result, "structuredContent", None)
    return structured


def _close_db():
    current = getattr(db._local, "db", None)
    if current is not None:
        current.close()
    db._local.db = None


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "ci.db"))
    _close_db()
    db.init_db()
    yield tmp_path
    _close_db()


@pytest.fixture
def ci_tools(isolated_db):
    mcp = StructuredFastMCP("web-ci-dev018-compatibility-contract")
    ci_mcp.register_private_ci_mcp_tools(mcp)
    return mcp


def _annotation_contract(tool):
    assert tool.annotations is not None
    return {
        "readOnlyHint": tool.annotations.readOnlyHint,
        "destructiveHint": tool.annotations.destructiveHint,
        "idempotentHint": tool.annotations.idempotentHint,
        "openWorldHint": tool.annotations.openWorldHint,
    }


def _parameter_contract(tool):
    properties = tool.inputSchema["properties"]
    projected = {}
    for name, schema in properties.items():
        item = {"type": schema.get("type")}
        if "default" in schema:
            item["default"] = schema["default"]
        projected[name] = item
    return {
        "parameters": projected,
        "required": list(tool.inputSchema.get("required", [])),
    }


def _tool_contract(tool):
    return {
        "name": tool.name,
        "description": tool.description or "",
        **_parameter_contract(tool),
        "annotations": _annotation_contract(tool),
    }


@pytest.mark.asyncio
async def test_canonical_and_compatibility_schema_have_one_intentional_structural_delta(monkeypatch):
    monkeypatch.setenv("MYGITHUB12_RUNTIME_MODE", "production")
    monkeypatch.setenv("MYGITHUB12_BUILD_SHA", "c" * 40)
    monkeypatch.setenv("MYGITHUB12_EXPOSE_DEPRECATED_TOOLS", "false")
    canonical = await service_mcp.list_tools()
    canonical_by_name = {tool.name: tool for tool in canonical}
    canonical_capabilities = json.loads(await get_mygithub_capabilities())

    monkeypatch.setenv("MYGITHUB12_EXPOSE_DEPRECATED_TOOLS", "true")
    compatibility = await service_mcp.list_tools()
    compatibility_by_name = {tool.name: tool for tool in compatibility}
    compatibility_capabilities = json.loads(await get_mygithub_capabilities())

    assert sorted(set(compatibility_by_name) - set(canonical_by_name)) == COMPATIBILITY_ONLY_TOOLS
    assert set(canonical_by_name) <= set(compatibility_by_name)
    for name, canonical_tool in canonical_by_name.items():
        assert _tool_contract(canonical_tool) == _tool_contract(compatibility_by_name[name])

    wait_contract = _tool_contract(compatibility_by_name["wait_private_ci_job"])
    assert wait_contract == {
        "name": "wait_private_ci_job",
        "description": (
            "Deprecated compatibility-only long-poll for explicit legacy/debug callers. Retains the "
            "legacy up-to-55-second status/step/revision wait contract. It is not the canonical ChatGPT "
            "Web CI tracking path; normal Web continuation uses get_private_ci_job snapshots and must not "
            "loop this tool until terminal."
        ),
        "parameters": {
            "job_id": {"type": "string"},
            "timeout_seconds": {"type": "integer", "default": 55},
            "last_known_status": {"type": "string", "default": ""},
            "last_known_step": {"type": "string", "default": ""},
            "last_known_revision": {"type": "integer", "default": 0},
        },
        "required": ["job_id"],
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    }

    canonical_deprecated = {
        item["name"]: item for item in canonical_capabilities["deprecated_tools"]
    }
    compatibility_deprecated = {
        item["name"]: item for item in compatibility_capabilities["deprecated_tools"]
    }
    expected_wait_metadata = {
        "name": "wait_private_ci_job",
        "deprecated": True,
        "compatibility_only": True,
        "replacement": "get_private_ci_job",
        "guidance": WAIT_DEPRECATION_GUIDANCE,
    }
    assert canonical_deprecated["wait_private_ci_job"] == expected_wait_metadata
    assert compatibility_deprecated["wait_private_ci_job"] == expected_wait_metadata
    assert "wait_private_ci_job" in canonical_capabilities["hidden_deprecated_tools"]
    assert "wait_private_ci_job" not in compatibility_capabilities["hidden_deprecated_tools"]
    assert canonical_capabilities["tool_schema_sha256"] != compatibility_capabilities["tool_schema_sha256"]


@pytest.mark.asyncio
async def test_legacy_wait_client_contract_preserves_running_timeout_and_terminal_truth(monkeypatch):
    monkeypatch.setenv("MYGITHUB12_EXPOSE_DEPRECATED_TOOLS", "true")
    mcp = StructuredFastMCP("web-ci-dev018-legacy-client")
    calls = []
    responses = iter(
        [
            {
                "ok": True,
                "job_id": "job-legacy-client",
                "status": "running",
                "changed": True,
                "timed_out": False,
                "terminal": False,
            },
            {
                "ok": True,
                "job_id": "job-legacy-client",
                "status": "running",
                "changed": False,
                "timed_out": True,
                "terminal": False,
            },
            {
                "ok": True,
                "job_id": "job-legacy-client",
                "status": "passed",
                "changed": True,
                "timed_out": False,
                "terminal": True,
            },
        ]
    )

    def fake_wait(job_id, timeout_seconds, last_known_status, last_known_step, last_known_revision):
        calls.append((job_id, timeout_seconds, last_known_status, last_known_step, last_known_revision))
        return next(responses)

    monkeypatch.setattr(ci_mcp, "wait_for_job_change", fake_wait)
    ci_mcp.register_private_ci_mcp_tools(mcp)
    assert "wait_private_ci_job" in {tool.name for tool in await mcp.list_tools()}

    legacy_arguments = {
        "job_id": "job-legacy-client",
        "timeout_seconds": 55,
        "last_known_status": "running",
        "last_known_step": "pytest",
        "last_known_revision": 9,
    }
    running = _structured_result(await mcp.call_tool("wait_private_ci_job", legacy_arguments))
    timed_out = _structured_result(await mcp.call_tool("wait_private_ci_job", legacy_arguments))
    terminal = _structured_result(await mcp.call_tool("wait_private_ci_job", legacy_arguments))

    assert calls == [
        ("job-legacy-client", 55, "running", "pytest", 9),
        ("job-legacy-client", 55, "running", "pytest", 9),
        ("job-legacy-client", 55, "running", "pytest", 9),
    ]
    assert running["status"] == "running" and running["terminal"] is False
    assert timed_out["timed_out"] is True
    assert timed_out["status"] == "running" and timed_out["terminal"] is False
    assert terminal["status"] == "passed" and terminal["terminal"] is True


async def _snapshot(mcp, **arguments):
    return _structured_result(await mcp.call_tool("get_private_ci_job", arguments))


def _create_running_request_and_job():
    identity = {
        "repository": REPOSITORY,
        "branch": BRANCH,
        "commit_sha": COMMIT,
        "tree_sha": TREE,
        "profile": PROFILE,
        "effective_config_digest": "config-v1",
    }
    request_hash = requests.compute_normalized_request_hash(
        {**identity, "timeout_seconds": 900, "priority": "normal"}
    )
    request = requests.create_or_get_ci_request(
        **identity,
        idempotency_key="dev018-running-identity",
        normalized_request_hash=request_hash,
    )
    request = requests.transition_ci_request(
        request["request_id"], 0, "preparing", "preparing"
    )
    job = db.create_or_get_job(
        repository=REPOSITORY,
        branch=BRANCH,
        commit_sha=COMMIT,
        profile=PROFILE,
        priority=100,
        timeout_seconds=900,
        force_rerun=True,
        supersede_previous=False,
    )
    request = requests.transition_ci_request(
        request["request_id"],
        1,
        "queued",
        "queued",
        worker_job_id=job["job_id"],
    )
    request = requests.transition_ci_request(
        request["request_id"], 2, "running", "running"
    )
    return request, job


@pytest.mark.asyncio
async def test_running_request_and_job_identity_survive_store_reopen_without_new_job(ci_tools):
    request, job = _create_running_request_and_job()
    before = await _snapshot(ci_tools, request_id=request["request_id"])
    identity_fields = ("request_id", "job_id", "commit_sha", "tree_sha", "branch", "profile")
    expected = {
        "request_id": request["request_id"],
        "job_id": job["job_id"],
        "commit_sha": COMMIT,
        "tree_sha": TREE,
        "branch": BRANCH,
        "profile": PROFILE,
    }
    assert {field: before[field] for field in identity_fields} == expected
    assert before["status"] == "running"

    connection = db._get_db()
    counts_before = {
        "requests": connection.execute("SELECT COUNT(*) FROM ci_requests").fetchone()[0],
        "jobs": connection.execute("SELECT COUNT(*) FROM ci_jobs").fetchone()[0],
    }
    _close_db()
    db.init_db()

    after = await _snapshot(ci_tools, job_id=job["job_id"])
    connection = db._get_db()
    counts_after = {
        "requests": connection.execute("SELECT COUNT(*) FROM ci_requests").fetchone()[0],
        "jobs": connection.execute("SELECT COUNT(*) FROM ci_jobs").fetchone()[0],
    }
    assert {field: after[field] for field in identity_fields} == expected
    assert after["status"] == "running"
    assert counts_after == counts_before == {"requests": 1, "jobs": 1}


@pytest.mark.asyncio
async def test_old_job_missing_new_evidence_remains_queryable_with_bounded_diagnostics(ci_tools):
    legacy = db.create_or_get_job(
        repository=REPOSITORY,
        branch="legacy/main",
        commit_sha="d" * 40,
        profile=PROFILE,
        priority=100,
        timeout_seconds=900,
        force_rerun=True,
        supersede_previous=False,
    )
    db.append_log_chunk(legacy["job_id"], "token=raw-secret legacy failure\n")
    db.append_log_chunk(legacy["job_id"], "second diagnostic line\n")
    connection = db._get_db()
    connection.execute(
        "UPDATE ci_jobs SET status='failed', exit_code=1, error_code='LEGACY_FAILURE' WHERE job_id=?",
        (legacy["job_id"],),
    )
    connection.commit()

    requests.init_ci_request_schema(connection)
    snapshot = await _snapshot(ci_tools, job_id=legacy["job_id"])
    assert snapshot["ok"] is True
    assert snapshot["request_id"] == legacy["job_id"]
    assert snapshot["job_id"] == legacy["job_id"]
    assert snapshot["commit_sha"] == "d" * 40
    assert snapshot["branch"] == "legacy/main"
    assert snapshot["profile"] == PROFILE
    assert snapshot["tree_sha"] is None
    assert snapshot["failure_pack_available"] is False
    assert snapshot["attestation_available"] is False
    assert snapshot["status"] == "failed"

    logs = _structured_result(
        await ci_tools.call_tool(
            "get_private_ci_logs", {"job_id": legacy["job_id"], "offset": 0, "limit": 1}
        )
    )
    serialized = json.dumps(logs, ensure_ascii=False)
    assert logs["ok"] is True
    assert logs["mode"] == "job"
    assert len(logs["chunks"]) == 1
    assert logs["has_more"] is True
    assert logs["next_offset"] is not None
    assert "raw-secret" not in serialized
    assert "[REDACTED]" in serialized


def test_wait_deprecation_guidance_is_ai_safe_and_keeps_merge_gate_truth():
    assert "compatibility-only" in WAIT_DEPRECATION_GUIDANCE
    assert "start_private_ci_job then get_private_ci_job snapshots" in WAIT_DEPRECATION_GUIDANCE
    assert "must not loop wait to terminal" in WAIT_DEPRECATION_GUIDANCE
    assert "not an OpenAI/ChatGPT Web timeout SLA" in WAIT_DEPRECATION_GUIDANCE
    assert "Fast feedback is not merge-eligible" in WAIT_DEPRECATION_GUIDANCE
    assert "full CI plus a reusable attestation" in WAIT_DEPRECATION_GUIDANCE
