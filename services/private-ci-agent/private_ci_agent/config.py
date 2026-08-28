"""CI Agent configuration."""

import os

import yaml

ALLOWED_WORKER_IDS = ("wsl-ci-01", "wsl-ci-02")
SHARED_CACHE_ROOT = "/srv/private-ci/cache"
WORKER_STATE_ROOT = "/srv/private-ci/workers"


def resolve_worker_id(value: str | None = None) -> str:
    worker_id = (value or os.environ.get("PRIVATE_CI_WORKER_ID") or "wsl-ci-01").strip()
    if worker_id not in ALLOWED_WORKER_IDS:
        raise RuntimeError("CI_WORKER_ID_INVALID")
    return worker_id


def worker_runtime_config(worker_id: str) -> dict:
    worker_id = resolve_worker_id(worker_id)
    root = f"{WORKER_STATE_ROOT}/{worker_id}"
    writable_cache = f"{root}/cache"
    return {
        "worker_id": worker_id,
        "workspace_root": f"{root}/workspaces",
        "cache_root": writable_cache,
        "writable_cache_root": writable_cache,
        "log_root": f"{root}/logs",
        "run_root": f"{root}/run",
        "shared_cache_root": SHARED_CACHE_ROOT,
        "environment_cache_root": f"{SHARED_CACHE_ROOT}/environments",
        "shared_playwright_cache": f"{SHARED_CACHE_ROOT}/ms-playwright",
        "source_mirror_root": f"{SHARED_CACHE_ROOT}/git",
    }


DEFAULT_CONFIG = {
    **worker_runtime_config("wsl-ci-01"),
    "controller_url": "http://100.127.108.20:8765",
    "max_concurrent_jobs": 1,
    "poll_interval_seconds": 5,
    "heartbeat_interval_seconds": 15,
    "max_job_seconds": 1200,
    "max_log_bytes": 10485760,
    "max_source_bytes": 268435456,
    "podman_binary": "/usr/bin/podman",
    "supported_profiles": [
        "repo-auto-check", "repo-fast-check", "python-check", "go-check", "node-check",
        "rust-check", "maven-check", "gradle-check", "dotnet-check", "openapi-check",
    ],
    # CI jobs use one serialized bare mirror. Writable source worktrees are
    # always created under the current worker's private workspace root.
    "source_mirror_enabled": True,
}


def load_config() -> dict:
    config = dict(DEFAULT_CONFIG)
    config_path = "/etc/private-ci/agent.yml"
    if os.path.exists(config_path):
        with open(config_path) as f:
            file_data = yaml.safe_load(f) or {}
            config.update(file_data)

    # Worker identity and writable roots are a fixed runtime contract. An
    # operator config file cannot redirect one worker into another worker's
    # state directory. The systemd instance supplies PRIVATE_CI_WORKER_ID.
    worker_id = resolve_worker_id(os.environ.get("PRIVATE_CI_WORKER_ID") or config.get("worker_id"))
    config.update(worker_runtime_config(worker_id))
    config["max_concurrent_jobs"] = 1

    token_path = "/etc/private-ci/worker.env"
    if os.path.exists(token_path):
        with open(token_path) as f:
            for line in f:
                if line.startswith("CI_WORKER_TOKEN="):
                    config["worker_token"] = line.split("=", 1)[1].strip()
                    break

    return config


def load_profiles() -> dict:
    path = "/etc/private-ci/profiles.yml"
    if os.path.exists(path):
        with open(path) as f:
            return yaml.safe_load(f) or {}
    return {}


def load_repositories() -> dict:
    path = "/etc/private-ci/repositories.yml"
    if os.path.exists(path):
        with open(path) as f:
            return yaml.safe_load(f) or {}
    return {}


def refresh_proxy_before_external_access() -> dict:
    """Return the proxy environment prepared by the launcher.

    The systemd launcher owns proxy discovery and exports the sanitized
    runtime values before starting Python.  Keeping this adapter here avoids
    re-discovering or logging credentials from inside the agent.
    """
    available = os.environ.get("PROXY_AVAILABLE", "0") == "1"
    if not available:
        raise RuntimeError("PROXY_UNAVAILABLE")
    return {
        "PROXY_AVAILABLE": "1",
        "PROXY_PROTOCOL": os.environ.get("PROXY_PROTOCOL", "http"),
        "PROXY_HOST": os.environ.get("PROXY_HOST", ""),
        "PROXY_PORT": os.environ.get("PROXY_PORT", ""),
    }
