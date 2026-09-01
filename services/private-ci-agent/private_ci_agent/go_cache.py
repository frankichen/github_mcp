"""Worker-scoped Go cache maintenance for Private CI."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable

from private_ci_agent.config import (
    ALLOWED_WORKER_IDS,
    resolve_worker_id,
    worker_runtime_config,
)
from private_ci_agent.podman import PodmanRunner

GOOSE_DEFAULT_VERSION = "v3.22.1"
GOOSE_BUILD_TAGS = (
    "no_clickhouse",
    "no_libsql",
    "no_mssql",
    "no_mysql",
    "no_sqlite3",
    "no_vertica",
    "no_ydb",
)
GO_CACHE_SUBDIRECTORIES = (
    "home",
    "gopath",
    "gomod",
    "gobuild",
    "config/go",
    "xdg-cache",
    "xdg-config",
    "tmp",
    ".tool-bin",
)
GOOSE_VERSION_RE = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")


class GoCachePreheatError(RuntimeError):
    """The worker-local Goose cache could not be prepared or verified."""


def worker_go_cache_root(worker_id: str | None = None) -> Path:
    """Return the authoritative writable Go cache for one fixed Worker."""
    runtime = worker_runtime_config(resolve_worker_id(worker_id))
    return Path(runtime["writable_cache_root"]) / "go"


def worker_runtime_proxy_config(worker_id: str | None = None) -> Path:
    """Return the worker-scoped proxy runtime file used by maintenance jobs."""
    runtime = worker_runtime_config(resolve_worker_id(worker_id))
    return Path(runtime["run_root"]) / "proxy.runtime.conf"


def _prepare_layout(cache_root: Path, maintenance_workspace: Path) -> None:
    for path in (cache_root, maintenance_workspace):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.chmod(0o700)
    for relative_path in GO_CACHE_SUBDIRECTORIES:
        path = cache_root / relative_path
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.chmod(0o700)


def verify_goose_binary(worker_id: str | None = None) -> Path:
    """Require an executable worker-local Goose binary that can report its version."""
    resolved_worker_id = resolve_worker_id(worker_id)
    binary = worker_go_cache_root(resolved_worker_id) / ".tool-bin" / "goose"
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise GoCachePreheatError(
            f"GOOSE_BINARY_UNAVAILABLE worker={resolved_worker_id} path={binary}"
        )
    try:
        result = subprocess.run(
            [str(binary), "-version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GoCachePreheatError(
            f"GOOSE_VERSION_CHECK_FAILED worker={resolved_worker_id} error={type(exc).__name__}"
        ) from exc
    if result.returncode != 0:
        raise GoCachePreheatError(
            f"GOOSE_VERSION_CHECK_FAILED worker={resolved_worker_id} exit={result.returncode}"
        )
    return binary


def preheat_go_cache(
    worker_id: str | None = None,
    goose_version: str = GOOSE_DEFAULT_VERSION,
    podman_binary: str = "/usr/bin/podman",
    *,
    runner_factory: Callable[..., PodmanRunner] = PodmanRunner,
) -> Path:
    """Build PostgreSQL-capable Goose into the current Worker's writable cache."""
    resolved_worker_id = resolve_worker_id(worker_id)
    if not GOOSE_VERSION_RE.fullmatch(goose_version):
        raise GoCachePreheatError("GOOSE_VERSION_INVALID")

    runtime = worker_runtime_config(resolved_worker_id)
    cache_root = Path(runtime["writable_cache_root"]) / "go"
    maintenance_workspace = Path(runtime["run_root"]) / "go-cache-maintenance"
    _prepare_layout(cache_root, maintenance_workspace)

    build_tags = " ".join(GOOSE_BUILD_TAGS)
    command = (
        "CGO_ENABLED=0 GOBIN=/ci-cache/.tool-bin "
        f"go install -tags='{build_tags}' "
        f"github.com/pressly/goose/v3/cmd/goose@{goose_version} 2>&1"
    )
    runner = runner_factory(podman_binary, resolved_worker_id)
    result = runner.run_command(
        "docker.io/library/golang:1.26.4",
        f"go-cache-maintenance-{os.getpid()}",
        str(maintenance_workspace),
        {"go": str(cache_root)},
        command,
        900,
        network=True,
        pass_proxy=True,
    )
    if result.get("exit_code") != 0:
        detail = (result.get("stderr") or result.get("stdout") or "")[-2000:]
        raise GoCachePreheatError(
            "GOOSE_PREHEAT_FAILED "
            f"worker={resolved_worker_id} exit={result.get('exit_code')} "
            f"timed_out={bool(result.get('timed_out'))} detail={detail}"
        )

    goose_module_dir = (
        cache_root
        / "gomod"
        / "cache"
        / "download"
        / "github.com"
        / "pressly"
        / "goose"
    )
    if not goose_module_dir.is_dir():
        raise GoCachePreheatError(
            f"GOOSE_MODULE_CACHE_UNAVAILABLE worker={resolved_worker_id} path={goose_module_dir}"
        )
    return verify_goose_binary(resolved_worker_id)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--list-worker-ids", action="store_true")
    actions.add_argument("--print-runtime-proxy-config", action="store_true")
    actions.add_argument("--verify-only", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.list_worker_ids:
        print(*ALLOWED_WORKER_IDS, sep="\n")
        return 0

    worker_id = resolve_worker_id()
    if os.getuid() != 1500:
        print("[go-cache] run as ciworker (uid 1500)", file=sys.stderr)
        return 2
    if args.print_runtime_proxy_config:
        print(worker_runtime_proxy_config(worker_id))
        return 0

    try:
        binary = (
            verify_goose_binary(worker_id)
            if args.verify_only
            else preheat_go_cache(
                worker_id,
                goose_version=os.environ.get("GOOSE_VERSION", GOOSE_DEFAULT_VERSION),
            )
        )
    except GoCachePreheatError as exc:
        print(f"[go-cache] {exc}", file=sys.stderr)
        return 1
    action = "verified" if args.verify_only else "ready"
    print(f"[go-cache] {action} worker={worker_id} binary={binary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
