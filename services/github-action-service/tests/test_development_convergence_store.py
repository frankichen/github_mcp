import hashlib
import importlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import development_convergence_store as store


HEAD = "1" * 40
TREE = "2" * 40
BASE = "3" * 40
OTHER_HEAD = "4" * 40
CALLER_HASH = hashlib.sha256(b"web-window-a").hexdigest()


def _reset_store_db(monkeypatch, tmp_path):
    db_path = tmp_path / "mygithub12.db"
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(db_path))
    store.init_convergence_db()
    return db_path


def _create(**overrides):
    values = {
        "repository": "owner/repo",
        "branch": "ai/dev-006",
        "development_session_id": "dev_session_1",
        "workspace_id": "ws_1",
        "session_revision": 7,
        "workspace_revision": 11,
        "head_sha": HEAD,
        "tree_sha": TREE,
        "base_branch": "main",
        "base_sha": BASE,
        "mode": "full",
        "idempotency_key": "dev-006-run-1",
        "caller_identity_hash": CALLER_HASH,
    }
    values.update(overrides)
    return store.create_or_get_convergence(**values)


def test_identity_ci_analysis_evidence_and_restart_recovery(monkeypatch, tmp_path):
    _reset_store_db(monkeypatch, tmp_path)
    created = _create()

    assert created["convergence_id"].startswith("conv_")
    assert created["development_session_id"] == "dev_session_1"
    assert created["workspace_id"] == "ws_1"
    assert created["session_revision"] == 7
    assert created["workspace_revision"] == 11
    assert created["head_sha"] == HEAD
    assert created["tree_sha"] == TREE
    assert created["mode"] == "full"
    assert created["base_branch"] == "main"
    assert created["base_sha"] == BASE
    assert created["phase"] == "accepted"
    assert created["revision"] == 0
    assert set(created["analysis"]) == set(store.ANALYSIS_STAGES)
    assert {item["state"] for item in created["analysis"].values()} == {"pending"}

    current = store.bind_index_job(created["convergence_id"], 0, "index_job_1")
    current = store.record_analysis_state(
        created["convergence_id"],
        current["revision"],
        stage="index",
        state="ready",
        resource_uri="mygithub12://index/exact",
        resource_identity="index-v12:exact-head",
    )
    current = store.record_analysis_state(
        created["convergence_id"],
        current["revision"],
        stage="change_context",
        state="ready",
        resource_uri="mygithub12://response/change-context",
        resource_identity="change-context-sha256",
    )
    current = store.record_analysis_state(
        created["convergence_id"],
        current["revision"],
        stage="change_impact",
        state="degraded",
        error_code="IMPACT_ANALYSIS_INCOMPLETE",
        error_message="bounded evidence is incomplete",
    )
    current = store.record_analysis_state(
        created["convergence_id"],
        current["revision"],
        stage="contract_detection",
        state="ready",
    )
    current = store.record_analysis_state(
        created["convergence_id"],
        current["revision"],
        stage="affected_tests",
        state="failed",
        error_code="AFFECTED_SELECTION_INCOMPLETE",
        error_message="selection failed",
    )

    current = store.bind_ci_request(
        created["convergence_id"], current["revision"], "ci_req_123"
    )
    assert current["ci_request_id"] == "ci_req_123"
    assert current["ci_job_id"] is None

    with pytest.raises(store.MyGithub12Error) as exc_info:
        store.bind_ci_job(
            created["convergence_id"],
            current["revision"],
            ci_request_id="ci_req_wrong",
            ci_job_id="worker_job_1",
        )
    assert exc_info.value.code == "DEVELOPMENT_CONVERGENCE_IDENTITY_MISMATCH"

    current = store.bind_ci_job(
        created["convergence_id"],
        current["revision"],
        ci_request_id="ci_req_123",
        ci_job_id="worker_job_1",
    )
    current = store.record_terminal_evidence(
        created["convergence_id"],
        current["revision"],
        attestation_id="attestation_1",
        failure_pack_id="failure_pack_1",
    )
    current = store.transition_convergence(
        created["convergence_id"],
        current["revision"],
        "post_ci_finalize",
    )

    expected_revision = current["revision"]
    convergence_id = current["convergence_id"]

    reloaded = importlib.reload(store)
    reloaded.init_convergence_db()
    recovered = reloaded.get_convergence(convergence_id)

    assert recovered["revision"] == expected_revision
    assert recovered["phase"] == "post_ci_finalize"
    assert recovered["index_job_id"] == "index_job_1"
    assert recovered["ci_request_id"] == "ci_req_123"
    assert recovered["ci_job_id"] == "worker_job_1"
    assert recovered["attestation_id"] == "attestation_1"
    assert recovered["failure_pack_id"] == "failure_pack_1"
    assert recovered["analysis"]["index"]["state"] == "ready"
    assert recovered["analysis"]["change_context"]["state"] == "ready"
    assert recovered["analysis"]["change_impact"]["state"] == "degraded"
    assert recovered["analysis"]["contract_detection"]["state"] == "ready"
    assert recovered["analysis"]["affected_tests"]["state"] == "failed"

    replayed = _create(session_revision=99, workspace_revision=101)
    assert replayed["convergence_id"] == convergence_id
    assert replayed["session_revision"] == 7
    assert replayed["workspace_revision"] == 11
    assert replayed["deduplicated"] is True


def test_revision_cas_rejects_stale_update(monkeypatch, tmp_path):
    _reset_store_db(monkeypatch, tmp_path)
    created = _create()

    updated = store.transition_convergence(
        created["convergence_id"], created["revision"], "index_requested"
    )
    assert updated["revision"] == created["revision"] + 1

    with pytest.raises(store.MyGithub12Error) as exc_info:
        store.transition_convergence(
            created["convergence_id"], created["revision"], "analysis_pending"
        )
    assert exc_info.value.code == "DEVELOPMENT_CONVERGENCE_REVISION_MISMATCH"

    persisted = store.get_convergence(created["convergence_id"])
    assert persisted["phase"] == "index_requested"
    assert persisted["revision"] == updated["revision"]


def test_sequential_idempotency_and_conflict(monkeypatch, tmp_path):
    _reset_store_db(monkeypatch, tmp_path)
    first = _create()
    replay = _create(session_revision=8, workspace_revision=12)

    assert replay["convergence_id"] == first["convergence_id"]
    assert replay["deduplicated"] is True
    assert replay["dedupe_reason"] == "idempotency_key"

    with pytest.raises(store.MyGithub12Error) as exc_info:
        _create(head_sha=OTHER_HEAD)
    assert exc_info.value.code == "IDEMPOTENCY_CONFLICT"


def test_concurrent_create_has_one_canonical_active_run(monkeypatch, tmp_path):
    db_path = _reset_store_db(monkeypatch, tmp_path)

    def create_one(index):
        return _create(idempotency_key=f"parallel-{index}")

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(create_one, range(16)))

    convergence_ids = {item["convergence_id"] for item in results}
    assert len(convergence_ids) == 1

    with sqlite3.connect(db_path) as db:
        active_count = db.execute(
            "SELECT COUNT(*) FROM development_convergences WHERE terminal=0"
        ).fetchone()[0]
    assert active_count == 1


def test_active_identity_prevents_second_window_but_terminal_allows_rerun(
    monkeypatch, tmp_path
):
    _reset_store_db(monkeypatch, tmp_path)
    first = _create(idempotency_key="window-a")
    second_window = _create(
        idempotency_key="window-b",
        caller_identity_hash=hashlib.sha256(b"web-window-b").hexdigest(),
    )

    assert second_window["convergence_id"] == first["convergence_id"]
    assert second_window["dedupe_reason"] == "active_identity"

    terminal = store.transition_convergence(
        first["convergence_id"], first["revision"], "passed"
    )
    assert terminal["terminal"] is True
    assert terminal["finished_at"] is not None

    rerun = _create(
        idempotency_key="window-c",
        caller_identity_hash=hashlib.sha256(b"web-window-c").hexdigest(),
    )
    assert rerun["convergence_id"] != first["convergence_id"]
    assert rerun["terminal"] is False

    old_key_replay = _create(idempotency_key="window-a")
    assert old_key_replay["convergence_id"] == first["convergence_id"]
    assert old_key_replay["terminal"] is True


def test_schema_initialization_is_idempotent_and_does_not_touch_existing_tables(
    monkeypatch, tmp_path
):
    db_path = _reset_store_db(monkeypatch, tmp_path)
    store.init_convergence_db()
    store.init_convergence_db()

    with sqlite3.connect(db_path) as db:
        tables = {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    assert "development_convergences" in tables
    assert "development_convergence_analysis" in tables
    assert "development_convergence_events" in tables
    assert "workspaces" in tables
