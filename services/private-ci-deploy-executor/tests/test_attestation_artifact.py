import json
import shutil
import pytest

from private_ci_agent.attestation import build_tree_attestation, can_reuse_attestation
from scripts.artifact_release import build_manifest, verify_manifest, build_release_artifact, verify_release_artifact


def test_tree_attestation_requires_exact_identity():
    item = build_tree_attestation("owner/repo", "c" * 40, "t" * 40, "b" * 40, "job", "repo-auto-check", "sha256:image", "go1.25", "node22", {"go.sum": "h"}, "config-hash", "2099-01-01T00:00:00Z")
    assert can_reuse_attestation(item, repository="owner/repo", commit_sha="c" * 40, tree_sha="t" * 40, profile="repo-auto-check", image_digest="sha256:image", test_config_hash="config-hash")
    assert not can_reuse_attestation(item, repository="owner/repo", commit_sha="d" * 40, tree_sha="t" * 40, profile="repo-auto-check", image_digest="sha256:image", test_config_hash="config-hash")


def test_artifact_manifest_and_checksum_verification(tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "app").write_bytes(b"artifact")
    manifest = build_manifest(tmp_path, "owner/repo", "c" * 40, "t" * 40, tmp_path / "manifest.json")
    verify_manifest(tmp_path, manifest)
    assert json.loads((tmp_path / "manifest.json").read_text())["commit_sha"] == "c" * 40


def test_release_bundle_contains_exact_payload_and_provenance(tmp_path):
    if not shutil.which("zstd"):
        pytest.skip("fixed zstd toolchain is exercised by the executor container")
    source = tmp_path / "source"; output = tmp_path / "artifact"
    (source / "bin").mkdir(parents=True); (source / "bin" / "app").write_bytes("你好\n".encode())
    metadata = {"repository": "owner/repo", "commit_sha": "c" * 40, "tree_sha": "t" * 40,
                "private_ci_job_id": "job", "profile": "repo-auto-check", "ci_image_digest": "sha256:image",
                "go_version": "go1.25", "node_version": "node22", "npm_version": "11"}
    result = build_release_artifact(source, output, metadata)
    assert result["archive_size_bytes"] > 0
    assert verify_release_artifact(output)["ok"] is True
    assert (output / "provenance.json").exists()
