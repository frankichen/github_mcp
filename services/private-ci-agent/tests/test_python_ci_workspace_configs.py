from pathlib import Path


CRITICAL_RUFF_RULES = '["E9", "F63", "F7", "F82"]'


def test_python_workspaces_use_critical_error_ruff_baseline():
    services = Path(__file__).parents[2]
    for workspace in (
        "github-action-service",
        "private-ci-agent",
        "private-deploy-agent",
    ):
        content = (services / workspace / "ruff.toml").read_text(encoding="utf-8")
        assert CRITICAL_RUFF_RULES in content


def test_github_action_pytest_only_collects_tests_directory():
    services = Path(__file__).parents[2]
    content = (services / "github-action-service" / "pytest.ini").read_text(encoding="utf-8")
    assert "testpaths = tests" in content
