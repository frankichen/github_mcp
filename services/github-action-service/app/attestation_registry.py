"""Persistent Tree Attestation and Artifact Registry for MyGithub10 PR3."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from app.ci_database import get_job


ATTESTATION_ERRORS = {
    "not_found": "ATTESTATION_NOT_FOUND", "expired": "ATTESTATION_EXPIRED", "revoked": "ATTESTATION_REVOKED",
    "job": "ATTESTATION_JOB_NOT_PASSED", "superseded": "ATTESTATION_JOB_SUPERSEDED", "base": "ATTESTATION_BASE_CHANGED",
    "tree": "ATTESTATION_TREE_MISMATCH", "source": "ATTESTATION_SOURCE_MUTATED",
    "toolchain": "ATTESTATION_TOOLCHAIN_MISMATCH", "dependency": "ATTESTATION_DEPENDENCY_MISMATCH",
    "config": "ATTESTATION_CONFIG_MISMATCH",
}


def _db() -> sqlite3.Connection:
    path = os.environ.get("CI_DB_PATH", "/data/ci.db")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=15)
    db.row_factory = sqlite3.Row
    return db


def init_registry_db() -> None:
    db = _db()
    db.executescript("""
    CREATE TABLE IF NOT EXISTS ci_tree_attestations (
      attestation_id TEXT PRIMARY KEY, repository TEXT NOT NULL, tested_commit_sha TEXT NOT NULL,
      tested_tree_sha TEXT NOT NULL, base_sha TEXT NOT NULL, private_ci_job_id TEXT NOT NULL,
      profile TEXT NOT NULL, ci_image_digest TEXT NOT NULL, go_version TEXT, node_version TEXT,
      npm_version TEXT, dependency_manifest_sha256 TEXT NOT NULL DEFAULT '',
      go_sum_sha256 TEXT NOT NULL DEFAULT '', admin_lock_sha256 TEXT NOT NULL DEFAULT '',
      console_lock_sha256 TEXT NOT NULL DEFAULT '', test_config_sha256 TEXT NOT NULL,
      changed_files_sha256 TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, created_at REAL NOT NULL,
      expires_at REAL NOT NULL, revoked_at REAL
    );
    CREATE INDEX IF NOT EXISTS idx_attestation_identity ON ci_tree_attestations(repository, tested_commit_sha, tested_tree_sha, status);
    CREATE TABLE IF NOT EXISTS release_artifacts (
      artifact_id TEXT PRIMARY KEY, repository TEXT NOT NULL, branch TEXT NOT NULL, commit_sha TEXT NOT NULL,
      tree_sha TEXT NOT NULL, private_ci_job_id TEXT NOT NULL, source_attestation_id TEXT NOT NULL DEFAULT '',
      profile TEXT NOT NULL, ci_image_digest TEXT NOT NULL, status TEXT NOT NULL, storage_path TEXT NOT NULL,
      storage_dir TEXT NOT NULL DEFAULT '', archive_path TEXT NOT NULL DEFAULT '',
      archive_sha256 TEXT NOT NULL, archive_size_bytes INTEGER NOT NULL, manifest_sha256 TEXT NOT NULL,
      checksums_sha256 TEXT NOT NULL, provenance_sha256 TEXT NOT NULL DEFAULT '', go_version TEXT NOT NULL DEFAULT '', node_version TEXT NOT NULL DEFAULT '', npm_version TEXT NOT NULL DEFAULT '', artifact_format_version INTEGER NOT NULL DEFAULT 1, migration_required INTEGER NOT NULL DEFAULT 0,
      deploy_config_changed INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, expires_at REAL NOT NULL,
      error_code TEXT, error_message TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_artifact_identity ON release_artifacts(repository, branch, commit_sha, status);
    """)
    attestation_columns = {
        row[1] for row in db.execute("PRAGMA table_info(ci_tree_attestations)").fetchall()
    }
    if "dependency_manifest_sha256" not in attestation_columns:
        db.execute(
            "ALTER TABLE ci_tree_attestations "
            "ADD COLUMN dependency_manifest_sha256 TEXT NOT NULL DEFAULT ''"
        )
    columns = {row[1] for row in db.execute("PRAGMA table_info(release_artifacts)").fetchall()}
    additions = {"storage_dir": "TEXT NOT NULL DEFAULT ''", "archive_path": "TEXT NOT NULL DEFAULT ''", "provenance_sha256": "TEXT NOT NULL DEFAULT ''", "go_version": "TEXT NOT NULL DEFAULT ''", "node_version": "TEXT NOT NULL DEFAULT ''", "npm_version": "TEXT NOT NULL DEFAULT ''", "artifact_format_version": "INTEGER NOT NULL DEFAULT 1"}
    for name, definition in additions.items():
        if name not in columns: db.execute(f"ALTER TABLE release_artifacts ADD COLUMN {name} {definition}")
    db.execute("UPDATE release_artifacts SET archive_path=COALESCE(NULLIF(archive_path,''),storage_path), storage_dir=COALESCE(NULLIF(storage_dir,''),substr(COALESCE(NULLIF(archive_path,''),storage_path),1,length(COALESCE(NULLIF(archive_path,''),storage_path))-length('/release.tar.zst'))) WHERE archive_path='' OR storage_dir=''")
    db.commit(); db.close()


def _row(row) -> dict | None:
    if not row: return None
    item = dict(row)
    for key in ("migration_required", "deploy_config_changed"):
        if key in item: item[key] = bool(item[key])
    if item.get("archive_path"): item["storage_path"] = item["archive_path"]
    return item


def _sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _verify_archive(item: dict) -> tuple[bool, str]:
    archive = Path(item["storage_path"]).resolve(); directory = archive.parent
    try:
        import importlib.util
        script = Path(__file__).resolve().parents[2] / "private-ci-deploy-executor" / "scripts" / "artifact_release.py"
        spec = importlib.util.spec_from_file_location("fixed_artifact_verifier", script)
        if not spec or not spec.loader: return False, "ARTIFACT_VERIFIER_NOT_CONFIGURED"
        verifier = importlib.util.module_from_spec(spec); spec.loader.exec_module(verifier)
        verifier.verify_release_artifact(directory)
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8")); provenance = json.loads((directory / "provenance.json").read_text(encoding="utf-8"))
        identity = {"repository": item["repository"], "branch": item["branch"], "commit_sha": item["commit_sha"], "tree_sha": item["tree_sha"], "private_ci_job_id": item["private_ci_job_id"], "source_attestation_id": item["source_attestation_id"], "profile": item["profile"], "ci_image_digest": item["ci_image_digest"]}
        if any(manifest.get(key) != value for key, value in identity.items()) or any(provenance.get(key) != value for key, value in identity.items()): return False, "ARTIFACT_IDENTITY_MISMATCH"
        if provenance.get("toolchain", {}) != {"go_version": item.get("go_version", ""), "node_version": item.get("node_version", ""), "npm_version": item.get("npm_version", "")}: return False, "ARTIFACT_TOOLCHAIN_MISMATCH"
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, json.JSONDecodeError):
        return False, "ARTIFACT_ARCHIVE_INVALID"
    return True, ""


def create_attestation_for_passed_job(*, job_id: str, expires_in_seconds: int = 604800) -> dict:
    job = get_job(job_id)
    if not job or job.get("status") != "passed" or job.get("exit_code") != 0:
        raise ValueError(ATTESTATION_ERRORS["job"])
    if job.get("superseded_by_job_id"):
        raise ValueError(ATTESTATION_ERRORS["superseded"])
    summary = job.get("summary") if isinstance(job.get("summary"), dict) else {}
    evidence = summary.get("evidence") if isinstance(summary.get("evidence"), dict) else {}
    repository, tested_commit_sha = job.get("repository", ""), job.get("commit_sha", "")
    profile = job.get("profile", "")
    tested_tree_sha = summary.get("git_tree_sha", "")
    base_sha = evidence.get("base_sha") or job.get("base_sha", "")
    ci_image_digest = summary.get("image_digest", "")
    go_version = summary.get("go_version", "") or ""
    node_version = summary.get("node_version", "") or ""
    npm_version = summary.get("npm_version", "") or ""
    dependency_manifest_sha256 = evidence.get("dependency_manifest_sha256", "")
    test_config_sha256 = evidence.get("test_config_sha256", "")
    changed_files = evidence.get("changed_files", job.get("changed_files", []))
    if profile != "repo-auto-check" or len(tested_commit_sha) != 40 or len(tested_tree_sha) != 40:
        raise ValueError(ATTESTATION_ERRORS["job"])
    if evidence.get("source_immutable") is not True:
        raise ValueError(ATTESTATION_ERRORS["source"])
    if not all((base_sha, ci_image_digest, dependency_manifest_sha256, test_config_sha256)):
        raise ValueError("ATTESTATION_EVIDENCE_INCOMPLETE")

    init_registry_db()
    attestation_id = str(uuid.uuid4())
    now = time.time()
    expiry = now + min(max(int(expires_in_seconds), 60), 30 * 86400)
    db = _db()
    db.execute(
        """INSERT INTO ci_tree_attestations(
        attestation_id,repository,tested_commit_sha,tested_tree_sha,base_sha,private_ci_job_id,
        profile,ci_image_digest,go_version,node_version,npm_version,dependency_manifest_sha256,
        go_sum_sha256,admin_lock_sha256,console_lock_sha256,test_config_sha256,
        changed_files_sha256,status,created_at,expires_at,revoked_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            attestation_id, repository, tested_commit_sha, tested_tree_sha, base_sha, job_id, profile,
            ci_image_digest, go_version, node_version, npm_version, dependency_manifest_sha256,
            "", "", "", test_config_sha256, _sha(changed_files), "active", now, expiry, None,
        ),
    )
    db.commit()
    row = db.execute(
        "SELECT * FROM ci_tree_attestations WHERE attestation_id=?", (attestation_id,)
    ).fetchone()
    db.close()
    return _row(row)


def get_attestation(attestation_id: str) -> dict | None:
    init_registry_db(); db = _db(); row = db.execute("SELECT * FROM ci_tree_attestations WHERE attestation_id=?", (attestation_id,)).fetchone(); db.close(); return _row(row)


def validate_attestation(attestation_id: str) -> dict:
    item = get_attestation(attestation_id)
    if not item:
        return {"ok": False, "error_code": ATTESTATION_ERRORS["not_found"], "reusable": False}
    if item["status"] == "revoked":
        return {"ok": False, "error_code": ATTESTATION_ERRORS["revoked"], "reusable": False}
    if item["expires_at"] <= time.time():
        return {"ok": False, "error_code": ATTESTATION_ERRORS["expired"], "reusable": False}
    job = get_job(item["private_ci_job_id"])
    if not job or job.get("status") != "passed" or job.get("exit_code") != 0:
        return {"ok": False, "error_code": ATTESTATION_ERRORS["job"], "reusable": False}
    if job.get("superseded_by_job_id"):
        return {"ok": False, "error_code": ATTESTATION_ERRORS["superseded"], "reusable": False}

    summary = job.get("summary") if isinstance(job.get("summary"), dict) else {}
    evidence = summary.get("evidence") if isinstance(summary.get("evidence"), dict) else {}
    if item["repository"] != job.get("repository") or item["tested_commit_sha"] != job.get("commit_sha"):
        return {"ok": False, "error_code": ATTESTATION_ERRORS["tree"], "reusable": False}
    if item["tested_tree_sha"] != summary.get("git_tree_sha"):
        return {"ok": False, "error_code": ATTESTATION_ERRORS["tree"], "reusable": False}
    if item["base_sha"] != (evidence.get("base_sha") or job.get("base_sha")):
        return {"ok": False, "error_code": ATTESTATION_ERRORS["base"], "reusable": False}

    # New attestations require a source tree that stayed equal to the requested
    # Git commit throughout CI. Old rows have no generic dependency identity
    # and retain their legacy validation path below.
    if item.get("dependency_manifest_sha256") and evidence.get("source_immutable") is not True:
        return {"ok": False, "error_code": ATTESTATION_ERRORS["source"], "reusable": False}

    if item["profile"] != job.get("profile") or item["ci_image_digest"] != summary.get("image_digest"):
        return {"ok": False, "error_code": ATTESTATION_ERRORS["toolchain"], "reusable": False}
    for key in ("go_version", "node_version", "npm_version"):
        if item.get(key) and item.get(key) != (summary.get(key) or ""):
            return {"ok": False, "error_code": ATTESTATION_ERRORS["toolchain"], "reusable": False}

    dependency_identity = item.get("dependency_manifest_sha256", "")
    if dependency_identity:
        if dependency_identity != evidence.get("dependency_manifest_sha256", ""):
            return {"ok": False, "error_code": ATTESTATION_ERRORS["dependency"], "reusable": False}
    elif any(
        item[key] != evidence.get(key, "")
        for key in ("go_sum_sha256", "admin_lock_sha256", "console_lock_sha256")
    ):
        # Backward-compatible validation for attestations created before the
        # generic dependency identity existed.
        return {"ok": False, "error_code": ATTESTATION_ERRORS["dependency"], "reusable": False}

    if item["test_config_sha256"] != evidence.get("test_config_sha256", ""):
        return {"ok": False, "error_code": ATTESTATION_ERRORS["config"], "reusable": False}
    if item["changed_files_sha256"] != _sha(
        evidence.get("changed_files", job.get("changed_files", []))
    ):
        return {"ok": False, "error_code": ATTESTATION_ERRORS["config"], "reusable": False}
    return {"ok": True, "reusable": True, "attestation": item}


def revoke_attestation(attestation_id: str) -> dict:
    init_registry_db(); db = _db(); now = time.time(); db.execute("UPDATE ci_tree_attestations SET status='revoked',revoked_at=? WHERE attestation_id=?", (now, attestation_id)); db.commit(); row = db.execute("SELECT * FROM ci_tree_attestations WHERE attestation_id=?", (attestation_id,)).fetchone(); db.close(); return _row(row) or {"attestation_id": attestation_id, "status": "missing"}


def register_release_artifact(metadata: dict) -> dict:
    init_registry_db(); artifact_id = metadata.get("artifact_id") or str(uuid.uuid4()); now = time.time(); expiry = float(metadata.get("expires_at") or now + 7 * 86400)
    if metadata.get("status", "ready") not in {"building", "ready", "failed"}: raise ValueError("ARTIFACT_STATUS_INVALID")
    metadata.setdefault("archive_path", metadata.get("storage_path", "")); metadata.setdefault("storage_dir", str(Path(metadata["archive_path"]).parent)); metadata.setdefault("provenance_sha256", "")
    db = _db()
    try:
        db.execute("INSERT INTO release_artifacts(artifact_id,repository,branch,commit_sha,tree_sha,private_ci_job_id,source_attestation_id,profile,ci_image_digest,status,storage_path,storage_dir,archive_path,archive_sha256,archive_size_bytes,manifest_sha256,checksums_sha256,provenance_sha256,go_version,node_version,npm_version,artifact_format_version,migration_required,deploy_config_changed,created_at,expires_at,error_code,error_message) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (artifact_id, metadata["repository"], metadata["branch"], metadata["commit_sha"], metadata["tree_sha"], metadata["private_ci_job_id"], metadata.get("source_attestation_id", ""), metadata["profile"], metadata["ci_image_digest"], metadata.get("status", "ready"), metadata.get("storage_path", metadata["archive_path"]), metadata["storage_dir"], metadata["archive_path"], metadata["archive_sha256"], int(metadata["archive_size_bytes"]), metadata["manifest_sha256"], metadata["checksums_sha256"], metadata.get("provenance_sha256", ""), metadata.get("go_version", ""), metadata.get("node_version", ""), metadata.get("npm_version", ""), int(metadata.get("artifact_format_version", 1)), int(bool(metadata.get("migration_required"))), int(bool(metadata.get("deploy_config_changed"))), now, expiry, metadata.get("error_code"), metadata.get("error_message")))
    except sqlite3.IntegrityError as exc:
        db.rollback(); db.close(); raise ValueError("ARTIFACT_ID_CONFLICT") from exc
    db.commit(); row = db.execute("SELECT * FROM release_artifacts WHERE artifact_id=?", (artifact_id,)).fetchone(); db.close(); return _row(row)


def get_artifact(artifact_id: str) -> dict | None:
    init_registry_db(); db = _db(); row = db.execute("SELECT * FROM release_artifacts WHERE artifact_id=?", (artifact_id,)).fetchone(); db.close(); return _row(row)


def list_artifacts(repository: str = "", status: str = "", limit: int = 50) -> list[dict]:
    init_registry_db(); db = _db(); rows = db.execute("SELECT * FROM release_artifacts WHERE (?='' OR repository=?) AND (?='' OR status=?) ORDER BY created_at DESC LIMIT ?", (repository, repository, status, status, min(max(limit, 1), 100))).fetchall(); db.close(); return [_row(row) for row in rows]


def validate_artifact(artifact_id: str, *, repository: str, branch: str, commit_sha: str, tree_sha: str, private_ci_job_id: str) -> dict:
    item = get_artifact(artifact_id)
    if not item: return {"ok": False, "error_code": "ARTIFACT_NOT_FOUND"}
    if item["status"] in ("revoked", "expired"): return {"ok": False, "error_code": f"ARTIFACT_{item['status'].upper()}"}
    if item["expires_at"] <= time.time():
        db = _db(); db.execute("UPDATE release_artifacts SET status='expired' WHERE artifact_id=? AND status='ready'", (artifact_id,)); db.commit(); db.close()
        return {"ok": False, "error_code": "ARTIFACT_EXPIRED"}
    if item["status"] != "ready": return {"ok": False, "error_code": "ARTIFACT_NOT_READY"}
    root = Path(os.environ.get("ARTIFACT_STORAGE_ROOT", "/var/lib/private-ci/artifacts")).resolve()
    archive = Path(item["storage_path"]).resolve()
    if root not in archive.parents or not archive.is_file(): return {"ok": False, "error_code": "ARTIFACT_STORAGE_UNAVAILABLE"}
    if _file_sha256(archive) != item["archive_sha256"] or archive.stat().st_size != item["archive_size_bytes"]: return {"ok": False, "error_code": "ARTIFACT_ARCHIVE_CHECKSUM_MISMATCH"}
    metadata_dir = archive.parent
    if not all((metadata_dir / name).is_file() for name in ("manifest.json", "checksums.sha256", "provenance.json")): return {"ok": False, "error_code": "ARTIFACT_METADATA_MISSING"}
    if _file_sha256(metadata_dir / "manifest.json") != item["manifest_sha256"] or _file_sha256(metadata_dir / "checksums.sha256") != item["checksums_sha256"]: return {"ok": False, "error_code": "ARTIFACT_METADATA_CHECKSUM_MISMATCH"}
    if item.get("provenance_sha256") and _file_sha256(metadata_dir / "provenance.json") != item["provenance_sha256"]: return {"ok": False, "error_code": "ARTIFACT_PROVENANCE_CHECKSUM_MISMATCH"}
    valid_archive, archive_error = _verify_archive(item)
    if not valid_archive: return {"ok": False, "error_code": archive_error}
    job = get_job(private_ci_job_id)
    if not job or job.get("status") != "passed" or job.get("exit_code") != 0: return {"ok": False, "error_code": "ARTIFACT_CI_NOT_PASSED"}
    if job.get("superseded_by_job_id"): return {"ok": False, "error_code": "ARTIFACT_CI_SUPERSEDED"}
    if any(item[key] != value for key, value in (("repository", repository), ("branch", branch), ("commit_sha", commit_sha), ("private_ci_job_id", private_ci_job_id))) or (tree_sha and item["tree_sha"] != tree_sha): return {"ok": False, "error_code": "ARTIFACT_IDENTITY_MISMATCH"}
    attestation = get_attestation(item["source_attestation_id"]) if item.get("source_attestation_id") else None
    if not attestation: return {"ok": False, "error_code": "ARTIFACT_ATTESTATION_NOT_FOUND"}
    attestation_check = validate_attestation(item["source_attestation_id"])
    if not attestation_check.get("ok"): return {"ok": False, "error_code": "ARTIFACT_ATTESTATION_INVALID"}
    return {"ok": True, "artifact": item}


def revoke_artifact(artifact_id: str) -> dict:
    init_registry_db(); db = _db(); db.execute("UPDATE release_artifacts SET status='revoked' WHERE artifact_id=?", (artifact_id,)); db.commit(); row = db.execute("SELECT * FROM release_artifacts WHERE artifact_id=?", (artifact_id,)).fetchone(); db.close(); return _row(row) or {"artifact_id": artifact_id, "status": "missing"}
