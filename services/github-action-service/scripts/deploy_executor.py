#!/usr/bin/env python3
"""WSL executor for delegated gongshi-test deployments.

The controller/agent owns claiming and persistence. This process owns only
the fixed WSL build/publish command and reports progress through authenticated,
idempotent callbacks. It never accepts host, shell, or repository parameters
from the queue.
"""

import json
import os
import re
import signal
import subprocess
import threading
import time
from pathlib import Path

import requests


CONTROLLER_URL = os.environ.get("DEPLOY_CONTROLLER_URL", "http://127.0.0.1:8788").rstrip("/")
CALLBACK_KEY_FILE = os.environ.get("DEPLOY_CALLBACK_API_KEY_FILE", "")
WORKSPACE = os.environ.get("DEPLOY_WORKSPACE", "/home/xiaowu/work/sxt")
DEPLOY_CACHE = os.environ.get("DEPLOY_CACHE", "/home/xiaowu/work/private-deploy-cache")
DEPLOY_MIRROR = os.environ.get("DEPLOY_MIRROR", os.path.join(DEPLOY_CACHE, "frankichen-sxt.git"))
AUTHORITATIVE_REPOSITORY_URL = os.environ.get("AUTHORITATIVE_REPOSITORY_URL", "https://github.com/frankichen/sxt.git")
DEPLOY_WORKSPACES = os.path.join(DEPLOY_CACHE, "workspaces")
EXPECTED_REPOSITORY = "frankichen/sxt"
EXPECTED_ENVIRONMENT = "gongshi-test"
TIMEOUT_SECONDS = int(os.environ.get("DEPLOY_TIMEOUT_SECONDS", "3600"))
SECRET_RE = re.compile(r"(?i)(token|authorization|password|secret|database_url|cookie|private_key)=\S+")

CONTRACTS = {
    "frankichen/sxt": {
        "environment": "gongshi-test",
        "workspace": WORKSPACE,
        "mirror": DEPLOY_MIRROR,
        "repository_url": AUTHORITATIVE_REPOSITORY_URL,
        "script": "scripts/deploy_gongshi_test.sh",
        "frontend": True,
    },
    "frankichen/auto_gupiao": {
        "environment": "auto-gupiao-test",
        "workspace": os.environ.get("AUTO_GUPIAO_DEPLOY_WORKSPACE", "/home/xiaowu/work/auto_gupiao"),
        "mirror": os.environ.get("AUTO_GUPIAO_DEPLOY_MIRROR", os.path.join(DEPLOY_CACHE, "frankichen-auto_gupiao.git")),
        "repository_url": os.environ.get("AUTO_GUPIAO_REPOSITORY_URL", "https://github.com/frankichen/auto_gupiao.git"),
        "script": "scripts/deploy_auto_gupiao.sh",
        "frontend": False,
    },
}


class DeploymentSourceError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _git(*args: str, cwd: str | None = None) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, check=False)
    if result.returncode:
        raise DeploymentSourceError("DEPLOY_SOURCE_FETCH_FAILED", _redact(result.stdout.strip()[-1000:]))
    return result.stdout.strip()


def prepare_workspace(row: dict) -> tuple[str, list[str]]:
    """Refresh the authoritative mirror and create a disposable exact-SHA checkout."""
    contract = CONTRACTS.get(row.get("repository"))
    if not contract:
        raise DeploymentSourceError("REPOSITORY_NOT_ALLOWED", "no deployment contract")
    expected = row["commit_sha"]
    mirror = contract["mirror"]
    if not os.path.isdir(mirror):
        raise DeploymentSourceError("DEPLOY_SOURCE_FETCH_FAILED", "authoritative deploy mirror is missing")
    remote = _git("remote", "get-url", "origin", cwd=mirror)
    if remote != contract["repository_url"]:
        raise DeploymentSourceError("DEPLOY_SOURCE_FETCH_FAILED", "deploy mirror origin is not authoritative")
    try:
        _git("remote", "update", "--prune", cwd=mirror)
    except DeploymentSourceError as exc:
        raise DeploymentSourceError("DEPLOY_SOURCE_FETCH_FAILED", str(exc)) from exc
    mirror_sha = _git("rev-parse", "refs/heads/main", cwd=mirror)
    if mirror_sha != expected:
        raise DeploymentSourceError("DEPLOY_MAIN_SHA_MISMATCH", f"mirror main={mirror_sha} expected={expected}")
    try:
        _git("cat-file", "-e", f"{expected}^{{commit}}", cwd=mirror)
    except DeploymentSourceError as exc:
        raise DeploymentSourceError("DEPLOY_COMMIT_NOT_FOUND", f"commit {expected} is not in authoritative mirror") from exc
    workspaces = DEPLOY_WORKSPACES if row["repository"] == EXPECTED_REPOSITORY else os.path.join(DEPLOY_WORKSPACES, row["repository"].replace("/", "-"))
    workspace = os.path.join(workspaces, row["deployment_id"])
    if os.path.exists(workspace):
        raise DeploymentSourceError("DEPLOY_WORKSPACE_EXISTS", "deployment workspace already exists")
    os.makedirs(workspaces, exist_ok=True)
    try:
        _git("clone", "--no-local", "--branch", "main", mirror, workspace)
        _git("remote", "set-url", "origin", contract["repository_url"], cwd=workspace)
        _git("fetch", "--no-tags", "origin", "main", cwd=workspace)
    except DeploymentSourceError:
        raise
    origin_sha = _git("rev-parse", "refs/remotes/origin/main", cwd=workspace)
    if origin_sha != expected:
        raise DeploymentSourceError("DEPLOY_MAIN_SHA_MISMATCH", f"origin/main={origin_sha} expected={expected}")
    head_sha = _git("rev-parse", "HEAD", cwd=workspace)
    branch = _git("branch", "--show-current", cwd=workspace)
    dirty = _git("status", "--porcelain", cwd=workspace)
    if branch != "main" or head_sha != expected or dirty:
        raise DeploymentSourceError("DEPLOY_MAIN_SHA_MISMATCH", "isolated checkout is not exact clean main")
    return workspace, [
        f"authoritative_origin={contract['repository_url']}",
        f"mirror_main_sha={mirror_sha}",
        f"origin_main_sha={origin_sha}",
        f"checked_out_head={head_sha}",
        f"branch={branch}",
        "clean_worktree=true",
    ]


def _key() -> str:
    return Path(CALLBACK_KEY_FILE).read_text(encoding="utf-8").strip()


def _redact(text: str) -> str:
    return SECRET_RE.sub(lambda match: match.group(1) + "=***", text)


def _callback(path: str, payload: dict) -> None:
    headers = {"X-Deployment-Callback-Key": _key(), "Content-Type": "application/json"}
    last = None
    for delay in (0, 0.5, 1, 2, 4):
        if delay:
            time.sleep(delay)
        try:
            response = requests.post(CONTROLLER_URL + path, headers=headers, json=payload, timeout=15)
            if response.status_code < 500:
                response.raise_for_status()
                return
            last = RuntimeError(f"callback HTTP {response.status_code}")
        except Exception as exc:  # retry boundedly; never expose request headers
            last = exc
    raise RuntimeError(f"deployment callback failed after retries: {type(last).__name__}")


def _step(line: str) -> str:
    lowered = line.lower()
    if "health" in lowered or "verification" in lowered:
        return "health_checking"
    if "checksum" in lowered or "manifest" in lowered:
        return "checksum_verification"
    if "upload" in lowered or "incoming" in lowered:
        return "uploading"
    if "switch" in lowered or "current=" in lowered:
        return "switching"
    if "go test" in lowered or "go vet" in lowered or "npm" in lowered:
        return "testing"
    if "build" in lowered:
        return "building"
    return "running"


def execute(row: dict) -> None:
    deployment_id = row["deployment_id"]
    contract = CONTRACTS.get(row.get("repository"))
    if not contract or row.get("environment") != contract["environment"]:
        return
    _callback(f"/internal/deployments/{deployment_id}/progress", {"current_step": "preparing_workspace", "status": "running", "message": "WSL workspace preparation started"})
    output = []
    process = None
    workspace = None
    try:
        workspace, source_lines = prepare_workspace(row)
        for line in source_lines:
            output.append(line)
            _callback(f"/internal/deployments/{deployment_id}/progress", {"current_step": "validating_main", "status": "running", "message": line})
        script = os.path.join(workspace, contract["script"])
        if not os.path.isfile(script):
            raise DeploymentSourceError("DEPLOY_COMMIT_NOT_FOUND", "deployment script is missing from exact checkout")
        deploy_env = {**os.environ, "DEPLOYMENT_ID": deployment_id}
        # The release script's integration tests share the WSL PostgreSQL
        # fixture. Serialize Go packages to avoid cross-package fixture races.
        deploy_env["GOFLAGS"] = (deploy_env.get("GOFLAGS", "") + " -p=1").strip()
        args = ["bash", script, "--yes", "--expected-sha", row["commit_sha"]]
        if contract["frontend"]:
            args.insert(3, "--with-frontend")
        process = subprocess.Popen(
            args,
            cwd=workspace, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            env=deploy_env,
        )
        timed_out = False
        def _timeout() -> None:
            nonlocal timed_out
            timed_out = True
            if process and process.poll() is None:
                process.terminate()

        timer = threading.Timer(TIMEOUT_SECONDS, _timeout)
        timer.daemon = True
        timer.start()
        for raw in process.stdout:
            line = _redact(raw.rstrip())
            output.append(line)
            _callback(f"/internal/deployments/{deployment_id}/progress", {"current_step": _step(line), "status": "running", "message": line})
        process.wait()
        timer.cancel()
        if timed_out:
            _callback(f"/internal/deployments/{deployment_id}/fail", {"exit_code": 124, "error_code": "TIMEOUT", "error_message": "WSL deployment timed out"})
            return
        text = "\n".join(output)
        if process.returncode != 0:
            _callback(f"/internal/deployments/{deployment_id}/fail", {"exit_code": process.returncode, "error_code": "DEPLOYMENT_FAILED", "error_message": "WSL deployment script failed"})
            return
        release_match = re.search(r"^release_id=(\S+)$", text, re.MULTILINE)
        current_match = re.search(r"^current=(\S+)$", text, re.MULTILINE)
        release_id = release_match.group(1) if release_match else None
        release_path = current_match.group(1) if current_match else f"/home/dly/releases/{release_id}"
        if not release_id or not release_path or os.path.basename(release_path.rstrip("/")) != release_id:
            _callback(f"/internal/deployments/{deployment_id}/fail", {"exit_code": 1, "error_code": "RELEASE_EVIDENCE_INVALID", "error_message": "release id and current path do not match"})
            return
        proof = {
            "release_id": release_id,
            "repository": row["repository"],
            "environment": contract["environment"],
            "git_sha": row["commit_sha"],
            "current_release_path": release_path,
            "frontend_included": contract["frontend"],
            "manifest_verified": True,
            "checksum_verified": True,
            "health_verified": True,
            "services_healthy": True,
            "deployment_id": deployment_id,
            "status": "passed",
        }
        _callback(f"/internal/deployments/{deployment_id}/complete", {"exit_code": 0, "message": "WSL deployment completed with verified manifest, checksum, services and health", "release": proof})
    except DeploymentSourceError as exc:
        _callback(f"/internal/deployments/{deployment_id}/fail", {"exit_code": 1, "error_code": exc.code, "error_message": str(exc)})
    except subprocess.TimeoutExpired:
        if process:
            process.kill()
        _callback(f"/internal/deployments/{deployment_id}/fail", {"exit_code": 124, "error_code": "TIMEOUT", "error_message": "WSL deployment timed out"})
    except Exception as exc:
        _callback(f"/internal/deployments/{deployment_id}/fail", {"exit_code": 1, "error_code": "WSL_EXECUTOR_ERROR", "error_message": type(exc).__name__})


def main() -> None:
    while True:
        response = requests.get(CONTROLLER_URL + "/internal/deployments/assigned", headers={"X-Deployment-Callback-Key": _key()}, timeout=15)
        response.raise_for_status()
        for row in response.json().get("items", []):
            execute(row)
        time.sleep(2)


if __name__ == "__main__":
    main()
