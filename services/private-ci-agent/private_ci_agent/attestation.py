"""Immutable CI tree attestation helpers."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def build_tree_attestation(repository: str, tested_commit_sha: str, tested_tree_sha: str, base_sha: str, private_ci_job_id: str, profile: str, container_image_digest: str, go_version: str | None, node_version: str | None, lockfile_hashes: dict, test_config_hash: str, expires_at: str) -> dict:
    return {
        "repository": repository, "tested_commit_sha": tested_commit_sha, "tested_tree_sha": tested_tree_sha,
        "base_sha": base_sha, "private_ci_job_id": private_ci_job_id, "profile": profile,
        "container_image_digest": container_image_digest, "go_version": go_version,
        "node_version": node_version, "lockfile_hashes": lockfile_hashes,
        "test_config_hash": test_config_hash, "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": expires_at,
    }


def attestation_fingerprint(attestation: dict) -> str:
    return _hash(attestation)


def can_reuse_attestation(attestation: dict, *, repository: str, commit_sha: str, tree_sha: str, profile: str, image_digest: str, test_config_hash: str) -> bool:
    return all((attestation.get("repository") == repository, attestation.get("tested_commit_sha") == commit_sha, attestation.get("tested_tree_sha") == tree_sha, attestation.get("profile") == profile, attestation.get("container_image_digest") == image_digest, attestation.get("test_config_hash") == test_config_hash))
