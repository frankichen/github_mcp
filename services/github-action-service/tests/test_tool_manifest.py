import pytest

from app.mcp_server import mcp


EXPECTED_TOOL_COUNT = 84


@pytest.mark.asyncio
async def test_registered_tool_manifest_is_stable_and_unique():
    actual = await mcp.list_tools()
    actual_names = [tool.name for tool in actual]

    assert len(actual_names) == EXPECTED_TOOL_COUNT
    assert len(actual_names) == len(set(actual_names))
    assert all(name and name.strip() == name for name in actual_names)
