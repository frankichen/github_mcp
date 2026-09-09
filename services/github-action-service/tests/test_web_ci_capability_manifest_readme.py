import json
import re
from pathlib import Path

import pytest

from app.mcp_server import get_mygithub_capabilities, mcp as service_mcp


TARGET_TOOLS = (
    "start_private_ci_job",
    "get_private_ci_job",
    "wait_private_ci_job",
    "validate_development_task",
    "converge_development_task",
)
ANNOTATION_FIELDS = (
    "readOnlyHint",
    "destructiveHint",
    "idempotentHint",
    "openWorldHint",
)
EXPECTED_WEB_SAFE_PRIVATE_CI = {
    "supported": True,
    "canonical_start_tool": "start_private_ci_job",
    "canonical_snapshot_tool": "get_private_ci_job",
    "recommended_continuation": "snapshot_resume",
    "compatibility_wait": {
        "tool": "wait_private_ci_job",
        "compatibility_only": True,
        "recommended": False,
        "canonical_schema_exposed": False,
    },
    "fast_ci": {
        "purpose": "feedback_only",
        "merge_eligible": False,
    },
    "full_ci": {
        "purpose": "formal_gate_candidate",
    },
    "formal_reuse_and_merge_gate": {
        "requires_full_ci": True,
        "requires_reusable_attestation": True,
        "requires_exact_head_tree": True,
    },
}


def _root() -> Path:
    return Path(__file__).resolve().parents[3]


def _manifest() -> dict:
    return json.loads(
        (_root() / "docs" / "MYGITHUB12_TOOL_MANIFEST.json").read_text(encoding="utf-8")
    )


def _tool_contract(tool, visibility: str) -> dict:
    properties = tool.inputSchema["properties"]
    return {
        "visibility": visibility,
        "description": tool.description or "",
        "parameters": list(properties),
        "required": list(tool.inputSchema.get("required", [])),
        "defaults": {
            name: schema["default"]
            for name, schema in properties.items()
            if "default" in schema
        },
    }


def _annotations(tool) -> dict:
    assert tool.annotations is not None
    return {field: getattr(tool.annotations, field) for field in ANNOTATION_FIELDS}


@pytest.mark.asyncio
async def test_web_safe_private_ci_capability_matches_manifest(monkeypatch):
    monkeypatch.setenv("MYGITHUB12_RUNTIME_MODE", "production")
    monkeypatch.setenv("MYGITHUB12_BUILD_SHA", "a" * 40)
    monkeypatch.setenv("MYGITHUB12_EXPOSE_DEPRECATED_TOOLS", "false")

    runtime = json.loads(await get_mygithub_capabilities())
    manifest = _manifest()

    assert runtime["web_safe_private_ci"] == EXPECTED_WEB_SAFE_PRIVATE_CI
    assert manifest["web_safe_private_ci"] == EXPECTED_WEB_SAFE_PRIVATE_CI

    contract = runtime["web_safe_private_ci"]
    assert contract["canonical_start_tool"] == "start_private_ci_job"
    assert contract["canonical_snapshot_tool"] == "get_private_ci_job"
    assert contract["compatibility_wait"]["compatibility_only"] is True
    assert contract["compatibility_wait"]["recommended"] is False
    assert contract["fast_ci"]["merge_eligible"] is False
    assert contract["full_ci"]["purpose"] == "formal_gate_candidate"
    assert contract["formal_reuse_and_merge_gate"] == {
        "requires_full_ci": True,
        "requires_reusable_attestation": True,
        "requires_exact_head_tree": True,
    }


@pytest.mark.asyncio
async def test_web_ci_runtime_manifest_description_parameter_default_parity(monkeypatch):
    manifest = _manifest()

    monkeypatch.setenv("MYGITHUB12_RUNTIME_MODE", "production")
    monkeypatch.setenv("MYGITHUB12_BUILD_SHA", "b" * 40)
    monkeypatch.setenv("MYGITHUB12_EXPOSE_DEPRECATED_TOOLS", "true")
    compatibility = await service_mcp.list_tools()
    compatibility_by_name = {tool.name: tool for tool in compatibility}
    capabilities = json.loads(await get_mygithub_capabilities())

    actual = {
        name: _tool_contract(
            compatibility_by_name[name],
            "compatibility_only" if name == "wait_private_ci_job" else "canonical",
        )
        for name in TARGET_TOOLS
    }
    deprecated = {
        item["name"]: item for item in capabilities["deprecated_tools"]
    }
    actual["wait_private_ci_job"]["deprecation"] = deprecated["wait_private_ci_job"]

    assert actual == manifest["web_ci_tool_contract_snapshot"]

    annotation_snapshot = manifest["tool_annotation_snapshot"]
    for name in TARGET_TOOLS:
        assert _annotations(compatibility_by_name[name]) == {
            field: annotation_snapshot[name][field] for field in ANNOTATION_FIELDS
        }

    monkeypatch.setenv("MYGITHUB12_EXPOSE_DEPRECATED_TOOLS", "false")
    canonical_names = {tool.name for tool in await service_mcp.list_tools()}
    assert "wait_private_ci_job" not in canonical_names
    assert set(TARGET_TOOLS) - {"wait_private_ci_job"} <= canonical_names


def test_readme_exposes_complete_web_safe_private_ci_flow():
    readme = (_root() / "README.md").read_text(encoding="utf-8")
    section = readme.split("### Web-safe Private CI canonical flow", 1)[1].split(
        "\n## MyGithut12 运行状态", 1
    )[0]

    ordered = (
        "`start_private_ci_job`",
        "`get_private_ci_job`",
        "terminal Full CI",
        "reusable Attestation",
        "exact HEAD/Tree",
    )
    positions = [section.index(item) for item in ordered]
    assert positions == sorted(positions)
    assert "Fast CI" in section and "feedback only" in section
    assert "Fast CI != merge eligible" in section
    assert "Full CI" in section and "formal CI gate candidate" in section
    assert "`wait_private_ci_job`" in section
    assert "compatibility-only" in section
    assert "must not loop" in section
    assert "55" not in section


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
