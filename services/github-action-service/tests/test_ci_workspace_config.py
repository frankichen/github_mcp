from pathlib import Path


CRITICAL_RUFF_RULES = '["E9", "F63", "F7", "F82"]'


def test_github_action_workspace_ci_configuration():
    workspace = Path(__file__).parents[1]

    ruff_config = (workspace / "ruff.toml").read_text(encoding="utf-8")
    pytest_config = (workspace / "pytest.ini").read_text(encoding="utf-8")

    assert CRITICAL_RUFF_RULES in ruff_config
    assert "testpaths = tests" in pytest_config
