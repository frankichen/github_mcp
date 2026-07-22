import json

from private_ci_agent.attestation import build_tree_attestation, can_reuse_attestation
from scripts.artifact_release import build_manifest, verify_manifest


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
