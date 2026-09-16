from pathlib import Path

import yaml


CONFIG_PATH = Path(__file__).parents[1] / "deploy" / "repositories.yml"
EXPECTED_WORKSPACES = [
    {"path": "services/github-action-service", "type": "python"},
    {"path": "services/private-ci-agent", "type": "python"},
    {"path": "services/private-deploy-agent", "type": "python"},
]


def test_github_mcp_worker_overrides_pin_real_python_workspaces():
    data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))

    repository = data["repositories"]["frankichen/github_mcp"]
    assert repository["allowed_profiles"] == [
        "repo-auto-check",
        "repo-fast-check",
        "python-check",
    ]
    assert repository["workspaces"] == EXPECTED_WORKSPACES
