"""Workspace management for CI jobs."""

import os
import shutil
import logging
from typing import Optional

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

    def cleanup(self, job_id: str):
        path = os.path.join(self.workspace_root, job_id)
        if os.path.exists(path):
            shutil.rmtree(path, ignore_errors=True)
            logger.info("Cleaned workspace: %s", job_id)

    def cleanup_stale(self, active_job_ids: list):
        """Remove workspaces for jobs no longer active."""
        if not os.path.exists(self.workspace_root):
            return
        for entry in os.listdir(self.workspace_root):
            if entry not in active_job_ids:
                path = os.path.join(self.workspace_root, entry)
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                    logger.info("Cleaned stale workspace: %s", entry)
