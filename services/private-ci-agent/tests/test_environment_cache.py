import os
import threading
from pathlib import Path

from private_ci_agent.environment_cache import DependencyEnvironmentCache


def _key(**overrides):
    values = {
        "repository": "owner/repo",
        "workspace": "services/api",
        "stack": "python",
        "profile": "repo-auto-check",
        "manifest_sha256": "a" * 64,
        "image_digest": "sha256:" + "b" * 64,
        "runtime_identity": "python:3.12",
    }
    values.update(overrides)
    return DependencyEnvironmentCache.build_key(**values)


def _payload(root: Path, content: str = "ready") -> Path:
    root.mkdir(parents=True)
    (root / "bin").mkdir()
    executable = root / "bin" / "python"
    executable.write_text(content, encoding="utf-8")
    executable.chmod(0o700)
    return root


def test_key_is_stable_and_isolates_repository_workspace_runtime_and_manifest():
    baseline = _key()

    assert baseline == _key()
    assert baseline != _key(repository="owner/other")
    assert baseline != _key(workspace="services/worker")
    assert baseline != _key(runtime_identity="python:3.13")
    assert baseline != _key(manifest_sha256="c" * 64)
    assert baseline != _key(image_digest="sha256:" + "d" * 64)


def test_publish_is_atomic_sealed_and_restore_is_job_private_writable(tmp_path):
    cache = DependencyEnvironmentCache(str(tmp_path / "cache"))
    source = _payload(tmp_path / "source")
    key = _key()

    published = cache.publish(key, str(source), {"repository": "attacker/value"})
    entry = tmp_path / "cache" / key
    restored_path = tmp_path / "jobs" / "one" / "environment"
    restored = cache.restore(key, str(restored_path))

    assert published["published"] is True
    assert published["metadata"]["key"] == key
    assert not os.access(entry / "payload" / "bin" / "python", os.W_OK)
    assert restored["hit"] is True
    assert restored["restored"] is True
    assert os.access(restored_path / "bin" / "python", os.W_OK)
    (restored_path / "bin" / "python").write_text("job mutation", encoding="utf-8")
    assert cache.inspect(key)["hit"] is True


def test_corruption_is_quarantined_and_can_be_rebuilt(tmp_path):
    cache = DependencyEnvironmentCache(str(tmp_path / "cache"))
    source = _payload(tmp_path / "source")
    key = _key()
    cache.publish(key, str(source), {})
    payload = tmp_path / "cache" / key / "payload" / "bin" / "python"
    payload.chmod(0o600)
    payload.write_text("corrupt", encoding="utf-8")

    restored = cache.restore(key, str(tmp_path / "restore"))

    assert restored["hit"] is False
    assert restored["reason"] == "payload_corrupt"
    assert restored["quarantined"] is True
    assert list((tmp_path / "cache").glob(f"{key}.invalid-*"))
    rebuilt = cache.publish(key, str(source), {})
    assert rebuilt["published"] is True


def test_symlink_sealing_never_chmods_external_target(tmp_path):
    cache = DependencyEnvironmentCache(str(tmp_path / "cache"))
    external = tmp_path / "external-python"
    external.write_text("runtime", encoding="utf-8")
    external.chmod(0o700)
    source = tmp_path / "source"
    source.mkdir()
    (source / "bin").mkdir()
    (source / "bin" / "python").symlink_to(external)

    cache.publish(_key(), str(source), {})

    assert external.stat().st_mode & 0o777 == 0o700


def test_concurrent_publish_deduplicates_one_immutable_entry(tmp_path):
    cache = DependencyEnvironmentCache(str(tmp_path / "cache"))
    source = _payload(tmp_path / "source")
    key = _key()
    results = []

    def publish():
        results.append(cache.publish(key, str(source), {}))

    threads = [threading.Thread(target=publish) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(bool(item.get("published")) for item in results) == 1
    assert sum(bool(item.get("deduplicated")) for item in results) == 3
    assert cache.inspect(key)["hit"] is True
