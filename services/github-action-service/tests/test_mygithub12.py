import json

import pytest

from app import mygithub12


def test_safe_path_rejects_escape_and_absolute_paths():
    for value in ("../secret", "/etc/passwd", "a\\b"):
        with pytest.raises(mygithub12.MyGithub12Error) as exc:
            mygithub12._safe_path(value)
        assert exc.value.code == "INVALID_REPOSITORY_PATH"


def test_python_symbol_ids_are_stable_and_qualified():
    content = "class Camera:\n    def bind(self):\n        return True\n"
    first = mygithub12._symbols("o/r", "a" * 40, "camera.py", "b" * 40, "python", content)
    second = mygithub12._symbols("o/r", "a" * 40, "camera.py", "b" * 40, "python", content)
    assert first == second
    assert [item["qualified_name"] for item in first] == ["Camera", "Camera.bind"]
    assert len({item["symbol_id"] for item in first}) == 2


def test_index_database_schema_and_job_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "index.db"))
    mygithub12.init_db()
    with mygithub12._db() as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"indexes", "files", "symbols", "jobs", "workspaces"} <= tables


def test_request_index_build_persists_queued_job(tmp_path, monkeypatch):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "index.db"))
    identity = {
        "repository": "o/r",
        "commit_sha": "a" * 40,
        "tree_sha": "b" * 40,
    }
    monkeypatch.setattr(mygithub12, "resolve_identity", lambda *args, **kwargs: identity)

    class FakeThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(mygithub12.threading, "Thread", FakeThread)

    result = mygithub12.request_index_build(
        object(),
        "o/r",
        "a" * 40,
        idempotency_key="index-job-test",
    )

    assert result["ok"] is True
    assert result["status"] == "queued"
    assert result["job_id"]
    with mygithub12._db() as db:
        row = db.execute("SELECT * FROM jobs WHERE job_id=?", (result["job_id"],)).fetchone()
    assert row["repository"] == "o/r"
    assert row["idempotency_key"] == "index-job-test"


def test_public_job_does_not_expose_idempotency_key():
    row = {
        "job_id": "job",
        "repository": "o/r",
        "commit_sha": "a" * 40,
        "tree_sha": "b" * 40,
        "version": mygithub12.INDEX_VERSION,
        "strategy": "incremental",
        "base_commit_sha": "c" * 40,
        "status": "completed",
        "step": "completed",
        "revision": 2,
        "progress_current": 3,
        "progress_total": 3,
        "reused_files": 2,
        "reindexed_files": 1,
        "idempotency_key": "secret-ish-key",
    }
    result = mygithub12._public_job(row)
    assert result["status"] == "completed"
    assert "idempotency_key" not in result
    json.dumps(result)


def test_workspace_is_required_for_ai_writes_when_enabled(monkeypatch):
    monkeypatch.setenv("REQUIRE_WORKSPACE_FOR_AI_WRITES", "true")
    with pytest.raises(mygithub12.MyGithub12Error) as exc:
        mygithub12.workspace_write_preflight(object(), "o/r", "ai/task", "a" * 40)
    assert exc.value.code == "WORKSPACE_LEASE_REQUIRED"


def test_workspace_completion_uses_revision_cas(tmp_path, monkeypatch):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "workspace.db"))
    mygithub12.init_db()
    timestamp = mygithub12._now()
    with mygithub12._db() as db:
        db.execute(
            "INSERT INTO workspaces VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "ws_test", "o/r", "ai/task", "main", "a" * 40,
                "a" * 40, "b" * 40, "active", 1, "test", timestamp + 600,
                "a" * 40, "{}", None, None, timestamp, timestamp,
            ),
        )
    result = mygithub12.workspace_write_complete("ws_test", 1, "c" * 40, "d" * 40)
    assert result["revision"] == 2
    assert result["head_sha"] == "c" * 40
    with pytest.raises(mygithub12.MyGithub12Error) as exc:
        mygithub12.workspace_write_complete("ws_test", 1, "e" * 40, "f" * 40)
    assert exc.value.code == "WORKSPACE_REVISION_MISMATCH"
