"""Cross-project shared-file writers: detect overlap without serializing Git branches."""

import pytest
from app import development_base_sync_recovery as base_sync
from app import mygithub12_workspace as workspace


@pytest.mark.parametrize(
    ("repository", "filename"),
    [
        ("frankichen/xyzl", "app/app/src/main/java/com/xiangyun/bed/lib/RestApiClient.java"),
        ("frankichen/sxt", "internal/modules/account/service.go"),
        ("example/generic", "src/shared.py"),
    ],
)
def test_parallel_workspaces_same_file_are_advisory(monkeypatch, repository, filename):
    primary = {
        "workspace_id": "ws_a", "repository": repository, "branch": "ai/feature-a",
        "base_commit_sha": "a" * 40, "head_sha": "b" * 40,
        "scope": {"paths": [filename], "apis": [], "tables": [], "migrations": []},
    }
    other = {
        **primary, "workspace_id": "ws_b", "branch": "ai/feature-b",
        "head_sha": "c" * 40,
    }
    monkeypatch.setattr(workspace, "get_workspace", lambda _svc, _id: primary)
    monkeypatch.setattr(
        workspace, "list_workspaces",
        lambda _svc, _repository, _status: {"items": [primary, other]},
    )
    monkeypatch.setattr(
        workspace, "_compare",
        lambda _svc, _repo, _base, _head: {"files": [{"path": filename}]},
    )
    actual = workspace.workspace_overlap(object(), "ws_a")
    assert actual["comparison_verified"] is True
    assert actual["items"][0]["level"] == "medium"
    kinds = {x["kind"]: x["items"] for x in actual["items"][0]["evidence"]}
    assert kinds["changed_paths"] == [filename]
    # Base-sync may filter imported upstream paths but must never re-upgrade
    # the same shared file to HIGH.
    normalized = base_sync._current_task_overlap(actual, [filename])
    assert normalized["items"][0]["level"] == "medium"


@pytest.mark.parametrize("protected_kind", ["apis", "tables", "migrations"])
def test_true_shared_contract_overlap_stays_high(monkeypatch, protected_kind):
    scope = {"paths": [], "apis": [], "tables": [], "migrations": []}
    scope[protected_kind] = ["same-critical-contract"]
    primary = {
        "workspace_id": "ws_a", "repository": "frankichen/xyzl",
        "branch": "ai/a", "base_commit_sha": "a" * 40, "head_sha": "b" * 40,
        "scope": scope,
    }
    other = {**primary, "workspace_id": "ws_b", "branch": "ai/b", "head_sha": "c" * 40}
    monkeypatch.setattr(workspace, "get_workspace", lambda *_: primary)
    monkeypatch.setattr(workspace, "list_workspaces", lambda *_: {"items": [primary, other]})
    monkeypatch.setattr(workspace, "_compare", lambda *_: {"files": [{"path": "src/shared.py"}]})
    overlap = workspace.workspace_overlap(object(), "ws_a")
    assert overlap["items"][0]["level"] == "high"
    assert base_sync._current_task_overlap(overlap, ["src/shared.py"])["items"][0]["level"] == "high"


def test_unreadable_comparison_stays_unverified(monkeypatch):
    primary = {
        "workspace_id": "ws_a", "repository": "frankichen/sxt",
        "branch": "ai/a", "base_commit_sha": "a" * 40, "head_sha": "b" * 40,
        "scope": {"paths": []},
    }
    other = {**primary, "workspace_id": "ws_b", "branch": "ai/b", "head_sha": "c" * 40}
    monkeypatch.setattr(workspace, "get_workspace", lambda *_: primary)
    monkeypatch.setattr(workspace, "list_workspaces", lambda *_: {"items": [primary, other]})
    def unavailable(*_args):
        raise workspace.MyGithub12Error("GITHUB_COMPARE_UNAVAILABLE", "comparison unavailable")
    monkeypatch.setattr(workspace, "_compare", unavailable)
    actual = workspace.workspace_overlap(object(), "ws_a")
    assert actual["comparison_verified"] is False
    assert actual["items"][0]["evidence"][0]["kind"] == "comparison_unavailable"
