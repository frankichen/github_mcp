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


def test_remove_source_worktree_does_not_fallback_to_sxt(monkeypatch, tmp_path):
    mirror_root = tmp_path / "mirrors"
    (mirror_root / "frankichen-sxt.git").mkdir(parents=True)
    commands = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        if command[:4] == ["git", "-C", str(tmp_path / "missing-source"), "rev-parse"]:
            return type("Result", (), {"returncode": 1, "stdout": "", "stderr": "not a worktree"})()
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(source.subprocess, "run", fake_run)
    source.remove_source_worktree(str(tmp_path / "missing-source"), str(mirror_root))

    assert len(commands) == 1
    assert not any("worktree" in command and "remove" in command for command in commands)


def test_remove_source_worktree_uses_only_resolved_mirror(monkeypatch, tmp_path):
    mirror_root = tmp_path / "mirrors"
    mirror = mirror_root / "frankichen-example.git"
    mirror.mkdir(parents=True)
    dest = tmp_path / "source"
    dest.mkdir()
    commands = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        if command[-2:] == ["rev-parse", "--git-common-dir"]:
            return type("Result", (), {"returncode": 0, "stdout": str(mirror) + "\n", "stderr": ""})()
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(source.subprocess, "run", fake_run)
    source.remove_source_worktree(str(dest), str(mirror_root))

    assert ["git", "-C", str(mirror), "worktree", "remove", "--force", str(dest)] in commands
