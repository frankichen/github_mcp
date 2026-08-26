"""Single source of truth for externally reported service/build metadata."""

import os
import re
import subprocess
from pathlib import Path


BUILD_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SERVICE_NAME = "MyGithut12"
SERVICE_VERSION = "12.1.2"
SOURCE_REPOSITORY = "https://github.com/frankichen/github_mcp"


def runtime_build_sha() -> str:
    configured = (os.environ.get("MYGITHUB12_BUILD_SHA", "") or os.environ.get("MYGITHUB10_BUILD_SHA", "")).strip()
    if BUILD_SHA_RE.fullmatch(configured):
        return configured
    runtime_mode = os.environ.get("MYGITHUB12_RUNTIME_MODE", os.environ.get("MYGITHUB10_RUNTIME_MODE", "development"))
    if runtime_mode != "development":
        raise RuntimeError("MYGITHUB12_BUILD_SHA must be a full lowercase 40-character Git commit SHA")

    ci_commit = os.environ.get("CI_COMMIT_SHA", "").strip()
    if BUILD_SHA_RE.fullmatch(ci_commit):
        return ci_commit

    configured_root = os.environ.get("CI_REPOSITORY_ROOT", "").strip()
    if configured_root:
        repository_root = Path(configured_root)
    else:
        resolved = Path(__file__).resolve()
        if len(resolved.parents) <= 3:
            raise RuntimeError("development repository root is unavailable; set CI_REPOSITORY_ROOT or CI_COMMIT_SHA")
        repository_root = resolved.parents[3]
    try:
        candidate = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=repository_root,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("development build SHA could not be resolved from Git") from exc
    if not BUILD_SHA_RE.fullmatch(candidate):
        raise RuntimeError("resolved development build SHA is invalid")
    return candidate


def validate_runtime_metadata() -> None:
    runtime_build_sha()
    configured_version = (os.environ.get("MYGITHUB12_VERSION", "") or os.environ.get("MYGITHUB10_VERSION", "")).strip()
    if configured_version and configured_version != SERVICE_VERSION:
        raise RuntimeError(
            f"MYGITHUB12_VERSION must match service version {SERVICE_VERSION}"
        )
