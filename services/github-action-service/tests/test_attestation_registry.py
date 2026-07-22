import hashlib
import json

from app import attestation_registry as registry


def _job():
    return {"status": "passed", "exit_code": 0, "superseded_by_job_id": None,
            "repository": "owner/repo", "commit_sha": "c" * 40, "profile": "repo-auto-check"}


def _args():
    return {"repository": "owner/repo", "job_id": "job", "tested_commit_sha": "c" * 40,
            "tested_tree_sha": "t" * 40, "base_sha": "b" * 40, "profile": "repo-auto-check",
            "ci_image_digest": "sha256:image", "go_version": "go1.25", "node_version": "node22",
            "npm_version": "11", "go_sum_sha256": "g", "admin_lock_sha256": "a",
            "console_lock_sha256": "c", "test_config_sha256": "x", "changed_files": ["a.go"]}


def test_attestation_persists_and_rejects_tree_mismatch(tmp_path, monkeypatch):
    monkeypatch.setenv("CI_DB_PATH", str(tmp_path / "ci.db")); monkeypatch.setattr(registry, "get_job", lambda _: _job())
    item = registry.create_attestation_for_passed_job(**_args())
    assert len(item["attestation_id"]) == 36
    good = registry.validate_attestation(item["attestation_id"], **{k: v for k, v in _args().items() if k != "job_id"})
    assert good["ok"] is True
    bad = registry.validate_attestation(item["attestation_id"], **{**{k: v for k, v in _args().items() if k != "job_id"}, "tested_tree_sha": "x" * 40})
    assert bad["error_code"] == "ATTESTATION_TREE_MISMATCH"
    assert registry.revoke_attestation(item["attestation_id"])["status"] == "revoked"


def test_artifact_validation_requires_real_bundle_files(tmp_path, monkeypatch):
    monkeypatch.setenv("CI_DB_PATH", str(tmp_path / "ci.db")); monkeypatch.setenv("ARTIFACT_STORAGE_ROOT", str(tmp_path))
    monkeypatch.setattr(registry, "get_job", lambda _: _job())
    archive = tmp_path / "release.tar.zst"; archive.write_bytes(b"archive")
    (tmp_path / "manifest.json").write_text(json.dumps({"repository": "owner/repo", "commit_sha": "c" * 40}), encoding="utf-8")
    (tmp_path / "checksums.sha256").write_text("", encoding="utf-8"); (tmp_path / "provenance.json").write_text("{}", encoding="utf-8")
    sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    item = registry.register_release_artifact({"repository": "owner/repo", "branch": "main", "commit_sha": "c" * 40, "tree_sha": "t" * 40, "private_ci_job_id": "job", "profile": "repo-auto-check", "ci_image_digest": "sha256:image", "storage_path": str(archive), "archive_sha256": sha(archive), "archive_size_bytes": archive.stat().st_size, "manifest_sha256": sha(tmp_path / "manifest.json"), "checksums_sha256": sha(tmp_path / "checksums.sha256")})
    assert registry.validate_artifact(item["artifact_id"], repository="owner/repo", branch="main", commit_sha="c" * 40, tree_sha="t" * 40, private_ci_job_id="job")["ok"] is True
