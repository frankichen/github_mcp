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


def test_source_download_request_carries_attempt_lease(monkeypatch, tmp_path):
    payload = b"source-bytes"
    expected_sha = source.hashlib.sha256(payload).hexdigest()
    captured = {}

    class FakeResponse:
        headers = {"X-SHA256": expected_sha}

        def __init__(self):
            self.done = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size):
            if self.done:
                return b""
            self.done = True
            return payload

    def fake_urlopen(request, timeout):
        captured.update({key.lower(): value for key, value in request.header_items()})
        return FakeResponse()

    monkeypatch.setattr(source.urllib.request, "urlopen", fake_urlopen)
    logs = []
    actual_sha, size = source.download_source_archive(
        "http://controller", "worker-a", "worker-token",
        "job-1", str(tmp_path / "source.tar.gz"), 1024,
        lease_token="attempt-lease", log_callback=logs.append,
    )

    assert actual_sha == expected_sha
    assert size == len(payload)
    assert captured["x-ci-lease-token"] == "attempt-lease"
    assert logs == ["[download] attempt=1/3\n"]
