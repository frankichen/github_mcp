"""Workspace management for CI jobs."""

import logging
import os

from private_ci_agent.cleanup import cleanup_workspaces, remove_tree_verified

logger = logging.getLogger(__name__)


class WorkspaceManager:
    def __init__(self, workspace_root: str):
        self.workspace_root = workspace_root
        os.makedirs(workspace_root, exist_ok=True)

    def create(self, job_id: str) -> str:
        path = os.path.join(self.workspace_root, job_id)
        os.makedirs(path, exist_ok=True)
        source_dir = os.path.join(path, "source")
        os.makedirs(source_dir, exist_ok=True)
        return path

    def get_source_dir(self, job_id: str) -> str:
        return os.path.join(self.workspace_root, job_id, "source")

    def cleanup(self, job_id: str) -> bool:
        path = os.path.join(self.workspace_root, job_id)
        if not os.path.lexists(path):
            return False
        try:
            removed = remove_tree_verified(path)
        except Exception as exc:
            logger.error(
                "Failed to clean workspace: %s cleanup_status=failed error=%s: %s",
                job_id,
                type(exc).__name__,
                str(exc)[:500],
            )
            raise
        if removed:
            logger.info("Cleaned workspace: %s", job_id)
        return removed

    def cleanup_stale(self, active_job_ids: list):
        """Remove stale workspaces only from this Worker's workspace root."""
        cleanup_workspaces(self.workspace_root, active_job_ids)
