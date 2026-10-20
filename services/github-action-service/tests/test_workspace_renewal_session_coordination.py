"""Managed Workspace renewal must not strand a same-HEAD original Session."""
from types import SimpleNamespace

import pytest

from app import development_session_store as sessions
from app import development_orchestrator as orchestrator
from app import mygithub12_workspace as workspace


HEAD = "a" * 40
TREE = "b" * 40


def _arrange(monkeypatch, *, pinned_revision=75, state="pr_ready"):
    ws = {
        "workspace_id": "ws_renew", "repository": "owner/repo",
        "branch": "ai/renew", "owner": "WORKER-B",
        "base_branch": "main", "base_commit_sha": "c" * 40,
        "head_sha": HEAD, "tree_sha": TREE, "status": "active",
        "revision": 75, "drift_reason": None, "lease_expires_at": 9999999999.0,
        "lease_valid": True,
    }
    session = {
        "session_id": "dev_original", "workspace_id": ws["workspace_id"],
        "repository": ws["repository"], "branch": ws["branch"],
        "owner": ws["owner"], "base_branch": ws["base_branch"],
        "base_commit_sha": ws["base_commit_sha"],
        "head_commit_sha": HEAD, "tree_sha": TREE, "status": state,
        "session_revision": 14, "workspace_revision": pinned_revision,
        "lease_expires_at": 9999999999.0,
    }
    service = SimpleNamespace(
        client=SimpleNamespace(get_branch=lambda *_: SimpleNamespace(
            commit=SimpleNamespace(sha=HEAD),
        ))
    )
    monkeypatch.setattr(workspace, "get_workspace", lambda *_: dict(ws))
    monkeypatch.setattr(workspace, "_workspace_cas", lambda *_: None)
    monkeypatch.setattr(workspace, "_service_repo", lambda *_: SimpleNamespace(
        get_commit=lambda *_: object(),
    ))
    monkeypatch.setattr(workspace, "_tree_sha", lambda *_: TREE)
    monkeypatch.setattr(
        sessions, "find_active_session_for_workspace", lambda *_: dict(session),
    )
    return ws, session, service


def test_managed_renew_uses_one_atomic_workspace_session_cas(monkeypatch):
    ws, session, service = _arrange(monkeypatch)
    calls = []
    monkeypatch.setattr(orchestrator, "recover_stale_session",
                        lambda *args, **kwargs: pytest.fail("exact pins need no recovery"))

    def renew(session_id, session_revision, workspace_id, workspace_revision, **kwargs):
        calls.append((session_id, session_revision, workspace_id, workspace_revision, kwargs))
        ws["revision"] += 1
        session["session_revision"] += 1
        session["workspace_revision"] = ws["revision"]
        return {"session": dict(session), "audit": {"source": "test"}}

    monkeypatch.setattr(sessions, "auto_renew_session_workspace_lease", renew)
    result = workspace.renew_workspace_lease(service, "ws_renew", 75, 7200)
    assert result["managed_session_coordinated"] is True
    assert result["revision"] == 76
    assert result["session_revision"] == 15
    assert result["session_workspace_revision"] == 76
    assert calls[0][:4] == ("dev_original", 14, "ws_renew", 75)
    assert calls[0][4]["event_data"]["writer_owner"] == "WORKER-B"


def test_lease_only_revision_split_recovers_original_session_then_renews(monkeypatch):
    ws, session, service = _arrange(monkeypatch, pinned_revision=74)
    calls = []

    def repair(_service, session_id, session_revision, workspace_revision, head, **kwargs):
        calls.append(("recover", session_id, session_revision, workspace_revision, head))
        repaired = dict(session)
        repaired["session_revision"] += 1
        repaired["workspace_revision"] = 75
        return {"session": repaired}

    def renew(session_id, session_revision, workspace_id, workspace_revision, **kwargs):
        calls.append(("renew", session_id, session_revision, workspace_revision))
        ws["revision"] += 1
        return {"session": {"session_revision": session_revision + 1,
                             "workspace_revision": ws["revision"]}, "audit": {}}

    monkeypatch.setattr(orchestrator, "recover_stale_session", repair)
    monkeypatch.setattr(sessions, "auto_renew_session_workspace_lease", renew)
    result = workspace.renew_workspace_lease(service, "ws_renew", 75)
    assert calls[0][:4] == ("recover", "dev_original", 14, 75)
    assert calls[1] == ("renew", "dev_original", 15, 75)
    assert result["revision"] == result["session_workspace_revision"] == 76


def test_live_branch_drift_never_renews_a_managed_session(monkeypatch):
    ws, _, service = _arrange(monkeypatch)
    service.client.get_branch = lambda *_: SimpleNamespace(
        commit=SimpleNamespace(sha="f" * 40)
    )
    monkeypatch.setattr(sessions, "auto_renew_session_workspace_lease",
                        lambda *args, **kwargs: pytest.fail("must fail closed"))
    with pytest.raises(workspace.MyGithub12Error) as error:
        workspace.renew_workspace_lease(service, ws["workspace_id"], 75)
    assert error.value.code == "WORKSPACE_BRANCH_DRIFTED"


def test_blocked_session_cannot_be_kept_alive_by_renewal(monkeypatch):
    ws, _, service = _arrange(monkeypatch, state="blocked")
    monkeypatch.setattr(sessions, "auto_renew_session_workspace_lease",
                        lambda *args, **kwargs: pytest.fail("must not renew"))
    with pytest.raises(workspace.MyGithub12Error) as error:
        workspace.renew_workspace_lease(service, ws["workspace_id"], 75)
    assert error.value.code == "DEVELOPMENT_SESSION_STATE_INVALID"
