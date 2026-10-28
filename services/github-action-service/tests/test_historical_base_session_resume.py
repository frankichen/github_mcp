"""Isolated regressions for #522/#525; never touches real Writer leases."""
import asyncio
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest

from app import development_drift_recovery as recovery
from app import development_resume as resume
from app import development_session_store as sessions
from app import mygithub12
from app import mygithub12_dx_mcp as dx_mcp
from app.mcp_response import StructuredFastMCP, read_response_resource_text
from tests.test_active_workspace_stale_session_recovery import (
    BASE, BASE_SHA, BRANCH, NEW_HEAD, NEW_TREE, OLD_HEAD, OTHER_HEAD,
    REPO, WORKSPACE_ID, _args, _call, _db_state, _patch_resume, _seed_active_stale,
)

LIVE_BASE = "e" * 40
TASK_MERGE_BASE = "f" * 40


def _advanced(service, monkeypatch, *, overlap=False, rewrite=False, diverged=False, no_merge_base=False):
    service.client.heads[BASE] = LIVE_BASE
    service.repo.trees.update({LIVE_BASE: "5" * 40, TASK_MERGE_BASE: "6" * 40})
    old_compare = service.repo.compare

    def compare(base, head):
        if base == OLD_HEAD and head == NEW_HEAD:
            return old_compare(base, head)
        if base == head:
            return SimpleNamespace(
                merge_base_commit=SimpleNamespace(sha=base), ahead_by=0, behind_by=0, files=[],
            )
        main_paths = ["allowed/feature.py"] if overlap else ["elsewhere/main.py"]
        task_paths = ["allowed/feature.py"]
        if base == BASE_SHA and head == LIVE_BASE:
            return SimpleNamespace(
                merge_base_commit=SimpleNamespace(sha=OTHER_HEAD if rewrite else BASE_SHA),
                ahead_by=2, behind_by=1 if rewrite else 0,
                files=[SimpleNamespace(filename=path, previous_filename=None) for path in main_paths],
            )
        if base == BASE_SHA and head == NEW_HEAD:
            merge_base = "" if no_merge_base else TASK_MERGE_BASE if diverged else BASE_SHA
            return SimpleNamespace(
                merge_base_commit=SimpleNamespace(sha=merge_base),
                ahead_by=2, behind_by=1 if diverged else 0,
                files=[SimpleNamespace(filename=path, previous_filename=None) for path in task_paths],
            )
        if diverged and base == TASK_MERGE_BASE and head in {BASE_SHA, LIVE_BASE, NEW_HEAD}:
            paths = ["pin-only.py"] if head == BASE_SHA else main_paths if head == LIVE_BASE else task_paths
            return SimpleNamespace(
                merge_base_commit=SimpleNamespace(sha=TASK_MERGE_BASE),
                ahead_by={BASE_SHA: 1, LIVE_BASE: 3, NEW_HEAD: 2}[head], behind_by=0,
                files=[SimpleNamespace(filename=path, previous_filename=None) for path in paths],
            )
        return SimpleNamespace(
            merge_base_commit=SimpleNamespace(sha=OTHER_HEAD), ahead_by=0, behind_by=1, files=[],
        )

    monkeypatch.setattr(service.repo, "compare", compare)
    monkeypatch.setattr(resume, "_current_main", lambda *_: {
        "branch": BASE, "repository": REPO, "commit_sha": LIVE_BASE, "tree_sha": "5" * 40,
    })


def test_main_advance_preserves_pinned_base_and_exact_writer_lease(tmp_path, monkeypatch):
    service, session = _seed_active_stale(tmp_path, monkeypatch)
    _advanced(service, monkeypatch)
    before, _, _ = _db_state(session["session_id"])
    result = _call(service, session, expected_base_sha=LIVE_BASE)
    assert result["verification"]["base_advance"]["base_advanced"] is True
    assert result["verification"]["base_advance"]["overlapping_paths"] == []
    assert result["verification"]["base_advance"]["task_merge_base_sha"] == BASE_SHA
    assert result["verification"]["base_advance"]["overlap_blocks_session_adoption"] is False
    assert result["workspace"]["base_commit_sha"] == result["development_session"]["base_commit_sha"] == BASE_SHA
    assert result["workspace"]["lease_expires_at"] == before["lease_expires_at"]
    assert result["development_session"]["lease_expires_at"] == before["lease_expires_at"]
    assert result["development_session"]["session_id"] == session["session_id"]
    assert result["workspace"]["revision"] == before["revision"]


def test_diverged_pinned_base_and_task_branch_resume_with_overlap_as_audit_only(tmp_path, monkeypatch):
    service, session = _seed_active_stale(tmp_path, monkeypatch)
    _advanced(service, monkeypatch, overlap=True, diverged=True)
    refs_before = dict(service.client.heads)

    result = _call(service, session, expected_base_sha=LIVE_BASE)

    evidence = result["verification"]["base_advance"]
    assert evidence["task_merge_base_sha"] == TASK_MERGE_BASE
    assert evidence["merge_base_to_pinned"]["verified"] is True
    assert evidence["merge_base_to_task_head"]["verified"] is True
    assert evidence["merge_base_to_live_base"]["verified"] is True
    assert evidence["overlapping_paths"] == ["allowed/feature.py"]
    assert evidence["overlap_blocks_session_adoption"] is False
    assert result["development_session"]["head_commit_sha"] == NEW_HEAD
    assert result["git_ref_changed"] is False
    assert service.client.heads == refs_before
    assert result["workspace"]["base_commit_sha"] == BASE_SHA


@pytest.mark.parametrize("rewrite,no_merge_base", [(True, False), (False, True)])
def test_unverifiable_base_lineage_or_merge_base_refuses_without_mutation(
    tmp_path, monkeypatch, rewrite, no_merge_base,
):
    service, session = _seed_active_stale(tmp_path, monkeypatch)
    _advanced(service, monkeypatch, rewrite=rewrite, no_merge_base=no_merge_base)
    before = _db_state(session["session_id"])
    with pytest.raises(mygithub12.MyGithub12Error) as exc:
        _call(service, session, expected_base_sha=LIVE_BASE)
    assert exc.value.code == "RECOVERY_ANCESTRY_MISMATCH"
    assert _db_state(session["session_id"]) == before


def test_resume_returns_executable_scope_review_then_original_cas_retry(tmp_path, monkeypatch):
    service, session = _seed_active_stale(tmp_path, monkeypatch)
    _patch_resume(service, monkeypatch)
    _advanced(service, monkeypatch)
    with sessions._db() as db:
        db.execute("UPDATE workspaces SET scope_json='{}' WHERE workspace_id=?", (WORKSPACE_ID,))
    args = dict(repository=REPO, branch=BRANCH, expected_workspace_revision=5,
                expected_session_revision=session["session_revision"], idempotency_key="resume-after-main")
    refused = resume.resume_task(service, **args)
    assert refused["recovery_blocker"] == "RECOVERY_SCOPE_VIOLATION"
    assert refused["next_allowed_actions"] == ["review_scope_expansion", "resume_development_task"]
    reviewed = refused["recovery"]["retry_arguments"]
    assert json.loads(reviewed["reviewed_scope_expansion_paths_json"]) == ["allowed/feature.py"]
    first = resume.resume_task(service, **reviewed)
    second = resume.resume_task(service, **reviewed)
    assert first["recovery_performed"] is True
    assert second["recovery_performed"] is False
    assert first["exact_head"] is second["exact_head"] is True
    assert second["workspace"]["revision"] == 6
    assert second["development_session"]["session_revision"] == first["development_session"]["session_revision"]


@pytest.mark.parametrize("mutation,code", [
    ("tree", "RECOVERY_TREE_MISMATCH"), ("owner", "RECOVERY_IDENTITY_MISMATCH"),
    ("lease", "ACTIVE_WORKSPACE_RECOVERY_STATE_INVALID"),
])
def test_identity_lease_guards(tmp_path, monkeypatch, mutation, code):
    service, session = _seed_active_stale(tmp_path, monkeypatch)
    if mutation == "tree":
        service.repo.trees[NEW_HEAD] = "9" * 40
    else:
        with sessions._db() as db:
            if mutation == "owner":
                db.execute("UPDATE workspaces SET owner='different-writer' WHERE workspace_id=?", (WORKSPACE_ID,))
            else:
                db.execute("UPDATE workspaces SET lease_expires_at=1 WHERE workspace_id=?", (WORKSPACE_ID,))
    before = _db_state(session["session_id"])
    with pytest.raises(mygithub12.MyGithub12Error) as exc:
        _call(service, session)
    assert exc.value.code == code
    assert _db_state(session["session_id"]) == before


@pytest.mark.parametrize("same_key", [True, False])
def test_simultaneous_recovery_writes_once_and_reconciles(tmp_path, monkeypatch, same_key):
    service, session = _seed_active_stale(tmp_path, monkeypatch)
    barrier = Barrier(2)
    original = recovery._atomic_recover

    def concurrent(*args, **kwargs):
        barrier.wait(timeout=10)
        return original(*args, **kwargs)

    monkeypatch.setattr(recovery, "_atomic_recover", concurrent)
    def call(n):
        return _call(service, session, idempotency_key="same" if same_key else f"key-{n}")
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(call, [1, 2]))
    assert sum(not r["replayed"] for r in results) == 1
    assert results[0]["after"] == results[1]["after"]
    _, adopted, events = _db_state(session["session_id"])
    assert adopted["session_revision"] == session["session_revision"] + 1
    assert sum(e["event_type"] == "active_workspace_stale_session_recovery" for e in events) == 1


def test_unrelated_session_cas_winner_is_not_overwritten(tmp_path, monkeypatch):
    service, session = _seed_active_stale(tmp_path, monkeypatch)
    original = recovery._atomic_recover
    def race(*args, **kwargs):
        with sessions._db() as db:
            db.execute("UPDATE development_sessions SET session_revision=session_revision+1 WHERE session_id=?", (session["session_id"],))
        return original(*args, **kwargs)
    monkeypatch.setattr(recovery, "_atomic_recover", race)
    with pytest.raises(mygithub12.MyGithub12Error) as exc:
        _call(service, session)
    assert exc.value.code == "DEVELOPMENT_SESSION_REVISION_MISMATCH"
    assert sessions.get_session(session["session_id"])["head_commit_sha"] == OLD_HEAD


def test_stale_index_is_not_writer_ready_and_old_ci_is_cleared(tmp_path, monkeypatch):
    service, session = _seed_active_stale(tmp_path, monkeypatch)
    monkeypatch.setattr(mygithub12, "get_index_status", lambda *a, **k: {
        "status": "ready", "commit_sha": NEW_HEAD, "tree_sha": "9" * 40,
    })
    monkeypatch.setattr(mygithub12, "request_index_build", lambda *a, **k: {"status": "queued"})
    result = _call(service, session)
    assert result["writer_ready"] is False and result["index_required"] is True
    assert result["development_session"]["index_commit_sha"] is None
    assert result["development_session"]["last_full_ci_job_id"] is None
    assert result["development_session"]["last_attestation_id"] is None


def test_payload_conflict_and_lease_change_cannot_replay(tmp_path, monkeypatch):
    service, session = _seed_active_stale(tmp_path, monkeypatch)
    _call(service, session)
    with pytest.raises(mygithub12.MyGithub12Error) as exc:
        _call(service, session, lease_seconds=3600)
    assert exc.value.code == "IDEMPOTENCY_CONFLICT"
    with sessions._db() as db:
        db.execute("UPDATE workspaces SET lease_expires_at=lease_expires_at+1 WHERE workspace_id=?", (WORKSPACE_ID,))
    with pytest.raises(mygithub12.MyGithub12Error) as exc:
        _call(service, session)
    assert exc.value.code == "IDEMPOTENCY_CONFLICT"


def test_revision_only_case_c_recovers_then_replays_original_cas(tmp_path, monkeypatch):
    service, session = _seed_active_stale(tmp_path, monkeypatch)
    _patch_resume(service, monkeypatch)
    _advanced(service, monkeypatch)
    with sessions._db() as db:
        db.execute("UPDATE development_sessions SET head_commit_sha=?,tree_sha=? WHERE session_id=?",
                   (NEW_HEAD, NEW_TREE, session["session_id"]))
    args = dict(repository=REPO, branch=BRANCH, expected_workspace_revision=5,
                expected_session_revision=session["session_revision"], idempotency_key="case-c-lease")
    first = resume.resume_task(service, **args)
    second = resume.resume_task(service, **args)
    assert first["exact_head"] is second["exact_head"] is True
    assert first["development_session"]["session_revision"] == second["development_session"]["session_revision"]
    assert second["recovery_performed"] is False


def test_resume_wrong_tree_index_cannot_continue_write(tmp_path, monkeypatch):
    service, session = _seed_active_stale(tmp_path, monkeypatch)
    _patch_resume(service, monkeypatch)
    monkeypatch.setattr(mygithub12, "get_index_status", lambda *a, **k: {
        "status": "ready", "commit_sha": NEW_HEAD, "tree_sha": "9" * 40,
    })
    monkeypatch.setattr(mygithub12, "request_index_build", lambda *a, **k: {"status": "queued"})
    result = resume.resume_task(service, REPO, branch=BRANCH, expected_workspace_revision=5,
                                expected_session_revision=session["session_revision"], idempotency_key="bad-index")
    assert "INDEX_IDENTITY_MISMATCH" in result["blockers"]
    assert "continue_write" not in result["next_allowed_actions"]


def test_different_writer_during_verification_cannot_win_cas(tmp_path, monkeypatch):
    service, session = _seed_active_stale(tmp_path, monkeypatch)
    original = recovery._atomic_recover
    def race(*args, **kwargs):
        with sessions._db() as db:
            db.execute("UPDATE workspaces SET owner='other-writer',revision=revision+1 WHERE workspace_id=?", (WORKSPACE_ID,))
        return original(*args, **kwargs)
    monkeypatch.setattr(recovery, "_atomic_recover", race)
    with pytest.raises(mygithub12.MyGithub12Error) as exc:
        _call(service, session)
    assert exc.value.code == "WORKSPACE_REVISION_MISMATCH"
    assert sessions.get_session(session["session_id"])["head_commit_sha"] == OLD_HEAD


@pytest.mark.parametrize("mutation", ["revision", "owner", "head_sha", "lease_expires_at"])
def test_revision_only_recovery_rechecks_persistent_workspace(tmp_path, monkeypatch, mutation):
    service, session = _seed_active_stale(tmp_path, monkeypatch)
    verified_snapshot = mygithub12.get_workspace(service, WORKSPACE_ID)
    with sessions._db() as db:
        if mutation in {"owner", "head_sha"}:
            db.execute(f"UPDATE workspaces SET {mutation}=? WHERE workspace_id=?", ("changed", WORKSPACE_ID))
        else:
            db.execute(f"UPDATE workspaces SET {mutation}={mutation}+1 WHERE workspace_id=?", (WORKSPACE_ID,))
    with pytest.raises(mygithub12.MyGithub12Error) as exc:
        sessions.recover_stale_session_from_workspace(session["session_id"], session["session_revision"], verified_snapshot)
    assert exc.value.code == "WORKSPACE_REVISION_MISMATCH"
    assert sessions.get_session(session["session_id"])["head_commit_sha"] == OLD_HEAD


def test_overlap_inputs_changed_after_verification_fail_closed(tmp_path, monkeypatch):
    service, session = _seed_active_stale(tmp_path, monkeypatch)
    original = recovery._atomic_recover
    def race(*args, **kwargs):
        with sessions._db() as db:
            db.execute("UPDATE workspaces SET scope_json=? WHERE workspace_id=?",
                       ('{"paths":["new-scope/**"]}', WORKSPACE_ID))
        return original(*args, **kwargs)
    monkeypatch.setattr(recovery, "_atomic_recover", race)
    with pytest.raises(mygithub12.MyGithub12Error) as exc:
        _call(service, session)
    assert exc.value.code == "WORKSPACE_REVISION_MISMATCH"
    assert sessions.get_session(session["session_id"])["head_commit_sha"] == OLD_HEAD


def test_process_restart_replays_persisted_result(tmp_path, monkeypatch):
    service, session = _seed_active_stale(tmp_path, monkeypatch)
    first = _call(service, session)
    # A new interpreter/connection must use durable metadata, not process locks
    # or a cached success response. All paths are fixture-owned temp resources.
    script = '''
import json, sys
from app import mygithub12
import tests.test_active_workspace_stale_session_recovery as t
mygithub12.workspace_overlap = lambda *a, **k: {"ok": True, "items": []}
mygithub12.get_index_status = lambda *a, **k: {"status": "ready", "commit_sha": t.NEW_HEAD, "tree_sha": t.NEW_TREE}
r = t.recovery.recover_active_workspace_stale_session(t.FakeService(), **json.loads(sys.argv[1]))
print(json.dumps({"replayed": r["replayed"], "after": r["after"]}))
'''
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(Path.cwd()), str(Path(__file__).parent)])}
    output = subprocess.check_output([sys.executable, "-c", script, json.dumps(_args(session))], env=env, text=True, timeout=30)
    second = json.loads(output)
    assert second["replayed"] is True and second["after"] == first["after"]


def test_real_mcp_dispatch_preserves_resume_contract(tmp_path, monkeypatch):
    # The MCP response-resource fallback must stay inside the test sandbox.
    monkeypatch.setenv("MYGITHUB12_SHARED_RESOURCE_DIR", str(tmp_path / "response-resources"))
    service, session = _seed_active_stale(tmp_path, monkeypatch)
    _patch_resume(service, monkeypatch)
    _advanced(service, monkeypatch)
    mcp = StructuredFastMCP("historical-base-resume")
    async def github_call(fn, *args, **kwargs):
        return fn(*args, **kwargs)
    async def finalize(*args, **kwargs):
        return {}
    dx_mcp.register_dx_tools(mcp, github_call, service, finalize)
    args = dict(repository=REPO, branch=BRANCH, expected_workspace_revision=5,
                expected_session_revision=session["session_revision"], idempotency_key="mcp-resume")
    async def call():
        called = await mcp.call_tool("resume_development_task", args)
        payload = called[1] if isinstance(called, tuple) else called.structured_content
        if (payload.get("response_meta") or {}).get("resource_uri"):
            payload = json.loads(read_response_resource_text(payload["response_meta"]["resource_uri"]))
        return payload
    result = asyncio.run(call())
    repeated = asyncio.run(call())
    assert result["exact_head"] is True
    assert result["recovery_performed"] is True
    assert repeated["exact_head"] is True and repeated["recovery_performed"] is False
