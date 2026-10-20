"""Regression: the CI Request projection must follow exact durable Worker facts.

Tests use isolated SQLite fixtures only. They never dispatch an external CI Job.
"""
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import ci_database as db
from app import ci_request_store as requests


HEAD = "a" * 40
TREE = "b" * 40


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    path = tmp_path / "ci-request-projection.db"
    monkeypatch.setattr(db, "DB_PATH", str(path))
    connection = getattr(db._local, "db", None)
    if connection is not None:
        connection.close()
    db._local.db = None
    db.init_db()
    yield path
    connection = getattr(db._local, "db", None)
    if connection is not None:
        connection.close()
    db._local.db = None


def _pair(*, running=True, name="projection"):
    identity = {
        "repository": "owner/repo",
        "branch": "ai/ci-projection",
        "commit_sha": HEAD,
        "tree_sha": TREE,
        "profile": "repo-auto-check",
        "effective_config_digest": "digest-v1",
    }
    request = requests.create_or_get_ci_request(
        **identity, idempotency_key=name,
        normalized_request_hash=requests.compute_normalized_request_hash(identity),
    )
    request = requests.transition_ci_request(
        request["request_id"], request["revision"], "preparing", "preparing"
    )
    job = db.create_or_get_job(
        repository=identity["repository"],
        branch=identity["branch"],
        commit_sha=identity["commit_sha"],
        profile=identity["profile"],
        priority=100,
        timeout_seconds=900,
        force_rerun=True,
        supersede_previous=False,
    )
    request = requests.transition_ci_request(
        request["request_id"], request["revision"], "queued", "queued",
        worker_job_id=job["job_id"],
    )
    if running:
        request = requests.transition_ci_request(
            request["request_id"], request["revision"], "running", "running"
        )
    return request, job


@pytest.mark.parametrize("outcome", ["passed", "failed", "cancelled", "worker_lost"])
def test_worker_completion_updates_request_exactly_once(isolated_db, outcome):
    request, job = _pair(name="outcome-" + outcome)
    summary = {"git_tree_sha": TREE} if outcome == "passed" else None
    result = db.complete_job(
        job["job_id"], 0 if outcome == "passed" else 1, outcome, summary,
    )
    assert result is True
    projected = requests.get_ci_request(request["request_id"])
    assert (projected["phase"], projected["status"]) == ("terminal", outcome)
    assert projected["revision"] == request["revision"] + 1
    assert projected["worker_job_id"] == job["job_id"]
    assert requests.reconcile_worker_request(job["job_id"]) is False
    assert requests.get_ci_request(request["request_id"]) == projected
    assert requests.get_ci_request_events(request["request_id"])[-1]["event_type"] == (
        "worker_terminal_reconciled"
    )


def test_queued_request_replays_passed_worker_on_startup_without_rerun(isolated_db):
    request, job = _pair(running=False, name="stale-queued")
    connection = db._get_db()
    connection.execute(
        "UPDATE ci_jobs SET status = 'passed', exit_code = 0, summary_json = ? "
        "WHERE job_id = ?",
        ('{"git_tree_sha":"' + TREE + '"}', job["job_id"]),
    )
    connection.commit()
    requests.init_ci_request_schema(connection)
    current = requests.get_ci_request(request["request_id"])
    assert (current["phase"], current["status"]) == ("terminal", "passed")
    assert current["revision"] == request["revision"] + 1
    assert requests.reconcile_worker_request(job["job_id"]) is False
    assert requests.get_ci_request(request["request_id"])["revision"] == current["revision"]


def test_pass_must_not_project_mismatched_or_missing_tree(isolated_db):
    request, job = _pair(name="wrong-tree")
    assert db.complete_job(job["job_id"], 0, "passed", {"git_tree_sha": "f" * 40})
    current = requests.get_ci_request(request["request_id"])
    assert (current["phase"], current["status"]) == ("running", "running")
    assert current["attestation_id"] is None


def test_link_only_exact_unexpired_attestation(isolated_db):
    request, job = _pair(name="attestation")
    assert db.complete_job(job["job_id"], 0, "passed", {"git_tree_sha": TREE})
    connection = db._get_db()
    connection.execute(
        "CREATE TABLE ci_tree_attestations "
        "(attestation_id TEXT, private_ci_job_id TEXT, repository TEXT,"
        "tested_commit_sha TEXT, tested_tree_sha TEXT, profile TEXT,"
        "status TEXT, revoked_at REAL, expires_at REAL, created_at REAL)"
    )
    common = (job["job_id"], "owner/repo", HEAD, TREE, "repo-auto-check")
    connection.execute(
        "INSERT INTO ci_tree_attestations VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("att-expired", *common, "active", None, db.now_ts() - 10, db.now_ts()),
    )
    connection.execute(
        "INSERT INTO ci_tree_attestations VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("att-exact", *common, "active", None, db.now_ts() + 3600, db.now_ts()),
    )
    connection.commit()
    assert requests.reconcile_worker_request(job["job_id"]) is True
    current = requests.get_ci_request(request["request_id"])
    assert current["attestation_id"] == "att-exact"
    assert requests.reconcile_worker_request(job["job_id"]) is False


def test_concurrent_replay_does_not_rollback_terminal(isolated_db):
    request, job = _pair(name="concurrent")
    assert db.complete_job(job["job_id"], 0, "passed", {"git_tree_sha": TREE})
    current = requests.get_ci_request(request["request_id"])
    with ThreadPoolExecutor(max_workers=4) as pool:
        changed = list(pool.map(
            lambda _: requests.reconcile_worker_request(job["job_id"]), range(12)
        ))
    assert changed == [False] * 12
    assert requests.get_ci_request(request["request_id"]) == current
