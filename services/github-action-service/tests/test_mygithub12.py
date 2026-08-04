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
