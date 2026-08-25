"""Sealed dependency-environment cache for private CI.

Cache identities are derived only from server-controlled repository/workspace,
manifest, runtime and image evidence.  A cache entry is immutable once
published; every job restores into a private writable copy, so one job cannot
mutate another job's environment.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path


CACHE_FORMAT_VERSION = 1


class EnvironmentCacheError(RuntimeError):
    pass


class DependencyEnvironmentCache:
    def __init__(self, root: str):
        self.root = Path(root)

    def enabled(self) -> bool:
        return bool(str(self.root))

    def _ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        (self.root / ".locks").mkdir(exist_ok=True, mode=0o700)

    @staticmethod
    def build_key(
        *, repository: str, workspace: str, stack: str, profile: str,
        manifest_sha256: str, image_digest: str, runtime_identity: str,
        bootstrap_version: str = "v1",
    ) -> str:
        material = {
            "format": CACHE_FORMAT_VERSION,
            "repository": repository,
            "workspace": workspace,
            "stack": stack,
            "profile": profile,
            "manifest_sha256": manifest_sha256,
            "image_digest": image_digest,
            "runtime_identity": runtime_identity,
            "bootstrap_version": bootstrap_version,
        }
        encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def _entry(self, key: str) -> Path:
        if len(key) != 64 or any(ch not in "0123456789abcdef" for ch in key):
            raise EnvironmentCacheError("CI_ENV_CACHE_INVALID")
        return self.root / key

    def _lock(self, key: str):
        self._ensure_root()
        handle = (self.root / ".locks" / f"{key}.lock").open("a+")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return handle

    def inspect(self, key: str) -> dict:
        entry = self._entry(key)
        metadata = entry / "metadata.json"
        payload = entry / "payload"
        if not entry.is_dir() or not metadata.is_file() or not payload.is_dir():
            return {"hit": False, "key": key, "reason": "missing"}
        try:
            value = json.loads(metadata.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"hit": False, "key": key, "reason": "metadata_invalid"}
        if value.get("format") != CACHE_FORMAT_VERSION or value.get("key") != key:
            return {"hit": False, "key": key, "reason": "identity_invalid"}
        return {"hit": True, "key": key, "metadata": value}

    @staticmethod
    def _make_writable(root: Path) -> None:
        for current, dirs, files in os.walk(root, followlinks=False):
            current_path = Path(current)
            if not current_path.is_symlink():
                os.chmod(current_path, 0o700)
            for name in dirs:
                path = current_path / name
                if not path.is_symlink():
                    os.chmod(path, 0o700)
            for name in files:
                path = current_path / name
                if path.is_symlink():
                    continue
                mode = path.stat().st_mode
                os.chmod(path, 0o700 if mode & 0o111 else 0o600)

    @classmethod
    def _remove_tree(cls, root: Path) -> None:
        if not root.exists():
            return
        cls._make_writable(root)
        shutil.rmtree(root)

    @staticmethod
    def _seal(root: Path) -> None:
        # Files retain only read/execute permissions. Directories stay
        # traversable but non-writable; jobs never mount this directory directly.
        # Never chmod symlinks: chmod follows links on Linux and a virtualenv can
        # legitimately contain links to an interpreter outside the cache root.
        for current, dirs, files in os.walk(root, topdown=False, followlinks=False):
            current_path = Path(current)
            for name in files:
                path = current_path / name
                if path.is_symlink():
                    continue
                mode = path.stat().st_mode
                os.chmod(path, 0o555 if mode & 0o111 else 0o444)
            for name in dirs:
                path = current_path / name
                if not path.is_symlink():
                    os.chmod(path, 0o555)
            if not current_path.is_symlink():
                os.chmod(current_path, 0o555)

    def restore(self, key: str, destination: str) -> dict:
        lock = self._lock(key)
        try:
            info = self.inspect(key)
            if not info["hit"]:
                return info
            dest = Path(destination)
            dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            tmp = dest.parent / f".{dest.name}.restore-{uuid.uuid4().hex}"
            shutil.rmtree(tmp, ignore_errors=True)
            shutil.copytree(self._entry(key) / "payload", tmp, symlinks=True)
            self._make_writable(tmp)
            if dest.exists():
                shutil.rmtree(dest)
            os.replace(tmp, dest)
            return {**info, "restored": True, "destination": str(dest)}
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()

    def publish(self, key: str, source: str, metadata: dict) -> dict:
        lock = self._lock(key)
        try:
            existing = self.inspect(key)
            if existing["hit"]:
                return {**existing, "published": False, "deduplicated": True}
            source_path = Path(source)
            if not source_path.is_dir():
                raise EnvironmentCacheError("CI_ENV_CACHE_BUILD_FAILED")
            temp = Path(tempfile.mkdtemp(prefix=f"env-{key[:12]}-", dir=self.root))
            try:
                payload = temp / "payload"
                shutil.copytree(source_path, payload, symlinks=True)
                value = {
                    "format": CACHE_FORMAT_VERSION,
                    "key": key,
                    **{k: v for k, v in metadata.items() if isinstance(k, str)},
                }
                (temp / "metadata.json").write_text(
                    json.dumps(value, sort_keys=True, separators=(",", ":")), encoding="utf-8"
                )
                os.chmod(temp / "metadata.json", 0o444)
                self._seal(payload)
                entry = self._entry(key)
                if entry.exists():
                    self._remove_tree(temp)
                    return {**self.inspect(key), "published": False, "deduplicated": True}
                os.replace(temp, entry)
                os.chmod(entry, 0o555)
                return {"hit": True, "key": key, "published": True, "metadata": value}
            except Exception:
                try:
                    self._remove_tree(temp)
                except OSError:
                    pass
                raise
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()

    def quarantine(self, key: str) -> bool:
        lock = self._lock(key)
        try:
            entry = self._entry(key)
            if not entry.exists():
                return False
            quarantine = self.root / f"{key}.invalid-{uuid.uuid4().hex[:8]}"
            try:
                os.replace(entry, quarantine)
            except OSError:
                self._remove_tree(entry)
            return True
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()
