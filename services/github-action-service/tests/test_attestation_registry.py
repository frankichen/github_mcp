import hashlib
import json
import sqlite3
import time

import pytest

from app import attestation_registry as registry


def _job():
    return {
        "status": "passed",
        "exit_code": 0,
        "superseded_by_job_id": None,
        "repository": "owner/repo",
        "commit_sha": "c" * 40,
        "profile": "repo-auto-check",
        "base_sha": "b" * 40,
        "changed_files": ["a.go"],
        "summary": {
            "git_tree_sha": "t" * 40,
            "image_digest": "sha256:image-set",
            "go_version": "go1.26",
            "node_version": "node22",
            "npm_version": "11",
            "evidence": {
                "base_sha": "b" * 40,
                "changed_files": ["a.go"],
                "dependency_manifest_sha256": "deps",
                "test_config_sha256": "config",
                "source_immutable": True,
            },
        },
    }


def _legacy_job():
    job = _job()
    job["summary"] = {
        **job["summary"],
        "image_digest": "sha256:legacy-image",
        "go_version": "go1.25",
        "node_version": "node22",
        "npm_version": "11",
        "evidence": {
            "base_sha": "b" * 40,
            "changed_files": ["a.go"],
            "go_sum_sha256": "go-sum",
            "admin_lock_sha256": "admin-lock",
            "console_lock_sha256": "console-lock",
            "test_config_sha256": "legacy-config",
        },
    }
    return job


def test_attestation_persists_generic_dependency_identity_and_rejects_tree_mismatch(tmp_path, monkeypatch):
    monkeypatch.setenv("CI_DB_PATH", str(tmp_path / "ci.db"))
    monkeypatch.setattr(registry, "get_job", lambda _: _job())

    item = registry.create_attestation_for_passed_job(job_id="job")

    assert len(item["attestation_id"]) == 36
    assert item["dependency_manifest_sha256"] == "deps"
    assert item["go_sum_sha256"] == ""
    assert registry.validate_attestation(item["attestation_id"])["ok"] is True

    mutated = _job()
    mutated["summary"] = {**mutated["summary"], "git_tree_sha": "x" * 40}
    monkeypatch.setattr(registry, "get_job", lambda _: mutated)
    bad = registry.validate_attestation(item["attestation_id"])
    assert bad["error_code"] == "ATTESTATION_TREE_MISMATCH"
    assert registry.revoke_attestation(item["attestation_id"])["status"] == "revoked"


def test_attestation_rejects_mutated_source(tmp_path, monkeypatch):
    monkeypatch.setenv("CI_DB_PATH", str(tmp_path / "ci.db"))
    job = _job()
    job["summary"] = {
        **job["summary"],
        "evidence": {**job["summary"]["evidence"], "source_immutable": False},
    }
    monkeypatch.setattr(registry, "get_job", lambda _: job)

    with pytest.raises(ValueError, match="ATTESTATION_SOURCE_MUTATED"):
        registry.create_attestation_for_passed_job(job_id="job")


def test_attestation_rejects_dependency_identity_change(tmp_path, monkeypatch):
    monkeypatch.setenv("CI_DB_PATH", str(tmp_path / "ci.db"))
    monkeypatch.setattr(registry, "get_job", lambda _: _job())
    item = registry.create_attestation_for_passed_job(job_id="job")

    changed = _job()
    changed["summary"] = {
        **changed["summary"],
        "evidence": {
            **changed["summary"]["evidence"],
            "dependency_manifest_sha256": "changed-deps",
        },
    }
    monkeypatch.setattr(registry, "get_job", lambda _: changed)

    result = registry.validate_attestation(item["attestation_id"])
    assert result["ok"] is False
    assert result["error_code"] == "ATTESTATION_DEPENDENCY_MISMATCH"


def test_legacy_attestation_schema_is_upgraded_without_losing_validation(tmp_path, monkeypatch):
    path = tmp_path / "ci.db"
    monkeypatch.setenv("CI_DB_PATH", str(path))
    db = sqlite3.connect(path)
    db.execute(
        """CREATE TABLE ci_tree_attestations (
        attestation_id TEXT PRIMARY KEY, repository TEXT NOT NULL, tested_commit_sha TEXT NOT NULL,
        tested_tree_sha TEXT NOT NULL, base_sha TEXT NOT NULL, private_ci_job_id TEXT NOT NULL,
        profile TEXT NOT NULL, ci_image_digest TEXT NOT NULL, go_version TEXT, node_version TEXT,
        npm_version TEXT, go_sum_sha256 TEXT NOT NULL DEFAULT '', admin_lock_sha256 TEXT NOT NULL DEFAULT '',
        console_lock_sha256 TEXT NOT NULL DEFAULT '', test_config_sha256 TEXT NOT NULL,
        changed_files_sha256 TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, created_at REAL NOT NULL,
        expires_at REAL NOT NULL, revoked_at REAL
        )"""
    )
    db.execute(
        "INSERT INTO ci_tree_attestations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "legacy-id", "owner/repo", "c" * 40, "t" * 40, "b" * 40, "legacy-job",
            "repo-auto-check", "sha256:legacy-image", "go1.25", "node22", "11",
            "go-sum", "admin-lock", "console-lock", "legacy-config",
            registry._sha(["a.go"]), "active", time.time(), time.time() + 3600, None,
        ),
    )
    db.commit()
    db.close()
    monkeypatch.setattr(registry, "get_job", lambda _: _legacy_job())

    registry.init_registry_db()
    item = registry.get_attestation("legacy-id")

    assert item["dependency_manifest_sha256"] == ""
    assert item["go_sum_sha256"] == "go-sum"
    assert registry.validate_attestation("legacy-id")["ok"] is True


def test_artifact_validation_requires_real_bundle_files(tmp_path, monkeypatch):
    monkeypatch.setenv("CI_DB_PATH", str(tmp_path / "ci.db"))
    monkeypatch.setenv("ARTIFACT_STORAGE_ROOT", str(tmp_path))
    monkeypatch.setattr(registry, "get_job", lambda _: _job())
    archive = tmp_path / "release.tar.zst"
    archive.write_bytes(b"archive")
    (tmp_path / "manifest.json").write_text(
        json.dumps({"repository": "owner/repo", "commit_sha": "c" * 40}), encoding="utf-8"
    )
    (tmp_path / "checksums.sha256").write_text("", encoding="utf-8")
    (tmp_path / "provenance.json").write_text("{}", encoding="utf-8")
    sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    item = registry.register_release_artifact({
        "repository": "owner/repo",
        "branch": "main",
        "commit_sha": "c" * 40,
        "tree_sha": "t" * 40,
        "private_ci_job_id": "job",
        "profile": "repo-auto-check",
        "ci_image_digest": "sha256:image-set",
        "storage_path": str(archive),
        "archive_sha256": sha(archive),
        "archive_size_bytes": archive.stat().st_size,
        "manifest_sha256": sha(tmp_path / "manifest.json"),
        "checksums_sha256": sha(tmp_path / "checksums.sha256"),
    })
    result = registry.validate_artifact(
        item["artifact_id"], repository="owner/repo", branch="main",
        commit_sha="c" * 40, tree_sha="t" * 40, private_ci_job_id="job",
    )
    assert result["ok"] is False
    assert result["error_code"] in {"ARTIFACT_ARCHIVE_INVALID", "ARTIFACT_UNSAFE_ARCHIVE_ENTRY"}
