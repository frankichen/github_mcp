"""Retired frontend workspaces must not run in SXT private CI."""
from pathlib import Path
import yaml

CONFIG_PATH = Path(__file__).parents[1] / "deploy" / "repositories.yml"


def test_sxt_all_legacy_and_frontend_node_workspaces_are_disabled():
    data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    workspaces = data["repositories"]["frankichen/sxt"]["workspaces"]
    assert [item["path"] for item in workspaces] == ["."]
    assert all(item["type"] == "go" for item in workspaces)
