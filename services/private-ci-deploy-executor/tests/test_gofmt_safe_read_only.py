import hashlib
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "gofmt_safe.sh"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_gofmt_failure_does_not_modify_commit_push_or_remote_head(tmp_path):
    remote = tmp_path / "remote.git"
    workspace = tmp_path / "workspace"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main", str(workspace)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(workspace), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(workspace), "config", "user.email", "test@example.invalid"], check=True)
    source = workspace / "bad.go"
    source.write_text("package main\nfunc main(){ }\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(workspace), "add", "bad.go"], check=True)
    subprocess.run(["git", "-C", str(workspace), "commit", "-m", "baseline"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(workspace), "remote", "add", "origin", str(remote)], check=True)
    subprocess.run(["git", "-C", str(workspace), "push", "-u", "origin", "main"], check=True, capture_output=True)

    file_before = _sha256(source)
    local_head_before = subprocess.check_output(
        ["git", "-C", str(workspace), "rev-parse", "HEAD"], text=True
    ).strip()
    remote_head_before = subprocess.check_output(
        ["git", "--git-dir", str(remote), "rev-parse", "refs/heads/main"], text=True
    ).strip()

    result = subprocess.run(
        ["bash", str(SCRIPT), str(workspace)],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "UNFORMATTED FILES:" in result.stdout
    assert "bad.go" in result.stdout
    assert _sha256(source) == file_before
    assert subprocess.check_output(
        ["git", "-C", str(workspace), "rev-parse", "HEAD"], text=True
    ).strip() == local_head_before
    assert subprocess.check_output(
        ["git", "--git-dir", str(remote), "rev-parse", "refs/heads/main"], text=True
    ).strip() == remote_head_before
    assert subprocess.check_output(
        ["git", "-C", str(workspace), "status", "--porcelain"], text=True
    ).strip() == ""
