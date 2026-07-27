"""private-deploy-agent 的最小受控 worker。

Worker 只执行固定仓库、固定脚本、固定 scope；SSH 主机、compose/env 路径来自进程受控配置，绝不来自 MCP 参数。
"""

import os
import re
import subprocess
import time
import json
import logging
import secrets
import fcntl

from app.deployment_service import init_deployment_db, _get_deploy_db

CONTRACTS = {
    "frankichen/sxt": {
        "workspace": "DEPLOY_WORKSPACE",
        "workspace_default": "/srv/private-ci/deploy-workspace/sxt",
        "script": "scripts/deploy_gongshi_test.sh",
        "environment": "gongshi-test",
        "scope": "fullstack",
        "status_file": "DEPLOY_STATUS_FILE",
        "status_default": "/var/lib/private-ci/gongshi-test-status.json",
    },
    "frankichen/auto_gupiao": {
        "workspace": "AUTO_GUPIAO_DEPLOY_WORKSPACE",
        "workspace_default": "/srv/private-ci/deploy-workspace/auto_gupiao",
        "script": "scripts/deploy_auto_gupiao.sh",
        "environment": "auto-gupiao-test",
        "scope": "reports",
        "status_file": "AUTO_GUPIAO_DEPLOY_STATUS_FILE",
        "status_default": "/var/lib/private-ci/auto-gupiao-status.json",
    },
}


def _contract(repository):
    return CONTRACTS.get(repository)


def _workspace(contract):
    return os.environ.get(contract["workspace"], contract["workspace_default"])


def _status_path(contract):
    return os.environ.get(contract["status_file"], contract["status_default"])


WORKSPACE = os.environ.get("DEPLOY_WORKSPACE", "/srv/private-ci/deploy-workspace/sxt")
SCRIPT = "scripts/deploy_gongshi_test.sh"
SECRET_RE = re.compile(r"(?i)(token|authorization|password|secret|database_url|cookie|private_key)=\S+")
STATUS_PATH = os.environ.get("DEPLOY_STATUS_FILE", "/var/lib/private-ci/gongshi-test-status.json")
logger = logging.getLogger("private-deploy-agent")
_status_repository = "frankichen/sxt"


def redact(value: str) -> str:
    return SECRET_RE.sub(lambda m: m.group(1) + "=***", value)


def write_status(agent_status: str, current_step: str = "idle", current_release_id=None, previous_release_id=None, message=None):
    contract = _contract(_status_repository) or CONTRACTS["frankichen/sxt"]
    status_path = _status_path(contract)
    prior = {}
    try:
        with open(status_path, encoding="utf-8") as handle:
            prior = json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    # Agent liveness updates must not erase the verified release registry.
    if current_release_id is None:
        current_release_id = prior.get("current_release_id")
    if previous_release_id is None:
        previous_release_id = prior.get("previous_release_id")
    payload = {
        "environment": contract.get("environment", "gongshi-test"),
        "environment_url": os.environ.get("ENVIRONMENT_URL", "http://gongshi-test"),
        "agent_status": agent_status,
        "current_step": current_step,
        "current_release_id": current_release_id,
        "previous_release_id": previous_release_id,
        "message": message or "deploy worker is online; no release was executed",
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    for key in ("current_git_sha", "current_release_path", "source", "verified_at",
                "manifest_verified", "checksum_verified", "health_verified",
                "services_healthy", "releases"):
        if key in prior:
            payload[key] = prior[key]
    directory = os.path.dirname(status_path)
    os.makedirs(directory, exist_ok=True)
    with open(status_path, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.seek(0)
        handle.truncate()
        json.dump(payload, handle, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def process_once() -> bool:
    global _status_repository
    init_deployment_db(); db = _get_deploy_db()
    rows = db.execute(
        "SELECT deployment_id FROM deployments "
        "WHERE status='queued' AND repository IN (?, ?) "
        "ORDER BY created_at LIMIT 20",
        ("frankichen/sxt", "frankichen/auto_gupiao"),
    ).fetchall()
    if rows:
        _status_repository = rows[0]["repository"]
    logger.info("polling queue: http_status=local_sqlite rows=%d", len(rows))
    retained_release = retained_previous = None
    try:
        with open(_status_path(_contract(_status_repository) or CONTRACTS["frankichen/sxt"]), encoding="utf-8") as handle:
            prior_status = json.load(handle)
        retained_release = prior_status.get("current_release_id")
        retained_previous = prior_status.get("previous_release_id")
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    write_status("online", "polling", retained_release, retained_previous, message=f"polling local deployment queue; queued_rows={len(rows)}")
    if not rows: return False
    row = db.execute("SELECT * FROM deployments WHERE deployment_id=? AND status='queued'", (rows[0]["deployment_id"],)).fetchone()
    if not row: return False
    dep_id = row["deployment_id"]
    lease_token = secrets.token_urlsafe(24)
    claimed = db.execute(
        "UPDATE deployments SET status='claimed',current_step='claimed',started_at=?,updated_at=?,lease_token=? "
        "WHERE deployment_id=? AND status='queued'",
        (time.time(), time.time(), lease_token, dep_id),
    )
    db.commit()
    if claimed.rowcount != 1:
        logger.info("claim skipped: deployment_id=%s no longer queued", dep_id)
        return False
    logger.info("claimed deployment_id=%s target_release=%s", dep_id, row["target_release"])
    # A queued target is not the environment's current release.  Keep the
    # verified current release untouched until the complete callback proves a
    # successful symlink switch and health check.
    write_status("busy", "claimed", retained_release, retained_previous, f"claimed deployment {dep_id}")
    if os.environ.get("DEPLOY_EXECUTION_MODE") == "claim_only":
        prior_log = row["log_text"] or ""
        log_text = (prior_log + ("\n" if prior_log else "") + f"agent claimed deployment {dep_id}; execution delegated to WSL")[-200000:]
        db.execute(
            "UPDATE deployments SET status='running',current_step='claimed',updated_at=?,log_revision=log_revision+1,log_text=? WHERE deployment_id=?",
            (time.time(), log_text, dep_id),
        )
        db.commit()
        logger.info("claim-only mode: deployment_id=%s delegated to WSL", dep_id)
        write_status("online", "polling", retained_release, retained_previous, f"deployment {dep_id} delegated to WSL")
        return True
    contract = _contract(row["repository"])
    if not contract:
        db.execute("UPDATE deployments SET status='failed',current_step='unsupported_repository',error_code='REPOSITORY_NOT_ALLOWED',error_message='no deployment contract',exit_code=1,finished_at=?,updated_at=? WHERE deployment_id=?", (time.time(), time.time(), dep_id)); db.commit()
        return True
    workspace = _workspace(contract)
    script = os.path.join(workspace, contract["script"])
    argv = ["bash", script, "--yes", "--expected-sha", row["commit_sha"]]
    if row["repository"] == "frankichen/sxt":
        argv.insert(3, "--with-frontend")
    try:
        deploy_env = os.environ.copy()
        date_shim_dir = deploy_env.get("DEPLOY_DATE_SHIM_DIR")
        if date_shim_dir:
            deploy_env["PATH"] = f"{date_shim_dir}:{deploy_env.get('PATH', '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin')}"
        proc = subprocess.Popen(argv, cwd=workspace, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=deploy_env)
        output = []
        for line in proc.stdout:
            safe_line = redact(line)
            output.append(safe_line)
            lowered = safe_line.lower()
            step = "verifying" if "health" in lowered or "verification" in lowered else "uploading" if "upload" in lowered or "incoming" in lowered else "building" if "build" in lowered or "npm" in lowered or "go test" in lowered else "preparing"
            db.execute("UPDATE deployments SET current_step=?,log_text=? WHERE deployment_id=?", (step, "".join(output)[-200000:], dep_id))
            db.commit()
            write_status("busy", step, retained_release, retained_previous, f"deployment {dep_id} {step}")
        proc.wait()
        final_status = "passed" if proc.returncode == 0 else "failed"
        db.execute("UPDATE deployments SET status=?,current_step=?,exit_code=?,finished_at=?,log_text=? WHERE deployment_id=?", (final_status, "completed" if proc.returncode == 0 else "failed", proc.returncode, time.time(), "".join(output)[-200000:], dep_id)); db.commit()
        logger.info("deployment finished: deployment_id=%s status=%s exit_code=%s", dep_id, final_status, proc.returncode)
        write_status("online", "idle", retained_release, retained_previous, f"deployment {dep_id} {final_status}")
    except Exception as exc:
        db.execute("UPDATE deployments SET status='failed',current_step='worker_error',exit_code=1,finished_at=?,error_code='DEPLOY_WORKER_ERROR',error_message=?,log_text=? WHERE deployment_id=?", (time.time(), type(exc).__name__, redact(str(exc)), dep_id)); db.commit()
        logger.exception("deployment worker failed: deployment_id=%s", dep_id)
        write_status("online", "idle", None, None, f"deployment {dep_id} failed")
    return True


def main():
    write_status("online", "starting")
    while True:
        if not process_once(): time.sleep(2)


if __name__ == "__main__":
    main()
