import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import ci_database as db


def isolated_db(tmp_path, monkeypatch):
    path = tmp_path / "ci.db"
    monkeypatch.setattr(db, "DB_PATH", str(path))
    if getattr(db._local, "db", None) is not None:
        db._local.db.close()
    db._local.db = None
    db.init_db()
    return path


def test_renew_lease_commits_heartbeat_write(tmp_path, monkeypatch):
    path = isolated_db(tmp_path, monkeypatch)
    token = "lease-token"
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    connection = db._get_db()
    connection.execute(
        "INSERT INTO ci_jobs(job_id,idempotency_key,repository,commit_sha,profile,created_at,"
        "worker_id,lease_token_hash,lease_expires_at,status) VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("job-1", "idem-1", "owner/repo", "a" * 40, "repo-auto-check", db.now_ts(),
         "wsl-ci-01", token_hash, db.now_ts() + 60, "running"),
    )
    connection.commit()

    assert db.renew_lease("job-1", token) is True
    assert connection.in_transaction is False

    other = sqlite3.connect(path, timeout=1)
    try:
        other.execute("UPDATE ci_jobs SET cancel_requested=1 WHERE job_id='job-1'")
        other.commit()
    finally:
        other.close()


def test_concurrent_job_creation_is_durable_and_idempotent(tmp_path, monkeypatch):
    isolated_db(tmp_path, monkeypatch)

    def create():
        return db.create_or_get_job(
            repository="owner/repo", branch="main", commit_sha="b" * 40,
            profile="repo-auto-check", priority=100, timeout_seconds=900,
            force_rerun=False, supersede_previous=False,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: create(), range(4)))

    assert len({item["job_id"] for item in results}) == 1
    job_id = results[0]["job_id"]
    assert db.get_job(job_id)["job_id"] == job_id
    assert len(db.list_jobs(repository="owner/repo", commit_sha="b" * 40)) == 1


def _insert_running_job(connection, job_id: str, worker_id: str):
    connection.execute(
        "INSERT INTO ci_jobs(job_id,idempotency_key,repository,commit_sha,profile,created_at,"
        "worker_id,lease_token_hash,lease_expires_at,status,started_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            job_id,
            f"idem-{job_id}",
            "owner/repo",
            "c" * 40,
            "repo-auto-check",
            db.now_ts(),
            worker_id,
            hashlib.sha256(f"lease-{job_id}".encode()).hexdigest(),
            db.now_ts() + 60,
            "running",
            db.now_ts(),
        ),
    )


def test_complete_job_clears_matching_worker_pointer_atomically(tmp_path, monkeypatch):
    isolated_db(tmp_path, monkeypatch)
    assert db.register_worker("wsl-ci-test", "token", ["repo-auto-check"], 1)
    connection = db._get_db()
    _insert_running_job(connection, "job-finish", "wsl-ci-test")
    connection.execute(
        "UPDATE ci_workers SET status='busy', current_job_id='job-finish' WHERE worker_id='wsl-ci-test'"
    )
    connection.commit()

    db.complete_job("job-finish", 0, "passed", {"status": "passed"})

    worker = connection.execute(
        "SELECT status,current_job_id FROM ci_workers WHERE worker_id='wsl-ci-test'"
    ).fetchone()
    job = connection.execute(
        "SELECT status,worker_id FROM ci_jobs WHERE job_id='job-finish'"
    ).fetchone()
    assert (worker["status"], worker["current_job_id"]) == ("idle", None)
    assert (job["status"], job["worker_id"]) == ("passed", None)


def test_late_complete_does_not_clear_newer_worker_job(tmp_path, monkeypatch):
    isolated_db(tmp_path, monkeypatch)
    assert db.register_worker("wsl-ci-test", "token", ["repo-auto-check"], 1)
    connection = db._get_db()
    _insert_running_job(connection, "job-old", "wsl-ci-test")
    _insert_running_job(connection, "job-new", "wsl-ci-test")
    connection.execute(
        "UPDATE ci_workers SET status='busy', current_job_id='job-new' WHERE worker_id='wsl-ci-test'"
    )
    connection.commit()

    db.complete_job("job-old", 0, "passed", {"status": "passed"})

    worker = connection.execute(
        "SELECT status,current_job_id FROM ci_workers WHERE worker_id='wsl-ci-test'"
    ).fetchone()
    assert (worker["status"], worker["current_job_id"]) == ("busy", "job-new")


def test_release_job_clears_matching_worker_pointer(tmp_path, monkeypatch):
    isolated_db(tmp_path, monkeypatch)
    assert db.register_worker("wsl-ci-test", "token", ["repo-auto-check"], 1)
    connection = db._get_db()
    _insert_running_job(connection, "job-release", "wsl-ci-test")
    connection.execute(
        "UPDATE ci_workers SET status='busy', current_job_id='job-release' WHERE worker_id='wsl-ci-test'"
    )
    connection.commit()

    db.release_job("job-release")

    worker = connection.execute(
        "SELECT status,current_job_id FROM ci_workers WHERE worker_id='wsl-ci-test'"
    ).fetchone()
    job = connection.execute(
        "SELECT status,worker_id FROM ci_jobs WHERE job_id='job-release'"
    ).fetchone()
    assert (worker["status"], worker["current_job_id"]) == ("idle", None)
    assert (job["status"], job["worker_id"]) == ("queued", None)


def _create_queued_job(repository: str, sha: str, priority: int = 100):
    return db.create_or_get_job(
        repository=repository, branch="main", commit_sha=sha,
        profile="repo-auto-check", priority=priority, timeout_seconds=900,
        force_rerun=False, supersede_previous=False,
    )


def test_two_workers_lease_distinct_jobs_and_preserve_repository_fairness(tmp_path, monkeypatch):
    isolated_db(tmp_path, monkeypatch)
    assert db.register_worker("worker-a", "token-a", ["repo-auto-check"], 1)
    assert db.register_worker("worker-b", "token-b", ["repo-auto-check"], 1)
    first = _create_queued_job("owner/repo-a", "1" * 40)
    second_same_repo = _create_queued_job("owner/repo-a", "2" * 40)
    other_repo = _create_queued_job("owner/repo-b", "3" * 40)

    lease_a = db.lease_job("worker-a", ["repo-auto-check"], 1)
    lease_b = db.lease_job("worker-b", ["repo-auto-check"], 1)

    assert lease_a["job_id"] == first["job_id"]
    assert lease_b["job_id"] == other_repo["job_id"]
    assert lease_a["job_id"] != lease_b["job_id"]
    assert db.get_job(second_same_repo["job_id"])["status"] == "queued"


def test_scheduler_keeps_priority_ahead_of_fifo_age(tmp_path, monkeypatch):
    isolated_db(tmp_path, monkeypatch)
    assert db.register_worker("worker-a", "token-a", ["repo-auto-check"], 1)
    low = _create_queued_job("owner/repo-a", "4" * 40, priority=100)
    high = _create_queued_job("owner/repo-b", "5" * 40, priority=200)

    lease = db.lease_job("worker-a", ["repo-auto-check"], 1)

    assert lease["job_id"] == high["job_id"]
    assert db.get_job(low["job_id"])["status"] == "queued"


def test_expired_attempt_cannot_write_after_reassignment(tmp_path, monkeypatch):
    isolated_db(tmp_path, monkeypatch)
    assert db.register_worker("worker-a", "token-a", ["repo-auto-check"], 1)
    assert db.register_worker("worker-b", "token-b", ["repo-auto-check"], 1)
    created = _create_queued_job("owner/repo-a", "6" * 40)
    old_lease = db.lease_job("worker-a", ["repo-auto-check"], 1)
    connection = db._get_db()
    connection.execute(
        "UPDATE ci_jobs SET lease_expires_at=? WHERE job_id=?",
        (db.now_ts() - 1, created["job_id"]),
    )
    connection.commit()

    assert db.recover_expired_leases() == 1
    new_lease = db.lease_job("worker-b", ["repo-auto-check"], 1)
    assert new_lease["job_id"] == created["job_id"]
    assert new_lease["attempt"] == 2

    with pytest.raises(db.StaleJobLeaseError):
        db.append_log_chunk(
            created["job_id"], "stale\n", "worker-a", old_lease["lease_token"]
        )
    with pytest.raises(db.StaleJobLeaseError):
        db.complete_job(
            created["job_id"], 0, "passed", {"status": "passed"},
            worker_id="worker-a", lease_token=old_lease["lease_token"],
        )

    assert db.append_log_chunk(
        created["job_id"], "current\n", "worker-b", new_lease["lease_token"]
    ) > 0
    assert db.complete_job(
        created["job_id"], 0, "passed", {"status": "passed"},
        worker_id="worker-b", lease_token=new_lease["lease_token"],
    ) is True
    assert db.get_job(created["job_id"])["status"] == "passed"


def test_renew_lease_rejects_wrong_worker_and_expired_attempt(tmp_path, monkeypatch):
    isolated_db(tmp_path, monkeypatch)
    assert db.register_worker("worker-a", "token-a", ["repo-auto-check"], 1)
    assert db.register_worker("worker-b", "token-b", ["repo-auto-check"], 1)
    created = _create_queued_job("owner/repo-a", "7" * 40)
    lease = db.lease_job("worker-a", ["repo-auto-check"], 1)

    assert db.renew_lease(created["job_id"], lease["lease_token"], "worker-b") is False
    connection = db._get_db()
    connection.execute(
        "UPDATE ci_jobs SET lease_expires_at=? WHERE job_id=?",
        (db.now_ts() - 1, created["job_id"]),
    )
    connection.commit()
    assert db.renew_lease(created["job_id"], lease["lease_token"], "worker-a") is False


def test_worker_recovery_updates_queue_counts_once(tmp_path, monkeypatch):
    isolated_db(tmp_path, monkeypatch)
    assert db.register_worker("worker-a", "token-a", ["repo-auto-check"], 1)
    created = _create_queued_job("owner/repo-a", "8" * 40)
    db.lease_job("worker-a", ["repo-auto-check"], 1)

    assert db.recover_worker_jobs("worker-a") == 1
    assert db.recover_worker_jobs("worker-a") == 0
    state = db._get_db().execute(
        "SELECT queued_jobs,running_jobs FROM ci_repository_queue_state WHERE repository=?",
        ("owner/repo-a",),
    ).fetchone()
    assert (state["queued_jobs"], state["running_jobs"]) == (1, 0)
    assert db.get_job(created["job_id"])["status"] == "queued"


def test_queued_job_reports_eligible_workers_and_capacity_reason(tmp_path, monkeypatch):
    isolated_db(tmp_path, monkeypatch)
    assert db.register_worker("worker-a", "token-a", ["repo-auto-check"], 1)
    assert db.register_worker("worker-b", "token-b", ["repo-auto-check"], 1)
    queued = _create_queued_job("owner/repo-b", "9" * 40)

    evidence = db.get_job(queued["job_id"])
    assert evidence["eligible_workers"] == ["worker-a", "worker-b"]
    assert evidence["unschedulable_reason"] is None

    first_high = _create_queued_job("owner/repo-a", "a" * 40, priority=200)
    second_high = _create_queued_job("owner/repo-a", "b" * 40, priority=200)
    lease_a = db.lease_job("worker-a", ["repo-auto-check"], 1)
    lease_b = db.lease_job("worker-b", ["repo-auto-check"], 1)
    assert {lease_a["job_id"], lease_b["job_id"]} == {
        first_high["job_id"], second_high["job_id"]
    }
    queued_capacity = db.get_job(queued["job_id"])
    assert queued_capacity["eligible_workers"] == []
    assert queued_capacity["unschedulable_reason"] == "eligible_workers_at_capacity"


def test_duplicate_log_batch_is_idempotent_for_current_attempt(tmp_path, monkeypatch):
    isolated_db(tmp_path, monkeypatch)
    assert db.register_worker("worker-a", "token-a", ["repo-auto-check"], 1)
    created = _create_queued_job("owner/repo-a", "c" * 40)
    lease = db.lease_job("worker-a", ["repo-auto-check"], 1)

    first_offset, first_idempotent = db.append_log_batch(
        created["job_id"], "batch-1", "hello\n", "worker-a", lease["lease_token"]
    )
    second_offset, second_idempotent = db.append_log_batch(
        created["job_id"], "batch-1", "hello\n", "worker-a", lease["lease_token"]
    )

    assert first_idempotent is False
    assert second_idempotent is True
    assert second_offset == first_offset
