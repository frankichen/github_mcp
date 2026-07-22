"""Artifact-only deployment state machine used by the private deploy executor."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

try:
    from scripts.artifact_release import verify_release_artifact
except ModuleNotFoundError:
    from artifact_release import verify_release_artifact


def deploy_artifact(artifact_dir: str | Path, incoming_root: str | Path, current_link: str | Path,
                    migration_required: bool = False, migration_runner=None, healthcheck=None,
                    restart_services=None) -> dict:
    artifact_dir = Path(artifact_dir).resolve(); incoming_root = Path(incoming_root).resolve(); current_link = Path(current_link)
    verified = verify_release_artifact(artifact_dir)
    incoming_root.mkdir(parents=True, exist_ok=True)
    release_dir = incoming_root / ("release-" + verified["archive_sha256"][:16]); release_dir.mkdir()
    try:
        subprocess.run(["tar", "--zstd", "--extract", "--no-same-owner", "--no-same-permissions", "-f", str(artifact_dir / "release.tar.zst"), "-C", str(release_dir)], check=True, timeout=120)
        previous = os.readlink(current_link) if current_link.is_symlink() else None
        if migration_required:
            if not migration_runner or migration_runner() is not True: raise RuntimeError("MIGRATION_FAILED")
        if restart_services: restart_services()
        if healthcheck and healthcheck() is not True: raise RuntimeError("HEALTH_CHECK_FAILED")
        tmp_link = current_link.with_name(current_link.name + ".next")
        if tmp_link.exists() or tmp_link.is_symlink(): tmp_link.unlink()
        tmp_link.symlink_to(release_dir)
        os.replace(tmp_link, current_link)
        return {"ok": True, "release_path": str(release_dir), "previous": previous, "rolled_back": False}
    except Exception as exc:
        if current_link.is_symlink():
            current_link.unlink()
        if 'previous' in locals() and previous:
            current_link.symlink_to(previous)
        return {"ok": False, "error_code": str(exc), "rolled_back": bool('previous' in locals() and previous)}
