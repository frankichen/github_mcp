"""受控 gongshi-test 部署编排。

这里只保存部署意图和状态；远程执行由独立 deploy worker 负责，Controller 不接受任意 SSH、Shell、主机或脚本路径。
"""

import json
import os
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone

from app.ci_database import get_job
from app.github_utils import SHA_RE, _get_gh, _error_response

REPOSITORY = "frankichen/sxt"
ENVIRONMENT = "gongshi-test"
SCOPE = "fullstack"
PROFILE = "repo-auto-check"
STATUSES = ("queued", "preparing", "building", "uploading", "migrating", "switching", "verifying", "passed", "failed", "cancel_requested", "cancelled", "rolling_back", "rolled_back")
INFRA_FILES = ("scripts/deploy_gongshi_test.sh", "deploy/", "scripts/sync_test_env.sh")
_lock = threading.Lock()
_local = threading.local()


def _get_deploy_db():
    """部署状态使用独立 SQLite 文件，避免与 private CI 队列互相锁表或混淆 ID。"""
    if not hasattr(_local, "db") or _local.db is None:
        path = os.environ.get("DEPLOYMENT_DB_PATH", "/data/deployments.db")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        db = sqlite3.connect(path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=15000")
        _local.db = db
    return _local.db


def _now() -> float:
    return time.time()


def _iso(value):
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat() if value else None


def init_deployment_db():
    db = _get_deploy_db()
    db.executescript("""
    CREATE TABLE IF NOT EXISTS deployments (
      deployment_id TEXT PRIMARY KEY, repository TEXT NOT NULL, environment TEXT NOT NULL,
      commit_sha TEXT NOT NULL, private_ci_job_id TEXT NOT NULL, requested_scope TEXT NOT NULL,
      current_release_before TEXT, target_release TEXT, requested_by TEXT NOT NULL DEFAULT 'mcp',
      status TEXT NOT NULL, current_step TEXT, exit_code INTEGER, error_code TEXT,
      error_message TEXT, rollback_attempted INTEGER NOT NULL DEFAULT 0,
      rollback_succeeded INTEGER NOT NULL DEFAULT 0, cancel_requested INTEGER NOT NULL DEFAULT 0,
      frontend_included INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL,
      started_at REAL, finished_at REAL, log_text TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_deployments_filter ON deployments(repository, environment, commit_sha, status, created_at);
    """)
    db.commit()


def _row(row):
    if not row: return None
    data = dict(row)
    for key in ("created_at", "started_at", "finished_at"):
        data[key] = _iso(data[key])
    data["frontend_included"] = bool(data["frontend_included"])
    data["rollback_attempted"] = bool(data["rollback_attempted"])
    data["rollback_succeeded"] = bool(data["rollback_succeeded"])
    data["cancel_requested"] = bool(data["cancel_requested"])
    data.pop("log_text", None)
    return data


def _validate_common(repository, environment, scope, commit_sha):
    if repository != REPOSITORY: return "REPOSITORY_NOT_ALLOWED"
    if environment != ENVIRONMENT: return "ENVIRONMENT_NOT_ALLOWED"
    if scope != SCOPE: return "SCOPE_NOT_ALLOWED"
    if not SHA_RE.fullmatch(commit_sha): return "INVALID_COMMIT_SHA"
    return None


def _repo_state(commit_sha):
    gh = _get_gh()
    repo = gh.get_repo(REPOSITORY)
    main_sha = repo.get_branch("main").commit.sha
    files = [f.filename for f in repo.get_commit(commit_sha).files]
    return main_sha, files


def _ci_gate(job_id, commit_sha):
    job = get_job(job_id)
    if not job: return "PRIVATE_CI_JOB_NOT_FOUND", None
    if job.get("repository") != REPOSITORY: return "PRIVATE_CI_REPOSITORY_MISMATCH", job
    if job.get("branch") != "main": return "PRIVATE_CI_BRANCH_MISMATCH", job
    if job.get("commit_sha") != commit_sha: return "PRIVATE_CI_SHA_MISMATCH", job
    if job.get("profile") != PROFILE: return "PRIVATE_CI_PROFILE_MISMATCH", job
    if job.get("status") != "passed" or job.get("exit_code") != 0: return "PRIVATE_CI_NOT_PASSED", job
    if job.get("superseded_by_job_id"): return "PRIVATE_CI_SUPERSEDED", job
    return None, job


def plan_test_deployment(repository, environment, commit_sha, private_ci_job_id, scope=SCOPE, expected_current_release_id="", allow_deploy_infrastructure_changes=False):
    reason = _validate_common(repository, environment, scope, commit_sha)
    if reason: return {"ok": True, "ready": False, "reasons": [reason]}
    reasons = []
    try:
        main_sha, files = _repo_state(commit_sha)
    except Exception as exc:
        return {"ok": True, "ready": False, "reasons": ["GITHUB_STATE_UNAVAILABLE"], "message": str(exc)}
    if commit_sha != main_sha: reasons.append("COMMIT_NOT_CURRENT_MAIN")
    ci_reason, ci_job = _ci_gate(private_ci_job_id, commit_sha)
    if ci_reason: reasons.append(ci_reason)
    infra_changed = any(path == "scripts/deploy_gongshi_test.sh" or path == "scripts/sync_test_env.sh" or path.startswith("deploy/") for path in files)
    if infra_changed and not allow_deploy_infrastructure_changes: reasons.append("DEPLOY_INFRASTRUCTURE_CHANGE_REQUIRES_EXPLICIT_ALLOW")
    migrations_changed = any(path.startswith("db/migrations/") for path in files)
    return {"ok": True, "ready": not reasons, "reasons": reasons, "repository": repository, "environment": environment,
            "commit_sha": commit_sha, "exact_main_sha": main_sha, "private_ci": ci_job,
            "current_release_id": None, "target_release_id": f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{commit_sha[:12]}",
            "changed_files": files, "migrations_changed": migrations_changed, "deploy_infrastructure_changed": infra_changed,
            "backend_components": ["lenshub-api", "lenshub-worker", "lenshub-scheduler", "lenshub-admin-bootstrap", "goose"],
            "frontend_components": ["admin", "newadmin", "root", "app", "h5", "tester", "docs", "openapi"],
            "restart_services": ["api", "worker", "scheduler", "web"],
            "health_endpoints": ["/public/v1/health", "/nginx-health", "/", "/app/", "/h5/", "/admin/", "/newadmin/", "/tester/", "/docs/"],
            "rollback_plan": "flock 后恢复 previous current；不执行 goose down，不删除 release", "expected_current_release_id": expected_current_release_id}


def start_test_deployment(repository, environment, commit_sha, private_ci_job_id, scope=SCOPE, expected_current_release_id="", allow_deploy_infrastructure_changes=False, force_redeploy=False, confirm=False, requested_by="mcp"):
    if not confirm: return _error_response("CONFIRM_REQUIRED", "confirm must be true")
    plan = plan_test_deployment(repository, environment, commit_sha, private_ci_job_id, scope, expected_current_release_id, allow_deploy_infrastructure_changes)
    if not plan.get("ready"): return {**plan, "error": {"code": plan.get("reasons", ["NOT_READY"])[0]}}
    init_deployment_db(); db = _get_deploy_db()
    with _lock:
        active = db.execute("SELECT deployment_id FROM deployments WHERE environment=? AND status IN ('queued','preparing','building','uploading','migrating','switching','verifying','cancel_requested')", (environment,)).fetchone()
        if active: return _error_response("DEPLOYMENT_ALREADY_ACTIVE", "another deployment is active", details={"deployment_id": active[0]})
        if not force_redeploy and db.execute("SELECT 1 FROM deployments WHERE environment=? AND commit_sha=? AND status='passed'", (environment, commit_sha)).fetchone():
            return _error_response("ALREADY_DEPLOYED", "same SHA already passed")
        deployment_id = "dep_" + uuid.uuid4().hex
        now = _now()
        db.execute("INSERT INTO deployments(deployment_id,repository,environment,commit_sha,private_ci_job_id,requested_scope,target_release,status,created_at,requested_by,frontend_included) VALUES(?,?,?,?,?,?,?,?,?,?,1)", (deployment_id, repository, environment, commit_sha, private_ci_job_id, scope, plan["target_release_id"], "queued", now, requested_by))
        db.commit()
    return {"ok": True, "deployment_id": deployment_id, "status": "queued", "target_release_id": plan["target_release_id"], "message": "queued for private-deploy-agent"}


def get_test_deployment(deployment_id):
    init_deployment_db(); row = _get_deploy_db().execute("SELECT * FROM deployments WHERE deployment_id=?", (deployment_id,)).fetchone()
    return {"ok": True, "deployment": _row(row)} if row else _error_response("DEPLOYMENT_NOT_FOUND", "deployment not found")


def get_test_deployment_logs(deployment_id, offset=0, limit=200):
    init_deployment_db(); row = _get_deploy_db().execute("SELECT log_text FROM deployments WHERE deployment_id=?", (deployment_id,)).fetchone()
    if not row: return _error_response("DEPLOYMENT_NOT_FOUND", "deployment not found")
    text = row[0] or ""; page = text[offset:offset + limit]
    return {"ok": True, "deployment_id": deployment_id, "offset": offset, "limit": limit, "content": page, "next_offset": offset + len(page) if offset + len(page) < len(text) else None, "total_bytes": len(text)}


def list_test_deployments(repository="", environment="", commit_sha="", status="", limit=20, offset=0):
    init_deployment_db(); clauses=[]; params=[]
    for col, value in (("repository",repository),("environment",environment),("commit_sha",commit_sha),("status",status)):
        if value: clauses.append(col+"=?"); params.append(value)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = _get_deploy_db().execute("SELECT * FROM deployments" + where + " ORDER BY created_at DESC LIMIT ? OFFSET ?", (*params, min(max(limit, 1), 100), max(offset,0))).fetchall()
    return {"ok": True, "items": [_row(row) for row in rows], "limit": limit, "offset": offset}


def cancel_test_deployment(deployment_id):
    init_deployment_db(); db=_get_deploy_db(); row=db.execute("SELECT * FROM deployments WHERE deployment_id=?",(deployment_id,)).fetchone()
    if not row: return _error_response("DEPLOYMENT_NOT_FOUND", "deployment not found")
    if row["status"] in ("passed","failed","rolled_back","cancelled"): return _error_response("DEPLOYMENT_NOT_CANCELLABLE", "deployment already finished")
    if row["status"] == "queued": db.execute("UPDATE deployments SET status='cancelled',finished_at=? WHERE deployment_id=?",(_now(),deployment_id))
    else: db.execute("UPDATE deployments SET status='cancel_requested',cancel_requested=1 WHERE deployment_id=?",(deployment_id,))
    db.commit(); return get_test_deployment(deployment_id)


def _status_snapshot():
    path = os.environ.get("DEPLOY_STATUS_FILE", "/data/gongshi-test-status.json")
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
        # 仅接受状态摘要，拒绝把 env/credential 内容带回 MCP。
        value.pop("env", None); value.pop("secrets", None); value.pop("credentials", None)
        return value
    except (OSError, ValueError):
        return None


def get_test_environment_status(repository, environment):
    reason = _validate_common(repository, environment, SCOPE, "0" * 40)
    if reason == "INVALID_COMMIT_SHA": reason = None
    if repository != REPOSITORY: return _error_response("REPOSITORY_NOT_ALLOWED", "repository is not allowed")
    if environment != ENVIRONMENT: return _error_response("ENVIRONMENT_NOT_ALLOWED", "environment is not allowed")
    snapshot = _status_snapshot()
    return {"ok": True, "environment": environment, "status_available": bool(snapshot), "status": snapshot or {"current_release_id": None, "message": "deploy worker status is not configured"}}


def list_test_releases(repository, environment, limit=20):
    if repository != REPOSITORY: return _error_response("REPOSITORY_NOT_ALLOWED", "repository is not allowed")
    if environment != ENVIRONMENT: return _error_response("ENVIRONMENT_NOT_ALLOWED", "environment is not allowed")
    snapshot = _status_snapshot() or {}
    releases = snapshot.get("releases", [])
    return {"ok": True, "items": releases[:min(max(limit, 1), 100)]}


def rollback_test_deployment(repository, environment, target_release_id, expected_current_release_id, confirm=False, requested_by="mcp"):
    if not confirm: return _error_response("CONFIRM_REQUIRED", "confirm must be true")
    if repository != REPOSITORY: return _error_response("REPOSITORY_NOT_ALLOWED", "repository is not allowed")
    if environment != ENVIRONMENT: return _error_response("ENVIRONMENT_NOT_ALLOWED", "environment is not allowed")
    snapshot = _status_snapshot() or {}
    current = snapshot.get("current_release_id")
    if not current: return _error_response("ENVIRONMENT_STATUS_UNAVAILABLE", "deploy worker status is unavailable")
    if current != expected_current_release_id: return _error_response("CURRENT_RELEASE_CHANGED", "current release changed", details={"current_release_id": current})
    if target_release_id == current: return _error_response("TARGET_IS_CURRENT", "target release is already current")
    releases = {r.get("release_id"): r for r in snapshot.get("releases", [])}
    target = releases.get(target_release_id)
    if not target: return _error_response("RELEASE_NOT_FOUND", "target release not found")
    if target.get("checksum_status") != "passed": return _error_response("RELEASE_CHECKSUM_FAILED", "target release checksum is not verified")
    init_deployment_db(); db=_get_deploy_db(); deployment_id="dep_"+uuid.uuid4().hex; now=_now()
    db.execute("INSERT INTO deployments(deployment_id,repository,environment,commit_sha,private_ci_job_id,requested_scope,current_release_before,target_release,status,created_at,requested_by,frontend_included) VALUES(?,?,?,?,?,?,?,?,?,?,?,1)", (deployment_id,repository,environment,target.get("git_sha", ""),"not_applicable", "rollback", current,target_release_id,"queued",now,requested_by))
    db.commit()
    return {"ok": True, "deployment_id": deployment_id, "type": "rollback", "status": "queued", "target_release_id": target_release_id}
