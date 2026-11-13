#!/usr/bin/env python3
"""Dedicated, fixed P2P lab deployment runner. Never operates on SXT."""
import os
import re
import subprocess
import time

import requests
from scripts import deploy_executor as base

REPOSITORY = "frankichen/p2p-server"
ENVIRONMENT = "gongshi-p2p-lab"
SCOPE = "p2p-lab"
SCRIPT = "scripts/deploy_gongshi_p2p_lab.sh"
RELEASE_ROOT = "/opt/p2p-server-lab/releases"
MIRROR = os.environ.get("P2P_DEPLOY_MIRROR", "/home/xiaowu/work/private-deploy-cache/frankichen-p2p-server.git")
ORIGIN = "https://github.com/frankichen/p2p-server.git"
WORKSPACES = os.environ.get("P2P_DEPLOY_WORKSPACES", "/home/xiaowu/work/private-deploy-cache/p2p-workspaces")


def approved(row):
    return (row.get("repository") == REPOSITORY
            and row.get("environment") == ENVIRONMENT
            and row.get("requested_scope") == SCOPE
            and re.fullmatch(r"[0-9a-f]{40}", str(row.get("commit_sha", ""))) is not None
            and re.fullmatch(r"dep_[0-9a-f]{32}", str(row.get("deployment_id", ""))) is not None)


def prepare_workspace(row):
    """Use existing exact SHA source verification with an isolated P2P mirror."""
    if not os.path.isdir(MIRROR):
        # Fixed public source mirror: no user-supplied URL, host or shell.
        os.makedirs(os.path.dirname(MIRROR), exist_ok=True)
        cloned = subprocess.run(["git", "clone", "--mirror", "--", ORIGIN, MIRROR],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=180, check=False)
        if cloned.returncode:
            raise base.DeploymentSourceError("P2P_MIRROR_BOOTSTRAP_FAILED",
                                             "fixed P2P repository mirror unavailable")
    names = ("DEPLOY_MIRROR", "AUTHORITATIVE_REPOSITORY_URL", "DEPLOY_WORKSPACES")
    original = {name: getattr(base, name) for name in names}
    try:
        base.DEPLOY_MIRROR = MIRROR
        base.AUTHORITATIVE_REPOSITORY_URL = ORIGIN
        base.DEPLOY_WORKSPACES = WORKSPACES
        return base.prepare_workspace(row)
    finally:
        for name, value in original.items():
            setattr(base, name, value)


def release_evidence(output, expected_sha):
    reports = [line.strip() for line in output.splitlines() if line.startswith("release_id=")]
    if not reports:
        return None
    values = {}
    for item in reports[-1].split():
        key, sep, value = item.partition("=")
        if not sep or key in values:
            return None
        values[key] = value
    if set(values) != {"release_id", "git_sha", "current", "health", "checksum", "manifest"}:
        return None
    release_id = values["release_id"]
    if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{12}", release_id):
        return None
    if values["git_sha"] != expected_sha or not release_id.endswith(expected_sha[:12]):
        return None
    if values["current"] != RELEASE_ROOT + "/" + release_id:
        return None
    if any(values[key] != "passed" for key in ("health", "checksum", "manifest")):
        return None
    return release_id, values["current"]


def execute(row):
    if not approved(row):
        return
    endpoint = "/internal/deployments/" + row["deployment_id"]
    base._callback(endpoint + "/progress", {"current_step": "preparing_workspace",
                                             "status": "running", "message": "P2P exact main checkout"})
    try:
        workspace, _ = prepare_workspace(row)
        script = os.path.join(workspace, SCRIPT)
        if not os.path.isfile(script) or os.path.islink(script):
            raise base.DeploymentSourceError("P2P_DEPLOY_SCRIPT_UNAVAILABLE",
                                             "exact-main fixed P2P deployment script missing")
        base._callback(endpoint + "/progress", {"current_step": "building",
                                                 "status": "running", "message": "P2P isolated build"})
        result = subprocess.run(
            ["bash", script, "--yes", "--expected-sha", row["commit_sha"]],
            cwd=workspace, env={**os.environ, "DEPLOYMENT_ID": row["deployment_id"]},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            timeout=base.TIMEOUT_SECONDS, check=False,
        )
        if result.returncode != 0:
            base._callback(endpoint + "/fail", {"exit_code": result.returncode,
                "error_code": "P2P_DEPLOYMENT_FAILED", "error_message": "P2P lab script failed"})
            return
        evidence = release_evidence(result.stdout[-32768:], row["commit_sha"])
        if evidence is None:
            base._callback(endpoint + "/fail", {"exit_code": 1,
                "error_code": "P2P_RELEASE_EVIDENCE_INVALID",
                "error_message": "P2P exact SHA, checksum or health was not verified"})
            return
        release_id, current_path = evidence
        release = {"release_id": release_id, "repository": REPOSITORY,
                   "environment": ENVIRONMENT, "git_sha": row["commit_sha"],
                   "current_release_path": current_path, "frontend_included": False,
                   "manifest_verified": True, "checksum_verified": True,
                   "health_verified": True, "services_healthy": True,
                   "deployment_id": row["deployment_id"], "status": "passed"}
        base._callback(endpoint + "/complete", {"exit_code": 0,
            "message": "P2P isolated lab release verified", "release": release})
    except base.DeploymentSourceError as exc:
        base._callback(endpoint + "/fail", {"exit_code": 1, "error_code": exc.code,
                                            "error_message": "P2P exact source validation failed"})
    except subprocess.TimeoutExpired:
        base._callback(endpoint + "/fail", {"exit_code": 124,
            "error_code": "P2P_DEPLOYMENT_TIMEOUT", "error_message": "P2P lab deployment timeout"})
    except Exception as exc:
        base._callback(endpoint + "/fail", {"exit_code": 1,
            "error_code": "P2P_DEPLOY_EXECUTOR_ERROR", "error_message": type(exc).__name__})


def main():
    while True:
        response = requests.get(base.CONTROLLER_URL + "/internal/deployments/assigned",
            headers={"X-Deployment-Callback-Key": base._key()}, timeout=15)
        response.raise_for_status()
        for row in response.json().get("items", []):
            if approved(row):
                execute(row)
        time.sleep(2)


if __name__ == "__main__":
    main()
