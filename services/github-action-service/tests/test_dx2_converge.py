import importlib
import inspect
from types import SimpleNamespace

import pytest

from app import ci_database
from app import ci_request_store
from app import development_convergence_store
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


async def _github_call(fn, *args, **kwargs):
    result = fn(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


@pytest.fixture(autouse=True)
def isolated_convergence_databases(monkeypatch, tmp_path):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "mygithub12.db"))
    monkeypatch.setattr(ci_database, "DB_PATH", str(tmp_path / "ci.db"))
    connection = getattr(ci_database._local, "db", None)
    if connection is not None:
        connection.close()
    ci_database._local.db = None
    ci_database.init_db()
    development_convergence_store.init_convergence_db()
    monkeypatch.setattr(
        converge, "schedule_ci_request_preparation", lambda _request_id: True
    )


def _install_identity_mocks(monkeypatch, session):
    monkeypatch.setattr(sessions, "_require_revision", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sessions, "get_session", lambda _session_id: dict(session))
    monkeypatch.setattr(
        mygithub12,
        "resolve_identity",
        lambda _service, repository, commit_sha="", ref="": {
            "repository": repository,
            "commit_sha": commit_sha or SHA_B,
            "tree_sha": TREE_B,
        },
    )
    monkeypatch.setattr(
        mygithub12,
        "workspace_write_preflight",
        lambda *_args, **_kwargs: {
            "ok": True,
            "workspace_id": session["workspace_id"],
            "repository": session["repository"],
            "branch": session["branch"],
            "head_sha": session["head_commit_sha"],
            "tree_sha": session["tree_sha"],
        },
    )
    monkeypatch.setattr(
        converge, "effective_ci_config_digest", lambda _repo: "d" * 64
    )
    monkeypatch.setattr(converge, "get_max_timeout", lambda _repo: 900)


def _install_analysis_mocks(monkeypatch, *, impact=None, calls=None):
    calls = calls if calls is not None else []
    monkeypatch.setattr(
        mygithub12,
        "change_context_pack",
        lambda *_args, **_kwargs: (
            calls.append("change_context")
            or {"ok": True, "items": [{"path": "app.py"}], "omitted_count": 0}
        ),
    )
    monkeypatch.setattr(
        mygithub12,
        "change_impact",
        lambda *_args, **_kwargs: (
            calls.append("change_impact")
            or (
                impact
                if impact is not None
                else {
                    "ok": True,
                    "complete": True,
                    "changed_paths": ["app.py"],
                    "affected_modules": ["app"],
                    "affected_tests": ["test_app.py"],
                    "contract_changes": [],
                }
            )
        ),
    )
    monkeypatch.setattr(
        mygithub12,
        "contract_changes",
        lambda *_args, **_kwargs: (
            calls.append("contract_detection")
            or {"ok": True, "summary": {"breaking": 0}, "changes": []}
        ),
    )
    monkeypatch.setattr(
        mygithub12,
        "affected_tests",
        lambda *_args, **_kwargs: (
            calls.append("affected_tests")
            or {
                "ok": True,
                "authoritative": False,
                "tests": [{"path": "test_app.py"}],
            }
        ),
    )
    monkeypatch.setattr(
        converge,
        "store_response_resource",
        lambda _value: {
            "resource_uri": "mygithub12://response/convergence",
            "total_bytes": 123,
            "sha256": "e" * 64,
        },
    )
    return calls


def _install_index_mocks(monkeypatch, statuses, *, request_calls=None):
    request_calls = request_calls if request_calls is not None else []
    reads = []

    def index_status(_service, _repository, commit_sha="", ref=""):
        del ref
        reads.append(commit_sha)
        current = statuses[0] if len(statuses) == 1 else statuses.pop(0)
        return {"repository": "owner/repo", "commit_sha": commit_sha, **current}

    monkeypatch.setattr(mygithub12, "get_index_status", index_status)
    monkeypatch.setattr(
        mygithub12,
        "request_index_build",
        lambda _service, repository, commit_sha, strategy, base_sha, priority, key, force: (
            request_calls.append(
                {
                    "repository": repository,
                    "commit_sha": commit_sha,
                    "strategy": strategy,
                    "base_sha": base_sha,
                    "priority": priority,
                    "idempotency_key": key,
                    "force": force,
                }
            )
            or {
                "job_id": "idx-1",
                "status": "running",
                "revision": 2,
                "step": "snapshot",
                "commit_sha": commit_sha,
                "tree_sha": TREE_B,
            }
        ),
    )
    return reads


def _ci_request(response):
    return ci_request_store.get_ci_request(response["ci_request"]["request_id"])


def _queue_ci_request(request):
    job = ci_database.create_or_get_job(
        repository=request["repository"],
        branch=request["branch"],
        commit_sha=request["commit_sha"],
        profile=request["profile"],
        priority=100,
        timeout_seconds=900,
        force_rerun=True,
        supersede_previous=False,
        base_sha=SHA_A,
    )
    return ci_request_store.transition_ci_request(
        request["request_id"],
        request["revision"],
        "queued",
        "queued",
        worker_job_id=job["job_id"],
    )


def test_convergence_analysis_returns_index_pending_without_wait(monkeypatch):
    session = _session()
    calls = []
    monkeypatch.setattr(
        mygithub12,
        "resolve_identity",
        lambda _service, repository, commit_sha="": {
            "repository": repository,
            "commit_sha": commit_sha,
            "tree_sha": TREE_B,
        },
    )
    monkeypatch.setattr(
        mygithub12,
        "get_index_status",
        lambda *_args, **_kwargs: {
            "status": "running",
            "commit_sha": SHA_B,
            "tree_sha": TREE_B,
        },
    )
    monkeypatch.setattr(
        mygithub12,
        "request_index_build",
        lambda *_args, **_kwargs: calls.append("request")
        or {
            "job_id": "idx-1",
            "status": "running",
            "commit_sha": SHA_B,
            "tree_sha": TREE_B,
        },
    )
    monkeypatch.setattr(
        mygithub12,
        "wait_index_job",
        lambda *_args, **_kwargs: pytest.fail("Index wait must not be called"),
    )
    for name in (
        "change_context_pack",
        "change_impact",
        "contract_changes",
        "affected_tests",
    ):
        monkeypatch.setattr(
            mygithub12,
            name,
            lambda *_args, _name=name, **_kwargs: pytest.fail(
                f"analysis stage {_name} must wait for exact Index readiness"
            ),
        )

    result = converge.convergence_analysis(
        object(), session, index_wait_seconds=55, idempotency_key="window-a"
    )

    assert calls == ["request"]
    assert result["index"]["ready"] is False
    assert result["pending"] is True
    assert result["pending_reasons"][0]["code"] == "INDEX_NOT_READY"
    assert result["conservative_ci_required"] is True


def test_convergence_analysis_runs_all_tracks_only_after_exact_index(monkeypatch):
    session = _session()
    _install_identity_mocks(monkeypatch, session)
    reads = _install_index_mocks(
        monkeypatch,
        [{"status": "ready", "tree_sha": TREE_B, "index_version": "12.0.0-1"}],
    )
    calls = _install_analysis_mocks(monkeypatch)

    result = converge.convergence_analysis(object(), session, index_wait_seconds=55)

    assert reads == [SHA_B]
    assert calls == [
        "change_context",
        "change_impact",
        "contract_detection",
        "affected_tests",
    ]
    assert result["identity"]["repository"] == "owner/repo"
    assert result["index"]["ready"] is True
    assert result["impact"]["complete"] is True
    assert result["contracts"]["summary"] == {"breaking": 0}
    assert result["affected_tests"]["tests"] == [{"path": "test_app.py"}]
    assert result["degraded"] is False


def test_convergence_analysis_reports_truthful_degraded_stage(monkeypatch):
    session = _session()
    _install_identity_mocks(monkeypatch, session)
    _install_index_mocks(
        monkeypatch,
        [{"status": "ready", "tree_sha": TREE_B, "index_version": "12.0.0-1"}],
    )
    calls = _install_analysis_mocks(
        monkeypatch,
        impact={"ok": True, "complete": False, "changed_paths": []},
    )

    result = converge.convergence_analysis(object(), session)

    assert calls == [
        "change_context",
        "change_impact",
        "contract_detection",
        "affected_tests",
    ]
    assert result["degraded"] is True
    assert result["conservative_ci_required"] is True
    assert any(
        reason["stage"] == "change_impact" for reason in result["degraded_reasons"]
    )


@pytest.mark.asyncio
async def test_first_call_creates_one_convergence_and_returns_fast(monkeypatch):
    session = _session()
    _install_identity_mocks(monkeypatch, session)
    _install_index_mocks(monkeypatch, [{"status": "running", "tree_sha": TREE_B}])
    _install_analysis_mocks(monkeypatch)
    forbidden = lambda *_args, **_kwargs: pytest.fail("legacy wait path was called")
    monkeypatch.setattr(mygithub12, "wait_index_job", forbidden)
    monkeypatch.setattr(dx, "wait_validation", forbidden)
    monkeypatch.setattr(ci_database, "wait_for_job_change", forbidden)

    result = await converge.converge_task(
        _github_call,
        SimpleNamespace(),
        session["session_id"],
        session["session_revision"],
        mode="full",
        index_wait_seconds=55,
        wait_seconds=55,
        idempotency_key="window-a",
    )

    assert result["ok"] is True
    assert result["convergence_id"].startswith("conv_")
    assert result["phase"] == "ci_requested"
    assert result["terminal"] is False
    assert result["continuation_required"] is True
    assert result["index"]["ready"] is False
    assert result["ci_request"]["status"] == "accepted"
    assert result["ci_request"]["request_id"]
    assert result["convergence"]["index_job_id"] == "idx-1"


@pytest.mark.asyncio
async def test_repeated_call_reuses_convergence_and_ci_request(monkeypatch):
    session = _session()
    _install_identity_mocks(monkeypatch, session)
    request_calls = []
    _install_index_mocks(
        monkeypatch,
        [{"status": "running", "tree_sha": TREE_B}],
        request_calls=request_calls,
    )
    _install_analysis_mocks(monkeypatch)
    create_calls = []
    original_create = ci_request_store.create_or_get_ci_request

    def counted_create(**kwargs):
        create_calls.append(kwargs["idempotency_key"])
        return original_create(**kwargs)

    monkeypatch.setattr(ci_request_store, "create_or_get_ci_request", counted_create)
    first = await converge.converge_task(
        _github_call,
        object(),
        session["session_id"],
        session["session_revision"],
        idempotency_key="window-a",
    )
    second = await converge.converge_task(
        _github_call,
        object(),
        session["session_id"],
        session["session_revision"],
        idempotency_key="window-a",
    )

    assert second["convergence_id"] == first["convergence_id"]
    assert second["ci_request"]["request_id"] == first["ci_request"]["request_id"]
    assert second["convergence"]["revision"] >= first["convergence"]["revision"]
    assert request_calls and len(request_calls) == 1
    assert create_calls == [f"convergence:{first['convergence_id']}"]


@pytest.mark.asyncio
async def test_ci_request_preparing_queued_and_running_are_snapshots(monkeypatch):
    session = _session()
    _install_identity_mocks(monkeypatch, session)
    _install_index_mocks(monkeypatch, [{"status": "running", "tree_sha": TREE_B}])
    _install_analysis_mocks(monkeypatch)
    first = await converge.converge_task(
        _github_call,
        object(),
        session["session_id"],
        session["session_revision"],
    )
    request = _ci_request(first)

    preparing = ci_request_store.transition_ci_request(
        request["request_id"], request["revision"], "preparing", "preparing"
    )
    observed_preparing = await converge.converge_task(
        _github_call,
        object(),
        session["session_id"],
        session["session_revision"],
        idempotency_key="window-a",
    )
    assert observed_preparing["ci_request"]["phase"] == "preparing"
    assert observed_preparing["ci_status"] == "preparing"
    assert observed_preparing["terminal"] is False

    queued = _queue_ci_request(preparing)
    observed_queued = await converge.converge_task(
        _github_call,
        object(),
        session["session_id"],
        session["session_revision"],
        idempotency_key="window-a",
    )
    assert observed_queued["ci_request"]["phase"] == "queued"
    assert observed_queued["ci_job_id"] == queued["worker_job_id"]
    assert observed_queued["continuation_required"] is True

    running = ci_request_store.transition_ci_request(
        queued["request_id"], queued["revision"], "running", "running"
    )
    observed_running = await converge.converge_task(
        _github_call,
        object(),
        session["session_id"],
        session["session_revision"],
        idempotency_key="window-a",
    )
    assert running["phase"] == "running"
    assert observed_running["phase"] == "ci_running"
    assert observed_running["ci_status"] == "running"
    assert observed_running["validation"]["terminal"] is False


@pytest.mark.asyncio
async def test_index_ready_unlocks_analysis_without_recreating_index(monkeypatch):
    session = _session()
    _install_identity_mocks(monkeypatch, session)
    index_states = [
        {"status": "running", "tree_sha": TREE_B},
        {"status": "ready", "tree_sha": TREE_B},
    ]
    request_calls = []
    _install_index_mocks(monkeypatch, index_states, request_calls=request_calls)
    analysis_calls = []
    _install_analysis_mocks(monkeypatch, calls=analysis_calls)

    first = await converge.converge_task(
        _github_call,
        object(),
        session["session_id"],
        session["session_revision"],
        idempotency_key="window-a",
    )
    assert analysis_calls == []
    assert first["analysis"]["pending"] is True

    second = await converge.converge_task(
        _github_call,
        object(),
        session["session_id"],
        session["session_revision"],
        idempotency_key="window-a",
    )

    assert request_calls and len(request_calls) == 1
    assert analysis_calls == [
        "change_context",
        "change_impact",
        "contract_detection",
        "affected_tests",
    ]
    assert second["index"]["ready"] is True
    assert second["analysis"]["pending"] is False
    assert second["convergence"]["analysis"]["index"]["state"] == "ready"


@pytest.mark.asyncio
async def test_passed_ci_waits_for_all_required_analysis_evidence(monkeypatch):
    session = _session()
    _install_identity_mocks(monkeypatch, session)
    _install_index_mocks(monkeypatch, [{"status": "ready", "tree_sha": TREE_B}])
    _install_analysis_mocks(monkeypatch, impact={"ok": True, "complete": False})
    first = await converge.converge_task(
        _github_call,
        object(),
        session["session_id"],
        session["session_revision"],
        idempotency_key="window-a",
    )
    request = _ci_request(first)
    preparing = ci_request_store.transition_ci_request(
        request["request_id"], request["revision"], "preparing", "preparing"
    )
    queued = _queue_ci_request(preparing)
    running = ci_request_store.transition_ci_request(
        queued["request_id"], queued["revision"], "running", "running"
    )
    ci_request_store.transition_ci_request(
        running["request_id"],
        running["revision"],
        "terminal",
        "passed",
        attestation_id="att-1",
    )

    result = await converge.converge_task(
        _github_call,
        object(),
        session["session_id"],
        session["session_revision"],
    )

    assert result["ci_status"] == "passed"
    assert result["phase"] == "post_ci_finalize"
    assert result["converged"] is False
    assert result["convergence"]["analysis"]["change_impact"]["state"] == "degraded"
    assert result["analysis"]["degraded"] is True


@pytest.mark.asyncio
async def test_evidence_complete_after_ci_pass_is_the_only_success(monkeypatch):
    session = _session()
    _install_identity_mocks(monkeypatch, session)
    _install_index_mocks(monkeypatch, [{"status": "ready", "tree_sha": TREE_B}])
    _install_analysis_mocks(monkeypatch)
    first = await converge.converge_task(
        _github_call,
        object(),
        session["session_id"],
        session["session_revision"],
        idempotency_key="window-a",
    )
    request = _ci_request(first)
    preparing = ci_request_store.transition_ci_request(
        request["request_id"], request["revision"], "preparing", "preparing"
    )
    queued = _queue_ci_request(preparing)
    running = ci_request_store.transition_ci_request(
        queued["request_id"], queued["revision"], "running", "running"
    )
    passed = ci_request_store.transition_ci_request(
        running["request_id"],
        running["revision"],
        "terminal",
        "passed",
        attestation_id="att-1",
    )

    result = await converge.converge_task(
        _github_call,
        object(),
        session["session_id"],
        session["session_revision"],
    )

    assert passed["attestation_id"] == "att-1"
    assert result["phase"] == "passed"
    assert result["terminal"] is True
    assert result["converged"] is True
    assert result["attestation_id"] == "att-1"


@pytest.mark.asyncio
async def test_ci_failed_is_truthfully_failed_with_failure_evidence(monkeypatch):
    session = _session()
    _install_identity_mocks(monkeypatch, session)
    _install_index_mocks(monkeypatch, [{"status": "running", "tree_sha": TREE_B}])
    _install_analysis_mocks(monkeypatch)
    first = await converge.converge_task(
        _github_call,
        object(),
        session["session_id"],
        session["session_revision"],
        idempotency_key="window-a",
    )
    request = _ci_request(first)
    preparing = ci_request_store.transition_ci_request(
        request["request_id"], request["revision"], "preparing", "preparing"
    )
    queued = _queue_ci_request(preparing)
    running = ci_request_store.transition_ci_request(
        queued["request_id"], queued["revision"], "running", "running"
    )
    ci_request_store.transition_ci_request(
        running["request_id"],
        running["revision"],
        "terminal",
        "failed",
        failure_pack_id="failure-1",
    )

    result = await converge.converge_task(
        _github_call,
        object(),
        session["session_id"],
        session["session_revision"],
        idempotency_key="window-a",
    )

    assert result["phase"] == "failed"
    assert result["terminal"] is True
    assert result["converged"] is False
    assert result["failure_pack_id"] == "failure-1"
    assert result["validation"]["failure_pack"]["failure_pack_id"] == "failure-1"


@pytest.mark.asyncio
async def test_session_head_tree_drift_blocks_and_requires_recovery(monkeypatch):
    session = _session()
    current = {**session}
    _install_identity_mocks(monkeypatch, current)
    _install_index_mocks(monkeypatch, [{"status": "running", "tree_sha": TREE_B}])
    _install_analysis_mocks(monkeypatch)
    first = await converge.converge_task(
        _github_call,
        object(),
        session["session_id"],
        session["session_revision"],
    )
    current["head_commit_sha"] = "d" * 40
    current["tree_sha"] = "e" * 40

    result = await converge.converge_task(
        _github_call,
        object(),
        session["session_id"],
        session["session_revision"],
    )

    assert result["convergence_id"] == first["convergence_id"]
    assert result["phase"] == "blocked"
    assert result["status"] == "blocked"
    assert result["recovery_required"] is True
    assert result["convergence"]["error_code"] == "DEVELOPMENT_SESSION_RECOVERY_REQUIRED"


def test_convergence_revision_cas_is_not_session_revision(monkeypatch):
    del monkeypatch
    created = development_convergence_store.create_or_get_convergence(
        repository="owner/repo",
        branch="ai/converge",
        development_session_id="dev_converge",
        workspace_id="ws_converge",
        session_revision=7,
        workspace_revision=4,
        head_sha=SHA_B,
        tree_sha=TREE_B,
        base_branch="main",
        base_sha=SHA_A,
        mode="full",
        idempotency_key="window-a",
    )
    advanced = development_convergence_store.transition_convergence(
        created["convergence_id"], created["revision"], "index_requested"
    )

    with pytest.raises(mygithub12.MyGithub12Error) as exc_info:
        development_convergence_store.transition_convergence(
            created["convergence_id"], created["revision"], "analysis_pending"
        )

    assert exc_info.value.code == "DEVELOPMENT_CONVERGENCE_REVISION_MISMATCH"
    assert advanced["revision"] != created["session_revision"]


@pytest.mark.asyncio
async def test_ten_minute_fake_ci_requires_no_wall_clock_wait(fake_long_running_ci, monkeypatch):
    session = _session()
    _install_identity_mocks(monkeypatch, session)
    _install_index_mocks(monkeypatch, [{"status": "running", "tree_sha": TREE_B}])
    _install_analysis_mocks(monkeypatch)
    fake_long_running_ci.transition("running", at_seconds=1)
    fake_long_running_ci.transition("running", at_seconds=601)
    fake_long_running_ci.transition("passed", at_seconds=602)
    monkeypatch.setattr(
        dx, "wait_validation", lambda *_args, **_kwargs: pytest.fail("must not wait")
    )
    monkeypatch.setattr(
        mygithub12,
        "wait_index_job",
        lambda *_args, **_kwargs: pytest.fail("must not wait"),
    )

    result = await converge.converge_task(
        _github_call,
        object(),
        session["session_id"],
        session["session_revision"],
        index_wait_seconds=55,
        wait_seconds=55,
    )

    assert result["terminal"] is False
    assert fake_long_running_ci.history[-1]["logical_time_seconds"] == 602.0


@pytest.mark.asyncio
async def test_reopen_store_continues_same_convergence_and_request(monkeypatch):
    session = _session()
    _install_identity_mocks(monkeypatch, session)
    _install_index_mocks(monkeypatch, [{"status": "running", "tree_sha": TREE_B}])
    _install_analysis_mocks(monkeypatch)
    first = await converge.converge_task(
        _github_call,
        object(),
        session["session_id"],
        session["session_revision"],
        idempotency_key="window-a",
    )
    first_id = first["convergence_id"]
    first_request_id = first["ci_request"]["request_id"]
    first_revision = first["revision"]

    importlib.reload(development_convergence_store)
    reopened = development_convergence_store.get_convergence(first_id)
    second = await converge.converge_task(
        _github_call,
        object(),
        session["session_id"],
        session["session_revision"],
        idempotency_key="window-a",
        convergence_id=first_id,
    )

    assert reopened["convergence_id"] == first_id
    assert reopened["revision"] == first_revision
    assert second["convergence_id"] == first_id
    assert second["ci_request"]["request_id"] == first_request_id
    assert second["convergence"]["index_job_id"] == "idx-1"


def test_wait_worker_final_state_reports_one_local_snapshot(monkeypatch):
    monkeypatch.setattr(
        converge,
        "get_job",
        lambda job_id: {
            "job_id": job_id,
            "status": "passed",
            "worker_id": "wsl-ci-01",
        },
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

    result = converge.wait_worker_final_state("job-1", wait_seconds=55)

    assert result["released"] is True
    assert result["idle"] is True
    assert result["current_job"] is None
