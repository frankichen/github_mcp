import inspect

import pytest

from app import development_converge as converge
from app import development_orchestrator as dx
from app import development_session_store as sessions
from app import mygithub12


SHA_A = "a" * 40
SHA_B = "b" * 40
TREE_B = "c" * 40


def _session(**overrides):
    value = {
        "session_id": "dev_converge",
        "workspace_id": "ws_converge",
        "repository": "owner/repo",
        "branch": "ai/converge",
        "base_branch": "main",
        "base_commit_sha": SHA_A,
        "head_commit_sha": SHA_B,
        "tree_sha": TREE_B,
        "session_revision": 7,
        "workspace_revision": 4,
        "status": "active",
        "pull_number": None,
        "metadata": {"task_name": "converge tests"},
    }
    value.update(overrides)
    return value


async def _direct_call(fn, *args, **kwargs):
    result = fn(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


def test_convergence_analysis_requests_current_head_index_and_collects_all_stages(monkeypatch):
    session = _session()
    status_calls = []
    request_calls = []

    monkeypatch.setattr(
        mygithub12,
        "resolve_identity",
        lambda service, repository, commit_sha="": {
            "repository": repository,
            "commit_sha": commit_sha,
            "tree_sha": TREE_B,
        },
    )

    def index_status(service, repository, commit_sha="", ref=""):
        status_calls.append(commit_sha)
        if len(status_calls) == 1:
            return {"status": "missing", "commit_sha": commit_sha, "tree_sha": TREE_B}
        return {
            "status": "ready",
            "commit_sha": commit_sha,
            "tree_sha": TREE_B,
            "index_version": "12.0.0-1",
        }

    monkeypatch.setattr(mygithub12, "get_index_status", index_status)

    def request_index(service, repository, commit_sha, strategy, base_commit_sha, priority, idempotency_key, force):
        request_calls.append((repository, commit_sha, base_commit_sha, strategy, priority, force))
        return {"job_id": "idx-1", "status": "running", "revision": 2, "step": "snapshot"}

    monkeypatch.setattr(mygithub12, "request_index_build", request_index)
    monkeypatch.setattr(mygithub12, "wait_index_job", lambda *args, **kwargs: {"status": "completed"})
    monkeypatch.setattr(
        mygithub12,
        "change_context_pack",
        lambda *args, **kwargs: {"ok": True, "items": [{"path": "app.py"}], "omitted_count": 0},
    )
    monkeypatch.setattr(
        mygithub12,
        "change_impact",
        lambda *args, **kwargs: {
            "ok": True,
            "complete": True,
            "changed_paths": ["app.py"],
            "affected_modules": ["app"],
            "affected_tests": ["test_app.py"],
            "contract_changes": [],
        },
    )
    monkeypatch.setattr(
        mygithub12,
        "contract_changes",
        lambda *args, **kwargs: {"ok": True, "summary": {"breaking": 0}, "changes": []},
    )
    monkeypatch.setattr(
        mygithub12,
        "affected_tests",
        lambda *args, **kwargs: {"ok": True, "authoritative": False, "tests": [{"path": "test_app.py"}]},
    )
    monkeypatch.setattr(
        converge,
        "store_response_resource",
        lambda value: {"resource_uri": "mygithub12://response/test", "total_bytes": 123, "sha256": "d" * 64},
    )

    result = converge.convergence_analysis(object(), session, index_wait_seconds=10, idempotency_key="conv-1")

    assert status_calls == [SHA_B, SHA_B]
    assert request_calls == [("owner/repo", SHA_B, SHA_A, "auto", "interactive", False)]
    assert result["index"]["ready"] is True
    assert result["index"]["commit_sha"] == SHA_B
    assert result["impact"]["complete"] is True
    assert result["contracts"]["summary"] == {"breaking": 0}
    assert result["affected_tests"]["tests"] == [{"path": "test_app.py"}]
    assert result["degraded"] is False
    assert result["analysis_resource"]["resource_uri"] == "mygithub12://response/test"


def test_convergence_analysis_marks_analysis_failure_degraded(monkeypatch):
    session = _session()
    monkeypatch.setattr(
        mygithub12,
        "resolve_identity",
        lambda *args, **kwargs: {"commit_sha": SHA_B, "tree_sha": TREE_B},
    )
    monkeypatch.setattr(
        mygithub12,
        "get_index_status",
        lambda *args, **kwargs: {
            "status": "ready",
            "commit_sha": SHA_B,
            "tree_sha": TREE_B,
            "index_version": "12.0.0-1",
        },
    )
    monkeypatch.setattr(mygithub12, "change_context_pack", lambda *args, **kwargs: {"ok": True, "items": []})
    monkeypatch.setattr(mygithub12, "change_impact", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("impact down")))
    monkeypatch.setattr(mygithub12, "contract_changes", lambda *args, **kwargs: {"ok": True, "summary": {}, "changes": []})
    monkeypatch.setattr(mygithub12, "affected_tests", lambda *args, **kwargs: {"ok": True, "tests": []})
    monkeypatch.setattr(
        converge,
        "store_response_resource",
        lambda value: {"resource_uri": "mygithub12://response/test", "total_bytes": 1, "sha256": "e" * 64},
    )

    result = converge.convergence_analysis(object(), session)

    assert result["degraded"] is True
    assert result["conservative_ci_required"] is True
    assert any(item["stage"] == "impact" for item in result["degraded_reasons"])


def _install_task_mocks(monkeypatch, *, analysis, validation_result):
    session = _session()
    monkeypatch.setattr(sessions, "_require_revision", lambda session_id, revision: session)
    monkeypatch.setattr(sessions, "get_session", lambda session_id: session)
    monkeypatch.setattr(
        dx,
        "maybe_auto_renew_session_workspace",
        lambda *args, **kwargs: {
            "renewed": False,
            "session": session,
            "workspace": {"revision": 4},
            "remaining_seconds": 3600.0,
            "audit": None,
            "recovery": None,
        },
    )
    monkeypatch.setattr(mygithub12, "workspace_write_preflight", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(converge, "convergence_analysis", lambda *args, **kwargs: analysis)
    monkeypatch.setattr(
        dx,
        "validation_preflight",
        lambda *args, **kwargs: {"profile": "repo-auto-check", "selection": {"complete": False}},
    )
    revisions = []

    def transition(session_id, expected_revision, status, **kwargs):
        revisions.append((expected_revision, status, kwargs.get("event_type")))
        return {**session, "status": status, "session_revision": expected_revision + 1}

    monkeypatch.setattr(sessions, "transition", transition)
    started = []
    job = {
        "job_id": "job-1",
        "status": validation_result["job"]["status"],
        "profile": "repo-auto-check",
        "commit_sha": SHA_B,
        "worker_id": "wsl-ci-01",
    }

    def start_validation(service, phase_session, mode, base_sha, force_rerun, supersede_previous, prepared):
        started.append((mode, prepared["profile"], base_sha))
        return job, prepared["selection"]

    monkeypatch.setattr(dx, "start_validation_job", start_validation)
    monkeypatch.setattr(dx, "wait_validation", lambda job_id, wait_seconds: job)
    monkeypatch.setattr(dx, "validation_result", lambda *args, **kwargs: validation_result)
    monkeypatch.setattr(
        converge,
        "wait_worker_final_state",
        lambda job_id, wait_seconds=5: {
            "worker_id": "wsl-ci-01",
            "released": True,
            "idle": True,
            "status": "idle",
            "current_job": None,
        },
    )
    return session, started, revisions


@pytest.mark.asyncio
async def test_full_convergence_runs_repo_auto_check_even_when_analysis_is_degraded(monkeypatch):
    analysis = {
        "identity": {"repository": "owner/repo", "commit_sha": SHA_B, "tree_sha": TREE_B},
        "index": {"ready": True},
        "degraded": True,
        "degraded_reasons": [{"stage": "impact", "code": "IMPACT_ANALYSIS_INCOMPLETE"}],
    }
    validation = {
        "job": {"job_id": "job-1", "status": "passed", "profile": "repo-auto-check", "commit_sha": SHA_B},
        "merge_eligible": True,
        "attestation": {"attestation_id": "att-1"},
        "failure_pack": None,
        "terminal": True,
    }
    _, started, _ = _install_task_mocks(monkeypatch, analysis=analysis, validation_result=validation)

    result = await converge.converge_task(_direct_call, object(), "dev_converge", 7, mode="full")

    assert started == [("full", "repo-auto-check", SHA_A)]
    assert result["validation"]["job"]["status"] == "passed"
    assert result["converged"] is False
    assert result["next_allowed_actions"] == ["inspect_convergence_resource", "converge_development_task"]
    assert result["safety"] == {
        "merge_performed": False,
        "deploy_performed": False,
        "rollback_performed": False,
        "branch_moved": False,
    }


@pytest.mark.asyncio
async def test_convergence_failure_returns_failure_pack_and_never_claims_success(monkeypatch):
    analysis = {
        "identity": {"repository": "owner/repo", "commit_sha": SHA_B, "tree_sha": TREE_B},
        "index": {"ready": True},
        "degraded": False,
        "degraded_reasons": [],
    }
    validation = {
        "job": {"job_id": "job-1", "status": "failed", "profile": "repo-auto-check", "commit_sha": SHA_B},
        "merge_eligible": False,
        "attestation": None,
        "failure_pack": {"resource_uri": "mygithub12://response/failure"},
        "terminal": True,
    }
    _install_task_mocks(monkeypatch, analysis=analysis, validation_result=validation)

    result = await converge.converge_task(_direct_call, object(), "dev_converge", 7, mode="full")

    assert result["converged"] is False
    assert result["validation"]["failure_pack"]["resource_uri"] == "mygithub12://response/failure"
    assert result["next_allowed_actions"] == ["inspect_failure_pack"]
    assert result["merge_eligibility"]["ci_merge_eligible"] is False


@pytest.mark.asyncio
async def test_convergence_rejects_stale_session_revision_before_other_work(monkeypatch):
    def reject(session_id, revision):
        raise mygithub12.MyGithub12Error(
            "DEVELOPMENT_SESSION_REVISION_MISMATCH",
            "stale session revision",
        )

    monkeypatch.setattr(sessions, "_require_revision", reject)
    monkeypatch.setattr(
        sessions,
        "get_session",
        lambda session_id: pytest.fail("stale revision must fail before session orchestration"),
    )

    with pytest.raises(mygithub12.MyGithub12Error) as exc:
        await converge.converge_task(_direct_call, object(), "dev_converge", 6, mode="full")

    assert exc.value.code == "DEVELOPMENT_SESSION_REVISION_MISMATCH"


def test_wait_worker_final_state_reports_terminal_idle_release(monkeypatch):
    monkeypatch.setattr(
        converge,
        "get_job",
        lambda job_id: {"job_id": job_id, "status": "passed", "worker_id": "wsl-ci-01"},
    )
    monkeypatch.setattr(converge, "reconcile_stale_workers", lambda: 0)
    monkeypatch.setattr(
        converge,
        "get_workers",
        lambda: [
            {
                "worker_id": "wsl-ci-01",
                "online": True,
                "status": "idle",
                "current_job": None,
                "max_concurrent": 1,
            }
        ],
    )

    result = converge.wait_worker_final_state("job-1", wait_seconds=0)

    assert result["released"] is True
    assert result["idle"] is True
    assert result["current_job"] is None
