from pathlib import Path

from private_ci_agent import source


def write_repositories(path, content):
    path.write_text(content, encoding="utf-8")


def test_source_mirror_rejects_unapproved_repository(tmp_path, monkeypatch):
    write_repositories(tmp_path / "repositories.yml", "repositories: {}\n")
    monkeypatch.setattr(source, "REPOSITORY_CONFIG_PATH", str(tmp_path / "repositories.yml"))

    result = source.prepare_source_from_mirror("evil/repo", "a" * 40, str(tmp_path / "source"), str(tmp_path / "mirror"))
    assert result["error_code"] == "SOURCE_REPOSITORY_NOT_ALLOWED"


def test_source_mirror_url_comes_from_enabled_repository_allowlist(tmp_path, monkeypatch):
    write_repositories(
        tmp_path / "repositories.yml",
        """
repositories:
  frankichen/auto_gupiao:
    enabled: true
  frankichen/disabled:
    enabled: false
""",
    )
    monkeypatch.setattr(source, "REPOSITORY_CONFIG_PATH", str(tmp_path / "repositories.yml"))

    assert source._authoritative_repository_url("frankichen/auto_gupiao") == "https://github.com/frankichen/auto_gupiao.git"
    assert source._authoritative_repository_url("frankichen/disabled") is None
    assert source._authoritative_repository_url("../bad") is None
