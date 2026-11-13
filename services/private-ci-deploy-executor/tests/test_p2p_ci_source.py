"""P2P source bundles must retain exact Git identity without credentials."""
import subprocess
from pathlib import Path

import pytest

from scripts import export_p2p_ci_source as exporter
from scripts import p2p_deploy_executor as runner


def git(*args):
    result = subprocess.run(["git", *map(str, args)], check=True, capture_output=True, text=True)
    return result.stdout.strip()


@pytest.fixture
def mirror_bundle(tmp_path):
    source = tmp_path / "working"
    source.mkdir()
    git("init", "-b", "main", source)
    git("-C", source, "config", "user.email", "fixture@example.test")
    git("-C", source, "config", "user.name", "P2P source fixture")
    (source / "go.mod").write_text("module fixture\ngo 1.26\n")
    git("-C", source, "add", "go.mod")
    git("-C", source, "commit", "-m", "Fixture")
    sha = git("-C", source, "rev-parse", "HEAD")
    mirror = tmp_path / "ci-mirror.git"
    git("clone", "--mirror", source, mirror)
    git("--git-dir=" + str(mirror), "remote", "set-url",
        "origin", exporter.AUTHORITATIVE_URL)
    return mirror, tmp_path / "main.bundle", sha


def test_export_verifies_origin_and_git_bundle(mirror_bundle):
    mirror, bundle, sha = mirror_bundle
    assert exporter.create_checked_bundle(mirror, bundle) == sha
    assert exporter.bundle_main_sha(bundle) == sha


def test_export_rejects_untrusted_ci_origin(mirror_bundle):
    mirror, bundle, sha = mirror_bundle
    git("--git-dir=" + str(mirror), "remote", "set-url",
        "origin", "https://example.test/other.git")
    with pytest.raises(ValueError, match="ORIGIN_MISMATCH"):
        exporter.create_checked_bundle(mirror, bundle)


def test_runner_uses_verified_git_bundle_only(mirror_bundle, tmp_path, monkeypatch):
    mirror, bundle, sha = mirror_bundle
    exporter.create_checked_bundle(mirror, bundle)
    monkeypatch.setattr(runner, "BUNDLE", str(bundle))
    monkeypatch.setattr(runner, "WORKSPACES", str(tmp_path / "workspaces"))
    monkeypatch.setattr(runner, "_trusted_bundle", lambda path: True)
    row = {"repository": runner.REPOSITORY, "environment": runner.ENVIRONMENT,
           "requested_scope": runner.SCOPE, "deployment_id": "dep_" + "a" * 32,
           "commit_sha": sha}
    workspace, evidence = runner.prepare_workspace(row)
    assert git("-C", workspace, "rev-parse", "HEAD") == sha
    assert git("-C", workspace, "branch", "--show-current") == "main"
    assert git("-C", workspace, "status", "--porcelain") == ""
    assert "verified_bundle_sha=" + sha in evidence


def test_runner_rejects_stale_bundle(mirror_bundle, monkeypatch):
    mirror, bundle, sha = mirror_bundle
    exporter.create_checked_bundle(mirror, bundle)
    monkeypatch.setattr(runner, "BUNDLE", str(bundle))
    monkeypatch.setattr(runner, "_trusted_bundle", lambda path: True)
    with pytest.raises(runner.base.DeploymentSourceError, match="bundle"):
        runner._wait_for_source("b" * 40, timeout_seconds=0)
