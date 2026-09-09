import json
import os
import re
from pathlib import Path

import pytest

from app.mcp_server import get_mygithub_capabilities, mcp as service_mcp


CURRENT_WEB_CI_CONTRACT_TOOLS = (
    "start_private_ci_job",
    "get_private_ci_job",
    "get_private_ci_logs",
    "list_private_ci_workers",
    "wait_private_ci_job",
    "validate_development_task",
    "converge_development_task",
    "get_github_pull_request_merge_readiness",
    "plan_github_pull_request_merge",
    "merge_github_pull_request",
    "create_attestation_for_passed_job",
    "validate_attestation",
)
ANNOTATION_FIELDS = (
    "readOnlyHint",
    "destructiveHint",
    "idempotentHint",
    "openWorldHint",
)


def _root() -> Path:
    configured = os.environ.get("CI_REPOSITORY_ROOT", "")
    if configured:
        return Path(configured)
    path = Path(__file__).resolve()
    return path.parents[3] if len(path.parents) > 3 else path.parents[1]


def _manifest() -> dict:
    return json.loads(
        (_root() / "docs" / "MYGITHUB12_TOOL_MANIFEST.json").read_text(encoding="utf-8")
    )


def _annotations(tool):
    if tool.annotations is None:
        return None
    return {field: getattr(tool.annotations, field) for field in ANNOTATION_FIELDS}


def _runtime_contract(tool, *, hidden: set[str], deprecated_by_name: dict[str, dict]) -> dict:
    deprecation = deprecated_by_name.get(tool.name)
    compatibility_only = bool((deprecation or {}).get("compatibility_only"))
    return {
        "name": tool.name,
        "description": tool.description or "",
        "input_schema": tool.inputSchema,
        "output_schema": tool.outputSchema,
        "annotations": _annotations(tool),
        "deprecated": bool(deprecation),
        "compatibility_only": compatibility_only,
        "visibility": "compatibility_only" if tool.name in hidden else "canonical",
        "exposed_by_default": tool.name not in hidden,
        "deprecation": deprecation,
    }


@pytest.mark.asyncio
async def test_web_safe_private_ci_capability_matches_canonical_manifest(monkeypatch):
    manifest = _manifest()
    monkeypatch.setenv("MYGITHUB12_RUNTIME_MODE", "production")
    monkeypatch.setenv("MYGITHUB12_BUILD_SHA", "a" * 40)
    monkeypatch.setenv("MYGITHUB12_EXPOSE_DEPRECATED_TOOLS", "false")

    runtime = json.loads(await get_mygithub_capabilities())
    contract = runtime["web_safe_private_ci"]

    assert contract == manifest["web_safe_private_ci"]
    assert contract["schema_version"] == 1
    assert contract["canonical_start_tool"] == "start_private_ci_job"
    assert contract["canonical_snapshot_tool"] == "get_private_ci_job"
    assert contract["continuation_strategy"] == "snapshot_resume"
    assert contract["formal_writer_route"] == "managed_development"
    assert contract["direct_private_ci"] == {
        "use_case": "standalone_exact_commit_ci",
        "start_tool": "start_private_ci_job",
        "snapshot_tool": "get_private_ci_job",
    }
    managed = contract["managed_development"]
    assert managed["formal_writer_recommended"] is True
    assert managed["validate_tool"] == "validate_development_task"
    assert managed["converge_tool"] == "converge_development_task"
    assert managed["finalize_tool"] == "finalize_development_task"
    assert managed["converge_compatibility_wait_inputs"] == {
        "parameters": ["index_wait_seconds", "wait_seconds"],
        "compatibility_only": True,
        "accepted_but_ignored": True,
        "blocking_wait": False,
    }
    assert contract["fast_feedback"] == {
        "purpose": "feedback_only",
        "profile": "repo-fast-check",
        "merge_eligible": False,
    }
    assert contract["full_ci"] == {
        "formal_gate_candidate": True,
        "profile": "repo-auto-check",
    }
    assert contract["attestation"]["required"] is True
    assert contract["attestation"]["reusable_required"] is True
    assert contract["identity"] == {
        "commit_sha_required": True,
        "tree_sha_required": True,
    }
    assert contract["legacy_wait"]["compatibility_only"] is True
    assert contract["legacy_wait"]["recommended"] is False
    assert contract["readiness_tool"] == "get_github_pull_request_merge_readiness"
    assert contract["merge_plan_tool"] == "plan_github_pull_request_merge"
    assert contract["merge_tool"] == "merge_github_pull_request"
    assert contract["shared_formal_evidence"] == [
        "full_private_ci",
        "reusable_attestation",
        "exact_commit_sha",
        "exact_tree_sha",
    ]


@pytest.mark.asyncio
async def test_web_ci_current_manifest_is_exact_runtime_registration_contract(monkeypatch):
    manifest = _manifest()
    assert manifest["legacy_manifest_role"] == "historical_inventory_snapshot_only"
    assert manifest["current_contract_source"] == "runtime_registration"
    assert manifest["current_contract_scope"] == list(CURRENT_WEB_CI_CONTRACT_TOOLS)

    monkeypatch.setenv("MYGITHUB12_RUNTIME_MODE", "production")
    monkeypatch.setenv("MYGITHUB12_BUILD_SHA", "b" * 40)
    monkeypatch.setenv("MYGITHUB12_EXPOSE_DEPRECATED_TOOLS", "true")
    compatibility = await service_mcp.list_tools()
    compatibility_by_name = {tool.name: tool for tool in compatibility}
    capabilities = json.loads(await get_mygithub_capabilities())
    deprecated_by_name = {
        item["name"]: item for item in capabilities["deprecated_tools"]
    }
    hidden = set(manifest["hidden_deprecated_tools"])

    actual = {
        name: _runtime_contract(
            compatibility_by_name[name],
            hidden=hidden,
            deprecated_by_name=deprecated_by_name,
        )
        for name in CURRENT_WEB_CI_CONTRACT_TOOLS
    }
    assert actual == manifest["current_tool_contracts"]

    # Full schemas make type/required/default/enum/output parity structural,
    # rather than a second hand-maintained list of selected defaults.
    for name, contract in actual.items():
        assert contract["name"] == name
        assert contract["input_schema"]["type"] == "object"
        assert "properties" in contract["input_schema"]
        assert contract["output_schema"] == manifest["current_tool_contracts"][name]["output_schema"]

    monkeypatch.setenv("MYGITHUB12_EXPOSE_DEPRECATED_TOOLS", "false")
    canonical_names = {tool.name for tool in await service_mcp.list_tools()}
    assert "wait_private_ci_job" not in canonical_names
    assert set(CURRENT_WEB_CI_CONTRACT_TOOLS) - {"wait_private_ci_job"} <= canonical_names
    wait = actual["wait_private_ci_job"]
    assert wait["deprecated"] is True
    assert wait["compatibility_only"] is True
    assert wait["visibility"] == "compatibility_only"
    assert wait["exposed_by_default"] is False


def test_readme_exposes_managed_and_direct_formal_web_ci_flows():
    readme = (_root() / "README.md").read_text(encoding="utf-8")
    section = readme.split("### Web-safe Private CI canonical flow", 1)[1].split(
        "\n## MyGithut12 运行状态", 1
    )[0]
    canonical = section.split("\n\nMyGithut12 源码当前版本", 1)[0]

    managed = re.search(
        r"#### Managed Writer / formal development\n\n```text\n(?P<flow>.*?)\n```",
        canonical,
        re.DOTALL,
    )
    direct = re.search(
        r"#### Direct Private CI\n\n```text\n(?P<flow>.*?)\n```",
        canonical,
        re.DOTALL,
    )
    assert managed is not None and direct is not None
    assert managed.group("flow").splitlines() == [
        "prepare_development_task / resume_development_task",
        "→ validate_development_task or converge_development_task(mode=full)",
        "→ terminal Full CI passed",
        "→ reusable Attestation",
        "→ exact HEAD/Tree readiness",
        "→ explicit merge",
    ]
    assert direct.group("flow").splitlines() == [
        "start_private_ci_job",
        "→ durable Request accepted",
        "→ get_private_ci_job snapshots",
        "→ terminal Full CI passed",
        "→ reusable Attestation",
        "→ exact HEAD/Tree readiness",
    ]
    assert "正式 Writer 推荐" in canonical
    assert "standalone exact-commit CI" in canonical
    assert "共享同一组正式 merge evidence" in canonical
    assert "Fast CI != merge gate" in canonical
    assert "`wait_private_ci_job`" in canonical
    assert "compatibility-only" in canonical
    assert "must not loop" in canonical
    assert "55" not in canonical
    assert "OpenAI" in canonical and "ChatGPT" in canonical and "SLA" in canonical


def test_product_facing_timeout_wording_never_claims_a_numbered_openai_chatgpt_sla():
    roots = [
        _root() / "README.md",
        _root() / "docs",
        _root() / "services" / "github-action-service" / "app",
    ]
    text_suffixes = {".md", ".py", ".json", ".yaml", ".yml", ".toml", ".txt"}
    brand_timeout = re.compile(r"(OpenAI|ChatGPT).*timeout", re.IGNORECASE)
    numbered = re.compile(r"\d+")
    negations = (
        "not ",
        "not an ",
        "不是",
        "并非",
        "不得",
        "没有",
        "未",
        "而非",
        "不把",
        "不再",
    )

    candidates = []
    for root in roots:
        paths = [root] if root.is_file() else root.rglob("*")
        for path in paths:
            if not path.is_file() or path.suffix.lower() not in text_suffixes:
                continue
            for line_number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if brand_timeout.search(line) and numbered.search(line):
                    candidates.append((path, line_number, line))
                    assert any(marker in line for marker in negations), (
                        f"{path}:{line_number} looks like a numbered OpenAI/ChatGPT "
                        "timeout claim without an explicit negation"
                    )

    assert candidates


def test_capabilities_helper_leaves_runtime_schema_counts_to_registration_identity():
    from app import mygithub10

    helper = mygithub10.capabilities("c" * 40)
    assert "tool_count" not in helper
    assert "tool_manifest_count" not in helper


def test_converge_wait_inputs_are_manifested_as_ignored_compatibility_inputs():
    contract = _manifest()["current_tool_contracts"]["converge_development_task"]
    assert "compatibility-only accepted-but-ignored" in contract["description"]
    properties = contract["input_schema"]["properties"]
    assert properties["index_wait_seconds"]["default"] == 55
    assert properties["wait_seconds"]["default"] == 55
    routing = _manifest()["web_safe_private_ci"]["managed_development"]
    assert routing["converge_compatibility_wait_inputs"] == {
        "parameters": ["index_wait_seconds", "wait_seconds"],
        "compatibility_only": True,
        "accepted_but_ignored": True,
        "blocking_wait": False,
    }
