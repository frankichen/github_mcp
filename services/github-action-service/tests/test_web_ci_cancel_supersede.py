import hashlib
import json
import time

import pytest

from app import attestation_registry as registry
from app import ci_database as db
from app import ci_mcp
from app import ci_request_store as requests
from app import development_orchestrator as orchestrator
from app import github_utils
from app.mcp_response import StructuredFastMCP


REPOSITORY = "frankichen/github_mcp"
BRANCH = "ai/web-ci-dev-014-test"
PROFILE = "repo-auto-check"
TREE = "b" * 40


def _structured_result(call_result):
    if isinstance(call_result, tuple):
        return call_result[1]
    structured = getattr(call_result, "structured_content", None)
    if structured is None:
        structured = getattr(call_result, "structuredContent", None)
    return structured


def _reset_db_connection():
    current = getattr(db._local, "db", None)
    if current is not None:
        current.close()
    db._local.db = None


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    path = tmp_path / "ci-dev014.db"
    monkeypatch.setattr(db, "DB_PATH", str(path))
    monkeypatch.setenv("CI_DB_PATH", str(path))
    _reset_db_connection()
    db.init_db()
    yield path
    _reset_db_connection()


@pytest.fixture
def cancel_mcp(isolated_db):
    mcp = StructuredFastMCP("web-ci-dev014-cancel")
    ci_mcp.register_private_ci_mcp_tools(mcp)
    return mcp


def _create_job(commit_sha="a" * 40, *, force_rerun=True, supersede_previous=False):
    return db.create_or_get_job(
        repository=REPOSITORY,
        branch=BRANCH,
        commit_sha=commit_sha,
        profile=PROFILE,
        priority=100,
        timeout_seconds=900,
        force_rerun=force_rerun,
        supersede_previous=supersede_previous,
    )


def _events(job_id):
    rows = db._get_db().execute(
        "SELECT event_type, event_data, created_at FROM ci_job_events WHERE job_id = ? ORDER BY id",
        (job_id,),
    ).fetchall()
    return [
        {
            "event_type": row["event_type"],
            "event_data": json.loads(row["event_data"]),
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def _queue_state(repository=REPOSITORY):
    row = db._get_db().execute(
        "SELECT queued_jobs, running_jobs FROM ci_repository_queue_state WHERE repository = ?",
        (repository,),
    ).fetchone()
    return dict(row) if row else {"queued_jobs": 0, "running_jobs": 0}


async def _cancel(mcp, job_id):
    return _structured_result(
        await mcp.call_tool("cancel_private_ci_job", {"job_id": job_id})
    )


@pytest.mark.asyncio
async def test_cancel_requires_exact_existing_job_id(cancel_mcp, isolated_db):
    job = _create_job()

    result = await _cancel(cancel_mcp, "does-not-exist")

    assert result["error"]["code"] == "PRIVATE_CI_JOB_NOT_FOUND"
    assert db.get_job(job["job_id"])["status"] == "queued"
    assert _events(job["job_id"]) == []


@pytest.mark.asyncio
async def test_queued_cancel_is_terminal_accounted_and_not_leased(cancel_mcp, isolated_db):
    job = _create_job()
    assert _queue_state() == {"queued_jobs": 1, "running_jobs": 0}
    assert db.register_worker("worker-queued", "token", [PROFILE], 1)

    result = await _cancel(cancel_mcp, job["job_id"])
    cancelled = db.get_job(job["job_id"])

    assert {key: result[key] for key in ("ok", "status", "job_id")} == {
        "ok": True, "status": "cancelled", "job_id": job["job_id"]
    }
    assert cancelled["status"] == "cancelled"
    assert cancelled["finished_at"] is not None
    assert cancelled["worker_id"] is None
    assert _queue_state() == {"queued_jobs": 0, "running_jobs": 0}
    assert [event["event_type"] for event in _events(job["job_id"])] == ["cancelled"]
    assert db.lease_job("worker-queued", [PROFILE], 1) is None


@pytest.mark.asyncio
async def test_repeated_queued_cancel_has_no_new_state_or_event(cancel_mcp, isolated_db):
    job = _create_job()

    first = await _cancel(cancel_mcp, job["job_id"])
    after_first = db.get_job(job["job_id"])
    events_after_first = _events(job["job_id"])
    second = await _cancel(cancel_mcp, job["job_id"])
    after_second = db.get_job(job["job_id"])

    assert first["status"] == "cancelled"
    assert second["error"]["code"] == "PRIVATE_CI_JOB_ALREADY_FINISHED"
    assert after_second["status"] == after_first["status"] == "cancelled"
    assert after_second["finished_at"] == after_first["finished_at"]
    assert _events(job["job_id"]) == events_after_first
    assert _queue_state() == {"queued_jobs": 0, "running_jobs": 0}


@pytest.mark.parametrize("active_status", ["leased", "downloading", "preparing", "running"])
def test_active_cancel_is_observable_and_repeat_event_free(active_status, isolated_db):
    job = _create_job()
    token = f"lease-{active_status}"
    connection = db._get_db()
    connection.execute(
        """UPDATE ci_jobs
           SET status = ?, worker_id = ?, lease_token_hash = ?, lease_expires_at = ?
           WHERE job_id = ?""",
        (
            active_status,
            "worker-active",
            hashlib.sha256(token.encode()).hexdigest(),
            db.now_ts() + 120,
            job["job_id"],
        ),
    )
    connection.commit()

    assert db.request_cancel_job(job["job_id"]) is True
    first = db.get_job(job["job_id"])
    assert first["status"] == active_status
    assert first["cancel_requested"] is True
    assert db.need_heartbeat(job["job_id"]) is True
    assert [event["event_type"] for event in _events(job["job_id"])] == ["cancel_requested"]

    assert db.request_cancel_job(job["job_id"]) is False
    second = db.get_job(job["job_id"])
    assert second["status"] == active_status
    assert second["cancel_requested"] is True
    assert [event["event_type"] for event in _events(job["job_id"])] == ["cancel_requested"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_status",
    ["passed", "failed", "cancelled", "timed_out", "superseded", "worker_lost", "internal_error"],
)
async def test_terminal_cancel_is_immutable_and_truthful(cancel_mcp, isolated_db, terminal_status):
    job = _create_job()
    token_hash = hashlib.sha256(b"terminal-lease").hexdigest()
    connection = db._get_db()
    connection.execute(
        """UPDATE ci_jobs
           SET status = ?, exit_code = ?, finished_at = ?, worker_id = ?,
               lease_token_hash = ?, lease_expires_at = ?, superseded_by_job_id = ?
           WHERE job_id = ?""",
        (
            terminal_status,
            0 if terminal_status == "passed" else -1,
            1234.5,
            "worker-terminal",
            token_hash,
            5678.0,
            "replacement" if terminal_status == "superseded" else None,
            job["job_id"],
        ),
    )
    connection.commit()
    before = db.get_job(job["job_id"])
    events_before = _events(job["job_id"])

    result = await _cancel(cancel_mcp, job["job_id"])
    after = db.get_job(job["job_id"])

    assert result["error"]["code"] == "PRIVATE_CI_JOB_ALREADY_FINISHED"
    assert after["status"] == before["status"] == terminal_status
    assert after["finished_at"] == before["finished_at"]
    assert after["worker_id"] == before["worker_id"]
    assert _events(job["job_id"]) == events_before


def test_supersede_is_atomic_audited_and_queue_accounted(isolated_db, monkeypatch):
    old = _create_job("a" * 40)
    notified = []
    monkeypatch.setattr(db, "_notify_job_change", notified.append)

    new = _create_job("b" * 40, supersede_previous=True)

    old_row = db.get_job(old["job_id"])
    new_row = db.get_job(new["job_id"])
    events = _events(old["job_id"])

    assert old_row["status"] == "superseded"
    assert old_row["superseded_by_job_id"] == new["job_id"]
    assert old_row["finished_at"] is not None
    assert new_row["status"] == "queued"
    assert _queue_state() == {"queued_jobs": 1, "running_jobs": 0}
    assert notified == [old["job_id"]]
    assert [event["event_type"] for event in events] == ["superseded"]
    event = events[0]
    assert event["created_at"] is not None
    assert event["event_data"] == {
        "branch": BRANCH,
        "new_commit_sha": "b" * 40,
        "new_job_id": new["job_id"],
        "old_commit_sha": "a" * 40,
        "old_job_id": old["job_id"],
        "profile": PROFILE,
        "repository": REPOSITORY,
        "superseded_at": event["event_data"]["superseded_at"],
        "superseded_by_job_id": new["job_id"],
    }


def test_superseded_old_lease_callback_fails_closed(isolated_db):
    old = _create_job("a" * 40)
    token = "old-lease"
    connection = db._get_db()
    connection.execute(
        "UPDATE ci_jobs SET worker_id=?, lease_token_hash=?, lease_expires_at=? WHERE job_id=?",
        ("worker-old", hashlib.sha256(token.encode()).hexdigest(), db.now_ts() + 120, old["job_id"]),
    )
    connection.commit()
    new = _create_job("b" * 40, supersede_previous=True)

    with pytest.raises(db.StaleJobLeaseError):
        db.complete_job(
            old["job_id"], 0, "passed", {"status": "passed"},
            worker_id="worker-old", lease_token=token,
        )
    assert db.get_job(old["job_id"])["status"] == "superseded"
    assert db.get_job(old["job_id"])["superseded_by_job_id"] == new["job_id"]


def _create_linked_request():
    commit_sha = "c" * 40
    payload = {
        "schema": "private-ci-start-v2",
        "repository": REPOSITORY,
        "branch": BRANCH,
        "commit_sha": commit_sha,
        "tree_sha": "derived_from_exact_commit_during_preflight",
        "profile": PROFILE,
        "timeout_seconds": 900,
        "priority": 100,
        "base_sha": "",
        "supersede_previous": False,
    }
    request = requests.create_or_get_ci_request(
        repository=REPOSITORY,
        branch=BRANCH,
        commit_sha=commit_sha,
        tree_sha=None,
        profile=PROFILE,
        effective_config_digest="config-dev014",
        idempotency_key="dev014-snapshot",
        normalized_request_hash=requests.compute_normalized_request_hash(payload),
        request_payload=payload,
    )
    request = requests.transition_ci_request(
        request["request_id"], request["revision"], "preparing", "preparing"
    )
    return requests.dispatch_ci_request(
        request["request_id"],
        expected_revision=request["revision"],
        tree_sha=TREE,
        changed_files=[],
        changed_files_total=0,
        changed_files_truncated=False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("worker_status", ["cancelled", "superseded"])
async def test_request_worker_snapshot_projects_terminal_worker_truth(cancel_mcp, isolated_db, worker_status):
    queued = _create_linked_request()
    connection = db._get_db()
    connection.execute(
        "UPDATE ci_jobs SET status=?, finished_at=?, superseded_by_job_id=? WHERE job_id=?",
        (
            worker_status,
            db.now_ts(),
            "replacement-job" if worker_status == "superseded" else None,
            queued["worker_job_id"],
        ),
    )
    connection.commit()

    result = _structured_result(
        await cancel_mcp.call_tool(
            "get_private_ci_job", {"request_id": queued["request_id"]}
        )
    )
    request_row = requests.get_ci_request(queued["request_id"])

    assert request_row["status"] == request_row["phase"] == "queued"
    assert result["request_status"] == "queued"
    assert result["worker_status"] == worker_status
    assert result["status"] == worker_status
    assert result["phase"] == "terminal"
    assert result["terminal"] is True
    assert result["continuation_required"] is False


def test_cancelled_completion_releases_worker_and_fences_stale_callback(isolated_db):
    assert db.register_worker("worker-cancel", "token-cancel", [PROFILE], 1)
    job = _create_job()
    lease = db.lease_job("worker-cancel", [PROFILE], 1)
    assert lease["job_id"] == job["job_id"]
    assert db.request_cancel_job(job["job_id"]) is True

    assert db.complete_job(
        job["job_id"], -1, "cancelled", {"status": "cancelled"},
        worker_id="worker-cancel", lease_token=lease["lease_token"],
    ) is True
    finished = db.get_job(job["job_id"])
    worker = db.get_worker("worker-cancel")
    assert finished["status"] == "cancelled"
    assert finished["worker_id"] is None
    assert finished["cancel_requested"] is True
    assert worker["status"] == "idle"
    assert worker["current_job"] is None
    assert _queue_state() == {"queued_jobs": 0, "running_jobs": 0}

    with pytest.raises(db.StaleJobLeaseError):
        db.complete_job(
            job["job_id"], 0, "passed", {"status": "passed"},
            worker_id="worker-cancel", lease_token=lease["lease_token"],
        )
    assert db.get_job(job["job_id"])["status"] == "cancelled"


def _passed_job_with_evidence():
    summary = {
        "git_tree_sha": TREE,
        "image_digest": "sha256:image-set",
        "evidence": {
            "base_sha": "d" * 40,
            "changed_files": ["app.py"],
            "dependency_manifest_sha256": "deps",
            "test_config_sha256": "config",
            "source_immutable": True,
        },
    }
    job = _create_job("e" * 40)
    db.complete_job(job["job_id"], 0, "passed", summary)
    return job, summary


def test_superseded_passed_job_cannot_create_or_validate_attestation(isolated_db):
    job, _ = _passed_job_with_evidence()
    item = registry.create_attestation_for_passed_job(job_id=job["job_id"])
    connection = db._get_db()
    connection.execute(
        "UPDATE ci_jobs SET superseded_by_job_id=? WHERE job_id=?",
        ("new-head-job", job["job_id"]),
    )
    connection.commit()

    validation = registry.validate_attestation(item["attestation_id"])
    assert validation == {
        "ok": False,
        "error_code": "ATTESTATION_JOB_SUPERSEDED",
        "reusable": False,
    }
    with pytest.raises(ValueError, match="ATTESTATION_JOB_SUPERSEDED"):
        registry.create_attestation_for_passed_job(job_id=job["job_id"])


def test_superseded_passed_job_is_not_merge_eligible(monkeypatch, isolated_db):
    job, _ = _passed_job_with_evidence()
    job = {**db.get_job(job["job_id"]), "superseded_by_job_id": "new-head-job"}
    monkeypatch.setattr(orchestrator.sessions, "record_validation", lambda *args, **kwargs: 1)

    result = orchestrator.validation_result(
        "session-dev014", 1, "full", job, {"changed_files": []}, include_failure_pack=False
    )

    assert result["merge_eligible"] is False
    assert result["attestation"] is None


def test_superseded_job_is_rejected_by_pr_merge_gate(monkeypatch, isolated_db):
    head = "a" * 40
    base = {
        "ok": True, "state": "open", "merged": False, "draft": False,
        "base_branch": "main", "base_sha": "b" * 40, "head_branch": BRANCH,
        "head_sha": head, "mergeable": True, "mergeable_state": "clean",
        "review_decision": "APPROVED", "reviews": [], "requested_reviewers": [],
        "requested_teams": [],
    }
    monkeypatch.setattr(github_utils, "get_github_pull_request", lambda *args: base)
    monkeypatch.setattr(
        github_utils, "_get_gh",
        lambda: type("GH", (), {"get_repo": lambda *_: type("Repo", (), {"allow_squash_merge": True})()})(),
    )
    monkeypatch.setattr(github_utils, "_review_policy", lambda *args: {"required_approvals": 0, "current_approvals": 0, "source": "none", "changes_requested": False})
    monkeypatch.setattr(github_utils, "get_github_pull_request_checks", lambda *args: {"ok": True, "checks": [], "statuses": [], "overall_conclusion": "neutral", "required_check_sources": {"errors": []}})
    monkeypatch.setattr(github_utils, "get_github_repository", lambda *args: {"allow_squash_merge": True})
    monkeypatch.setattr(
        github_utils,
        "_private_ci_job",
        lambda *_: {
            "repository": REPOSITORY, "branch": BRANCH, "commit_sha": head,
            "profile": PROFILE, "status": "passed", "exit_code": 0,
            "superseded_by_job_id": "new-head-job",
        },
    )

    result = github_utils._readiness(REPOSITORY, 14, head, "old-job")

    assert result["ready"] is False
    assert "PRIVATE_CI_SUPERSEDED" in result["blocking"]


def test_superseded_job_is_rejected_as_release_artifact_evidence(tmp_path, monkeypatch, isolated_db):
    archive = tmp_path / "release.tar.zst"
    manifest = tmp_path / "manifest.json"
    checksums = tmp_path / "checksums.sha256"
    provenance = tmp_path / "provenance.json"
    archive.write_bytes(b"archive")
    manifest.write_text("{}", encoding="utf-8")
    checksums.write_text("", encoding="utf-8")
    provenance.write_text("{}", encoding="utf-8")
    sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    item = {
        "artifact_id": "artifact-old-job",
        "status": "ready",
        "expires_at": time.time() + 3600,
        "storage_path": str(archive),
        "archive_sha256": sha(archive),
        "archive_size_bytes": archive.stat().st_size,
        "manifest_sha256": sha(manifest),
        "checksums_sha256": sha(checksums),
        "provenance_sha256": "",
        "repository": REPOSITORY,
        "branch": BRANCH,
        "commit_sha": "a" * 40,
        "tree_sha": TREE,
        "private_ci_job_id": "old-job",
        "source_attestation_id": "att-old",
    }
    monkeypatch.setenv("ARTIFACT_STORAGE_ROOT", str(tmp_path))
    monkeypatch.setattr(registry, "get_artifact", lambda _: item)
    monkeypatch.setattr(registry, "_verify_archive", lambda _: (True, ""))
    monkeypatch.setattr(
        registry,
        "get_job",
        lambda _: {"status": "passed", "exit_code": 0, "superseded_by_job_id": "new-head-job"},
    )

    result = registry.validate_artifact(
        item["artifact_id"], repository=REPOSITORY, branch=BRANCH,
        commit_sha=item["commit_sha"], tree_sha=TREE, private_ci_job_id="old-job",
    )

    assert result == {"ok": False, "error_code": "ARTIFACT_CI_SUPERSEDED"}
