from types import SimpleNamespace

from private_ci_agent.executor import JobExecutor
from private_ci_agent.models import Job


OLD_SHA = "a" * 40
NEW_SHA = "b" * 40


class CaptureLogManager:
    def __init__(self):
        self.messages = []

    def upload(self, _job_id, message):
        self.messages.append(message)


def _executor():
    executor = object.__new__(JobExecutor)
    executor.log_manager = CaptureLogManager()
    return executor


def _job(source_dir, branch="ai/test-gofmt"):
    return Job(
        job_id="job-gofmt",
        repository="frankichen/sxt",
        branch=branch,
        commit_sha=OLD_SHA,
        profile="repo-auto-check",
        timeout_seconds=60,
        lease_token="lease",
        lease_expires_at="",
        source_dir=str(source_dir),
    )


def _completed(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _fake_git(commands, remote_sha=NEW_SHA):
    rev_parse_calls = 0

    def run(command, **kwargs):
        nonlocal rev_parse_calls
        commands.append((command, kwargs))
        if command[:3] == ["git", "check-ref-format", "--branch"]:
            return _completed()
        if "rev-parse" in command and command[-1] == "HEAD":
            rev_parse_calls += 1
            return _completed(stdout=f"{OLD_SHA if rev_parse_calls == 1 else NEW_SHA}\n")
        if "status" in command:
            return _completed(stdout=" M internal/example.go\n")
        if "add" in command:
            return _completed()
        if "commit" in command:
            return _completed(stdout="[detached HEAD] gofmt\n")
        if "push" in command:
            return _completed()
        if "ls-remote" in command:
            return _completed(
                stdout=f"{remote_sha}\trefs/heads/ai/test-gofmt\n"
            )
        raise AssertionError(f"unexpected git command: {command}")

    return run


def test_gofmt_git_commit_uses_identity_and_verifies_remote_sha(tmp_path, monkeypatch):
    (tmp_path / ".git").write_text("gitdir: /tmp/fake\n", encoding="utf-8")
    commands = []
    monkeypatch.setenv("CI_GITHUB_TOKEN", "secret-token")
    monkeypatch.setattr(
        "private_ci_agent.executor.subprocess.run",
        _fake_git(commands),
    )

    result = _executor()._git_push_autofix(_job(tmp_path), str(tmp_path))

    assert result == {
        "committed": True,
        "pushed": True,
        "verified": True,
        "commit_sha": NEW_SHA,
        "remote_sha": NEW_SHA,
        "reason": "ok",
    }

    commit_command = next(command for command, _ in commands if "commit" in command)
    assert "user.name=LensHub CI" in commit_command
    assert "user.email=lenshub-ci@users.noreply.github.com" in commit_command
    assert "style: gofmt automatic formatting [skip ci]" in commit_command
    assert ":(glob)**/*.go" in commit_command

    push_command, push_kwargs = next(
        (command, kwargs) for command, kwargs in commands if "push" in command
    )
    assert "https://github.com/frankichen/sxt.git" in push_command
    assert "HEAD:refs/heads/ai/test-gofmt" in push_command
    assert push_kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert all("secret-token" not in argument for command, _ in commands for argument in command)
    assert not any("set-url" in command for command, _ in commands)


def test_gofmt_push_token_missing_does_not_create_commit(tmp_path, monkeypatch):
    (tmp_path / ".git").write_text("gitdir: /tmp/fake\n", encoding="utf-8")
    commands = []
    monkeypatch.delenv("CI_GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(
        "private_ci_agent.executor.subprocess.run",
        _fake_git(commands),
    )

    result = _executor()._git_push_autofix(_job(tmp_path), str(tmp_path))

    assert result["reason"] == "push_token_unavailable"
    assert result["committed"] is False
    assert not any("commit" in command or "push" in command for command, _ in commands)


def test_gofmt_remote_sha_mismatch_is_not_verified(tmp_path, monkeypatch):
    (tmp_path / ".git").write_text("gitdir: /tmp/fake\n", encoding="utf-8")
    commands = []
    monkeypatch.setenv("CI_GITHUB_TOKEN", "secret-token")
    monkeypatch.setattr(
        "private_ci_agent.executor.subprocess.run",
        _fake_git(commands, remote_sha="c" * 40),
    )

    result = _executor()._git_push_autofix(_job(tmp_path), str(tmp_path))

    assert result["committed"] is True
    assert result["pushed"] is True
    assert result["verified"] is False
    assert result["reason"] == "remote_verification_failed"


def test_gofmt_never_pushes_default_branch(tmp_path, monkeypatch):
    (tmp_path / ".git").write_text("gitdir: /tmp/fake\n", encoding="utf-8")

    def unexpected_run(*_args, **_kwargs):
        raise AssertionError("git must not run for the default branch")

    monkeypatch.setattr(
        "private_ci_agent.executor.subprocess.run",
        unexpected_run,
    )

    result = _executor()._git_push_autofix(
        _job(tmp_path, branch="main"),
        str(tmp_path),
    )

    assert result["reason"] == "default_branch_protected"
    assert result["pushed"] is False
    assert result["verified"] is False
