import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor

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
