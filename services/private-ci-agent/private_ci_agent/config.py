"""CI Agent configuration."""

import os
import yaml
from pathlib import Path

DEFAULT_CONFIG = {
    "worker_id": "wsl-ci-01",
    "controller_url": "http://100.118.124.97:8788",
    "max_concurrent_jobs": 1,
    "poll_interval_seconds": 5,
    "heartbeat_interval_seconds": 15,
    "workspace_root": "/srv/private-ci/workspaces",
    "cache_root": "/srv/private-ci/cache",
    "log_root": "/srv/private-ci/logs",
    "max_job_seconds": 1200,
    "max_log_bytes": 10485760,
    "max_source_bytes": 268435456,
    "podman_binary": "/usr/bin/podman",
    "supported_profiles": ["repo-auto-check", "repo-fast-check", "python-check", "go-check", "node-check"],
    # CI jobs must use the isolated bare mirror by default.  The archive
    # downloader remains an explicit compatibility fallback in config.
    "source_mirror_enabled": True,
    "source_mirror_root": "/srv/private-ci/cache/git",
}


def load_config() -> dict:
    config = dict(DEFAULT_CONFIG)
    config_path = "/etc/private-ci/agent.yml"
    if os.path.exists(config_path):
        with open(config_path) as f:
            file_data = yaml.safe_load(f) or {}
            config.update(file_data)

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
