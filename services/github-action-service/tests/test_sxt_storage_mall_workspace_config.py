"""SXT storage mall is owned by separate frontend verification, not private Go CI."""
from pathlib import Path

import yaml


CONFIG_PATH = Path(__file__).parents[1] / "config" / "ci_repositories.yml"


def test_sxt_storage_mall_is_excluded_from_repo_auto_check():
    data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    config = data["repositories"]["frankichen/sxt"]

    assert config["allowed_profiles"] == ["repo-auto-check", "go-check"]
    assert [workspace["path"] for workspace in config["workspaces"]] == ["."]
    assert config["workspaces"][0]["type"] == "go"
