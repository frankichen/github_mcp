import json
from pathlib import Path

import pytest

from app.mcp_server import mcp


@pytest.mark.asyncio
async def test_checked_in_manifest_matches_registered_tools():
    candidates = [parent / "docs" / "MYGITHUB10_TOOL_MANIFEST.json" for parent in Path(__file__).parents]
    manifest_path = next((path for path in candidates if path.is_file()), Path("/app/docs/MYGITHUB10_TOOL_MANIFEST.json"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    actual = await mcp.list_tools()
    actual_names = [tool.name for tool in actual]
    assert actual_names == [tool["name"] for tool in manifest["tools"]]
    assert manifest["tool_count"] == len(actual_names)
    assert len(actual_names) == len(set(actual_names))
