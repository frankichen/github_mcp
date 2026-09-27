import logging
from types import SimpleNamespace

import pytest

from private_ci_agent.podman_resources import (
    JOB_LABEL,
    RESOURCE_LABEL,
    WORKER_LABEL,
    PodmanCleanupError,
    PodmanResourceManager,
)


def _labels(job="job-1", worker="wsl-ci-01", resource="postgres-data"):
    return {
        JOB_LABEL: job,
        WORKER_LABEL: worker,
        RESOURCE_LABEL: resource,
    }


def test_cleanup_command_failure_is_visible_and_never_logs_success(monkeypatch, caplog):
    manager = PodmanResourceManager("podman", "wsl-ci-01")
    monkeypatch.setattr(manager, "_exists", lambda *_args: True)
    monkeypatch.setattr(manager, "_inspect_labels", lambda *_args: _labels())
    monkeypatch.setattr(
        manager,
        "_command",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=125, stdout="", stderr="boom"),
    )

    with caplog.at_level(logging.INFO):
        with pytest.raises(PodmanCleanupError, match="remove failed"):
            manager.remove_verified("volume", "job-volume", "job-1", "postgres-data")

    assert "podman cleanup failed" in caplog.text
    assert "podman cleanup succeeded" not in caplog.text


def test_cleanup_requires_verified_physical_absence(monkeypatch):
    manager = PodmanResourceManager("podman", "wsl-ci-01")
    exists = iter([True, True])
    monkeypatch.setattr(manager, "_exists", lambda *_args: next(exists))
    monkeypatch.setattr(manager, "_inspect_labels", lambda *_args: _labels())
    monkeypatch.setattr(
        manager,
        "_command",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    with pytest.raises(PodmanCleanupError, match="post-condition"):
        manager.remove_verified("volume", "job-volume", "job-1", "postgres-data")


def test_cleanup_is_idempotent_when_resource_is_already_absent(monkeypatch):
    manager = PodmanResourceManager("podman", "wsl-ci-01")
    calls = []
    monkeypatch.setattr(manager, "_exists", lambda *_args: False)
    monkeypatch.setattr(
        manager,
        "_command",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    assert manager.remove_verified("volume", "missing", "job-1", "postgres-data") is False
    assert manager.remove_verified("volume", "missing", "job-1", "postgres-data") is False
    assert calls == []


@pytest.mark.parametrize(
    ("labels", "state", "anonymous", "expected"),
    [
        (_labels(), {"status": "running", "worker_id": "wsl-ci-01"}, False, "ACTIVE"),
        (_labels(), {"status": "queued", "worker_id": None}, False, "SAFE_STALE"),
        (_labels(), {"status": "running", "worker_id": "wsl-ci-02"}, False, "SAFE_STALE"),
        (_labels(worker="wsl-ci-02"), {"status": "failed", "worker_id": None}, False, "UNKNOWN"),
        (_labels(resource="shared-cache"), {"status": "failed", "worker_id": None}, False, "SHARED"),
        ({}, None, True, "LEGACY_UNOWNED"),
    ],
)
def test_resource_classification(labels, state, anonymous, expected):
    manager = PodmanResourceManager("podman", "wsl-ci-01")
    resolver = (lambda _job_id: state) if state is not None else None

    classification, _reason = manager._classification_for_labels(
        labels,
        anonymous=anonymous,
        job_state_resolver=resolver,
    )

    assert classification == expected


def test_reconcile_removes_only_safe_stale_owned_resources(monkeypatch):
    manager = PodmanResourceManager("podman", "wsl-ci-01")
    inventory = [
        {
            "kind": "pod",
            "name": "stale-pod",
            "job": "job-stale",
            "worker": "wsl-ci-01",
            "resource_type": "pod",
            "classification": "SAFE_STALE",
        },
        {
            "kind": "volume",
            "name": "active-volume",
            "job": "job-active",
            "worker": "wsl-ci-01",
            "resource_type": "postgres-data",
            "classification": "ACTIVE",
        },
        {
            "kind": "volume",
            "name": "other-worker",
            "job": "job-stale",
            "worker": "wsl-ci-02",
            "resource_type": "postgres-data",
            "classification": "UNKNOWN",
        },
        {
            "kind": "volume",
            "name": "unknown-volume",
            "job": None,
            "worker": None,
            "resource_type": None,
            "classification": "LEGACY_UNOWNED",
        },
        {
            "kind": "volume",
            "name": "shared-volume",
            "job": "job-stale",
            "worker": "wsl-ci-01",
            "resource_type": "shared-cache",
            "classification": "SHARED",
        },
    ]
    removed = []
    resolver = lambda _job_id: {"status": "queued", "worker_id": None}
    monkeypatch.setattr(manager, "inventory_resources", lambda *_args, **_kwargs: inventory)
    monkeypatch.setattr(
        manager,
        "remove_verified",
        lambda kind, name, job, resource, **kwargs: removed.append(
            (kind, name, job, resource, kwargs["require_stale"])
        ) or True,
    )

    result = manager.reconcile_stale(resolver)

    assert removed == [("pod", "stale-pod", "job-stale", "pod", True)]
    assert result["removed"] == 1
    assert result["classifications"]["ACTIVE"] == 1
    assert result["classifications"]["SHARED"] == 1
    assert result["classifications"]["LEGACY_UNOWNED"] == 1


def test_stale_delete_rechecks_controller_truth_before_remove(monkeypatch):
    manager = PodmanResourceManager("podman", "wsl-ci-01")
    monkeypatch.setattr(manager, "_exists", lambda *_args: True)
    monkeypatch.setattr(manager, "_inspect_labels", lambda *_args: _labels())

    with pytest.raises(PodmanCleanupError, match="stale cleanup refused"):
        manager.remove_verified(
            "volume",
            "job-volume",
            "job-1",
            "postgres-data",
            require_stale=True,
            job_state_resolver=lambda _job_id: {
                "status": "running",
                "worker_id": "wsl-ci-01",
            },
        )


def test_dry_run_inventory_classifies_legacy_anonymous_volume_without_deleting(monkeypatch):
    manager = PodmanResourceManager("podman", "wsl-ci-01")
    command_calls = []

    def fake_list(args):
        command_calls.append(args)
        if args[:2] == ["volume", "ls"]:
            return [{
                "Name": "legacy-anon",
                "Labels": {},
                "Anonymous": True,
                "MountCount": 0,
                "Mountpoint": "/unused",
                "CreatedAt": "2026-09-01T00:00:00Z",
            }]
        return []

    monkeypatch.setattr(manager, "_list_json", fake_list)
    items = manager.inventory_resources(lambda _job_id: None)

    assert len(items) == 1
    assert items[0]["name"] == "legacy-anon"
    assert items[0]["classification"] == "LEGACY_UNOWNED"
    assert items[0]["referenced_by"] == []
    assert "estimated_size_bytes" in items[0]
    assert all("rm" not in args for args in command_calls)
