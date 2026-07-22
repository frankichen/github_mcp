"""Cleanup routines for containers and workspaces."""

import logging
import os
import subprocess
import shutil

logger = logging.getLogger(__name__)


def cleanup_containers(podman_binary: str, job_id_prefixes: list):
    """Remove containers matching ci- prefix that aren't in active list."""
    try:
        result = subprocess.run(
            [podman_binary, "ps", "-a", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=10,
        )
        for name in result.stdout.strip().split("\n"):
            if name.startswith("ci-"):
                if not any(name == f"ci-{p[:12]}" for p in job_id_prefixes):
                    logger.info("Removing stale container: %s", name)
                    subprocess.run([podman_binary, "rm", "-f", name],
                                   capture_output=True, timeout=10)
    except Exception as e:
        logger.warning("Container cleanup failed: %s", e)


def cleanup_workspaces(workspace_root: str, active_job_ids: list):
    """Remove workspace directories not in active job list."""
    if not os.path.exists(workspace_root):
        return
    for entry in os.listdir(workspace_root):
        if entry not in active_job_ids:
            path = os.path.join(workspace_root, entry)
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
                logger.info("Cleaned stale workspace: %s", entry)
