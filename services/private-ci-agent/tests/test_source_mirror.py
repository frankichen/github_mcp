from pathlib import Path

from private_ci_agent import source


def test_source_mirror_rejects_unapproved_repository(tmp_path):
    result = source.prepare_source_from_mirror("evil/repo", "a" * 40, str(tmp_path / "source"), str(tmp_path / "mirror"))
    assert result["error_code"] == "SOURCE_REPOSITORY_NOT_ALLOWED"


def test_source_mirror_constants_are_authoritative():
    assert source.AUTHORITATIVE_REPOSITORY_URL == "https://github.com/frankichen/sxt.git"
