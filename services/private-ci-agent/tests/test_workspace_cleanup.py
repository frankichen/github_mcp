import errno
import logging
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from private_ci_agent import cleanup as cleanup_module
from private_ci_agent import workspace as workspace_module
from private_ci_agent.cleanup import WorkspaceCleanupError, cleanup_workspaces, remove_tree_verified
from private_ci_agent.workspace import WorkspaceManager


def make_readonly_go_cache(workspace: Path) -> Path:
    module_dir = workspace / "go-cache" / "gomod" / "example.com" / "example@v1"
    module_dir.mkdir(parents=True)
    (module_dir / "go.mod").write_text("module example.com/example\n", encoding="utf-8")
    (module_dir / "ziphash").write_text("h1:example\n", encoding="utf-8")
    for file_path in workspace.rglob("*"):
        if file_path.is_file():
            os.chmod(file_path, 0o444)
    for directory in sorted((path for path in workspace.rglob("*") if path.is_dir()), key=lambda path: len(path.parts), reverse=True):
        os.chmod(directory, 0o555)
    os.chmod(workspace, 0o555)
    return workspace


def test_remove_tree_verified_deletes_normal_tree_and_checks_physical_absence(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "result.txt").write_text("ok", encoding="utf-8")
    assert remove_tree_verified(str(workspace)) is True
    assert not os.path.exists(workspace)


def test_remove_tree_verified_deletes_readonly_current_uid_go_cache(tmp_path):
    workspace = make_readonly_go_cache(tmp_path / "workspace")
    assert remove_tree_verified(str(workspace)) is True
    assert not os.path.exists(workspace)


def test_remove_tree_verified_handles_partial_tree_and_missing_path(tmp_path):
    workspace = tmp_path / "workspace"
    nested = workspace / "nested"
    nested.mkdir(parents=True)
    removed_early = nested / "already-removed"
    removed_early.write_text("x", encoding="utf-8")
    removed_early.unlink()
    (nested / "remaining").write_text("x", encoding="utf-8")
    assert remove_tree_verified(str(workspace)) is True
    assert not workspace.exists()
    assert remove_tree_verified(str(workspace)) is False


def test_remove_tree_verified_unlinks_symlink_without_following_target(tmp_path):
    target = tmp_path / "outside"
    target.mkdir()
    sentinel = target / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    link = tmp_path / "workspace-link"
    link.symlink_to(target, target_is_directory=True)
    assert remove_tree_verified(str(link)) is True
    assert not os.path.lexists(link)
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_non_current_uid_tree_fails_before_chmod_or_delete(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    foreign = workspace / "foreign"
    foreign.write_text("x", encoding="utf-8")
    real_stat = cleanup_module.os.stat
    chmod_calls = []

    def fake_stat(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if os.path.abspath(os.fspath(path)) == os.path.abspath(os.fspath(foreign)):
            return SimpleNamespace(st_mode=result.st_mode, st_uid=os.geteuid() + 1)
        return result

    monkeypatch.setattr(cleanup_module.os, "stat", fake_stat)
    monkeypatch.setattr(cleanup_module.os, "chmod", lambda *args, **kwargs: chmod_calls.append((args, kwargs)))
    with pytest.raises(PermissionError):
        remove_tree_verified(str(workspace))
    assert chmod_calls == []
    assert workspace.exists()
    assert foreign.exists()


def test_remove_readonly_refuses_chmod_for_non_current_uid(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "readonly-file"
    target.write_text("x", encoding="utf-8")
    os.chmod(target, 0o444)
    original_stat = os.stat(target, follow_symlinks=False)
    fake_stat = SimpleNamespace(st_mode=original_stat.st_mode, st_uid=os.geteuid() + 1)
    retry_called = False
    chmod_called = False

    def retry(_path):
        nonlocal retry_called
        retry_called = True

    def fake_chmod(*_args, **_kwargs):
        nonlocal chmod_called
        chmod_called = True

    monkeypatch.setattr(cleanup_module.os, "stat", lambda *_args, **_kwargs: fake_stat)
    monkeypatch.setattr(cleanup_module.os, "chmod", fake_chmod)
    with pytest.raises(PermissionError):
        cleanup_module._remove_readonly(
            retry,
            str(target),
            PermissionError(errno.EACCES, "permission denied"),
            workspace_root=str(root),
        )
    assert retry_called is False
    assert chmod_called is False
    assert stat.S_IMODE(original_stat.st_mode) == 0o444


def test_workspace_manager_handles_normal_readonly_and_missing_workspaces(tmp_path):
    manager = WorkspaceManager(str(tmp_path / "workspaces"))
    normal = Path(manager.create("normal-job"))
    (normal / "source" / "result.txt").write_text("ok", encoding="utf-8")
    assert manager.cleanup("normal-job") is True
    assert not os.path.exists(normal)
    readonly = Path(manager.create("readonly-job"))
    make_readonly_go_cache(readonly / "go-cache")
    assert manager.cleanup("readonly-job") is True
    assert not os.path.exists(readonly)
    assert manager.cleanup("already-gone") is False


def test_cleanup_stale_preserves_active_workspace_and_deletes_stale(tmp_path):
    manager = WorkspaceManager(str(tmp_path / "workspaces"))
    active = Path(manager.create("active-job"))
    stale = Path(manager.create("stale-job"))
    make_readonly_go_cache(stale / "go-cache")
    manager.cleanup_stale(["active-job"])
    assert active.exists()
    assert not os.path.exists(stale)


def test_cleanup_workspaces_uses_verified_physical_postcondition(tmp_path):
    root = tmp_path / "workspaces"
    stale = root / "stale-job"
    stale.mkdir(parents=True)
    (stale / "file").write_text("x", encoding="utf-8")
    cleanup_workspaces(str(root), [])
    assert not os.path.exists(stale)


def test_stale_cleanup_failure_is_error_and_not_false_success(tmp_path, caplog, monkeypatch):
    root = tmp_path / "workspaces"
    failed = root / "failed-job"
    removable = root / "removable-job"
    failed.mkdir(parents=True)
    removable.mkdir(parents=True)
    real_remove = cleanup_module.remove_tree_verified

    def selective_remove(path):
        if Path(path).name == "failed-job":
            raise PermissionError("simulated UID-mapped tree")
        return real_remove(path)

    monkeypatch.setattr(cleanup_module, "remove_tree_verified", selective_remove)
    with caplog.at_level(logging.INFO, logger="private_ci_agent.cleanup"):
        with pytest.raises(WorkspaceCleanupError):
            cleanup_workspaces(str(root), [])
    assert failed.exists()
    assert not removable.exists()
    assert "Failed to clean stale workspace: failed-job" in caplog.text
    assert "cleanup_status=failed" in caplog.text
    assert "simulated UID-mapped tree" in caplog.text
    assert "Cleaned stale workspace: failed-job" not in caplog.text
    assert "Cleaned stale workspace: removable-job" in caplog.text


def test_cleanup_failure_is_logged_without_false_success(tmp_path, caplog, monkeypatch):
    manager = WorkspaceManager(str(tmp_path / "workspaces"))
    workspace = Path(manager.create("failed-job"))

    def fail(_path):
        raise PermissionError("simulated cleanup failure")

    monkeypatch.setattr(workspace_module, "remove_tree_verified", fail)
    with caplog.at_level(logging.ERROR, logger="private_ci_agent.workspace"):
        with pytest.raises(PermissionError):
            manager.cleanup("failed-job")
    assert workspace.exists()
    assert "Failed to clean workspace: failed-job" in caplog.text
    assert "cleanup_status=failed" in caplog.text
    assert "simulated cleanup failure" in caplog.text
    assert "Cleaned workspace: failed-job" not in caplog.text


def test_worker_workspace_roots_remain_isolated(tmp_path):
    worker_one = WorkspaceManager(str(tmp_path / "wsl-ci-01" / "workspaces"))
    worker_two = WorkspaceManager(str(tmp_path / "wsl-ci-02" / "workspaces"))
    stale_one = Path(worker_one.create("stale-one"))
    untouched_two = Path(worker_two.create("job-two"))
    worker_one.cleanup_stale([])
    assert not stale_one.exists()
    assert untouched_two.exists()
