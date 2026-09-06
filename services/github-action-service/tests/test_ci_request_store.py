import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import ci_database as db
from app import ci_request_store as requests


def isolated_db(tmp_path, monkeypatch):
    path = tmp_path / "ci.db"
    monkeypatch.setattr(db, "DB_PATH", str(path))
    if getattr(db._local, "db", None) is not None:
        db._local.db.close()
    db._local.db = None
    db.init_db()
    return path


def request_identity(seed: str = "a") -> dict:
    return {
        "repository": "owner/repo",
        "branch": "main",
        "commit_sha": seed * 40,
        "tree_sha": chr(ord(seed) + 1) * 40,
        "profile": "repo-auto-check",
        "effective_config_digest": f"config-{seed}",
    }


def create_request(seed: str = "a", key: str = "idem-1") -> dict:
    identity = request_identity(seed)
    payload = {**identity, "timeout_seconds": 900, "priority": "normal"}
    return requests.create_or_get_ci_request(
        **identity,
        idempotency_key=key,
        normalized_request_hash=requests.compute_normalized_request_hash(payload),
    )


def create_worker_job(identity: dict) -> dict:
    return db.create_or_get_job(
        repository=identity["repository"],
        branch=identity["branch"],
        commit_sha=identity["commit_sha"],
        profile=identity["profile"],
        priority=100,
        timeout_seconds=900,
        force_rerun=True,
        supersede_previous=False,
    )


def advance_to_running(seed: str = "a", key: str = "idem-1") -> tuple[dict, dict]:
    request = create_request(seed, key)
    request = requests.transition_ci_request(
        request["request_id"], 0, "preparing", "preparing"
    )
    identity = request_identity(seed)
    job = create_worker_job(identity)
    request = requests.transition_ci_request(
        request["request_id"],
        1,
        "queued",
        "queued",
        worker_job_id=job["job_id"],
    )
    request = requests.transition_ci_request(
        request["request_id"], 2, "running", "running"
    )
    return request, job


def test_normalized_request_hash_is_canonical():
    first = {"repository": "owner/repo", "profile": "repo-auto-check", "timeout": 900}
    second = {"timeout": 900, "profile": "repo-auto-check", "repository": "owner/repo"}
    assert requests.compute_normalized_request_hash(first) == requests.compute_normalized_request_hash(second)


def test_idempotency_same_key_same_hash_returns_same_request(tmp_path, monkeypatch):
    isolated_db(tmp_path, monkeypatch)
    identity = request_identity("a")
    payload = {**identity, "timeout_seconds": 900}
    request_hash = requests.compute_normalized_request_hash(payload)

    first = requests.create_or_get_ci_request(
        **identity,
        idempotency_key="stable-key",
        normalized_request_hash=request_hash,
    )
    second = requests.create_or_get_ci_request(
        **identity,
        idempotency_key="stable-key",
        normalized_request_hash=request_hash,
    )

    assert first["request_id"] == second["request_id"]
    assert first["revision"] == second["revision"] == 0
    assert first["deduplicated"] is False
    assert second["deduplicated"] is True
    assert requests.get_ci_request_by_idempotency_key("stable-key")["request_id"] == first["request_id"]


def test_idempotency_same_key_different_hash_is_conflict(tmp_path, monkeypatch):
    isolated_db(tmp_path, monkeypatch)
    identity = request_identity("a")
    first_hash = requests.compute_normalized_request_hash({**identity, "timeout": 900})
    second_hash = requests.compute_normalized_request_hash({**identity, "timeout": 1200})
    requests.create_or_get_ci_request(
        **identity,
        idempotency_key="stable-key",
        normalized_request_hash=first_hash,
    )

    with pytest.raises(requests.CIRequestIdempotencyConflictError) as exc:
        requests.create_or_get_ci_request(
            **identity,
            idempotency_key="stable-key",
            normalized_request_hash=second_hash,
        )
    assert exc.value.code == "IDEMPOTENCY_CONFLICT"


def test_concurrent_create_or_get_has_one_durable_identity(tmp_path, monkeypatch):
    isolated_db(tmp_path, monkeypatch)
    identity = request_identity("a")
    request_hash = requests.compute_normalized_request_hash({**identity, "timeout": 900})

    def create():
        return requests.create_or_get_ci_request(
            **identity,
            idempotency_key="concurrent-key",
            normalized_request_hash=request_hash,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: create(), range(4)))

    assert len({item["request_id"] for item in results}) == 1
    connection = db._get_db()
    assert connection.execute(
        "SELECT COUNT(*) FROM ci_requests WHERE idempotency_key='concurrent-key'"
    ).fetchone()[0] == 1


def test_legal_state_machine_transitions_and_revision(tmp_path, monkeypatch):
    isolated_db(tmp_path, monkeypatch)
    request, _ = advance_to_running()
    assert (request["phase"], request["status"], request["revision"]) == (
        "running",
        "running",
        3,
    )

    request = requests.transition_ci_request(
        request["request_id"],
        3,
        "terminal",
        "passed",
        attestation_id="attestation-test",
    )
    assert (request["phase"], request["status"], request["revision"]) == (
        "terminal",
        "passed",
        4,
    )
    events = requests.get_ci_request_events(request["request_id"])
    assert [event["revision"] for event in events] == [0, 1, 2, 3, 4]
    assert request["last_event_id"] == events[-1]["event_id"]


@pytest.mark.parametrize(
    "terminal_status",
    [
        "failed",
        "timed_out",
        "cancelled",
        "superseded",
        "worker_lost",
        "internal_error",
    ],
)
def test_running_supports_required_terminal_statuses(tmp_path, monkeypatch, terminal_status):
    isolated_db(tmp_path, monkeypatch)
    request, _ = advance_to_running()
    request = requests.transition_ci_request(
        request["request_id"], 3, "terminal", terminal_status
    )
    assert request["phase"] == "terminal"
    assert request["status"] == terminal_status
    assert request["revision"] == 4


def test_preparing_to_preflight_failed_persists_error_identity(tmp_path, monkeypatch):
    isolated_db(tmp_path, monkeypatch)
    request = create_request()
    request = requests.transition_ci_request(
        request["request_id"], 0, "preparing", "preparing"
    )
    request = requests.transition_ci_request(
        request["request_id"],
        1,
        "terminal",
        "preflight_failed",
        preflight_error_id="preflight-1",
        preflight_error_code="CI_PREFLIGHT_TREE_MISMATCH",
        terminal_reason="exact_tree_validation_failed",
    )
    assert request["status"] == "preflight_failed"
    assert request["preflight_error_id"] == "preflight-1"
    assert request["preflight_error_code"] == "CI_PREFLIGHT_TREE_MISMATCH"
    assert request["revision"] == 2


@pytest.mark.parametrize(
    ("terminal_status", "target_phase", "target_status"),
    [
        ("passed", "running", "running"),
        ("failed", "queued", "queued"),
        ("cancelled", "running", "running"),
    ],
)
def test_terminal_requests_reject_reopen(
    tmp_path, monkeypatch, terminal_status, target_phase, target_status
):
    isolated_db(tmp_path, monkeypatch)
    request, _ = advance_to_running()
    request = requests.transition_ci_request(
        request["request_id"], 3, "terminal", terminal_status
    )

    with pytest.raises(requests.CIRequestTransitionError):
        requests.transition_ci_request(
            request["request_id"], 4, target_phase, target_status
        )
    persisted = requests.get_ci_request(request["request_id"])
    assert persisted["status"] == terminal_status
    assert persisted["revision"] == 4


def test_invalid_phase_status_combination_fails_stop(tmp_path, monkeypatch):
    isolated_db(tmp_path, monkeypatch)
    request = create_request()
    with pytest.raises(requests.CIRequestTransitionError):
        requests.transition_ci_request(
            request["request_id"], 0, "terminal", "running"
        )
    assert requests.get_ci_request(request["request_id"])["revision"] == 0


def test_stale_revision_fails_without_mutation(tmp_path, monkeypatch):
    isolated_db(tmp_path, monkeypatch)
    request = create_request()
    request = requests.transition_ci_request(
        request["request_id"], 0, "preparing", "preparing"
    )
    with pytest.raises(requests.CIRequestRevisionConflictError):
        requests.transition_ci_request(
            request["request_id"], 0, "terminal", "preflight_failed",
            preflight_error_code="STALE_SHOULD_NOT_WRITE",
        )
    persisted = requests.get_ci_request(request["request_id"])
    assert persisted["revision"] == 1
    assert persisted["status"] == "preparing"
    assert persisted["preflight_error_code"] is None


def test_identity_mismatch_fails_without_mutation(tmp_path, monkeypatch):
    isolated_db(tmp_path, monkeypatch)
    request = create_request()
    with pytest.raises(requests.CIRequestIdentityMismatchError):
        requests.transition_ci_request(
            request["request_id"],
            0,
            "preparing",
            "preparing",
            expected_identity={"commit_sha": "f" * 40},
        )
    assert requests.get_ci_request(request["request_id"])["revision"] == 0


def test_worker_job_identity_mismatch_rejected(tmp_path, monkeypatch):
    isolated_db(tmp_path, monkeypatch)
    request = create_request()
    request = requests.transition_ci_request(
        request["request_id"], 0, "preparing", "preparing"
    )
    wrong = request_identity("c")
    job = create_worker_job(wrong)

    with pytest.raises(requests.CIRequestIdentityMismatchError):
        requests.transition_ci_request(
            request["request_id"],
            1,
            "queued",
            "queued",
            worker_job_id=job["job_id"],
        )
    persisted = requests.get_ci_request(request["request_id"])
    assert persisted["revision"] == 1
    assert persisted["worker_job_id"] is None


def test_concurrent_cas_allows_only_one_writer_from_same_revision(tmp_path, monkeypatch):
    isolated_db(tmp_path, monkeypatch)
    request = create_request()

    def advance():
        try:
            updated = requests.transition_ci_request(
                request["request_id"], 0, "preparing", "preparing"
            )
            return ("success", updated["revision"])
        except requests.CIRequestRevisionConflictError:
            return ("stale", None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: advance(), range(2)))

    assert sorted(kind for kind, _ in results) == ["stale", "success"]
    persisted = requests.get_ci_request(request["request_id"])
    assert persisted["revision"] == 1
    assert persisted["status"] == "preparing"
    assert len(requests.get_ci_request_events(request["request_id"])) == 2


def create_pre_dev_002_database(path):
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE ci_jobs (
            job_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL,
            repository TEXT NOT NULL,
            branch TEXT NOT NULL DEFAULT '',
            commit_sha TEXT NOT NULL,
            base_sha TEXT NOT NULL DEFAULT '',
            changed_files_json TEXT NOT NULL DEFAULT '[]',
            changed_files_total INTEGER NOT NULL DEFAULT 0,
            changed_files_truncated INTEGER NOT NULL DEFAULT 0,
            performance_json TEXT NOT NULL DEFAULT '{}',
            profile TEXT NOT NULL,
            profile_version TEXT NOT NULL DEFAULT 'v1',
            priority INTEGER NOT NULL DEFAULT 100,
            status TEXT NOT NULL DEFAULT 'queued',
            worker_id TEXT,
            lease_token_hash TEXT,
            lease_expires_at REAL,
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            superseded_by_job_id TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 2,
            exit_code INTEGER,
            summary_json TEXT,
            source_sha256 TEXT,
            source_size_bytes INTEGER,
            timeout_seconds INTEGER NOT NULL DEFAULT 900,
            error_code TEXT,
            error_message TEXT,
            created_at REAL NOT NULL,
            queued_at REAL,
            started_at REAL,
            finished_at REAL,
            duration_seconds REAL,
            log_total_bytes INTEGER NOT NULL DEFAULT 0,
            log_truncated INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    rows = [
        ("legacy-queued", "queued", None, None, None),
        ("legacy-running", "running", 20.0, None, None),
        ("legacy-passed", "passed", 30.0, 40.0, 0),
        ("legacy-failed", "failed", 30.0, 45.0, 1),
    ]
    for index, (job_id, status, started_at, finished_at, exit_code) in enumerate(rows):
        connection.execute(
            """
            INSERT INTO ci_jobs (
                job_id, idempotency_key, repository, branch, commit_sha, profile,
                priority, status, attempts, exit_code, created_at, queued_at,
                started_at, finished_at, duration_seconds
            ) VALUES (?, ?, 'owner/repo', 'main', ?, 'repo-auto-check', 100, ?, 1, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                f"legacy-idem-{index}",
                str(index + 1) * 40,
                status,
                exit_code,
                10.0 + index,
                11.0 + index,
                started_at,
                finished_at,
                (finished_at - started_at) if finished_at and started_at else None,
            ),
        )
    connection.commit()
    connection.close()


def test_real_init_migrates_pre_dev_002_database_without_rewriting_jobs(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    create_pre_dev_002_database(path)
    monkeypatch.setattr(db, "DB_PATH", str(path))
    if getattr(db._local, "db", None) is not None:
        db._local.db.close()
    db._local.db = None

    db.init_db()

    expected_statuses = {
        "legacy-queued": "queued",
        "legacy-running": "running",
        "legacy-passed": "passed",
        "legacy-failed": "failed",
    }
    for job_id, expected_status in expected_statuses.items():
        legacy_job = db.get_job(job_id)
        assert legacy_job["job_id"] == job_id
        assert legacy_job["repository"] == "owner/repo"
        assert legacy_job["branch"] == "main"
        assert legacy_job["profile"] == "repo-auto-check"
        assert legacy_job["status"] == expected_status
        request = requests.get_ci_request(job_id)
        assert request["request_id"] == job_id
        assert request["worker_job_id"] == job_id
        assert request["repository"] == "owner/repo"
        assert request["branch"] == "main"
        assert request["profile"] == "repo-auto-check"
        assert request["tree_sha"] is None
        assert request["effective_config_digest"] is None
        assert request["idempotency_key"] is None
        assert request["normalized_request_hash"] is None
        assert request["revision"] == 0

    assert requests.get_ci_request("legacy-running")["phase"] == "running"
    assert requests.get_ci_request("legacy-passed")["phase"] == "terminal"
    assert requests.get_ci_request("legacy-failed")["phase"] == "terminal"
    connection = db._get_db()
    before = connection.execute(
        "SELECT job_id,status,attempts,commit_sha FROM ci_jobs ORDER BY job_id"
    ).fetchall()
    assert connection.execute("SELECT COUNT(*) FROM ci_job_events").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM ci_requests").fetchone()[0] == 4
    assert connection.execute("SELECT COUNT(*) FROM ci_request_events").fetchone()[0] == 4

    connection.close()
    db._local.db = None
    db.init_db()
    connection = db._get_db()
    after = connection.execute(
        "SELECT job_id,status,attempts,commit_sha FROM ci_jobs ORDER BY job_id"
    ).fetchall()
    assert [tuple(row) for row in after] == [tuple(row) for row in before]
    assert connection.execute("SELECT COUNT(*) FROM ci_requests").fetchone()[0] == 4
    assert connection.execute("SELECT COUNT(*) FROM ci_request_events").fetchone()[0] == 4
    assert db.get_job("legacy-running")["status"] == "running"
    assert db.get_job("legacy-passed")["status"] == "passed"
    assert db.get_job("legacy-failed")["status"] == "failed"


def test_request_identity_and_terminal_reference_survive_reopen(tmp_path, monkeypatch):
    isolated_db(tmp_path, monkeypatch)
    request, job = advance_to_running()
    request = requests.transition_ci_request(
        request["request_id"],
        3,
        "terminal",
        "failed",
        terminal_reason="tests_failed",
        failure_pack_id="failure-pack-test",
    )
    request_id = request["request_id"]
    expected = {
        "request_id": request_id,
        "repository": request["repository"],
        "branch": request["branch"],
        "commit_sha": request["commit_sha"],
        "tree_sha": request["tree_sha"],
        "profile": request["profile"],
        "effective_config_digest": request["effective_config_digest"],
        "phase": "terminal",
        "status": "failed",
        "revision": 4,
        "worker_job_id": job["job_id"],
        "terminal_reason": "tests_failed",
        "failure_pack_id": "failure-pack-test",
    }

    db._local.db.close()
    db._local.db = None
    db.init_db()
    reopened = requests.get_ci_request(request_id)

    for field, value in expected.items():
        assert reopened[field] == value
    assert reopened["last_event_id"] is not None
    assert reopened["last_event_revision"] == 4
    assert [event["revision"] for event in requests.get_ci_request_events(request_id)] == [0, 1, 2, 3, 4]
