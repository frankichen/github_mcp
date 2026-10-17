#!/usr/bin/env python3
"""Bounded, single-writer online SQLite backup. Run on the Controller host.

The supervisor owns publication; the disposable child only writes a private
partial file. A lost CLI connection cannot remove the child's own deadline.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import ctypes
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time


class BackupError(RuntimeError):
    pass


def readonly(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
    db.execute("PRAGMA query_only=ON")
    return db


def copy_snapshot(source: Path, partial: Path, *, seconds: float, rate: float) -> dict:
    """Pin a WAL read snapshot so concurrent commits cannot restart the copy."""
    started = time.monotonic()
    inode = partial.stat()
    with closing(readonly(source)) as src, closing(sqlite3.connect(partial)) as dst:
        # BEGIN alone is deferred; force a read before calling backup().
        src.execute("BEGIN")
        src.execute("SELECT count(*) FROM sqlite_schema").fetchone()
        pages = src.execute("PRAGMA page_count").fetchone()[0]
        page_size = src.execute("PRAGMA page_size").fetchone()[0]
        previous = pages
        copied = 0

        def progress(status: int, remaining: int, total: int) -> None:
            nonlocal previous, copied
            if time.monotonic() - started >= seconds:
                raise BackupError("BACKUP_DEADLINE_EXCEEDED")
            try:
                current = partial.stat()
            except FileNotFoundError as exc:
                raise BackupError("BACKUP_TARGET_REMOVED") from exc
            if (current.st_dev, current.st_ino) != (inode.st_dev, inode.st_ino):
                raise BackupError("BACKUP_TARGET_REPLACED")
            if total != pages or remaining > previous:
                raise BackupError("BACKUP_SNAPSHOT_RESTARTED")
            copied += previous - remaining
            previous = remaining
            if copied > pages:
                raise BackupError("BACKUP_COPY_BUDGET_EXCEEDED")
            # Limit logical copy rate even without systemd's physical I/O cap.
            delay = copied * page_size / rate - (time.monotonic() - started)
            if delay > 0:
                time.sleep(min(delay, max(0, seconds - (time.monotonic() - started))))

        dst.execute("PRAGMA journal_mode=DELETE")
        src.backup(dst, pages=256, progress=progress, sleep=0.05)
        src.rollback()  # release the pinned reader before verifying the copy
    with closing(readonly(partial)) as check:
        if check.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise BackupError("BACKUP_INTEGRITY_FAILED")
    with partial.open("rb") as handle:
        os.fsync(handle.fileno())
    return {"page_count": pages, "page_size": page_size, "copied_pages": copied}


def worker(source: Path, partial: Path, seconds: float, rate: float, parent: int) -> None:
    # Linux safety net if the supervisor itself is killed (including SIGKILL).
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent:
        raise BackupError("BACKUP_PARENT_LOST")
    def expired(*_):
        raise BackupError("BACKUP_DEADLINE_EXCEEDED")
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    result = copy_snapshot(source, partial, seconds=seconds, rate=rate)
    print(json.dumps(result), flush=True)


def stop_child(child: subprocess.Popen | None) -> None:
    if child is None or child.poll() is not None:
        return
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        child.wait()
        return
    try:
        child.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()


def backup(source: Path, destination: Path, *, seconds: float = 600,
           rate: float = 16 * 1024**2, lock_dir: Path | None = None) -> dict:
    if not math.isfinite(seconds) or not 0 < seconds <= 600:
        raise BackupError("BACKUP_INVALID_DEADLINE")
    if not math.isfinite(rate) or not 0 < rate <= 32 * 1024**2:
        raise BackupError("BACKUP_INVALID_RATE")
    source = source.resolve(strict=True)
    destination = destination.absolute()
    if not source.is_file() or destination.exists() or destination.is_symlink():
        raise BackupError("BACKUP_PATH_CONFLICT")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_dir = lock_dir or Path("/run/mygithub-maintenance")
    lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = hashlib.sha256(str(source).encode()).hexdigest() + ".lock"
    child = None
    partial = None
    started = time.monotonic()
    with (lock_dir / name).open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BackupError("BACKUP_ALREADY_RUNNING") from exc
        try:
            fd, temp = tempfile.mkstemp(prefix=".backup-", suffix=".partial", dir=destination.parent)
            os.close(fd)
            partial = Path(temp)
            identity = partial.stat()
            child = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--worker", "--source", str(source),
                 "--destination", str(partial), "--seconds", str(seconds), "--bytes-per-second", str(rate),
                 "--parent-pid", str(os.getpid())],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
            )
            try:
                out, err = child.communicate(timeout=seconds)
            except subprocess.TimeoutExpired as exc:
                raise BackupError("BACKUP_DEADLINE_EXCEEDED") from exc
            if child.returncode:
                raise BackupError("BACKUP_WORKER_FAILED: " + err[-1500:])
            current = partial.stat()
            if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
                raise BackupError("BACKUP_TARGET_REPLACED")
            # Atomic no-clobber publication only after child exits successfully.
            os.link(partial, destination, follow_symlinks=False)
            partial.unlink()
            directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return {"status": "passed", "source": str(source), "destination": str(destination),
                    "elapsed_seconds": round(time.monotonic() - started, 3), **json.loads(out)}
        finally:
            stop_child(child)
            if partial is not None:
                for path in (partial, Path(str(partial) + "-journal"), Path(str(partial) + "-wal"), Path(str(partial) + "-shm")):
                    path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--lock-dir", type=Path)
    parser.add_argument("--seconds", type=float, default=600)
    parser.add_argument("--bytes-per-second", type=float, default=16 * 1024**2)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--parent-pid", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    def cancelled(*_):
        raise BackupError("BACKUP_CANCELLED")
    signal.signal(signal.SIGTERM, cancelled)
    try:
        if args.worker:
            worker(args.source, args.destination, args.seconds, args.bytes_per_second, args.parent_pid)
        else:
            print(json.dumps(backup(args.source, args.destination, seconds=args.seconds,
                                    rate=args.bytes_per_second, lock_dir=args.lock_dir)))
        return 0
    except (BackupError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
