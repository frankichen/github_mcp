from pathlib import Path


CRITICAL_RUFF_RULES = '["E9", "F63", "F7", "F82"]'


def test_private_ci_workspace_uses_critical_error_ruff_baseline():
    workspace = Path(__file__).parents[1]
    content = (workspace / "ruff.toml").read_text(encoding="utf-8")
    assert CRITICAL_RUFF_RULES in content
