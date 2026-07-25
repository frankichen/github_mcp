from pathlib import Path


def test_private_ci_agent_pyproject_limits_setuptools_package_discovery():
    pyproject = Path(__file__).parents[1] / "pyproject.toml"
    content = pyproject.read_text(encoding="utf-8")

    assert "[build-system]" in content
    assert "[tool.setuptools.packages.find]" in content
    assert 'include = ["private_ci_agent*"]' in content
    assert 'exclude = ["deploy*", "tests*"]' in content
