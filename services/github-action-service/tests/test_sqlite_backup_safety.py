"""All database and process fault injection is confined to pytest tmp_path."""
from contextlib import closing
import importlib.util
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sqlite_backup.py"
spec = importlib.util.spec_from_file_location("safe_backup", SCRIPT)
safe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(safe)


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "source.sqlite"
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE data (id INTEGER PRIMARY KEY, body BLOB)")
        db.executemany("INSERT INTO data(body) VALUES (?)", [(b"x" * 4096,)] * 512)
        db.execute("CREATE TABLE version (n INTEGER)")
        db.execute("INSERT INTO version VALUES (0)")
        db.commit()
    return path


def test_unpinned_backup_restarts_under_wal_writes(source, tmp_path):
    remaining_pages = []
    class Reproduced(Exception):
        pass
    with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(source)) as writer, closing(sqlite3.connect(tmp_path / "old.sqlite")) as dst:
        def progress(status, remaining, total):
            remaining_pages.append(remaining)
            writer.execute("UPDATE version SET n=n+1")
            writer.commit()
            if len(remaining_pages) == 12:
                raise Reproduced()
        with pytest.raises(Reproduced):
            src.backup(dst, pages=8, progress=progress)
    assert len(set(remaining_pages)) == 1  # no forward progress despite copying


def test_pinned_snapshot_finishes_with_writes_between_every_step(source, tmp_path, monkeypatch):
    partial = tmp_path / "partial"
    partial.touch(mode=0o600)
    remaining_pages = []
    with closing(sqlite3.connect(source)) as writer:
        class ChurnConnection(sqlite3.Connection):
            def backup(self, target, **kwargs):
                original = kwargs["progress"]
                def progress(status, remaining, total):
                    remaining_pages.append(remaining)
                    writer.execute("UPDATE version SET n=n+1")
                    writer.commit()
                    original(status, remaining, total)
                kwargs["progress"] = progress
                return super().backup(target, **kwargs)
        def readonly(path):
            return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, factory=ChurnConnection)
        monkeypatch.setattr(safe, "readonly", readonly)
        result = safe.copy_snapshot(source, partial, seconds=5, rate=32 * 1024**2)
        assert result["copied_pages"] == result["page_count"]
        assert all(a > b for a, b in zip(remaining_pages, remaining_pages[1:]))
        with closing(sqlite3.connect(partial)) as copied:
            assert copied.execute("SELECT n FROM version").fetchone()[0] == 0
        assert writer.execute("SELECT n FROM version").fetchone()[0] > 0


def test_success_and_existing_destination_fail_closed(source, tmp_path):
    target = tmp_path / "backup.sqlite"
    result = safe.backup(source, target, seconds=5, lock_dir=tmp_path / "locks")
    assert result["status"] == "passed"
    assert target.stat().st_mode & 0o777 == 0o600
    original = target.read_bytes()
    with pytest.raises(safe.BackupError, match="PATH_CONFLICT"):
        safe.backup(source, target, lock_dir=tmp_path / "locks")
    assert target.read_bytes() == original
    assert not list(tmp_path.glob("*.partial"))


def test_hard_deadline_cleans_partial_and_releases_lock(source, tmp_path):
    target = tmp_path / "timeout.sqlite"
    started = time.monotonic()
    with pytest.raises(safe.BackupError, match="DEADLINE|WORKER_FAILED"):
        safe.backup(source, target, seconds=0.15, rate=1024, lock_dir=tmp_path / "locks")
    assert time.monotonic() - started < 4
    assert not target.exists()
    assert not list(tmp_path.glob(".backup-*"))
    assert safe.backup(source, target, seconds=5, lock_dir=tmp_path / "locks")["status"] == "passed"


def start_slow(source, tmp_path):
    process = subprocess.Popen([sys.executable, str(SCRIPT), "--source", str(source),
        "--destination", str(tmp_path / "slow.sqlite"), "--lock-dir", str(tmp_path / "locks"),
        "--seconds", "10", "--bytes-per-second", "1024"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        children = Path(f"/proc/{process.pid}/task/{process.pid}/children").read_text().split()
        if children and list(tmp_path.glob(".backup-*.partial")):
            return process, int(children[0])
        time.sleep(0.01)
    process.kill()
    process.wait()
    pytest.fail("worker did not start")


def dead(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1].split()[0] == "Z"
    except FileNotFoundError:
        return True


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGKILL])
def test_supervisor_death_cannot_orphan_worker(source, tmp_path, sig):
    process, worker = start_slow(source, tmp_path)
    try:
        # Also checks a different destination shares the same source lock.
        with pytest.raises(safe.BackupError, match="ALREADY_RUNNING"):
            safe.backup(source, tmp_path / "other.sqlite", lock_dir=tmp_path / "locks")
        os.kill(process.pid, sig)
        process.communicate(timeout=4)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not dead(worker):
            time.sleep(0.01)
        assert dead(worker)
        assert not (tmp_path / "slow.sqlite").exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def test_deleted_target_aborts(source, tmp_path, monkeypatch):
    partial = tmp_path / "removed.partial"
    partial.touch()
    real_stat = Path.stat
    calls = 0
    def stat(path, *args, **kwargs):
        nonlocal calls
        if path == partial:
            calls += 1
            if calls == 2:
                partial.unlink()
        return real_stat(path, *args, **kwargs)
    monkeypatch.setattr(Path, "stat", stat)
    with pytest.raises(safe.BackupError, match="TARGET_REMOVED"):
        safe.copy_snapshot(source, partial, seconds=5, rate=32 * 1024**2)


@pytest.mark.parametrize("seconds,rate", [(0, 1024), (601, 1024), (float("nan"), 1024), (1, float("inf"))])
def test_unbounded_options_rejected(source, tmp_path, seconds, rate):
    with pytest.raises(safe.BackupError, match="INVALID"):
        safe.backup(source, tmp_path / "bad.sqlite", seconds=seconds, rate=rate)


def test_invalid_database_not_published(tmp_path):
    source = tmp_path / "corrupt.sqlite"
    source.write_bytes(b"not a database" * 100)
    target = tmp_path / "backup.sqlite"
    with pytest.raises(safe.BackupError, match="WORKER_FAILED"):
        safe.backup(source, target, seconds=5, lock_dir=tmp_path / "locks")
    assert not target.exists()
    assert not list(tmp_path.glob(".backup-*"))


def test_busy_source_hits_hard_deadline(source, tmp_path):
    # A rollback-journal exclusive writer blocks even opening the read snapshot.
    with closing(sqlite3.connect(source)) as writer:
        writer.execute("PRAGMA journal_mode=DELETE")
        writer.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        with pytest.raises(safe.BackupError, match="DEADLINE|WORKER_FAILED"):
            safe.backup(source, tmp_path / "busy.sqlite", seconds=0.15, lock_dir=tmp_path / "locks")
        assert time.monotonic() - started < 4
        writer.rollback()
    assert not (tmp_path / "busy.sqlite").exists()


def test_destination_created_during_copy_is_not_overwritten(source, tmp_path, monkeypatch):
    target = tmp_path / "raced.sqlite"
    real_link = os.link
    def raced_link(src, dst, **kwargs):
        target.write_bytes(b"another completed backup")
        return real_link(src, dst, **kwargs)
    monkeypatch.setattr(os, "link", raced_link)
    with pytest.raises(FileExistsError):
        safe.backup(source, target, seconds=5, lock_dir=tmp_path / "locks")
    assert target.read_bytes() == b"another completed backup"
    assert not list(tmp_path.glob(".backup-*"))


def test_wal_database_without_sidecars(source, tmp_path):
    # Last connection closed: WAL mode persists in the DB, sidecars disappear.
    assert not Path(str(source) + "-wal").exists()
    target = tmp_path / "wal-no-sidecars.sqlite"
    assert safe.backup(source, target, seconds=5, lock_dir=tmp_path / "locks")["status"] == "passed"
    with closing(sqlite3.connect(target)) as copied:
        assert copied.execute("SELECT count(*) FROM data").fetchone()[0] == 512
