from pathlib import Path

from private_ci_agent import source


def test_source_mirror_rejects_unapproved_repository(tmp_path):
    result = source.prepare_source_from_mirror("evil/repo", "a" * 40, str(tmp_path / "source"), str(tmp_path / "mirror"))
    assert result["error_code"] == "SOURCE_REPOSITORY_NOT_ALLOWED"


def test_source_mirror_url_is_derived_from_enabled_allowlist(tmp_path, monkeypatch):
    config = tmp_path / "repositories.yml"
    config.write_text(
        "repositories:\n  frankichen/github_mcp:\n    enabled: true\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(source, "REPOSITORY_CONFIG_PATH", str(config))
    assert source._authoritative_repository_url("frankichen/github_mcp") == "https://github.com/frankichen/github_mcp.git"
    assert source._authoritative_repository_url("evil/repo") is None
