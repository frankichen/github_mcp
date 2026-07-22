import shutil
import pytest

from scripts.artifact_deployment import deploy_artifact


def test_artifact_deploy_rolls_back_on_health_failure(tmp_path, monkeypatch):
    if not shutil.which("zstd"):
        pytest.skip("fixed zstd toolchain is exercised by the executor container")
    artifact = tmp_path / "artifact"; artifact.mkdir()
    previous = tmp_path / "previous"; previous.mkdir(); (previous / "old").write_text("old")
    current = tmp_path / "current"; current.symlink_to(previous)
    monkeypatch.setattr("scripts.artifact_deployment.verify_release_artifact", lambda _: {"archive_sha256": "a" * 64})
    monkeypatch.setattr("scripts.artifact_deployment.subprocess.run", lambda *args, **kwargs: None)
    result = deploy_artifact(artifact, tmp_path / "incoming", current, healthcheck=lambda: False)
    assert result["ok"] is False
    assert current.resolve() == previous
