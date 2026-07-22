import json
from pathlib import Path

import pytest

from app.mcp_server import mcp


@pytest.mark.asyncio
async def test_checked_in_manifest_matches_registered_tools():
    manifest_path = Path(__file__).parents[3] / "docs" / "MYGITHUB09_TOOL_MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    actual = await mcp.list_tools()
    actual_names = [tool.name for tool in actual]
    assert actual_names == [tool["name"] for tool in manifest["tools"]]
    assert manifest["tool_count"] == len(actual_names)
    assert len(actual_names) == len(set(actual_names))
