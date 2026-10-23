"""Authorized manager-only first-session revision handoff, no Git ref moves."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest
sys.path.insert(0, str(Path(__file__).parent))
from app import development_sessionless_recovery as recovery
from app import development_session_store as sessions
from app import mygithub12
from test_sessionless_workspace_recovery import seed, state, args, NEW_HEAD, BASE, BASE_SHA, NEW_TREE, BRANCH

LIVE_SHA = "f" * 40
OVERLAP = "skills/lenshub-ai-development/references/device-push-contract.md"

def setup(tmp_path, monkeypatch):
    service = seed(tmp_path, monkeypatch, drifted=False)
    with sessions._LOCK, sessions._db() as db:
        db.execute("UPDATE workspaces SET owner=? WHERE workspace_id=?",
                   ("WORKER-C:#624:1", "ws_recovery"))
    service.client.heads[BASE] = LIVE_SHA
    service.repo.trees[LIVE_SHA] = "5" * 40
    def compare(start, end):
        assert start == BASE_SHA
        assert end in (LIVE_SHA, NEW_HEAD)
        return SimpleNamespace(
            merge_base_commit=SimpleNamespace(sha=BASE_SHA),
            ahead_by=1, behind_by=0,
            files=[SimpleNamespace(filename=OVERLAP, status="modified")],
        )
    service.repo.compare = compare
    options=args(drifted=False,
        expected_owner="WORKER-C:#624:1",
        next_owner="WORKER-C:#624:2",
        expected_writer_lease_id=287,
        expected_active_binding_id=195,
        expected_live_base_sha=LIVE_SHA,
        reviewed_base_overlap_paths_json=json.dumps([OVERLAP]),
        idempotency_key="owner-transition-624r2",
    )
    return service, options

def test_preview_then_confirm_atomically_transfers_original_workspace(tmp_path, monkeypatch):
    service, kw=setup(tmp_path, monkeypatch)
    before,_=state()
    preview=recovery.recover_sessionless_workspace(service,**kw)
    assert preview["base_advance"]["overlapping_paths"]==[OVERLAP]
    assert preview["next_owner"]=="WORKER-C:#624:2"
    assert state()[0]==before and state()[1]==[]
    result=recovery.recover_sessionless_workspace(service,**{**kw,"confirm":True})
    ws, ss=state()
    assert result["confirmed"] and result["created_first_session"]
    assert ws["revision"]==8 and ws["owner"]=="WORKER-C:#624:2"
    assert len(ss)==1 and ss[0]["owner"]=="WORKER-C:#624:2"
    assert ws["head_sha"]==NEW_HEAD and ws["tree_sha"]==NEW_TREE
    assert ws["base_commit_sha"]==BASE_SHA
    assert ws["lease_expires_at"]==before["lease_expires_at"]
    assert result["git_ref_changed"] is False
    replay=recovery.recover_sessionless_workspace(service,**{**kw,"confirm":True})
    assert replay["replayed"] is True and state()[0]==ws and state()[1]==ss

@pytest.mark.parametrize("changes,expected_code",[
    ({"reviewed_base_overlap_paths_json":"[]"}, "RECOVERY_BASE_OVERLAP"),
    ({"reviewed_base_overlap_paths_json":'["unrelated.md"]'}, "RECOVERY_BASE_OVERLAP_REVIEW_MISMATCH"),
    ({"expected_live_base_sha":"e"*40}, "RECOVERY_BASE_CHANGED"),
    ({"next_owner":"WORKER-B:#624:2"}, "RECOVERY_OWNER_TRANSITION_INVALID"),
    ({"next_owner":"WORKER-C:#625:2"}, "RECOVERY_OWNER_TRANSITION_INVALID"),
    ({"next_owner":"WORKER-C:#624:3"}, "RECOVERY_OWNER_TRANSITION_INVALID"),
    ({"expected_writer_lease_id":0}, "RECOVERY_OPERATOR_ATTESTATION_REQUIRED"),
    ({"expected_active_binding_id":0}, "RECOVERY_OPERATOR_ATTESTATION_REQUIRED"),
])
def test_bad_transition_or_overlap_fails_without_mutation(tmp_path, monkeypatch, changes, expected_code):
    service, kw=setup(tmp_path, monkeypatch)
    before=state()
    with pytest.raises(mygithub12.MyGithub12Error) as exc:
        recovery.recover_sessionless_workspace(service,**{**kw,**changes,"confirm":True})
    assert exc.value.code==expected_code
    assert state()==before

def test_old_public_recovery_without_operator_confirmation_stays_fail_closed(tmp_path, monkeypatch):
    service, kw=setup(tmp_path, monkeypatch)
    public_kw={k:v for k,v in kw.items()
               if k not in {"next_owner","expected_writer_lease_id",
                            "expected_active_binding_id","reviewed_base_overlap_paths_json"}}
    with pytest.raises(mygithub12.MyGithub12Error) as exc:
        recovery.recover_sessionless_workspace(service,**{**public_kw,"confirm":True})
    assert exc.value.code=="RECOVERY_BASE_OVERLAP"
    assert not state()[1]

def _drifted_revision_handoff_setup(tmp_path, monkeypatch):
    """A #624 Rev1 Workspace, no Session, an external commit, an expired lease."""
    service = seed(tmp_path, monkeypatch, drifted=True)
    with sessions._LOCK, sessions._db() as db:
        db.execute(
            "UPDATE workspaces SET owner=?, lease_expires_at=0 WHERE workspace_id=?",
            ("WORKER-C:#624:1", "ws_recovery"),
        )
    service.client.heads[BASE] = LIVE_SHA
    service.repo.trees[LIVE_SHA] = "5" * 40
    original_compare = service.repo.compare

    def compare(start, end):
        if start == BASE_SHA and end in (LIVE_SHA, NEW_HEAD):
            return SimpleNamespace(
                merge_base_commit=SimpleNamespace(sha=BASE_SHA),
                ahead_by=1,
                behind_by=0,
                files=[SimpleNamespace(filename=OVERLAP, status="modified")],
            )
        return original_compare(start, end)

    service.repo.compare = compare
    request = args(
        expected_owner="WORKER-C:#624:1",
        next_owner="WORKER-C:#624:2",
        expected_writer_lease_id=287,
        expected_active_binding_id=195,
        expected_live_base_sha=LIVE_SHA,
        reviewed_base_overlap_paths_json=json.dumps([OVERLAP]),
        idempotency_key="sessionless-forward-drift-revision-handoff-624",
    )
    return service, request


def test_drifted_revision_handoff_recovers_original_workspace_without_git_write(
    tmp_path, monkeypatch,
):
    from time import time

    service, request = _drifted_revision_handoff_setup(tmp_path, monkeypatch)
    old_state = state()
    preview = recovery.recover_sessionless_workspace(service, **request)
    assert preview["next_owner"] == "WORKER-C:#624:2"
    assert preview["base_advance"]["overlapping_paths"] == [OVERLAP]
    assert preview["required_scope_expansion_paths"] == []
    assert old_state == state()

    result = recovery.recover_sessionless_workspace(
        service, **{**request, "confirm": True}
    )
    ws, sessions_list = state()
    assert result["confirmed"] and result["created_first_session"]
    assert result["recovery_kind"] == "revision_handoff_first_session"
    assert result["git_ref_changed"] is False
    assert ws["revision"] == 8 and ws["owner"] == "WORKER-C:#624:2"
    assert ws["status"] == "active" and ws["drift_reason"] is None
    assert ws["lease_expires_at"] > time()
    assert ws["head_sha"] == NEW_HEAD and ws["tree_sha"] == NEW_TREE
    assert ws["index_commit_sha"] == NEW_HEAD and ws["base_commit_sha"] == BASE_SHA
    assert len(sessions_list) == 1
    assert sessions_list[0]["owner"] == "WORKER-C:#624:2"
    assert sessions_list[0]["head_commit_sha"] == NEW_HEAD
    assert service.client.heads[BRANCH] == NEW_HEAD
    with sessions._db() as db:
        assert db.execute("SELECT count(*) FROM workspaces").fetchone()[0] == 1
    replay = recovery.recover_sessionless_workspace(
        service, **{**request, "confirm": True}
    )
    assert replay["replayed"] and state() == (ws, sessions_list)


@pytest.mark.parametrize("changes,expected_code", [
    ({"expected_previous_head_sha": NEW_HEAD}, "RECOVERY_ANCESTRY_MISMATCH"),
    ({"expected_workspace_revision": 6}, "WORKSPACE_REVISION_MISMATCH"),
    ({"expected_current_head_sha": "e" * 40}, "RECOVERY_IDENTITY_MISMATCH"),
    ({"reviewed_base_overlap_paths_json": "[]"}, "RECOVERY_BASE_OVERLAP"),
    ({"expected_writer_lease_id": 0}, "RECOVERY_OPERATOR_ATTESTATION_REQUIRED"),
    ({"next_owner": "WORKER-C:#624:3"}, "RECOVERY_OWNER_TRANSITION_INVALID"),
])
def test_drifted_revision_handoff_rejects_unreviewed_or_stale_input(
    tmp_path, monkeypatch, changes, expected_code,
):
    service, request = _drifted_revision_handoff_setup(tmp_path, monkeypatch)
    before = state()
    with pytest.raises(mygithub12.MyGithub12Error) as exc:
        recovery.recover_sessionless_workspace(
            service, **{**request, **changes, "confirm": True}
        )
    assert exc.value.code == expected_code
    assert state() == before
