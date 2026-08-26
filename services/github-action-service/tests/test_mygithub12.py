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


def _seed_retention_snapshot(db, repository: str, commit_sha: str, order: int):
    tree_sha = f"{order + 1000:040x}"
    content = f"def f_{order}():\n    return {order}\n"
    blob_sha = f"{order + 2000:040x}"
    now = float(order)
    db.execute(
        "INSERT INTO indexes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (repository, commit_sha, tree_sha, mygithub12.INDEX_VERSION, "ready", "full", None, 1, 1, len(content), now, now, "{}"),
    )
    db.execute(
        "INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?)",
        (repository, commit_sha, f"f{order}.py", blob_sha, len(content), "python", f"{order:064x}", 2, content),
    )
    symbol = mygithub12._symbols(repository, commit_sha, f"f{order}.py", blob_sha, "python", content)[0]
    db.execute(
        "INSERT INTO symbols VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            repository, commit_sha, symbol["symbol_id"], symbol["name"], symbol["qualified_name"],
            symbol["kind"], symbol["language"], symbol["path"], symbol["blob_sha"],
            symbol["start_line"], symbol["end_line"], symbol["signature"], symbol["parent_name"],
            symbol["bases_json"],
        ),
    )


def test_index_retention_prunes_lru_but_preserves_workspace_pins(tmp_path, monkeypatch):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "retention.db"))
    mygithub12.init_db()
    repository = "o/r"
    commits = [f"{i:040x}" for i in range(1, 6)]
    with mygithub12._db() as db:
        for order, commit_sha in enumerate(commits, 1):
            _seed_retention_snapshot(db, repository, commit_sha, order)
        db.execute(
            "INSERT INTO workspaces VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "ws-retention", repository, "ai/task", "main", commits[0], commits[0],
                "f" * 40, "active", 1, "test", mygithub12._now() + 600,
                commits[0], "{}", None, None, mygithub12._now(), mygithub12._now(),
            ),
        )

    result = mygithub12.prune_repository_indexes(repository, keep_limit=2)

    assert result == {
        "repository": repository,
        "retention_limit": 2,
        "pruned_commits": 2,
        "pruned_files": 2,
        "pruned_symbols": 2,
    }
    with mygithub12._db() as db:
        remaining = {row[0] for row in db.execute("SELECT commit_sha FROM indexes WHERE repository=?", (repository,))}
        assert remaining == {commits[0], commits[3], commits[4]}
        assert db.execute("SELECT COUNT(*) FROM files WHERE repository=?", (repository,)).fetchone()[0] == 3
        assert db.execute("SELECT COUNT(*) FROM symbols WHERE repository=?", (repository,)).fetchone()[0] == 3


def test_index_retention_preserves_active_job_target_and_base(tmp_path, monkeypatch):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "retention-job.db"))
    mygithub12.init_db()
    repository = "o/r"
    commits = [f"{i:040x}" for i in range(11, 15)]
    with mygithub12._db() as db:
        for order, commit_sha in enumerate(commits, 1):
            _seed_retention_snapshot(db, repository, commit_sha, order)
        db.execute(
            """INSERT INTO jobs(job_id,repository,commit_sha,tree_sha,version,strategy,base_commit_sha,status,step,revision,
               progress_current,progress_total,reused_files,reindexed_files,cancel_requested,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("job-retention", repository, commits[0], "a" * 40, mygithub12.INDEX_VERSION, "incremental", commits[1], "running", "indexing", 1, 0, 1, 0, 0, 0, mygithub12._now()),
        )

    result = mygithub12.prune_repository_indexes(repository, keep_limit=1)

    assert result["pruned_commits"] == 1
    with mygithub12._db() as db:
        remaining = {row[0] for row in db.execute("SELECT commit_sha FROM indexes WHERE repository=?", (repository,))}
    assert remaining == {commits[0], commits[1], commits[3]}


def test_index_retention_zero_disables_pruning(tmp_path, monkeypatch):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "retention-disabled.db"))
    mygithub12.init_db()
    repository = "o/r"
    with mygithub12._db() as db:
        for order in range(1, 4):
            _seed_retention_snapshot(db, repository, f"{order:040x}", order)

    result = mygithub12.prune_repository_indexes(repository, keep_limit=0)

    assert result["pruned_commits"] == 0
    with mygithub12._db() as db:
        assert db.execute("SELECT COUNT(*) FROM indexes WHERE repository=?", (repository,)).fetchone()[0] == 3


def test_recover_orphaned_index_jobs_fails_previous_process_work(tmp_path, monkeypatch):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "orphaned-jobs.db"))
    mygithub12.init_db()
    now = mygithub12._now()
    with mygithub12._db() as db:
        for job_id, status in (("queued-old", "queued"), ("running-old", "running"), ("done", "completed")):
            db.execute(
                """INSERT INTO jobs(job_id,repository,commit_sha,tree_sha,version,strategy,status,step,revision,
                   progress_current,progress_total,reused_files,reindexed_files,cancel_requested,created_at,started_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (job_id, "o/r", (job_id[0] * 40), "t" * 40, mygithub12.INDEX_VERSION, "auto", status, status, 1, 0, 1, 0, 0, 0, now, now),
            )

    result = mygithub12.recover_orphaned_index_jobs()

    assert result == {"recovered_jobs": 2, "queued_jobs": 1, "running_jobs": 1}
    with mygithub12._db() as db:
        rows = {row["job_id"]: row for row in db.execute("SELECT * FROM jobs")}
    for job_id in ("queued-old", "running-old"):
        assert rows[job_id]["status"] == "failed"
        assert rows[job_id]["step"] == "failed"
        assert rows[job_id]["error_code"] == "INDEX_CONTROLLER_RESTARTED"
        assert rows[job_id]["finished_at"] is not None
    assert rows["done"]["status"] == "completed"


def _seed_workspace_pin(
    db,
    *,
    workspace_id: str,
    repository: str,
    branch: str,
    status: str,
    lease_expires_at: float,
    index_commit_sha: str,
    base_commit_sha: str,
    head_sha: str,
):
    db.execute(
        "INSERT INTO workspaces VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            workspace_id,
            repository,
            branch,
            "main",
            base_commit_sha,
            head_sha,
            "f" * 40,
            status,
            1,
            "test",
            lease_expires_at,
            index_commit_sha,
            "{}",
            None,
            None,
            1.0,
            1.0,
        ),
    )


def test_expired_workspace_pin_grace_defaults_and_bounds(monkeypatch):
    monkeypatch.delenv("MYGITHUB12_EXPIRED_WORKSPACE_PIN_GRACE_SECONDS", raising=False)
    assert (
        mygithub12._expired_workspace_pin_grace_seconds()
        == mygithub12.DEFAULT_EXPIRED_WORKSPACE_PIN_GRACE_SECONDS
    )

    monkeypatch.setenv("MYGITHUB12_EXPIRED_WORKSPACE_PIN_GRACE_SECONDS", "0")
    assert mygithub12._expired_workspace_pin_grace_seconds() == 0

    monkeypatch.setenv("MYGITHUB12_EXPIRED_WORKSPACE_PIN_GRACE_SECONDS", "999999999")
    assert (
        mygithub12._expired_workspace_pin_grace_seconds()
        == mygithub12.MAX_EXPIRED_WORKSPACE_PIN_GRACE_SECONDS
    )

    monkeypatch.setenv("MYGITHUB12_EXPIRED_WORKSPACE_PIN_GRACE_SECONDS", "invalid")
    assert (
        mygithub12._expired_workspace_pin_grace_seconds()
        == mygithub12.DEFAULT_EXPIRED_WORKSPACE_PIN_GRACE_SECONDS
    )


def test_workspace_index_pin_grace_is_independent_from_write_status(monkeypatch):
    monkeypatch.setenv("MYGITHUB12_EXPIRED_WORKSPACE_PIN_GRACE_SECONDS", "3600")
    now = 10_000.0

    assert mygithub12._workspace_index_pin_active("active", now + 1, now=now) is True
    assert mygithub12._workspace_index_pin_active("active", now - 1800, now=now) is True
    assert mygithub12._workspace_index_pin_active("active", now - 3601, now=now) is False
    assert mygithub12._workspace_index_pin_active("drifted", now - 1800, now=now) is True
    assert mygithub12._workspace_index_pin_active("closed", now + 3600, now=now) is False


def test_retention_only_protects_workspace_commits_inside_pin_grace(tmp_path, monkeypatch):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "workspace-pin-grace.db"))
    monkeypatch.setenv("MYGITHUB12_EXPIRED_WORKSPACE_PIN_GRACE_SECONDS", "3600")
    mygithub12.init_db()
    repository = "o/r"
    now = 10_000.0
    warm = [f"{i:040x}" for i in (101, 102, 103)]
    stale = [f"{i:040x}" for i in (201, 202, 203)]
    drifted = [f"{i:040x}" for i in (301, 302, 303)]
    closed = [f"{i:040x}" for i in (401, 402, 403)]
    job_target = "5" * 40
    job_base = "6" * 40

    with mygithub12._db() as db:
        _seed_workspace_pin(
            db,
            workspace_id="warm",
            repository=repository,
            branch="ai/warm",
            status="active",
            lease_expires_at=now - 1800,
            index_commit_sha=warm[0],
            base_commit_sha=warm[1],
            head_sha=warm[2],
        )
        _seed_workspace_pin(
            db,
            workspace_id="stale",
            repository=repository,
            branch="ai/stale",
            status="active",
            lease_expires_at=now - 7200,
            index_commit_sha=stale[0],
            base_commit_sha=stale[1],
            head_sha=stale[2],
        )
        _seed_workspace_pin(
            db,
            workspace_id="drifted",
            repository=repository,
            branch="ai/drifted",
            status="drifted",
            lease_expires_at=now - 60,
            index_commit_sha=drifted[0],
            base_commit_sha=drifted[1],
            head_sha=drifted[2],
        )
        _seed_workspace_pin(
            db,
            workspace_id="closed",
            repository=repository,
            branch="ai/closed",
            status="closed",
            lease_expires_at=now + 7200,
            index_commit_sha=closed[0],
            base_commit_sha=closed[1],
            head_sha=closed[2],
        )
        db.execute(
            """INSERT INTO jobs(job_id,repository,commit_sha,tree_sha,version,strategy,base_commit_sha,status,step,revision,
               progress_current,progress_total,reused_files,reindexed_files,cancel_requested,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "job-active-pin",
                repository,
                job_target,
                "7" * 40,
                mygithub12.INDEX_VERSION,
                "incremental",
                job_base,
                "running",
                "indexing",
                1,
                0,
                1,
                0,
                0,
                0,
                now,
            ),
        )
        workspace_pins = mygithub12._workspace_protected_index_commits(
            db, repository, now=now
        )
        all_pins = workspace_pins | mygithub12._active_index_job_commits(db, repository)

    assert workspace_pins == set(warm + drifted)
    assert not (set(stale) & workspace_pins)
    assert not (set(closed) & workspace_pins)
    assert {job_target, job_base} <= all_pins


def test_list_indexes_reports_the_same_grace_aware_workspace_pin_state(tmp_path, monkeypatch):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "workspace-pin-report.db"))
    monkeypatch.setenv("MYGITHUB12_EXPIRED_WORKSPACE_PIN_GRACE_SECONDS", "100")
    monkeypatch.setattr(mygithub12, "_now", lambda: 1000.0)
    monkeypatch.setattr(mygithub12, "_service_repo", lambda *args, **kwargs: object())
    mygithub12.init_db()
    repository = "o/r"
    recent_commit = "1" * 40
    stale_commit = "2" * 40

    with mygithub12._db() as db:
        _seed_retention_snapshot(db, repository, recent_commit, 1)
        _seed_retention_snapshot(db, repository, stale_commit, 2)
        _seed_workspace_pin(
            db,
            workspace_id="recent-expiry",
            repository=repository,
            branch="ai/recent-expiry",
            status="active",
            lease_expires_at=950.0,
            index_commit_sha=recent_commit,
            base_commit_sha=recent_commit,
            head_sha=recent_commit,
        )
        _seed_workspace_pin(
            db,
            workspace_id="stale-expiry",
            repository=repository,
            branch="ai/stale-expiry",
            status="active",
            lease_expires_at=800.0,
            index_commit_sha=stale_commit,
            base_commit_sha=stale_commit,
            head_sha=stale_commit,
        )

    result = mygithub12.list_indexes(object(), repository, limit=10)
    items = {item["commit_sha"]: item for item in result["items"]}

    assert items[recent_commit]["pinned_by_workspace"] is True
    assert items[stale_commit]["pinned_by_workspace"] is False


class _FastPathEntry:
    def __init__(self, path: str, sha: str, size: int):
        self.path = path
        self.sha = sha
        self.size = size
        self.type = "blob"


class _FastPathRepo:
    def __init__(self, entries):
        self.entries = entries
        self.tree_calls = 0

    def get_git_tree(self, tree_sha, recursive=True):
        self.tree_calls += 1
        return type("Tree", (), {"tree": self.entries})()


def _seed_fast_path_job(db, *, job_id: str, repository: str, commit_sha: str, tree_sha: str, strategy: str, base_commit_sha: str | None = None):
    db.execute(
        """INSERT INTO jobs(job_id,repository,commit_sha,tree_sha,version,strategy,base_commit_sha,status,step,revision,
           progress_current,progress_total,reused_files,reindexed_files,cancel_requested,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            job_id,
            repository,
            commit_sha,
            tree_sha,
            mygithub12.INDEX_VERSION,
            strategy,
            base_commit_sha,
            "queued",
            "queued",
            1,
            0,
            0,
            0,
            0,
            0,
            mygithub12._now(),
        ),
    )


def _seed_fast_path_snapshot(
    db,
    *,
    repository: str,
    commit_sha: str,
    tree_sha: str,
    path: str = "code.py",
    blob_sha: str = "b" * 40,
    content: str = "def run():\n    return 1\n",
    version: str | None = None,
):
    version = version or mygithub12.INDEX_VERSION
    symbols = mygithub12._symbols(repository, commit_sha, path, blob_sha, "python", content)
    manifest = {
        "repository": repository,
        "commit_sha": commit_sha,
        "tree_sha": tree_sha,
        "index_version": version,
        "file_count": 1,
        "symbol_count": len(symbols),
        "size_bytes": len(content.encode()),
        "languages": ["python"],
        "reused_file_count": 0,
        "reindexed_file_count": 1,
        "build_strategy": "full",
    }
    db.execute(
        "INSERT INTO indexes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            repository,
            commit_sha,
            tree_sha,
            version,
            "ready",
            "full",
            None,
            1,
            len(symbols),
            len(content.encode()),
            mygithub12._now(),
            mygithub12._now(),
            json.dumps(manifest),
        ),
    )
    db.execute(
        "INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?)",
        (
            repository,
            commit_sha,
            path,
            blob_sha,
            len(content.encode()),
            "python",
            "d" * 64,
            len(content.splitlines()),
            content,
        ),
    )
    for symbol in symbols:
        db.execute(
            "INSERT INTO symbols VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                repository,
                commit_sha,
                symbol["symbol_id"],
                symbol["name"],
                symbol["qualified_name"],
                symbol["kind"],
                symbol["language"],
                symbol["path"],
                symbol["blob_sha"],
                symbol["start_line"],
                symbol["end_line"],
                symbol["signature"],
                symbol["parent_name"],
                symbol["bases_json"],
            ),
        )
    return symbols


def test_binary_paths_are_filtered_before_blob_fetch_and_telemetry_is_exposed(tmp_path, monkeypatch):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "binary-fast-path.db"))
    mygithub12.init_db()
    repository = "o/r"
    target = "1" * 40
    tree_sha = "2" * 40
    entries = [
        _FastPathEntry("code.py", "3" * 40, 24),
        _FastPathEntry("assets/logo.png", "4" * 40, 1024),
        _FastPathEntry("gradle/wrapper/gradle-wrapper.jar", "5" * 40, 4096),
    ]
    repo = _FastPathRepo(entries)
    monkeypatch.setattr(mygithub12, "_service_repo", lambda *args, **kwargs: repo)
    calls = []

    def fake_decode(_repo, sha):
        calls.append(sha)
        assert sha == "3" * 40
        return b"def run():\n    return 1\n"

    monkeypatch.setattr(mygithub12, "_decode_blob", fake_decode)
    with mygithub12._db() as db:
        _seed_fast_path_job(
            db,
            job_id="binary-job",
            repository=repository,
            commit_sha=target,
            tree_sha=tree_sha,
            strategy="full",
        )

    mygithub12._run_index_build(
        object(),
        {"repository": repository, "commit_sha": target, "tree_sha": tree_sha},
        "binary-job",
        "full",
        "",
    )

    with mygithub12._db() as db:
        job = db.execute("SELECT * FROM jobs WHERE job_id='binary-job'").fetchone()
        files = db.execute("SELECT path FROM files WHERE repository=? AND commit_sha=?", (repository, target)).fetchall()
        index = db.execute("SELECT manifest_json FROM indexes WHERE repository=? AND commit_sha=?", (repository, target)).fetchone()
    manifest = json.loads(index["manifest_json"])
    assert job["status"] == "completed"
    assert job["progress_total"] == 1
    assert [row["path"] for row in files] == ["code.py"]
    assert calls == ["3" * 40]
    assert manifest["binary_path_skipped_count"] == 2
    assert manifest["changed_blob_entries"] == 1
    assert manifest["blob_fetch_requests"] == 1
    assert manifest["build_strategy"] == "full"
    assert manifest["timings_ms"]["build_total_ms"] >= 0
    assert manifest["timings_ms"]["retention_ms"] >= 0


def test_unknown_non_utf8_content_is_still_safely_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "unknown-binary.db"))
    mygithub12.init_db()
    repository = "o/r"
    target = "6" * 40
    tree_sha = "7" * 40
    repo = _FastPathRepo([_FastPathEntry("payload.unknown", "8" * 40, 2)])
    monkeypatch.setattr(mygithub12, "_service_repo", lambda *args, **kwargs: repo)
    monkeypatch.setattr(mygithub12, "_decode_blob", lambda *args, **kwargs: b"\xff\xfe")
    with mygithub12._db() as db:
        _seed_fast_path_job(
            db,
            job_id="unknown-binary-job",
            repository=repository,
            commit_sha=target,
            tree_sha=tree_sha,
            strategy="full",
        )

    mygithub12._run_index_build(
        object(),
        {"repository": repository, "commit_sha": target, "tree_sha": tree_sha},
        "unknown-binary-job",
        "full",
        "",
    )

    with mygithub12._db() as db:
        index = db.execute("SELECT manifest_json FROM indexes WHERE repository=? AND commit_sha=?", (repository, target)).fetchone()
        file_count = db.execute("SELECT COUNT(*) FROM files WHERE repository=? AND commit_sha=?", (repository, target)).fetchone()[0]
    manifest = json.loads(index["manifest_json"])
    assert file_count == 0
    assert manifest["decode_skipped_count"] == 1
    assert manifest["binary_path_skipped_count"] == 0


def test_index_job_pulse_batches_progress_and_detects_cancel(tmp_path, monkeypatch):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "pulse.db"))
    monkeypatch.setenv("MYGITHUB12_INDEX_PROGRESS_BATCH_FILES", "32")
    monkeypatch.setenv("MYGITHUB12_INDEX_PROGRESS_INTERVAL_SECONDS", "5")
    monkeypatch.setattr(mygithub12.time, "monotonic", lambda: 100.0)
    mygithub12.init_db()
    with mygithub12._db() as db:
        _seed_fast_path_job(
            db,
            job_id="pulse-job",
            repository="o/r",
            commit_sha="9" * 40,
            tree_sha="a" * 40,
            strategy="full",
        )
    pulse = mygithub12._IndexJobPulse("pulse-job")
    for current in range(1, 101):
        assert pulse.pulse(current) is False
    assert pulse.pulse(100, force=True) is False
    with mygithub12._db() as db:
        row = db.execute("SELECT revision,progress_current FROM jobs WHERE job_id='pulse-job'").fetchone()
    assert row["progress_current"] == 100
    assert row["revision"] == 5
    with mygithub12._db() as db:
        db.execute("UPDATE jobs SET cancel_requested=1 WHERE job_id='pulse-job'")
    assert pulse.pulse(100, force=True) is True


def test_incremental_base_requires_current_version_ready_index(tmp_path, monkeypatch):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "base-version.db"))
    mygithub12.init_db()
    repository = "o/r"
    base = "b" * 40
    with mygithub12._db() as db:
        _seed_fast_path_snapshot(
            db,
            repository=repository,
            commit_sha=base,
            tree_sha="c" * 40,
            version="old-index-version",
        )
    files, symbols = mygithub12._load_incremental_base(repository, base)
    assert files == {}
    assert symbols == {}


def test_unchanged_blob_reuses_symbol_metadata_with_target_commit_id(tmp_path, monkeypatch):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "symbol-reuse.db"))
    mygithub12.init_db()
    repository = "o/r"
    base = "c" * 40
    target = "d" * 40
    blob = "e" * 40
    content = "class Camera:\n    def bind(self):\n        return True\n"
    with mygithub12._db() as db:
        base_symbols = _seed_fast_path_snapshot(
            db,
            repository=repository,
            commit_sha=base,
            tree_sha="1" * 40,
            path="camera.py",
            blob_sha=blob,
            content=content,
        )
        _seed_fast_path_job(
            db,
            job_id="symbol-reuse-job",
            repository=repository,
            commit_sha=target,
            tree_sha="2" * 40,
            strategy="incremental",
            base_commit_sha=base,
        )
    repo = _FastPathRepo([_FastPathEntry("camera.py", blob, len(content.encode()))])
    monkeypatch.setattr(mygithub12, "_service_repo", lambda *args, **kwargs: repo)
    monkeypatch.setattr(
        mygithub12,
        "_symbols",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unchanged file must not be reparsed")),
    )

    mygithub12._run_index_build(
        object(),
        {"repository": repository, "commit_sha": target, "tree_sha": "2" * 40},
        "symbol-reuse-job",
        "incremental",
        base,
    )

    with mygithub12._db() as db:
        job = db.execute("SELECT * FROM jobs WHERE job_id='symbol-reuse-job'").fetchone()
        target_symbols = db.execute(
            "SELECT * FROM symbols WHERE repository=? AND commit_sha=? ORDER BY start_line",
            (repository, target),
        ).fetchall()
        manifest = json.loads(db.execute(
            "SELECT manifest_json FROM indexes WHERE repository=? AND commit_sha=?",
            (repository, target),
        ).fetchone()[0])
    assert job["status"] == "completed"
    assert job["reused_files"] == 1
    assert job["reindexed_files"] == 0
    assert len(target_symbols) == len(base_symbols)
    for base_symbol, target_symbol in zip(base_symbols, target_symbols):
        assert target_symbol["name"] == base_symbol["name"]
        assert target_symbol["qualified_name"] == base_symbol["qualified_name"]
        assert target_symbol["symbol_id"] == mygithub12._symbol_id(
            repository,
            target,
            target_symbol["path"],
            target_symbol["name"],
            target_symbol["kind"],
            target_symbol["start_line"],
        )
        assert target_symbol["symbol_id"] != base_symbol["symbol_id"]
    assert manifest["build_strategy"] == "incremental"
    assert manifest["blob_fetch_requests"] == 0


def test_blob_prefetch_deduplicates_sha_and_worker_setting_is_bounded(monkeypatch):
    entries = [
        _FastPathEntry("a.py", "1" * 40, 1),
        _FastPathEntry("b.py", "1" * 40, 1),
        _FastPathEntry("c.py", "2" * 40, 1),
    ]
    calls = []
    monkeypatch.setenv("MYGITHUB12_INDEX_BLOB_FETCH_WORKERS", "99")
    assert mygithub12._index_blob_fetch_workers() == mygithub12.MAX_INDEX_BLOB_FETCH_WORKERS
    monkeypatch.setenv("MYGITHUB12_INDEX_BLOB_FETCH_WORKERS", "0")
    assert mygithub12._index_blob_fetch_workers() == 1
    monkeypatch.setenv("MYGITHUB12_INDEX_BLOB_FETCH_WORKERS", "4")
    monkeypatch.setattr(mygithub12, "_decode_blob", lambda _repo, sha: calls.append(sha) or sha.encode())
    executor, futures, request_count = mygithub12._start_blob_prefetch(object(), entries)
    try:
        assert request_count == 2
        assert set(futures) == {"1" * 40, "2" * 40}
        assert {sha: future.result() for sha, future in futures.items()} == {
            "1" * 40: ("1" * 40).encode(),
            "2" * 40: ("2" * 40).encode(),
        }
    finally:
        executor.shutdown(wait=True)
    assert sorted(calls) == ["1" * 40, "2" * 40]


def test_same_tree_reuse_clones_index_without_github_tree_or_blob_reads(tmp_path, monkeypatch):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "tree-reuse.db"))
    mygithub12.init_db()
    repository = "o/r"
    source = "3" * 40
    target = "4" * 40
    tree_sha = "5" * 40
    with mygithub12._db() as db:
        source_symbols = _seed_fast_path_snapshot(
            db,
            repository=repository,
            commit_sha=source,
            tree_sha=tree_sha,
        )
        _seed_fast_path_job(
            db,
            job_id="tree-reuse-job",
            repository=repository,
            commit_sha=target,
            tree_sha=tree_sha,
            strategy="auto",
        )
    monkeypatch.setattr(
        mygithub12,
        "_service_repo",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("tree reuse must not access GitHub")),
    )

    mygithub12._run_index_build(
        object(),
        {"repository": repository, "commit_sha": target, "tree_sha": tree_sha},
        "tree-reuse-job",
        "auto",
        "",
    )

    with mygithub12._db() as db:
        job = db.execute("SELECT * FROM jobs WHERE job_id='tree-reuse-job'").fetchone()
        manifest = json.loads(db.execute(
            "SELECT manifest_json FROM indexes WHERE repository=? AND commit_sha=?",
            (repository, target),
        ).fetchone()[0])
        target_symbols = db.execute(
            "SELECT * FROM symbols WHERE repository=? AND commit_sha=? ORDER BY start_line",
            (repository, target),
        ).fetchall()
    assert job["status"] == "completed"
    assert manifest["build_strategy"] == "tree_reuse"
    assert manifest["tree_reuse_commit_sha"] == source
    assert manifest["reused_file_count"] == 1
    assert manifest["blob_fetch_requests"] == 0
    assert len(target_symbols) == len(source_symbols)
    assert all(row["symbol_id"] != source_symbols[i]["symbol_id"] for i, row in enumerate(target_symbols))
    assert "tree_fetch_ms" not in manifest["timings_ms"]


def test_full_strategy_bypasses_same_tree_reuse(tmp_path, monkeypatch):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "full-bypass.db"))
    mygithub12.init_db()
    repository = "o/r"
    source = "6" * 40
    target = "7" * 40
    tree_sha = "8" * 40
    blob = "9" * 40
    content = "def fresh():\n    return True\n"
    with mygithub12._db() as db:
        _seed_fast_path_snapshot(
            db,
            repository=repository,
            commit_sha=source,
            tree_sha=tree_sha,
            blob_sha=blob,
            content=content,
        )
        _seed_fast_path_job(
            db,
            job_id="full-bypass-job",
            repository=repository,
            commit_sha=target,
            tree_sha=tree_sha,
            strategy="full",
        )
    repo = _FastPathRepo([_FastPathEntry("code.py", blob, len(content.encode()))])
    monkeypatch.setattr(mygithub12, "_service_repo", lambda *args, **kwargs: repo)
    monkeypatch.setattr(mygithub12, "_decode_blob", lambda *args, **kwargs: content.encode())

    mygithub12._run_index_build(
        object(),
        {"repository": repository, "commit_sha": target, "tree_sha": tree_sha},
        "full-bypass-job",
        "full",
        source,
    )

    with mygithub12._db() as db:
        manifest = json.loads(db.execute(
            "SELECT manifest_json FROM indexes WHERE repository=? AND commit_sha=?",
            (repository, target),
        ).fetchone()[0])
    assert repo.tree_calls == 1
    assert manifest["build_strategy"] == "full"
    assert manifest.get("tree_reuse_commit_sha") is None
    assert manifest["reused_file_count"] == 0
    assert manifest["reindexed_file_count"] == 1


class _CIPlanEntry:
    def __init__(self, path: str):
        self.path = path
        self.type = "blob"


class _CIPlanService:
    def __init__(self, makefile: str = ""):
        self.makefile = makefile

    def get_file(self, repository: str, path: str, ref: str):
        assert path == "Makefile"
        return {"content": self.makefile, "truncated": False}


def _configure_ci_plan(monkeypatch, paths, allowed_profiles, repository_config=None):
    from app import ci_repository_config

    identity = {"repository": "o/r", "commit_sha": "a" * 40, "tree_sha": "b" * 40}
    entries = [_CIPlanEntry(path) for path in paths]
    monkeypatch.setattr(mygithub12, "_recursive_tree", lambda *args, **kwargs: (identity, entries))
    monkeypatch.setattr(ci_repository_config, "get_allowed_profiles", lambda repository: list(allowed_profiles))
    monkeypatch.setattr(ci_repository_config, "get_repository_config", lambda repository: repository_config or {})
    monkeypatch.setattr(ci_repository_config, "get_repository_policy_source", lambda repository: "test")
    monkeypatch.setattr(ci_repository_config, "is_private_ci_enabled", lambda repository: True)


def test_private_ci_plan_auto_selects_only_detected_manifest_stacks(monkeypatch):
    _configure_ci_plan(
        monkeypatch,
        ["go.mod", "h5/app/package.json", "h5/app/package-lock.json", "services/github-action-service/requirements.txt"],
        ["repo-auto-check", "go-check", "node-check", "python-check"],
    )
    result = mygithub12.plan_private_ci_job(_CIPlanService(), "o/r", "a" * 40, "repo-auto-check")
    assert result["applicable"] is True
    assert result["detected_stacks"] == ["go", "node", "python"]
    assert result["selected_profiles"] == ["go-check", "node-check", "python-check"]
    assert result["workspaces"] == [
        {"path": ".", "stack": "go"},
        {"path": "h5/app", "stack": "node", "package_manager": "npm"},
        {"path": "services/github-action-service", "stack": "python"},
    ]


def test_private_ci_plan_explicit_python_matches_worker_root_only_semantics(monkeypatch):
    _configure_ci_plan(monkeypatch, ["services/github-action-service/requirements.txt"], ["python-check"])
    result = mygithub12.plan_private_ci_job(_CIPlanService(), "o/r", "a" * 40, "python-check")
    assert result["applicable"] is False
    assert result["reason"] == "no_python_manifest"
    assert result["detected_stacks"] == []
    assert result["workspaces"] == []


def test_private_ci_plan_fast_check_requires_real_make_entrypoint(monkeypatch):
    _configure_ci_plan(monkeypatch, ["Makefile"], ["repo-fast-check"])
    missing = mygithub12.plan_private_ci_job(_CIPlanService("test:\n\ttrue\n"), "o/r", "a" * 40, "repo-fast-check")
    assert missing["applicable"] is False
    assert missing["reason"] == "fast_check_entrypoint_missing"

    applicable = mygithub12.plan_private_ci_job(_CIPlanService("ai-integrity-check:\n\ttrue\n"), "o/r", "a" * 40, "repo-fast-check")
    assert applicable["applicable"] is True
    assert applicable["selected_profiles"] == ["repo-fast-check"]
