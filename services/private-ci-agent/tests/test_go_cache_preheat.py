import os
from pathlib import Path

import pytest

import private_ci_agent.config as agent_config
from private_ci_agent.executor import build_cache_map
from private_ci_agent.go_cache import (
    GOOSE_BUILD_TAGS,
    GoCachePreheatError,
    main,
    preheat_go_cache,
    verify_goose_binary,
    worker_go_cache_root,
    worker_runtime_proxy_config,
)


def _successful_runner_factory(calls):
    class SuccessfulRunner:
        def __init__(self, podman_binary, worker_id):
            self.podman_binary = podman_binary
            self.worker_id = worker_id

        def run_command(
            self,
            image,
            job_id,
            source_dir,
            cache_dirs,
            command,
            timeout,
            **options,
        ):
            calls.append(
                {
                    "podman_binary": self.podman_binary,
                    "worker_id": self.worker_id,
                    "image": image,
                    "job_id": job_id,
                    "source_dir": source_dir,
                    "cache_dirs": cache_dirs,
                    "command": command,
                    "timeout": timeout,
                    "options": options,
                }
            )
            cache_root = Path(cache_dirs["go"])
            (cache_root / "gomod/cache/download/github.com/pressly/goose").mkdir(
                parents=True, exist_ok=True
            )
            binary = cache_root / ".tool-bin/goose"
            binary.write_text("#!/bin/sh\necho 'goose version test'\n", encoding="utf-8")
            binary.chmod(0o700)
            return {"exit_code": 0, "stdout": "", "stderr": "", "timed_out": False}

    return SuccessfulRunner


@pytest.mark.parametrize("worker_id", agent_config.ALLOWED_WORKER_IDS)
def test_preheat_uses_worker_runtime_cache_and_matching_podman_identity(
    exec_capable_tmp_path, monkeypatch, worker_id
):
    monkeypatch.setattr(
        agent_config,
        "WORKER_STATE_ROOT",
        str(exec_capable_tmp_path / "workers"),
    )
    calls = []

    binary = preheat_go_cache(
        worker_id,
        runner_factory=_successful_runner_factory(calls),
    )

    runtime = agent_config.worker_runtime_config(worker_id)
    expected_cache = Path(runtime["writable_cache_root"]) / "go"
    assert binary == expected_cache / ".tool-bin/goose"
    assert binary.is_file()
    assert os.access(binary, os.X_OK)
    assert expected_cache.stat().st_mode & 0o777 == 0o700
    assert binary.parent.stat().st_mode & 0o777 == 0o700
    assert verify_goose_binary(worker_id) == binary
    assert worker_go_cache_root(worker_id) == expected_cache
    assert worker_runtime_proxy_config(worker_id) == Path(runtime["run_root"]) / "proxy.runtime.conf"
    assert len(calls) == 1
    call = calls[0]
    assert call["worker_id"] == worker_id
    assert call["cache_dirs"] == {"go": str(expected_cache)}
    assert call["cache_dirs"]["go"] == build_cache_map(runtime)["go"]
    assert call["source_dir"].startswith(runtime["run_root"] + "/")
    assert call["options"] == {"network": True, "pass_proxy": True}

    from private_ci_agent.podman import PodmanRunner

    mounts, mounted_go_cache = PodmanRunner._cache_mounts(build_cache_map(runtime))
    assert mounted_go_cache == os.path.realpath(expected_cache)
    assert f"type=bind,src={mounted_go_cache},dst=/ci-cache,rw" in mounts


def test_preheat_uses_postgresql_only_goose_build_tags(
    exec_capable_tmp_path, monkeypatch
):
    monkeypatch.setattr(
        agent_config,
        "WORKER_STATE_ROOT",
        str(exec_capable_tmp_path / "workers"),
    )
    calls = []

    preheat_go_cache(
        "wsl-ci-01",
        runner_factory=_successful_runner_factory(calls),
    )

    command = calls[0]["command"]
    assert "CGO_ENABLED=0" in command
    assert f"-tags='{' '.join(GOOSE_BUILD_TAGS)}'" in command
    for build_tag in (
        "no_clickhouse",
        "no_libsql",
        "no_mssql",
        "no_mysql",
        "no_sqlite3",
        "no_vertica",
        "no_ydb",
    ):
        assert build_tag in command
    assert "no_postgres" not in command


def test_preheat_fails_when_binary_contract_is_not_satisfied(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_config, "WORKER_STATE_ROOT", str(tmp_path / "workers"))

    class MissingBinaryRunner:
        def __init__(self, _podman_binary, _worker_id):
            pass

        def run_command(self, _image, _job_id, _source_dir, cache_dirs, *_args, **_kwargs):
            Path(cache_dirs["go"], "gomod/cache/download/github.com/pressly/goose").mkdir(
                parents=True, exist_ok=True
            )
            return {"exit_code": 0, "stdout": "", "stderr": "", "timed_out": False}

    with pytest.raises(GoCachePreheatError, match="GOOSE_BINARY_UNAVAILABLE"):
        preheat_go_cache("wsl-ci-02", runner_factory=MissingBinaryRunner)


def test_prepare_script_has_no_legacy_global_go_cache_path():
    script = (Path(__file__).parents[1] / "deploy/prepare-go-cache").read_text(
        encoding="utf-8"
    )

    assert 'CACHE_ROOT="/srv/private-ci/cache/go"' not in script
    assert "private_ci_agent.go_cache" in script
    assert "--print-runtime-proxy-config" in script


def test_worker_list_cli_exposes_the_config_allowlist(capsys):
    assert main(["--list-worker-ids"]) == 0
    assert capsys.readouterr().out.splitlines() == list(agent_config.ALLOWED_WORKER_IDS)
