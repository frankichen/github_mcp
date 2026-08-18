from private_ci_agent import source


def test_source_mirror_rejects_invalid_repository_name(tmp_path):
    result = source.prepare_source_from_mirror(
        "../bad",
        "a" * 40,
        str(tmp_path / "source"),
        str(tmp_path / "mirror"),
    )

    assert result["error_code"] == "SOURCE_REPOSITORY_NOT_ALLOWED"


def test_source_mirror_url_comes_from_controller_authorized_identity():
    assert source._authoritative_repository_url("frankichen/new-project") == "https://github.com/frankichen/new-project.git"
    assert source._authoritative_repository_url("another-owner/service") == "https://github.com/another-owner/service.git"
    assert source._authoritative_repository_url("../bad") is None
    assert source._authoritative_repository_url("bad") is None
