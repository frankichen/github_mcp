from pathlib import Path

import yaml


CONFIG_PATH = Path(__file__).parents[1] / "config" / "ci_repositories.yml"


def test_sxt_storage_mall_workspace_is_formally_covered():
    data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    workspace = next(
        item
        for item in data["repositories"]["frankichen/sxt"]["workspaces"]
        if item["path"] == "h5/lenshub-storage-mall"
    )

    assert workspace["type"] == "node"
    assert workspace["package_manager"] == "npm"
    assert workspace["required_scripts"] == ["test", "typecheck", "build"]
