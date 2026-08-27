#!/usr/bin/env python3
"""Fixed executor for MyGithut12 production control-plane deployment.

Queue rows never choose a host, shell command, script path, failure mode, or
rollback action.  This executor only accepts exact-main deployment identities
from the Controller, prepares an isolated ``frankichen/github_mcp`` checkout,
and runs the repository-owned deployment script with fail-stop forced locally.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import requests

CONTROLLER_URL = os.environ.get(
    "INFRASTRUCTURE_DEPLOY_CONTROLLER_URL",
    "http://127.0.0.1:8765",
).rstrip("/")
CALLBACK_KEY_FILE = os.environ.get("INFRASTRUCTURE_DEPLOY_CALLBACK_API_KEY_FILE", "")
DEPLOY_CACHE = os.environ.get(
    "INFRASTRUCTURE_DEPLOY_CACHE",
    "/home/xiaowu/work/mygithub12-infrastructure-deploy-cache",
)
DEPLOY_MIRROR = os.environ.get(
    "INFRASTRUCTURE_DEPLOY_MIRROR",
    os.path.join(DEPLOY_CACHE, "frankichen-github-mcp.git"),
)
AUTHORITATIVE_REPOSITORY_URL = "https://github.com/frankichen/github_mcp.git"
DEPLOY_WORKSPACES = os.path.join(DEPLOY_CACHE, "workspaces")
EXPECTED_REPOSITORY = "frankichen/github_mcp"
EXPECTED_ENVIRONMENT = "mygithub12-production"
EXPECTED_SCOPE = "control-plane"
EXECUTOR_ID = "mygithub12-infrastructure-deploy-01"
DEPLOY_SCRIPT = "services/private-ci-agent/deploy/apply-fixes.sh"
POLL_SECONDS = 2.0
HEARTBEAT_SECONDS = 5.0
TIMEOUT_SECONDS = int(os.environ.get("INFRASTRUCTURE_DEPLOY_TIMEOUT_SECONDS", "3600"))
SECRET_RE = re.compile(
    r"(?i)(authorization|cookie|token|password|secret|database_url|private_key)\s*[=:]\s*\S+"
)
DEPLOYMENT_PHASES = frozenset(
    {"controller_build", "controller_switch", "health", "preheat", "post_verify"}
)
PHASE_MARKER_RE = re.compile(r"^\[deploy\]\s+DX2_PHASE=([a-z_]+)\s*$")


class InfrastructureDeploymentSourceError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _key() -> str:
    path = Path(CALLBACK_KEY_FILE)
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError("infrastructure deployment callback key is empty")
    return value


def _redact(text: str) -> str:
    return SECRET_RE.sub(lambda match: match.group(1) + "=***", str(text or ""))


def _request(
    method: str,
    path: str,
    payload: dict | None = None,
    *,
    required: bool,
    attempts: int = 5,
) -> dict | None:
    headers = {
        "X-Infrastructure-Deployment-Callback-Key": _key(),
        "Content-Type": "application/json",
    }
    last: Exception | None = None
    for attempt in range(attempts):
        if attempt:
            time.sleep(min(8.0, 0.5 * (2 ** (attempt - 1))))
        try:
            response = requests.request(
                method,
                CONTROLLER_URL + path,
                headers=headers,
                json=payload,
                timeout=15,
            )
            if response.status_code >= 500:
                last = RuntimeError(f"controller HTTP {response.status_code}")
                continue
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            last = exc
    if required:
        raise RuntimeError(
            f"infrastructure deployment callback failed after retries: {type(last).__name__}"
        )
    return None


def _git(*args: str, cwd: str | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if result.returncode:
        raise InfrastructureDeploymentSourceError(
            "INFRASTRUCTURE_SOURCE_FETCH_FAILED",
            _redact(result.stdout.strip()[-1000:]),
        )
    return result.stdout.strip()


def prepare_workspace(row: dict) -> tuple[str, list[str]]:
    expected = str(row["commit_sha"])
    deployment_id = str(row["deployment_id"])
    if row.get("repository") != EXPECTED_REPOSITORY:
        raise InfrastructureDeploymentSourceError(
            "INFRASTRUCTURE_CONTRACT_MISMATCH",
            "deployment repository is not the fixed MyGithut12 repository",
        )
    if row.get("environment") != EXPECTED_ENVIRONMENT or row.get("requested_scope") != EXPECTED_SCOPE:
        raise InfrastructureDeploymentSourceError(
            "INFRASTRUCTURE_CONTRACT_MISMATCH",
            "deployment environment/scope is not the fixed production contract",
        )
    if not os.path.isdir(DEPLOY_MIRROR):
        raise InfrastructureDeploymentSourceError(
            "INFRASTRUCTURE_SOURCE_FETCH_FAILED",
            "authoritative infrastructure deployment mirror is missing",
        )
    remote = _git("remote", "get-url", "origin", cwd=DEPLOY_MIRROR)
    if remote != AUTHORITATIVE_REPOSITORY_URL:
        raise InfrastructureDeploymentSourceError(
            "INFRASTRUCTURE_SOURCE_FETCH_FAILED",
            "infrastructure deployment mirror origin is not authoritative",
        )
    _git("remote", "update", "--prune", cwd=DEPLOY_MIRROR)
    mirror_sha = _git("rev-parse", "refs/heads/main", cwd=DEPLOY_MIRROR)
    if mirror_sha != expected:
        raise InfrastructureDeploymentSourceError(
            "INFRASTRUCTURE_MAIN_SHA_MISMATCH",
            f"mirror main={mirror_sha} expected={expected}",
        )
    _git("cat-file", "-e", f"{expected}^{{commit}}", cwd=DEPLOY_MIRROR)

    workspace = os.path.join(DEPLOY_WORKSPACES, deployment_id)
    if os.path.exists(workspace):
        raise InfrastructureDeploymentSourceError(
            "INFRASTRUCTURE_WORKSPACE_EXISTS",
            "infrastructure deployment workspace already exists",
        )
    os.makedirs(DEPLOY_WORKSPACES, exist_ok=True)
    _git("clone", "--no-local", "--branch", "main", DEPLOY_MIRROR, workspace)
    _git("remote", "set-url", "origin", AUTHORITATIVE_REPOSITORY_URL, cwd=workspace)
    _git("fetch", "--no-tags", "origin", "main", cwd=workspace)
    origin_sha = _git("rev-parse", "refs/remotes/origin/main", cwd=workspace)
    head_sha = _git("rev-parse", "HEAD", cwd=workspace)
    branch = _git("branch", "--show-current", cwd=workspace)
    dirty = _git("status", "--porcelain", cwd=workspace)
    if origin_sha != expected or head_sha != expected or branch != "main" or dirty:
        raise InfrastructureDeploymentSourceError(
            "INFRASTRUCTURE_MAIN_SHA_MISMATCH",
            "isolated infrastructure checkout is not exact clean main",
        )
    script = os.path.join(workspace, DEPLOY_SCRIPT)
    if not os.path.isfile(script):
        raise InfrastructureDeploymentSourceError(
            "INFRASTRUCTURE_SCRIPT_MISSING",
            "fixed infrastructure deployment script is missing from exact checkout",
        )
    return workspace, [
        f"authoritative_origin={AUTHORITATIVE_REPOSITORY_URL}",
        f"mirror_main_sha={mirror_sha}",
        f"origin_main_sha={origin_sha}",
        f"checked_out_head={head_sha}",
        f"branch={branch}",
        "clean_worktree=true",
        "deploy_failure_mode=fail-stop",
        "automatic_rollback=false",
    ]


def _progress(deployment_id: str, step: str, message: str) -> None:
    _request(
        "POST",
        f"/internal/infrastructure-deployments/{deployment_id}/progress",
        {"current_step": step, "message": _redact(message)},
        required=False,
        attempts=2,
    )


def _phase_from_output(line: str, current_phase: str) -> str:
    match = PHASE_MARKER_RE.fullmatch(line)
    if match and match.group(1) in DEPLOYMENT_PHASES:
        return match.group(1)
    return current_phase


def _fixed_health_evidence() -> tuple[bool, bool]:
    controller = requests.get(CONTROLLER_URL + "/health", timeout=5)
    controller_healthy = controller.status_code == 200 and controller.json().get("status") == "ok"
    agent = subprocess.run(
        ["systemctl", "is-active", "--quiet", "private-ci-agent.service"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return controller_healthy, agent.returncode == 0


def execute(row: dict) -> None:
    deployment_id = str(row["deployment_id"])
    workspace: str | None = None
    process: subprocess.Popen[str] | None = None
    timed_out = False
    try:
        _progress(deployment_id, "source_prepare", "preparing isolated exact-main source")
        workspace, source_lines = prepare_workspace(row)
        for line in source_lines:
            _progress(deployment_id, "validation", line)

        deploy_env = dict(os.environ)
        deploy_env["MYGITHUB12_DEPLOY_FAILURE_MODE"] = "fail-stop"
        deploy_env["MYGITHUB12_EXPECTED_PARENT_BUILD_SHA"] = str(
            row["expected_current_build_sha"]
        )
        script = os.path.join(workspace, DEPLOY_SCRIPT)
        process = subprocess.Popen(
            ["bash", script],
            cwd=workspace,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=deploy_env,
        )

        def _timeout() -> None:
            nonlocal timed_out
            timed_out = True
            if process and process.poll() is None:
                process.terminate()

        timer = threading.Timer(TIMEOUT_SECONDS, _timeout)
        timer.daemon = True
        timer.start()
        assert process.stdout is not None
        current_phase = "validation"
        for raw in process.stdout:
            line = _redact(raw.rstrip())
            current_phase = _phase_from_output(line, current_phase)
            _progress(deployment_id, current_phase, line)
        process.wait()
        timer.cancel()

        if timed_out:
            _request(
                "POST",
                f"/internal/infrastructure-deployments/{deployment_id}/fail",
                {
                    "exit_code": 124,
                    "error_code": "INFRASTRUCTURE_DEPLOYMENT_TIMEOUT",
                    "error_message": "fixed MyGithut12 infrastructure deployment timed out; no rollback attempted",
                },
                required=True,
                attempts=12,
            )
            return
        if process.returncode != 0:
            _request(
                "POST",
                f"/internal/infrastructure-deployments/{deployment_id}/fail",
                {
                    "exit_code": process.returncode,
                    "error_code": "INFRASTRUCTURE_DEPLOYMENT_FAILED",
                    "error_message": "fixed MyGithut12 deployment script failed in fail-stop mode; no rollback attempted",
                },
                required=True,
                attempts=12,
            )
            return

        controller_healthy, private_ci_agent_healthy = _fixed_health_evidence()
        _request(
            "POST",
            f"/internal/infrastructure-deployments/{deployment_id}/complete",
            {
                "exit_code": 0,
                "controller_healthy": controller_healthy,
                "private_ci_agent_healthy": private_ci_agent_healthy,
                "message": "fixed MyGithut12 infrastructure deployment completed; runtime SHA is verified by the restarted Controller",
            },
            required=True,
            attempts=12,
        )
    except InfrastructureDeploymentSourceError as exc:
        _request(
            "POST",
            f"/internal/infrastructure-deployments/{deployment_id}/fail",
            {
                "exit_code": 1,
                "error_code": exc.code,
                "error_message": str(exc),
            },
            required=True,
            attempts=12,
        )
    except Exception as exc:
        _request(
            "POST",
            f"/internal/infrastructure-deployments/{deployment_id}/fail",
            {
                "exit_code": 1,
                "error_code": "INFRASTRUCTURE_EXECUTOR_ERROR",
                "error_message": type(exc).__name__,
            },
            required=True,
            attempts=12,
        )
    finally:
        if workspace and os.path.isdir(workspace):
            shutil.rmtree(workspace, ignore_errors=True)


def _heartbeat(state: str, current_deployment_id: str = "") -> None:
    _request(
        "POST",
        "/internal/infrastructure-deployments/heartbeat",
        {
            "executor_id": EXECUTOR_ID,
            "state": state,
            "current_deployment_id": current_deployment_id,
        },
        required=False,
        attempts=1,
    )


def _running_heartbeat_loop(deployment_id: str, stop_event: threading.Event) -> None:
    while not stop_event.wait(HEARTBEAT_SECONDS):
        try:
            _heartbeat("running", deployment_id)
        except Exception:
            # Heartbeat is best-effort while the fixed deployment keeps running.
            # A short Controller switch or callback transport failure must not abort it.
            pass


def _execute_with_heartbeat(row: dict) -> None:
    deployment_id = str(row["deployment_id"])
    try:
        _heartbeat("running", deployment_id)
    except Exception:
        # Establish the running identity before execute starts, but keep a short
        # Controller switch non-fatal just like background heartbeat failures.
        pass
    stop_event = threading.Event()
    heartbeat_thread = threading.Thread(
        target=_running_heartbeat_loop,
        args=(deployment_id, stop_event),
        name=f"infrastructure-deploy-heartbeat-{deployment_id}",
        daemon=False,
    )
    heartbeat_thread.start()
    try:
        execute(row)
    finally:
        stop_event.set()
        heartbeat_thread.join()
        _heartbeat("idle")


def main() -> None:
    last_heartbeat = 0.0
    while True:
        now = time.monotonic()
        if now - last_heartbeat >= HEARTBEAT_SECONDS:
            _heartbeat("idle")
            last_heartbeat = now
        response = _request(
            "POST",
            "/internal/infrastructure-deployments/claim",
            {"executor_id": EXECUTOR_ID},
            required=True,
            attempts=5,
        )
        row = (response or {}).get("deployment")
        if not row:
            time.sleep(POLL_SECONDS)
            continue
        _execute_with_heartbeat(row)
        last_heartbeat = time.monotonic()
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
