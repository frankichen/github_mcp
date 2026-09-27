"""Cleanup routines for containers and workspaces."""

import errno
import logging
import os
import shutil
import stat
import subprocess

logger = logging.getLogger(__name__)


class WorkspaceCleanupError(RuntimeError):
    """Workspace cleanup did not satisfy its filesystem post-condition."""


def _is_within(root: str, path: str) -> bool:
    root = os.path.abspath(root)
    path = os.path.abspath(path)
    try:
        return os.path.commonpath([root, path]) == root
    except ValueError:
        return False


def _assert_tree_owned_by_current_uid(path: str) -> None:
    """Fail closed before deletion if any tree entry belongs to another UID."""
    current_uid = os.geteuid()

    def visit(entry_path: str) -> None:
        info = os.stat(entry_path, follow_symlinks=False)
        if info.st_uid != current_uid:
            raise PermissionError(
                errno.EPERM,
                f"refusing workspace cleanup for uid {info.st_uid}; current uid is {current_uid}",
                entry_path,
            )
        if stat.S_ISDIR(info.st_mode):
            with os.scandir(entry_path) as entries:
                for entry in entries:
                    visit(entry.path)

    visit(path)


def _remove_readonly(func, path: str, exc: BaseException, *, workspace_root: str) -> None:
    """Retry a removal only after minimally fixing current-UID-owned permissions."""
    if not isinstance(exc, OSError) or exc.errno not in {errno.EACCES, errno.EPERM}:
        raise exc

    root = os.path.abspath(workspace_root)
    target = os.path.abspath(path)
    if not _is_within(root, target):
        raise PermissionError(errno.EPERM, "cleanup callback escaped workspace root", target)

    current_uid = os.geteuid()
    target_info = os.stat(target, follow_symlinks=False)
    if target_info.st_uid != current_uid:
        raise PermissionError(
            errno.EPERM,
            f"refusing chmod for uid {target_info.st_uid}; current uid is {current_uid}",
            target,
        )

    chmod_targets: list[tuple[str, os.stat_result]] = []
    if not stat.S_ISLNK(target_info.st_mode):
        chmod_targets.append((target, target_info))

    parent = os.path.dirname(target)
    if parent != target and _is_within(root, parent):
        parent_info = os.stat(parent, follow_symlinks=False)
        if parent_info.st_uid != current_uid:
            raise PermissionError(
                errno.EPERM,
                f"refusing chmod for parent uid {parent_info.st_uid}; current uid is {current_uid}",
                parent,
            )
        if stat.S_ISDIR(parent_info.st_mode):
            chmod_targets.append((parent, parent_info))

    for chmod_path, info in chmod_targets:
        required = stat.S_IWUSR | (stat.S_IXUSR if stat.S_ISDIR(info.st_mode) else 0)
        if info.st_mode & required != required:
            os.chmod(chmod_path, info.st_mode | required, follow_symlinks=False)

    func(target)


def remove_tree_verified(path: str) -> bool:
    """Remove one tree and verify the path is physically absent before success."""
    target = os.path.abspath(path)
    if not os.path.lexists(target):
        return False

    _assert_tree_owned_by_current_uid(target)
    target_info = os.stat(target, follow_symlinks=False)
    if stat.S_ISLNK(target_info.st_mode) or not stat.S_ISDIR(target_info.st_mode):
        os.unlink(target)
    else:
        def onerror(func, failed_path, exc_info):
            exc = exc_info[1] if isinstance(exc_info, tuple) else exc_info
            _remove_readonly(func, failed_path, exc, workspace_root=target)

        shutil.rmtree(target, onerror=onerror)

    if os.path.lexists(target):
        raise WorkspaceCleanupError(f"workspace cleanup post-condition failed: {target}")
    return True


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
    """Remove stale workspaces in this Worker root and verify every success."""
    if not os.path.exists(workspace_root):
        return

    active = set(active_job_ids)
    failures: list[tuple[str, str]] = []
    with os.scandir(workspace_root) as entries:
        for entry in entries:
            if entry.name in active:
                continue
            if not entry.is_dir(follow_symlinks=False) and not entry.is_symlink():
                continue
            try:
                removed = remove_tree_verified(entry.path)
            except Exception as exc:
                logger.error(
                    "Failed to clean stale workspace: %s (%s)",
                    entry.name,
                    type(exc).__name__,
                )
                failures.append((entry.name, type(exc).__name__))
                continue
            if removed:
                logger.info("Cleaned stale workspace: %s", entry.name)

    if failures:
        details = ", ".join(f"{name}:{error}" for name, error in failures)
        raise WorkspaceCleanupError(f"stale workspace cleanup failed: {details}")
