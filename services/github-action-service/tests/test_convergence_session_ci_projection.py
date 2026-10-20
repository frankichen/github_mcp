"""Reconcile current-head full CI without promoting a historical Session revision."""
import pytest

from app import development_converge as converge
from app import development_session_store as sessions


HEAD = "a" * 40
TREE = "b" * 40
BASE = "c" * 40
WORKER = "job-current"
ATTESTATION = "att-current"


def facts():
    session = {
        "session_id": "dev_original", "workspace_id": "ws_original",
        "repository": "owner/repo", "branch": "ai/feature",
        "base_branch": "main", "base_commit_sha": BASE,
        "head_commit_sha": HEAD, "tree_sha": TREE, "status": "active",
        "session_revision": 23, "workspace_revision": 59,
        "last_full_ci_job_id": "job-old", "last_attestation_id": "att-old",
    }
    run = {
        "convergence_id": "conv_original", "repository": "owner/repo",
        "branch": "ai/feature", "development_session_id": "dev_original",
        "workspace_id": "ws_original", "head_sha": HEAD, "tree_sha": TREE,
        "base_branch": "main", "base_sha": BASE, "mode": "full",
        "session_revision": 21, "workspace_revision": 53,
    }
    request = {
        "request_id": "ci_req_original", "worker_job_id": WORKER,
        "repository": "owner/repo", "branch": "ai/feature",
        "commit_sha": HEAD, "tree_sha": TREE, "profile": "repo-auto-check",
        "phase": "terminal", "status": "passed", "attestation_id": ATTESTATION,
    }
    job = {
        "job_id": WORKER, "repository": "owner/repo", "branch": "ai/feature",
        "commit_sha": HEAD, "profile": "repo-auto-check",
        "status": "passed", "exit_code": 0, "summary": {"git_tree_sha": TREE},
    }
    att = {
        "attestation_id": ATTESTATION, "repository": "owner/repo",
        "tested_commit_sha": HEAD, "tested_tree_sha": TREE,
        "base_sha": BASE, "private_ci_job_id": WORKER,
        "profile": "repo-auto-check",
    }
    return session, run, request, job, att


def install(monkeypatch, job, att):
    monkeypatch.setattr(converge, "get_job", lambda job_id: dict(job))
    monkeypatch.setattr(
        converge.attestation_registry, "validate_attestation",
        lambda attestation_id: {"ok": True, "reusable": True, "attestation": dict(att)},
    )


def test_historical_convergence_revision_never_overwrites_current_session(monkeypatch):
    session, run, request, job, att = facts()
    install(monkeypatch, job, att)
    events = []

    def record(session_id, revision, mode, head, tree, **kwargs):
        events.append(("validation", session_id, revision, mode, head, tree, kwargs))
        return 17

    def transition(session_id, revision, status, **kwargs):
        events.append(("session", session_id, revision, status, kwargs))
        return {**session, **kwargs["fields"], "session_revision": revision + 1}

    monkeypatch.setattr(sessions, "record_validation", record)
    monkeypatch.setattr(sessions, "transition", transition)
    result = converge._sync_full_ci_session_evidence(session, run, request, ATTESTATION)
    assert result["session_revision"] == 24
    assert result["last_full_ci_job_id"] == WORKER
    assert result["last_attestation_id"] == ATTESTATION
    assert events[0][:6] == ("validation", "dev_original", 23, "full", HEAD, TREE)
    assert events[0][6]["request_id"] == request["request_id"]
    assert events[1][:4] == ("session", "dev_original", 23, "active")
    assert events[1][4]["event_type"] == "convergence_full_ci_reconciled"
    assert (run["session_revision"], run["workspace_revision"]) == (21, 53)


def test_replay_with_matching_evidence_creates_no_new_state(monkeypatch):
    session, run, request, job, att = facts()
    session["last_full_ci_job_id"] = WORKER
    session["last_attestation_id"] = ATTESTATION
    install(monkeypatch, job, att)
    monkeypatch.setattr(sessions, "record_validation",
                        lambda *args, **kwargs: pytest.fail("already reconciled"))
    monkeypatch.setattr(sessions, "transition",
                        lambda *args, **kwargs: pytest.fail("already reconciled"))
    assert converge._sync_full_ci_session_evidence(
        session, run, request, ATTESTATION
    ) == session


@pytest.mark.parametrize("field", ["tested_tree_sha", "private_ci_job_id", "base_sha"])
def test_attestation_identity_mismatch_stays_fail_closed(monkeypatch, field):
    session, run, request, job, att = facts()
    att[field] = "f" * 40
    install(monkeypatch, job, att)
    monkeypatch.setattr(sessions, "record_validation",
                        lambda *args, **kwargs: pytest.fail("untrusted evidence"))
    with pytest.raises(converge.MyGithub12Error) as error:
        converge._sync_full_ci_session_evidence(session, run, request, ATTESTATION)
    assert error.value.code == "DEVELOPMENT_SESSION_RECOVERY_REQUIRED"


def test_concurrent_same_result_session_cas_is_idempotent(monkeypatch):
    session, run, request, job, att = facts()
    install(monkeypatch, job, att)
    monkeypatch.setattr(sessions, "record_validation", lambda *args, **kwargs: 18)

    def conflict(*args, **kwargs):
        raise converge.MyGithub12Error(
            "DEVELOPMENT_SESSION_REVISION_MISMATCH", "new Session revision"
        )

    monkeypatch.setattr(sessions, "transition", conflict)
    monkeypatch.setattr(
        sessions, "get_session", lambda session_id: {
            **session, "session_revision": 24,
            "last_full_ci_job_id": WORKER, "last_attestation_id": ATTESTATION,
        }
    )
    assert converge._sync_full_ci_session_evidence(
        session, run, request, ATTESTATION
    )["session_revision"] == 24


def test_historical_head_worker_is_not_promoted(monkeypatch):
    session, run, request, job, att = facts()
    job["commit_sha"] = "f" * 40
    install(monkeypatch, job, att)
    monkeypatch.setattr(sessions, "record_validation",
                        lambda *args, **kwargs: pytest.fail("wrong HEAD"))
    with pytest.raises(converge.MyGithub12Error):
        converge._sync_full_ci_session_evidence(session, run, request, ATTESTATION)
