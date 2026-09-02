import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import development_orchestrator as dx
from app import development_session_store as sessions
from app import local_git_mirror as mirror
from app import mygithub12
from app import mygithub12_dx_mcp as dx_mcp
from app import runtime_generation
from app import ci_repository_config
from app.mcp_response import StructuredFastMCP


SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40


def _structured_result(call_result):
    if isinstance(call_result, tuple):
        return call_result[1]
    structured = getattr(call_result, "structured_content", None)
    if structured is None:
        structured = getattr(call_result, "structuredContent", None)
    return structured


def _workspace(**overrides):
    value = {
        "workspace_id": "ws_test",
        "repository": "owner/repo",
        "branch": "ai/task",
        "base_branch": "main",
        "base_commit_sha": SHA_A,
        "head_sha": SHA_B,
        "tree_sha": SHA_C,
        "revision": 3,
        "status": "active",
        "owner": "test",
        "lease_expires_at": 10_000.0,
        "index_commit_sha": SHA_B,
        "pr_number": None,
    }
    value.update(overrides)
    return value


def _no_renewal(session, workspace_revision=3):
    return {
        "renewed": False,
        "session": {"session_revision": 1, "workspace_revision": workspace_revision, **session},
        "workspace": {"workspace_id": session.get("workspace_id", "ws_test"), "revision": workspace_revision},
        "remaining_seconds": 4000.0,
        "audit": None,
    }


def test_development_session_cas_events_and_idempotency(tmp_path, monkeypatch):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "mygithub12.db"))
    monkeypatch.setattr(sessions, "_now", lambda: 1000.0)

    created = sessions.create_session(
        _workspace(), idempotency_key="prepare-key", metadata={"task_name": "DX"}
    )
    assert created["session_revision"] == 1
    assert created["workspace_revision"] == 3
    assert created["lease_valid"] is True

    replay = sessions.create_session(
        _workspace(), idempotency_key="prepare-key", metadata={"task_name": "DX"}
    )
    assert replay["session_id"] == created["session_id"]
    assert replay["replayed"] is True

    advanced = sessions.transition(
        created["session_id"], 1, "validating_fast", allowed_from={"active"}
    )
    assert advanced["session_revision"] == 2
    with pytest.raises(mygithub12.MyGithub12Error) as exc:
        sessions.transition(created["session_id"], 1, "active")
    assert exc.value.code == "DEVELOPMENT_SESSION_REVISION_MISMATCH"

    synced = sessions.sync_from_workspace(
        created["session_id"], 2, _workspace(revision=4, head_sha=SHA_C, tree_sha=SHA_A)
    )
    assert synced["session_revision"] == 3
    assert synced["workspace_revision"] == 4
    assert synced["head_commit_sha"] == SHA_C
    events = sessions.list_events(created["session_id"])
    assert [event["event_type"] for event in events] == [
        "session_created", "state_changed", "workspace_synced"
    ]


def test_development_session_idempotency_conflict_and_restart_recovery(tmp_path, monkeypatch):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "session.db"))
    monkeypatch.setattr(sessions, "_now", lambda: 1000.0)
    first = sessions.create_session(_workspace(), idempotency_key="same")
    with pytest.raises(mygithub12.MyGithub12Error) as exc:
        sessions.create_session(
            _workspace(workspace_id="ws_other", branch="ai/other"), idempotency_key="same"
        )
    assert exc.value.code == "IDEMPOTENCY_CONFLICT"

    validating = sessions.transition(
        first["session_id"], first["session_revision"], "validating_full",
        allowed_from={"active"},
    )
    recovered = sessions.recover_sessions(
        lambda workspace_id: _workspace(
            workspace_id=workspace_id, revision=7, head_sha=SHA_C, tree_sha=SHA_B
        )
    )
    assert recovered == {"checked_sessions": 1, "recovery_required": 0}
    current = sessions.get_session(validating["session_id"])
    assert current["status"] == "validating_full"
    assert current["workspace_revision"] == validating["workspace_revision"]
    assert current["head_commit_sha"] == validating["head_commit_sha"]


def test_validation_correlation_is_restart_durable_and_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "validation-correlation.db"))
    created = sessions.create_session(_workspace(), idempotency_key="correlation")
    validating = sessions.transition(created["session_id"], created["session_revision"], "validating_fast", allowed_from={"active"})

    first_id = sessions.record_validation(
        validating["session_id"], validating["session_revision"], "fast", SHA_B, SHA_C,
        job_id="job-exact", status="running", evidence={"selection": {"complete": True}},
    )
    second_id = sessions.record_validation(
        validating["session_id"], validating["session_revision"], "fast", SHA_B, SHA_C,
        job_id="job-exact", status="passed", evidence={"selection": {"complete": True}}, finished=True,
    )
    correlations = sessions.validation_correlations(
        validating["session_id"], validating["session_revision"], "fast", SHA_B, SHA_C,
    )

    assert second_id == first_id
    assert len(correlations) == 1
    assert correlations[0]["job_id"] == "job-exact"
    assert correlations[0]["status"] == "passed"
    assert correlations[0]["finished_at"] is not None


def test_atomic_session_workspace_auto_renew_syncs_revisions_and_audit(tmp_path, monkeypatch):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "auto-renew.db"))
    monkeypatch.setattr(sessions, "_now", lambda: 1000.0)
    mygithub12.init_db()
    with mygithub12._db() as db:
        db.execute(
            "INSERT INTO workspaces VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("ws_test","owner/repo","ai/task","main",SHA_A,SHA_B,SHA_C,"active",3,"test",1100.0,SHA_B,"{}",None,None,10.0,20.0),
        )
    created=sessions.create_session(_workspace(lease_expires_at=1100.0))
    renewed=sessions.auto_renew_session_workspace_lease(
        created["session_id"],1,"ws_test",3,lease_seconds=7200,event_data={"idempotency_key":"renew-1"},
    )
    assert renewed["session"]["session_revision"] == 2
    assert renewed["session"]["workspace_revision"] == 4
    assert renewed["session"]["lease_expires_at"] == 8200.0
    with mygithub12._db() as db:
        row=db.execute("SELECT revision,lease_expires_at FROM workspaces WHERE workspace_id='ws_test'").fetchone()
    assert tuple(row) == (4,8200.0)
    event=sessions.list_events(created["session_id"])[-1]
    assert event["event_type"] == "workspace_lease_auto_renewed"
    assert event["data"]["idempotency_key"] == "renew-1"
    assert event["data"]["before_workspace_revision"] == 3
    assert event["data"]["after_workspace_revision"] == 4
    with pytest.raises(mygithub12.MyGithub12Error) as exc:
        sessions.auto_renew_session_workspace_lease(created["session_id"],1,"ws_test",3,lease_seconds=7200,event_data={"idempotency_key":"renew-1"})
    assert exc.value.code == "DEVELOPMENT_SESSION_REVISION_MISMATCH"
    with mygithub12._db() as db:
        unchanged=db.execute("SELECT revision,lease_expires_at FROM workspaces WHERE workspace_id='ws_test'").fetchone()
    assert tuple(unchanged) == (4,8200.0)


def test_auto_renew_orchestrator_skips_long_lease_and_rejects_github_drift(monkeypatch):
    session={"session_id":"dev_test","workspace_id":"ws_test","workspace_revision":3,"session_revision":1,"repository":"owner/repo","branch":"ai/task","head_commit_sha":SHA_B,"tree_sha":SHA_C,"lease_expires_at":5000.0,"lease_valid":True,"status":"active"}
    workspace={**_workspace(lease_expires_at=5000.0),"lease_valid":True,"drift_reason":None}
    monkeypatch.setattr(sessions, "_require_revision", lambda *args, **kwargs: session)
    monkeypatch.setattr(sessions, "get_session", lambda *args, **kwargs: session)
    monkeypatch.setattr(mygithub12, "get_workspace", lambda *args, **kwargs: workspace)
    monkeypatch.setattr(mygithub12, "_now", lambda: 1000.0)
    service=SimpleNamespace(client=SimpleNamespace(get_branch=lambda *args: (_ for _ in ()).throw(AssertionError("fresh GitHub read should not run outside renewal threshold"))))
    skipped=dx.maybe_auto_renew_session_workspace(service,"dev_test",1,3,SHA_B,"renew-skip")
    assert skipped["renewed"] is False
    assert skipped["remaining_seconds"] == 4000.0

    near_session={**session,"lease_expires_at":1100.0}
    near_workspace={**workspace,"lease_expires_at":1100.0}
    monkeypatch.setattr(sessions, "get_session", lambda *args, **kwargs: near_session)
    monkeypatch.setattr(mygithub12, "get_workspace", lambda *args, **kwargs: near_workspace)
    called=[]
    monkeypatch.setattr(sessions, "auto_renew_session_workspace_lease", lambda *args, **kwargs: called.append(True))
    drift_service=SimpleNamespace(client=SimpleNamespace(get_branch=lambda *args: SimpleNamespace(commit=SimpleNamespace(sha="d" * 40))))
    with pytest.raises(mygithub12.MyGithub12Error) as exc:
        dx.maybe_auto_renew_session_workspace(drift_service,"dev_test",1,3,SHA_B,"renew-drift")
    assert exc.value.code == "WORKSPACE_BRANCH_DRIFTED"
    assert called == []


def test_session_recovery_cas_clears_stale_evidence_and_replays_idempotently(tmp_path, monkeypatch):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "session-recovery.db"))
    monkeypatch.setattr(sessions, "_now", lambda: 1000.0)
    created=sessions.create_session(_workspace(head_sha=SHA_A,tree_sha=SHA_B,revision=2,index_commit_sha=SHA_A))
    with mygithub12._db() as db:
        db.execute("UPDATE development_sessions SET last_fast_ci_job_id='fast-old',last_full_ci_job_id='full-old',last_attestation_id='att-old',last_failure_resource_uri='resource-old' WHERE session_id=?",(created["session_id"],))
    workspace=_workspace(head_sha=SHA_B,tree_sha=SHA_C,revision=3,index_commit_sha=SHA_B)
    recovered=sessions.recover_stale_session_from_workspace(
        created["session_id"],1,workspace,idempotency_key="recover-1",index_commit_sha=SHA_B,
        recovery_evidence={"github_head_sha":SHA_B,"github_tree_sha":SHA_C},
    )
    current=recovered["session"]
    assert recovered["recovered"] is True and recovered["replayed"] is False
    assert current["session_revision"] == 2 and current["workspace_revision"] == 3
    assert current["head_commit_sha"] == SHA_B and current["tree_sha"] == SHA_C
    assert current["index_commit_sha"] == SHA_B
    assert current["last_fast_ci_job_id"] is None and current["last_full_ci_job_id"] is None
    assert current["last_attestation_id"] is None and current["last_failure_resource_uri"] is None
    replay=sessions.recover_stale_session_from_workspace(
        created["session_id"],1,workspace,idempotency_key="recover-1",index_commit_sha=SHA_B,
        recovery_evidence={"github_head_sha":SHA_B,"github_tree_sha":SHA_C},
    )
    assert replay["replayed"] is True and replay["session"]["session_revision"] == 2
    assert [event["event_type"] for event in sessions.list_events(created["session_id"])] == ["session_created","session_recovered"]
    noop=sessions.recover_stale_session_from_workspace(created["session_id"],2,workspace,index_commit_sha=SHA_B)
    assert noop["recovered"] is False and noop["session"]["session_revision"] == 2


def test_session_recovery_preserves_exact_head_ci_when_only_workspace_revision_is_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "session-revision-recovery.db"))
    monkeypatch.setattr(sessions, "_now", lambda: 1000.0)
    created=sessions.create_session(_workspace(revision=2,index_commit_sha=SHA_B))
    with mygithub12._db() as db:
        db.execute("UPDATE development_sessions SET last_full_ci_job_id='full-exact',last_attestation_id='att-exact' WHERE session_id=?",(created["session_id"],))
    workspace=_workspace(revision=3,index_commit_sha=SHA_B)
    recovered=sessions.recover_stale_session_from_workspace(created["session_id"],1,workspace,index_commit_sha=SHA_B)
    assert recovered["session"]["workspace_revision"] == 3
    assert recovered["session"]["last_full_ci_job_id"] == "full-exact"
    assert recovered["session"]["last_attestation_id"] == "att-exact"
    assert recovered["audit"]["head_changed"] is False


def _recovery_service(actual_head=SHA_B, actual_tree=SHA_C, *, merge_base=SHA_A, ahead_by=1, behind_by=0):
    class Repo:
        def get_commit(self, sha):
            tree=SHA_B if sha == SHA_A else actual_tree
            return SimpleNamespace(sha=sha,commit=SimpleNamespace(tree=SimpleNamespace(sha=tree)))

        def compare(self, base, head):
            return SimpleNamespace(
                merge_base_commit=SimpleNamespace(sha=merge_base),ahead_by=ahead_by,behind_by=behind_by,
            )

    return SimpleNamespace(
        client=SimpleNamespace(get_branch=lambda *args: SimpleNamespace(commit=SimpleNamespace(sha=actual_head))),
        repo=Repo(),
    )


def test_orchestrator_recovers_proven_session_stale_and_requests_exact_index(tmp_path, monkeypatch):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "orchestrator-recovery.db"))
    monkeypatch.setattr(sessions, "_now", lambda: 1000.0)
    created=sessions.create_session(_workspace(head_sha=SHA_A,tree_sha=SHA_B,revision=2,index_commit_sha=SHA_A))
    workspace={**_workspace(head_sha=SHA_B,tree_sha=SHA_C,revision=3,index_commit_sha=SHA_B),"lease_valid":True,"drift_reason":None}
    service=_recovery_service()
    monkeypatch.setattr(mygithub12, "get_workspace", lambda *args, **kwargs: workspace)
    monkeypatch.setattr(mygithub12, "_service_repo", lambda *args, **kwargs: service.repo)
    monkeypatch.setattr(mygithub12, "_tree_sha", lambda commit: commit.commit.tree.sha)
    monkeypatch.setattr(mygithub12, "get_index_status", lambda *args, **kwargs: {"status":"missing","commit_sha":SHA_B,"tree_sha":SHA_C})
    requests=[]
    monkeypatch.setattr(mygithub12, "request_index_build", lambda *args, **kwargs: requests.append(args) or {"status":"queued","commit_sha":SHA_B})
    result=dx.recover_stale_session(service,created["session_id"],1,2,SHA_A,"recover-safe")
    assert result["recovered"] is True and result["session"]["head_commit_sha"] == SHA_B
    assert result["session"]["index_commit_sha"] is None
    assert result["index"]["request"]["commit_sha"] == SHA_B
    assert requests and requests[0][2] == SHA_B
    assert sessions.list_events(created["session_id"])[-1]["event_type"] == "session_recovered"


def test_orchestrator_recovery_rejects_external_drift_without_mutating_session(tmp_path, monkeypatch):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "orchestrator-drift.db"))
    monkeypatch.setattr(sessions, "_now", lambda: 1000.0)
    created=sessions.create_session(_workspace(revision=2))
    workspace={**_workspace(revision=3),"lease_valid":True,"drift_reason":None}
    service=_recovery_service(actual_head=SHA_C,actual_tree=SHA_A)
    monkeypatch.setattr(mygithub12, "get_workspace", lambda *args, **kwargs: workspace)
    monkeypatch.setattr(mygithub12, "_service_repo", lambda *args, **kwargs: service.repo)
    monkeypatch.setattr(mygithub12, "_tree_sha", lambda commit: commit.commit.tree.sha)
    with pytest.raises(mygithub12.MyGithub12Error) as exc:
        dx.recover_stale_session(service,created["session_id"],1,2,SHA_B,"recover-drift")
    assert exc.value.code == "WORKSPACE_BRANCH_DRIFTED"
    current=sessions.get_session(created["session_id"])
    assert current["session_revision"] == 1 and current["workspace_revision"] == 2
    assert sessions.list_events(created["session_id"])[-1]["event_type"] == "external_drift_detected"


def test_orchestrator_recovery_refuses_unproven_session_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "orchestrator-refused.db"))
    monkeypatch.setattr(sessions, "_now", lambda: 1000.0)
    created=sessions.create_session(_workspace(head_sha=SHA_A,tree_sha=SHA_B,revision=2,index_commit_sha=SHA_A))
    workspace={**_workspace(head_sha=SHA_B,tree_sha=SHA_C,revision=3,index_commit_sha=SHA_B),"lease_valid":True,"drift_reason":None}
    service=_recovery_service(merge_base=SHA_C,ahead_by=1,behind_by=1)
    monkeypatch.setattr(mygithub12, "get_workspace", lambda *args, **kwargs: workspace)
    monkeypatch.setattr(mygithub12, "_service_repo", lambda *args, **kwargs: service.repo)
    monkeypatch.setattr(mygithub12, "_tree_sha", lambda commit: commit.commit.tree.sha)
    with pytest.raises(mygithub12.MyGithub12Error) as exc:
        dx.recover_stale_session(service,created["session_id"],1,2,SHA_A,"recover-refused")
    assert exc.value.code == "DEVELOPMENT_SESSION_RECOVERY_REQUIRED"
    assert sessions.get_session(created["session_id"])["session_revision"] == 1
    assert sessions.list_events(created["session_id"])[-1]["event_type"] == "recovery_refused"


def test_auto_renew_recovers_revision_only_stale_session_before_threshold_check(monkeypatch):
    session={"session_id":"dev_test","workspace_id":"ws_test","workspace_revision":2,"session_revision":1,"repository":"owner/repo","branch":"ai/task","head_commit_sha":SHA_B,"tree_sha":SHA_C,"lease_expires_at":5000.0,"lease_valid":True,"index_commit_sha":SHA_B,"status":"active","metadata":{}}
    workspace={**_workspace(revision=3,lease_expires_at=5000.0,index_commit_sha=SHA_B),"lease_valid":True,"drift_reason":None}
    recovered_session={**session,"workspace_revision":3,"session_revision":2}
    monkeypatch.setattr(sessions, "get_session", lambda *args, **kwargs: session)
    monkeypatch.setattr(mygithub12, "get_workspace", lambda *args, **kwargs: workspace)
    monkeypatch.setattr(mygithub12, "_now", lambda: 1000.0)
    calls=[]
    monkeypatch.setattr(dx, "recover_stale_session", lambda *args, **kwargs: calls.append(args) or {"session":recovered_session,"workspace":workspace,"recovered":True,"replayed":False})
    result=dx.maybe_auto_renew_session_workspace(SimpleNamespace(),"dev_test",1,2,SHA_B,"recover-revision")
    assert result["renewed"] is False and result["session"]["session_revision"] == 2
    assert result["recovery"]["recovered"] is True and len(calls) == 1


def test_prepare_task_success_transitions_preparing_session_to_active(tmp_path, monkeypatch):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "prepare-success.db"))
    monkeypatch.setattr(sessions, "_now", lambda: 1000.0)
    workspace = _workspace()
    monkeypatch.setattr(dx, "operation_policy", lambda *args: {"github": True, "private_ci": True, "test_deploy": False, "self_deploy": False})
    monkeypatch.setattr(mygithub12, "resolve_identity", lambda *args, **kwargs: {"repository": "owner/repo", "commit_sha": SHA_A, "tree_sha": SHA_C})
    monkeypatch.setattr(mygithub12, "create_workspace", lambda *args, **kwargs: workspace)
    monkeypatch.setattr(mygithub12, "get_index_status", lambda *args, **kwargs: {"status": "ready"})
    monkeypatch.setattr(mygithub12, "agent_instructions", lambda *args, **kwargs: {"instructions": []})
    monkeypatch.setattr(dx, "context_pack_v2", lambda *args, **kwargs: {"schema_version": 2, "items": []})
    monkeypatch.setattr(mygithub12, "workspace_overlap", lambda *args, **kwargs: {"items": []})

    result = dx.prepare_task(object(), "owner/repo", "DX prepare", idempotency_key="prepare-success")
    session = result["development_session"]
    assert session["status"] == "active"
    assert session["session_revision"] == 2
    assert [event["event_type"] for event in sessions.list_events(session["session_id"])] == [
        "session_created", "preparation_completed"
    ]


def test_prepare_task_failure_closes_workspace_and_persists_terminal_session(tmp_path, monkeypatch):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "prepare-failure.db"))
    monkeypatch.setattr(sessions, "_now", lambda: 1000.0)
    workspace = _workspace()
    closed = {**workspace, "status": "closed", "revision": 4, "lease_expires_at": 0}
    close_calls = []
    monkeypatch.setattr(dx, "operation_policy", lambda *args: {"github": True, "private_ci": True, "test_deploy": False, "self_deploy": False})
    monkeypatch.setattr(mygithub12, "resolve_identity", lambda *args, **kwargs: {"repository": "owner/repo", "commit_sha": SHA_A, "tree_sha": SHA_C})
    monkeypatch.setattr(mygithub12, "create_workspace", lambda *args, **kwargs: workspace)
    monkeypatch.setattr(mygithub12, "get_index_status", lambda *args, **kwargs: {"status": "ready"})
    monkeypatch.setattr(
        mygithub12, "agent_instructions",
        lambda *args, **kwargs: (_ for _ in ()).throw(mygithub12.MyGithub12Error("CONTEXT_FAIL", "context failed")),
    )
    monkeypatch.setattr(
        mygithub12, "close_workspace",
        lambda service, workspace_id, revision: close_calls.append((workspace_id, revision)) or closed,
    )

    with pytest.raises(mygithub12.MyGithub12Error) as exc:
        dx.prepare_task(object(), "owner/repo", "DX prepare", idempotency_key="prepare-failure")
    assert exc.value.code == "CONTEXT_FAIL"
    assert exc.value.details["workspace_closed"] is True
    assert exc.value.details["recovery_required"] is False
    assert close_calls == [("ws_test", 3)]
    current = sessions.get_session(exc.value.details["development_session_id"])
    assert current["status"] == "prepare_failed"
    assert current["workspace_revision"] == 4
    assert current["lease_expires_at"] == 0
    assert [event["event_type"] for event in sessions.list_events(current["session_id"])] == [
        "session_created", "preparation_failed"
    ]


@pytest.mark.asyncio
async def test_apply_change_set_rejects_invalid_pr_config_before_write(monkeypatch):
    mcp = StructuredFastMCP("dx-invalid-pr")
    service = SimpleNamespace()
    session = {"repository": "owner/repo", "branch": "ai/task", "base_branch": "main"}
    workspace = {"workspace_id": "ws_test"}
    writes = []
    monkeypatch.setattr(dx, "require_session_workspace", lambda *args, **kwargs: (session, workspace))
    monkeypatch.setattr(dx, "execute_change_set", lambda *args, **kwargs: writes.append(True) or {})

    async def github_call(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def finalize_write(*args):
        raise AssertionError("write must not start")

    dx_mcp.register_dx_tools(mcp, github_call, service, finalize_write)
    result = _structured_result(await mcp.call_tool("apply_development_change_set", {
        "development_session_id": "dev_test", "expected_session_revision": 1,
        "expected_workspace_revision": 3, "expected_head_sha": SHA_B,
        "change_set_json": json.dumps({"schema_version": 1, "mode": "patch", "patch": "x"}),
        "commit_message": "test", "dry_run": False, "create_pull_request": True,
        "pull_request_json": "{bad-json",
    }))
    assert result["ok"] is False
    assert result["error"]["code"] == "SEARCH_QUERY_INVALID"
    assert writes == []


@pytest.mark.asyncio
async def test_apply_change_set_keeps_verified_commit_evidence_when_session_finalize_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "mygithub12.db"))
    monkeypatch.setenv("MYGITHUB12_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    mcp = StructuredFastMCP("dx-session-finalize-failure")
    service = SimpleNamespace()
    session = {"session_id": "dev_test", "session_revision": 1, "workspace_id": "ws_test", "repository": "owner/repo", "branch": "ai/task", "base_branch": "main", "head_commit_sha": SHA_B}
    workspace = {"workspace_id": "ws_test", "revision": 3}
    monkeypatch.setattr(dx, "require_session_workspace", lambda *args, **kwargs: (session, workspace))
    monkeypatch.setattr(dx, "maybe_auto_renew_session_workspace", lambda *args, **kwargs: _no_renewal(session))
    monkeypatch.setattr(dx, "execute_change_set", lambda *args, **kwargs: {"write_verified": not bool(args[7]), "changed_files": []})
    monkeypatch.setattr(
        dx, "after_verified_change",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            mygithub12.MyGithub12Error("DEVELOPMENT_SESSION_REVISION_MISMATCH", "session changed")
        ),
    )

    async def github_call(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def finalize_write(*args):
        return {"write_verified": True, "commit_sha": SHA_C, "tree_sha": SHA_A, "operation_id": "op-1"}

    dx_mcp.register_dx_tools(mcp, github_call, service, finalize_write)
    prepared = _structured_result(await mcp.call_tool("apply_development_change_set", {
        "development_session_id": "dev_test", "expected_session_revision": 1,
        "expected_workspace_revision": 3, "expected_head_sha": SHA_B,
        "change_set_json": json.dumps({"schema_version": 1, "mode": "patch", "patch": "x"}),
        "commit_message": "test", "dry_run": True,
    }))
    result = _structured_result(await mcp.call_tool("apply_development_change_set", {
        "development_session_id": "dev_test", "expected_session_revision": 1,
        "expected_workspace_revision": 3, "expected_head_sha": SHA_B,
        "prepared_change_set_id": prepared["prepared_change_set_id"],
        "commit_message": "test", "dry_run": False,
    }))
    assert result["ok"] is False
    assert result["write_verified"] is True
    assert result["commit_sha"] == SHA_C
    assert result["recovery_required"] is True
    assert result["failed_stage"] == "development_session_finalize"
    assert result["orchestration_error"]["code"] == "DEVELOPMENT_SESSION_REVISION_MISMATCH"


@pytest.mark.asyncio
async def test_apply_change_set_pr_failure_is_partial_success_after_verified_commit(monkeypatch):
    mcp = StructuredFastMCP("dx-pr-partial-success")

    class Service:
        def create_pull_request(self, *args):
            raise mygithub12.MyGithub12Error("GITHUB_API_ERROR", "PR create failed", {"retryable": True})

    service = Service()
    session = {
        "session_id": "dev_test", "session_revision": 1, "workspace_revision": 3,
        "repository": "owner/repo", "branch": "ai/task",
        "base_branch": "main", "head_commit_sha": SHA_B,
    }
    workspace = {"workspace_id": "ws_test", "revision": 3}
    monkeypatch.setattr(dx, "require_session_workspace", lambda *args, **kwargs: (session, workspace))
    monkeypatch.setattr(dx, "maybe_auto_renew_session_workspace", lambda *args, **kwargs: _no_renewal(session))
    monkeypatch.setattr(
        dx,
        "execute_change_set",
        lambda *args, **kwargs: {"write_verified": not bool(args[7]), "changed_files": []},
    )
    monkeypatch.setattr(
        dx, "after_verified_change",
        lambda *args, **kwargs: {"session": {"session_id": "dev_test", "session_revision": 2, "status": "active", "repository": "owner/repo", "branch": "ai/task", "base_branch": "main", "head_commit_sha": SHA_C, "pull_number": None}, "index": None, "index_error": None},
    )

    async def github_call(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def finalize_write(*args):
        return {"write_verified": True, "commit_sha": SHA_C, "tree_sha": SHA_A, "operation_id": "op-2"}

    dx_mcp.register_dx_tools(mcp, github_call, service, finalize_write)
    prepared = _structured_result(await mcp.call_tool("apply_development_change_set", {
        "development_session_id": "dev_test", "expected_session_revision": 1,
        "expected_workspace_revision": 3, "expected_head_sha": SHA_B,
        "change_set_json": json.dumps({"schema_version": 1, "mode": "patch", "patch": "x"}),
        "commit_message": "test", "dry_run": True,
    }))
    result = _structured_result(await mcp.call_tool("apply_development_change_set", {
        "development_session_id": "dev_test", "expected_session_revision": 1,
        "expected_workspace_revision": 3, "expected_head_sha": SHA_B,
        "prepared_change_set_id": prepared["prepared_change_set_id"],
        "commit_message": "test", "dry_run": False, "create_pull_request": True,
        "pull_request_json": "{}",
    }))
    assert result["ok"] is True
    assert result["commit_sha"] == SHA_C
    assert result["partial_success"] is True
    assert result["pull_request_error"]["code"] == "GITHUB_API_ERROR"
    assert result["development_session"]["status"] == "active"


@pytest.mark.asyncio
async def test_validate_invalid_mode_fails_before_session_state_transition(monkeypatch):
    mcp = StructuredFastMCP("dx-validation-preflight")
    service = SimpleNamespace()
    session = {
        "session_id": "dev_test", "workspace_id": "ws_test", "repository": "owner/repo",
        "branch": "ai/task", "head_commit_sha": SHA_B, "base_commit_sha": SHA_A,
        "status": "active", "session_revision": 1,
    }
    transitions = []
    monkeypatch.setattr(sessions, "get_session", lambda *args: session)
    monkeypatch.setattr(sessions, "_require_revision", lambda *args, **kwargs: session)
    monkeypatch.setattr(mygithub12, "get_workspace", lambda *args: {"revision": 3})
    monkeypatch.setattr(mygithub12, "workspace_write_preflight", lambda *args: {"revision": 3})
    monkeypatch.setattr(
        dx, "validation_preflight",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            mygithub12.MyGithub12Error("DEVELOPMENT_SESSION_STATE_INVALID", "invalid validation mode")
        ),
    )
    monkeypatch.setattr(sessions, "transition", lambda *args, **kwargs: transitions.append(args) or session)

    async def github_call(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def finalize_write(*args):
        return {}

    dx_mcp.register_dx_tools(mcp, github_call, service, finalize_write)
    result = _structured_result(await mcp.call_tool("validate_development_task", {
        "development_session_id": "dev_test", "expected_session_revision": 1,
        "mode": "invalid", "wait_seconds": 0,
    }))
    assert result["ok"] is False
    assert result["error"]["code"] == "DEVELOPMENT_SESSION_STATE_INVALID"
    assert transitions == []


@pytest.mark.asyncio
async def test_validate_job_start_failure_rolls_session_back(monkeypatch):
    mcp = StructuredFastMCP("dx-validation-start-rollback")
    service = SimpleNamespace()
    session = {
        "session_id": "dev_test", "workspace_id": "ws_test", "repository": "owner/repo",
        "branch": "ai/task", "head_commit_sha": SHA_B, "base_commit_sha": SHA_A,
        "status": "active", "session_revision": 1, "workspace_revision": 3,
    }
    phase_session = {**session, "status": "validating_fast", "session_revision": 2}
    rollback_session = {**session, "session_revision": 3}
    monkeypatch.setattr(sessions, "get_session", lambda *args: session)
    monkeypatch.setattr(sessions, "_require_revision", lambda *args, **kwargs: session)
    monkeypatch.setattr(mygithub12, "get_workspace", lambda *args: {"revision": 3})
    monkeypatch.setattr(mygithub12, "workspace_write_preflight", lambda *args: {"revision": 3})
    monkeypatch.setattr(dx, "validation_preflight", lambda *args, **kwargs: {"profile": "repo-fast-check"})
    monkeypatch.setattr(dx, "maybe_auto_renew_session_workspace", lambda *args, **kwargs: _no_renewal(session))
    monkeypatch.setattr(
        dx, "start_validation_job",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            mygithub12.MyGithub12Error("PRIVATE_CI_UNAVAILABLE", "CI unavailable", {"retryable": True})
        ),
    )

    def transition(_session_id, _revision, to_status, **kwargs):
        return phase_session if to_status == "validating_fast" else rollback_session

    monkeypatch.setattr(sessions, "transition", transition)

    async def github_call(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def finalize_write(*args):
        return {}

    dx_mcp.register_dx_tools(mcp, github_call, service, finalize_write)
    result = _structured_result(await mcp.call_tool("validate_development_task", {
        "development_session_id": "dev_test", "expected_session_revision": 1,
        "mode": "fast", "wait_seconds": 0,
    }))
    assert result["ok"] is False
    assert result["validation_started"] is False
    assert result["recovery_required"] is False
    assert result["development_session"]["status"] == "active"
    assert result["error"]["details"]["validation_state_rolled_back"] is True


@pytest.mark.asyncio
async def test_finalize_merge_keeps_merge_evidence_when_session_finalize_fails(monkeypatch):
    mcp = StructuredFastMCP("dx-merge-partial-finalize")
    service = SimpleNamespace()
    session = {
        "session_id": "dev_test", "workspace_id": "ws_test", "repository": "owner/repo",
        "branch": "ai/task", "base_branch": "main", "head_commit_sha": SHA_B,
        "status": "pr_ready", "session_revision": 4, "workspace_revision": 3, "pull_number": 54, "metadata": {},
    }
    monkeypatch.setattr(sessions, "get_session", lambda *args: session)
    monkeypatch.setattr(sessions, "_require_revision", lambda *args, **kwargs: session)
    monkeypatch.setattr(dx, "maybe_auto_renew_session_workspace", lambda *args, **kwargs: _no_renewal(session))
    monkeypatch.setattr(
        dx_mcp.github_utils, "merge_github_pull_request",
        lambda *args, **kwargs: {"ok": True, "merged": True, "merge_commit_sha": SHA_C},
    )
    monkeypatch.setattr(
        dx_mcp.managed_merge, "finalize_managed_pr_merge",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            mygithub12.MyGithub12Error("DEVELOPMENT_SESSION_REVISION_MISMATCH", "session changed")
        ),
    )

    async def github_call(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def finalize_write(*args):
        return {}

    dx_mcp.register_dx_tools(mcp, github_call, service, finalize_write)
    result = _structured_result(await mcp.call_tool("finalize_development_task", {
        "development_session_id": "dev_test", "expected_session_revision": 4,
        "action": "merge", "merge_method": "squash", "confirm": True,
    }))
    assert result["ok"] is False
    assert result["merge_completed"] is True
    assert result["merge"]["merged"] is True
    assert result["merge"]["merge_commit_sha"] == SHA_C
    assert result["recovery_required"] is True
    assert result["failed_stage"] == "development_session_merge_finalize"


def test_session_schema_is_expand_only_for_existing_mygithub12_db(tmp_path, monkeypatch):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "expand.db"))
    mygithub12.init_db()
    with mygithub12._db() as db:
        before = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        workspace_columns_before = [
            row[1] for row in db.execute("PRAGMA table_info(workspaces)").fetchall()
        ]
    sessions.init_session_db()
    with mygithub12._db() as db:
        after = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        workspace_columns_after = [
            row[1] for row in db.execute("PRAGMA table_info(workspaces)").fetchall()
        ]
    assert before <= after
    assert workspace_columns_before == workspace_columns_after
    assert {
        "development_sessions", "development_session_events",
        "development_session_validations",
    } <= after


def test_blue_green_generation_and_shared_leader_lease(tmp_path, monkeypatch):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "runtime.db"))
    monkeypatch.setenv("MYGITHUB12_BUILD_SHA", SHA_A)
    monkeypatch.setenv("MYGITHUB12_RUNTIME_MODE", "production")
    monkeypatch.setenv("MYGITHUB12_GENERATION_ID", "blue")
    monkeypatch.setenv("MYGITHUB12_RUNTIME_ROLE", "active")

    blue = runtime_generation.register_generation()
    assert blue["generation_id"] == "blue"
    assert blue["schema_compatible"] is True
    lease = runtime_generation.acquire_leader(ttl_seconds=30)
    assert lease["acquired"] is True
    assert runtime_generation.runtime_status()["ready_for_side_effects"] is True

    monkeypatch.setenv("MYGITHUB12_GENERATION_ID", "green")
    monkeypatch.setenv("MYGITHUB12_RUNTIME_ROLE", "standby")
    standby = runtime_generation.register_generation()
    assert standby["ready_for_reads"] is True
    assert standby["ready_for_side_effects"] is False
    assert runtime_generation.acquire_leader()["reason"] == "runtime_not_active"

    monkeypatch.setenv("MYGITHUB12_RUNTIME_ROLE", "active")
    conflict = runtime_generation.acquire_leader()
    assert conflict["acquired"] is False
    assert conflict["holder"] == "blue"
    assert runtime_generation.runtime_status()["ready_for_side_effects"] is False
    with mygithub12._db() as db:
        db.execute(
            "UPDATE runtime_leader_leases SET expires_at=0 WHERE lease_name='controller-maintenance'"
        )
    takeover = runtime_generation.acquire_leader()
    assert takeover["acquired"] is True
    assert takeover["generation_id"] == "green"
    assert runtime_generation.runtime_status()["ready_for_side_effects"] is True


@pytest.mark.asyncio
async def test_active_generation_renews_leader_lease_beyond_original_ttl(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "renewal.db"))
    monkeypatch.setenv("MYGITHUB12_BUILD_SHA", SHA_A)
    monkeypatch.setenv("MYGITHUB12_RUNTIME_MODE", "production")
    monkeypatch.setenv("MYGITHUB12_GENERATION_ID", "active-renewing")
    monkeypatch.setenv("MYGITHUB12_RUNTIME_ROLE", "active")
    now = [1_000.0]
    monkeypatch.setattr(runtime_generation.time, "time", lambda: now[0])

    runtime_generation.register_generation()
    initial = runtime_generation.acquire_leader(ttl_seconds=5)
    assert initial["acquired"] is True
    initial_expiry = initial["expires_at"]
    sleep_calls = 0

    async def advance_time(seconds):
        nonlocal sleep_calls
        now[0] += seconds
        sleep_calls += 1
        if sleep_calls == 4:
            raise asyncio.CancelledError

    monkeypatch.setattr(runtime_generation.asyncio, "sleep", advance_time)
    with pytest.raises(asyncio.CancelledError):
        await runtime_generation.maintain_leader_lease(
            ttl_seconds=5,
            renew_interval_seconds=2,
        )

    status = runtime_generation.runtime_status()
    assert now[0] > initial_expiry
    assert status["leader"]["is_leader"] is True
    assert status["leader"]["expires_at"] > now[0]
    assert status["ready_for_side_effects"] is True


@pytest.mark.asyncio
async def test_standby_generation_does_not_maintain_leader_lease(monkeypatch):
    monkeypatch.setenv("MYGITHUB12_RUNTIME_ROLE", "standby")

    async def unexpected_sleep(_seconds):
        raise AssertionError("standby must not start a lease renewal loop")

    monkeypatch.setattr(runtime_generation.asyncio, "sleep", unexpected_sleep)
    await runtime_generation.maintain_leader_lease()


def test_profile_discovery_is_strict_for_configured_repo_but_skips_unconfigured(monkeypatch):
    monkeypatch.setattr(ci_repository_config, "github_repository_is_allowed", lambda repo: True)
    monkeypatch.setattr(
        ci_repository_config,
        "_config_cache",
        {
            "auto_enroll": {"enabled": False},
            "repositories": {
                "owner/good": {"enabled": True, "allowed_profiles": ["repo-auto-check", "repo-fast-check"]},
                "owner/bad": {"enabled": True, "allowed_profiles": ["repo-auto-check"]},
            },
        },
    )
    assert ci_repository_config.validate_profile_discovery("owner/good")["ok"] is True
    bad = ci_repository_config.validate_profile_discovery("owner/bad")
    assert bad["ok"] is False
    assert bad["required_profiles_missing"] == ["repo-fast-check"]
    skipped = ci_repository_config.validate_profile_discovery("owner/none")
    assert skipped["ok"] is True and skipped["skipped"] is True


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def test_local_mirror_reads_exact_blob_from_bare_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("MYGITHUB12_MIRROR_ROOT", str(tmp_path / "mirror-root"))
    work = tmp_path / "work"
    bare = tmp_path / "bare.git"
    work.mkdir()
    _git("init", cwd=work)
    _git("config", "user.email", "test@example.invalid", cwd=work)
    _git("config", "user.name", "Test", cwd=work)
    (work / "hello.txt").write_text("hello mirror\n", encoding="utf-8")
    _git("add", "hello.txt", cwd=work)
    _git("commit", "-m", "one", cwd=work)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=work, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "clone", "--bare", str(work), str(bare)], check=True,
        capture_output=True, text=True,
    )
    monkeypatch.setattr(
        mirror, "ensure_mirror",
        lambda repository, fetch=True: {
            "path": str(bare), "generation": "unit", "fetched": fetch
        },
    )
    monkeypatch.setattr(mirror, "mirror_path", lambda repository: bare)
    data, evidence = mirror.read_blob("owner/repo", sha, "hello.txt")
    assert data == b"hello mirror\n"
    assert evidence["source"] == "mirror"
    assert len(evidence["blob_sha"]) == 40


@pytest.mark.parametrize("repository", ["owner/repo with space", "../repo", "owner//repo", "owner/repo/extra"])
def test_local_mirror_rejects_noncanonical_repository_identity(repository):
    with pytest.raises(mygithub12.MyGithub12Error):
        mirror._repo_slug(repository)


def test_get_files_batch_mirror_failure_falls_back_to_github(tmp_path, monkeypatch):
    class Entry:
        decoded_content = b"api fallback\n"
        sha = "d" * 40

    class Repo:
        def get_contents(self, path, ref):
            return Entry()

    monkeypatch.setenv("MYGITHUB12_MIRROR_READS_ENABLED", "true")
    monkeypatch.setattr(
        mygithub12, "resolve_identity",
        lambda *args, **kwargs: {
            "repository": "owner/repo", "commit_sha": SHA_A, "tree_sha": SHA_B
        },
    )
    monkeypatch.setattr(mygithub12, "_service_repo", lambda *args, **kwargs: Repo())
    monkeypatch.setattr(
        mirror, "read_blob",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            mygithub12.MyGithub12Error("MIRROR_OBJECT_MISSING", "missing")
        ),
    )
    result = mygithub12.get_files_batch(
        object(), "owner/repo", SHA_A, '["hello.txt"]'
    )
    assert result["mirror_enabled"] is True
    assert result["mirror_fallbacks"] == 1
    assert result["items"][0]["read_source"] == "github_api"
    assert result["items"][0]["mirror_fallback_error_code"] == "MIRROR_OBJECT_MISSING"
    assert result["items"][0]["content"] == "api fallback\n"


def test_get_files_batch_mirror_is_opt_in_even_in_production(monkeypatch):
    class Entry:
        decoded_content = b"api only\n"
        sha = "e" * 40

    class Repo:
        def get_contents(self, path, ref):
            return Entry()

    monkeypatch.delenv("MYGITHUB12_MIRROR_READS_ENABLED", raising=False)
    monkeypatch.setenv("MYGITHUB12_RUNTIME_MODE", "production")
    monkeypatch.setattr(
        mygithub12, "resolve_identity",
        lambda *args, **kwargs: {
            "repository": "owner/repo", "commit_sha": SHA_A, "tree_sha": SHA_B
        },
    )
    monkeypatch.setattr(mygithub12, "_service_repo", lambda *args, **kwargs: Repo())
    monkeypatch.setattr(
        mirror, "read_blob",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("mirror must stay disabled")),
    )
    result = mygithub12.get_files_batch(object(), "owner/repo", SHA_A, '["hello.txt"]')
    assert result["mirror_enabled"] is False
    assert result["mirror_fallbacks"] == 0
    assert result["items"][0]["read_source"] == "github_api"


def test_get_files_batch_raw_mirror_os_error_falls_back(monkeypatch):
    class Entry:
        decoded_content = b"fallback\n"
        sha = "f" * 40

    class Repo:
        def get_contents(self, path, ref):
            return Entry()

    monkeypatch.setenv("MYGITHUB12_MIRROR_READS_ENABLED", "true")
    monkeypatch.setattr(
        mygithub12, "resolve_identity",
        lambda *args, **kwargs: {
            "repository": "owner/repo", "commit_sha": SHA_A, "tree_sha": SHA_B
        },
    )
    monkeypatch.setattr(mygithub12, "_service_repo", lambda *args, **kwargs: Repo())
    monkeypatch.setattr(
        mirror, "read_blob",
        lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError("denied")),
    )
    result = mygithub12.get_files_batch(object(), "owner/repo", SHA_A, '["hello.txt"]')
    assert result["mirror_fallbacks"] == 1
    assert result["items"][0]["mirror_fallback_error_code"] == "MIRROR_UNAVAILABLE"
    assert result["items"][0]["content"] == "fallback\n"


def test_context_pack_v2_is_ranked_compact_and_resource_backed(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_RESPONSE_RESOURCE_DIR", str(tmp_path / "resources"))
    monkeypatch.setattr(
        mygithub12,
        "repository_context_pack",
        lambda *args, **kwargs: {
            "ok": True,
            "repository": "owner/repo",
            "commit_sha": SHA_A,
            "tree_sha": SHA_B,
            "items": [
                {"path": "docs/a.md", "blob_sha": "1" * 40, "reason": "documentation", "content": "docs", "size_bytes": 4},
                {"path": "app/core.py", "blob_sha": "2" * 40, "reason": "explicit_seed", "content": "def core():\n    pass\n", "size_bytes": 21},
            ],
            "omitted_count": 0,
        },
    )
    result = dx.context_pack_v2(
        object(), "owner/repo", SHA_A, "change core", '["app/core.py"]', "[]"
    )
    assert result["schema_version"] == 2
    assert result["items"][0]["path"] == "app/core.py"
    assert result["items"][0]["score"] > result["items"][1]["score"]
    assert result["resource_uri"].startswith("mygithub12://response/")


def test_change_set_v1_reuses_strict_modes_and_rejects_unknown_schema():
    patch = dx.parse_change_set(
        json.dumps({"schema_version": 1, "mode": "patch", "patch": "--- a/a\n+++ b/a\n@@ -1 +1 @@\n-a\n+b\n"})
    )
    assert patch["mode"] == "patch"
    assert len(patch["canonical_hash"]) == 64
    with pytest.raises(mygithub12.MyGithub12Error) as exc:
        dx.parse_change_set(json.dumps({"schema_version": 2, "mode": "patch", "patch": "x"}))
    assert exc.value.code == "PATCH_INVALID_FORMAT"


def test_upload_change_set_supports_atomic_multiple_finalized_files(monkeypatch):
    uploads = [
        {"path": "a.txt", "expected_blob_sha": "a" * 40, "upload_id": "upload-a"},
        {"path": "b.txt", "expected_blob_sha": "b" * 40, "upload_id": "upload-b"},
    ]
    parsed = dx.parse_change_set(json.dumps({"schema_version": 1, "mode": "upload", "uploaded_files": uploads}))
    assert parsed["mode"] == "upload"
    monkeypatch.setattr(dx.mygithub10, "_safe_path", lambda path: path)
    preflight_calls = []
    monkeypatch.setattr(dx.mygithub10, "preflight_upload_targets", lambda *args: preflight_calls.append(args) or {"head_sha": SHA_B})
    monkeypatch.setattr(dx.mygithub10, "_load_upload", lambda upload_id: (None, None, {"finalized": True, "size": 10, "sha256": upload_id}))
    dry = dx.execute_change_set(object(), {"repository": "owner/repo", "branch": "ai/task"}, {}, parsed, SHA_B, 3, "multi", True, "multi-key", {})
    assert [item["path"] for item in dry["changed_files"]] == ["a.txt", "b.txt"]
    assert len(preflight_calls) == 1

    calls = []
    monkeypatch.setattr(dx.mygithub10, "commit_uploads", lambda *args: calls.append(args) or {"ok": True, "commit_sha": SHA_C})
    result = dx.execute_change_set(object(), {"repository": "owner/repo", "branch": "ai/task"}, {}, parsed, SHA_B, 3, "multi", False, "multi-key", {})
    assert result["commit_sha"] == SHA_C
    assert len(calls) == 1
    assert calls[0][4] == uploads


def test_upload_change_set_enforces_batch_limits(monkeypatch):
    uploads = [{"path": "a.txt", "upload_id": "upload-a"}, {"path": "b.txt", "upload_id": "upload-b"}]
    monkeypatch.setattr(dx.mygithub10, "MAX_UPLOAD_CHANGE_SET_FILES", 1)
    with pytest.raises(mygithub12.MyGithub12Error) as count_exc:
        dx.parse_change_set(json.dumps({"schema_version": 1, "mode": "upload", "uploaded_files": uploads}))
    assert count_exc.value.code == "PATCH_INVALID_FORMAT"

    monkeypatch.setattr(dx.mygithub10, "MAX_UPLOAD_CHANGE_SET_FILES", 2)
    monkeypatch.setattr(dx.mygithub10, "MAX_UPLOAD_CHANGE_SET_BYTES", 15)
    parsed = dx.parse_change_set(json.dumps({"schema_version": 1, "mode": "upload", "uploaded_files": uploads}))
    monkeypatch.setattr(dx.mygithub10, "_safe_path", lambda path: path)
    monkeypatch.setattr(dx.mygithub10, "preflight_upload_targets", lambda *args: {"head_sha": SHA_B})
    monkeypatch.setattr(dx.mygithub10, "_load_upload", lambda upload_id: (None, None, {"finalized": True, "size": 10, "sha256": upload_id}))
    with pytest.raises(mygithub12.MyGithub12Error) as size_exc:
        dx.execute_change_set(object(), {"repository": "owner/repo", "branch": "ai/task"}, {}, parsed, SHA_B, 3, "multi", True, "limit-key", {})
    assert size_exc.value.code == "UPLOAD_SIZE_EXCEEDED"


def test_upload_change_set_rejects_duplicate_paths_and_upload_ids():
    with pytest.raises(mygithub12.MyGithub12Error) as path_exc:
        dx.parse_change_set(json.dumps({"schema_version": 1, "mode": "upload", "uploaded_files": [{"path": "same", "upload_id": "one"}, {"path": "same", "upload_id": "two"}]}))
    assert path_exc.value.code == "PATCH_INVALID_FORMAT"
    with pytest.raises(mygithub12.MyGithub12Error) as upload_exc:
        dx.parse_change_set(json.dumps({"schema_version": 1, "mode": "upload", "uploaded_files": [{"path": "a", "upload_id": "same"}, {"path": "b", "upload_id": "same"}]}))
    assert upload_exc.value.code == "PATCH_INVALID_FORMAT"


def test_mygithub12_db_open_retries_cantopen_once(tmp_path, monkeypatch):
    target = tmp_path / "nested" / "mygithub12.db"
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(target))
    original_connect = mygithub12.sqlite3.connect
    calls = {"count": 0}

    def flaky_connect(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise mygithub12.sqlite3.OperationalError("unable to open database file")
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(mygithub12.sqlite3, "connect", flaky_connect)
    with mygithub12._db() as db:
        assert db.execute("SELECT 1").fetchone()[0] == 1
    assert calls["count"] == 2
    assert target.parent.is_dir()


def test_mygithub12_db_open_does_not_retry_unrelated_sqlite_error(tmp_path, monkeypatch):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "mygithub12.db"))
    calls = {"count": 0}

    def broken_connect(*args, **kwargs):
        calls["count"] += 1
        raise mygithub12.sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(mygithub12.sqlite3, "connect", broken_connect)
    with pytest.raises(mygithub12.sqlite3.OperationalError, match="database is locked"):
        mygithub12._db()
    assert calls["count"] == 1


def test_mygithub12_db_context_always_closes_connection(tmp_path, monkeypatch):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "close.db"))
    with mygithub12._db() as db:
        assert db.execute("SELECT 1").fetchone()[0] == 1
    with pytest.raises(mygithub12.sqlite3.ProgrammingError, match="closed database"):
        db.execute("SELECT 1")
