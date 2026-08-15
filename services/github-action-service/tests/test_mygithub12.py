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
    verification = {
        "write_verified": True, "repository": "o/r", "branch": "ai/task",
        "verified_branch_head_sha": "c" * 40, "verified_commit_sha": "c" * 40,
        "verified_tree_sha": "d" * 40,
    }
    result = mygithub12.workspace_write_complete("ws_test", 1, "c" * 40, "d" * 40, verification)
    assert result["revision"] == 2
    assert result["head_sha"] == "c" * 40
    with pytest.raises(mygithub12.MyGithub12Error) as exc:
        stale_verification = {**verification, "verified_branch_head_sha": "e" * 40, "verified_commit_sha": "e" * 40, "verified_tree_sha": "f" * 40}
        mygithub12.workspace_write_complete("ws_test", 1, "e" * 40, "f" * 40, stale_verification)
    assert exc.value.code == "WORKSPACE_REVISION_MISMATCH"
    assert exc.value.details["github_write_verified"] is True


def _seed_symbol_index(tmp_path, monkeypatch, files):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "symbols.db"))
    mygithub12.init_db()
    repository = "o/r"
    commit_sha = "a" * 40
    identity = {"repository": repository, "commit_sha": commit_sha, "tree_sha": "b" * 40}
    symbols = {}
    with mygithub12._db() as db:
        for index, (path, content) in enumerate(files, 1):
            blob_sha = f"{index:040x}"
            digest = f"{index:064x}"
            db.execute(
                "INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?)",
                (repository, commit_sha, path, blob_sha, len(content.encode()), "python", digest, len(content.splitlines()), content),
            )
            for symbol in mygithub12._symbols(repository, commit_sha, path, blob_sha, "python", content):
                db.execute(
                    "INSERT INTO symbols VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        repository, commit_sha, symbol["symbol_id"], symbol["name"],
                        symbol["qualified_name"], symbol["kind"], symbol["language"],
                        symbol["path"], symbol["blob_sha"], symbol["start_line"],
                        symbol["end_line"], symbol["signature"], symbol["parent_name"],
                        symbol["bases_json"],
                    ),
                )
                symbols[(path, symbol["qualified_name"])] = symbol
    monkeypatch.setattr(mygithub12, "_ready", lambda *args, **kwargs: identity)
    return repository, commit_sha, symbols


def test_find_references_distinguishes_declarations_and_qualified_calls(tmp_path, monkeypatch):
    repository, commit_sha, symbols = _seed_symbol_index(
        tmp_path,
        monkeypatch,
        [(
            "main.py",
            "def target():\n    return True\n\n\nclass Client:\n    def target(self):\n        return True\n\n\ndef caller(client):\n    client.target()\n    return target()\n",
        )],
    )
    target = symbols[("main.py", "target")]
    caller = symbols[("main.py", "caller")]

    result = mygithub12.find_references(
        object(), repository, commit_sha, target["symbol_id"], include_definition=True
    )

    assert [(item["line"], item["reference_kind"]) for item in result["items"]] == [
        (1, "definition"),
        (11, "unknown"),
        (12, "call"),
    ]
    hierarchy = mygithub12.call_hierarchy(
        object(), repository, commit_sha, target["symbol_id"], direction="callers"
    )
    assert [(edge["from"], edge["line"]) for edge in hierarchy["edges"]] == [
        (caller["symbol_id"], 12),
    ]


def test_call_hierarchy_resolves_only_unambiguous_local_callees(tmp_path, monkeypatch):
    repository, commit_sha, symbols = _seed_symbol_index(
        tmp_path,
        monkeypatch,
        [
            ("other.py", "def helper():\n    return 2\n"),
            (
                "main.py",
                "def helper():\n    return 1\n\n\ndef execute():\n    return 0\n\n\ndef root():\n    db.execute()\n    return helper()\n",
            ),
        ],
    )
    root = symbols[("main.py", "root")]
    local_helper = symbols[("main.py", "helper")]
    external_helper = symbols[("other.py", "helper")]
    local_execute = symbols[("main.py", "execute")]

    result = mygithub12.call_hierarchy(
        object(), repository, commit_sha, root["symbol_id"], direction="callees"
    )

    targets = {edge["to"] for edge in result["edges"]}
    assert targets == {local_helper["symbol_id"]}
    assert external_helper["symbol_id"] not in targets
    assert local_execute["symbol_id"] not in targets


def test_call_hierarchy_skips_calls_inside_nested_declarations(tmp_path, monkeypatch):
    repository, commit_sha, symbols = _seed_symbol_index(
        tmp_path,
        monkeypatch,
        [(
            "main.py",
            "def helper():\n    return 1\n\n\ndef root():\n    def nested():\n        return helper()\n    return 0\n",
        )],
    )
    root = symbols[("main.py", "root")]

    result = mygithub12.call_hierarchy(
        object(), repository, commit_sha, root["symbol_id"], direction="callees"
    )

    assert result["edges"] == []


def test_call_hierarchy_resolves_python_self_and_module_calls(tmp_path, monkeypatch):
    repository, commit_sha, symbols = _seed_symbol_index(
        tmp_path,
        monkeypatch,
        [(
            "main.py",
            "def module_helper():\n    return 1\n\n\ndef execute():\n    return 0\n\n\nclass Worker:\n    def local(self):\n        return 2\n\n    def root(self):\n        self.local()\n        db.execute()\n        return module_helper()\n",
        )],
    )
    root = symbols[("main.py", "Worker.root")]
    local = symbols[("main.py", "Worker.local")]
    module_helper = symbols[("main.py", "module_helper")]
    execute = symbols[("main.py", "execute")]

    result = mygithub12.call_hierarchy(
        object(), repository, commit_sha, root["symbol_id"], direction="callees"
    )

    targets = {edge["to"] for edge in result["edges"]}
    assert targets == {local["symbol_id"], module_helper["symbol_id"]}
    assert execute["symbol_id"] not in targets
