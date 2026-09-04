"""Fixed-contract MyGithut12 infrastructure deployment control-plane.

The Controller validates and persists deployment intent only. It never accepts a
host, shell command, script path, rollback mode, or arbitrary executor target.
A separate fixed executor owns the single production operation and always runs
it in fail-stop mode.
"""

from __future__ import annotations

import re
import threading
import time
import uuid

from app import infrastructure_deployment_store as store
from app.ci_database import get_job
from app.ci_repository_config import get_repository_config, is_self_deploy_enabled
from app.github_utils import SHA_RE, _error_response, _get_gh
from app.version import runtime_build_sha

REPOSITORY = "frankichen/github_mcp"
ENVIRONMENT = "mygithub12-production"
SCOPE = "control-plane"
PROFILE = "repo-auto-check"
DEFAULT_EXECUTOR_ID = "mygithub12-infrastructure-deploy-01"
DEFAULT_HEARTBEAT_TTL_SECONDS = 30
MAX_WAIT_SECONDS = 55
DEFAULT_LOG_TAIL_LINES = 40
WAIT_POLL_SECONDS = 0.25
TERMINAL_STATUSES = ("passed", "failed")
STRUCTURED_PHASES = {
    "source_prepare",
    "validation",
    "controller_build",
    "controller_switch",
    "health",
    "preheat",
    "post_verify",
    "completed",
    "failed",
}
LEGACY_PHASE_MAP = {
    "queued": "validation",
    "claimed": "source_prepare",
    "preflight": "validation",
    "validating_main": "validation",
    "deploying_control_plane": "controller_build",
    "running": "controller_build",
}
ERROR_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_lock = threading.Lock()

EXECUTION_MODE_FULL = "full"
EXECUTION_MODE_POST_SWITCH_RECOVERY = "post_switch_recovery"
RECOVERY_SWITCH_MARKER = "DX2_PHASE=controller_switch"


def _validate_recovery_source(
    source,
    repository: str,
    environment: str,
    scope: str,
    commit_sha: str,
    tree_sha: str,
) -> str | None:
    if source is None:
        return "INFRASTRUCTURE_RECOVERY_SOURCE_NOT_FOUND"
    if str(source["status"] or "") != "failed":
        return "INFRASTRUCTURE_RECOVERY_SOURCE_NOT_FAILED"
    if (
        source["repository"] != repository
        or source["environment"] != environment
        or source["requested_scope"] != scope
    ):
        return "INFRASTRUCTURE_RECOVERY_SOURCE_SCOPE_MISMATCH"
    if source["commit_sha"] != commit_sha or source["tree_sha"] != tree_sha:
        return "INFRASTRUCTURE_RECOVERY_SOURCE_TARGET_MISMATCH"
    source_mode = str(source["execution_mode"] or EXECUTION_MODE_FULL)
    inherited_post_switch = (
        source_mode == EXECUTION_MODE_POST_SWITCH_RECOVERY
        and bool(str(source["recovery_of_deployment_id"] or ""))
    )
    if RECOVERY_SWITCH_MARKER not in str(source["log_text"] or "") and not inherited_post_switch:
        return "INFRASTRUCTURE_RECOVERY_SOURCE_NOT_POST_SWITCH"
    return None


def _recovery_source_summary(source) -> dict | None:
    if source is None:
        return None
    return {
        "deployment_id": source["deployment_id"],
        "status": source["status"],
        "error_code": source["error_code"],
        "execution_mode": source["execution_mode"],
        "recovery_of_deployment_id": source["recovery_of_deployment_id"],
    }


def init_infrastructure_deployment_db() -> None:
    store.init_db()


def _spec(repository: str) -> dict:
    if repository != REPOSITORY or not is_self_deploy_enabled(repository):
        return {}
    configured = dict(get_repository_config(repository).get("infrastructure_deployment") or {})
    if not configured.get("enabled"):
        return {}
    return {
        "environment": str(configured.get("environment") or ENVIRONMENT),
        "scope": str(configured.get("scope") or SCOPE),
        "profile": str(configured.get("profile") or PROFILE),
        "executor_id": str(configured.get("executor_id") or DEFAULT_EXECUTOR_ID),
        "heartbeat_ttl_seconds": int(
            configured.get("heartbeat_ttl_seconds") or DEFAULT_HEARTBEAT_TTL_SECONDS
        ),
    }


def _executor(repository: str = REPOSITORY) -> dict:
    spec = _spec(repository)
    return store.executor_snapshot(
        spec.get("executor_id") or DEFAULT_EXECUTOR_ID,
        int(spec.get("heartbeat_ttl_seconds") or DEFAULT_HEARTBEAT_TTL_SECONDS),
    )


def _validate_common(
    repository: str,
    environment: str,
    scope: str,
    commit_sha: str,
    expected_current_build_sha: str,
) -> str | None:
    spec = _spec(repository)
    if not spec:
        return "REPOSITORY_NOT_ALLOWED"
    if environment != spec["environment"]:
        return "ENVIRONMENT_NOT_ALLOWED"
    if scope != spec["scope"]:
        return "SCOPE_NOT_ALLOWED"
    if not SHA_RE.fullmatch(commit_sha):
        return "INVALID_COMMIT_SHA"
    if not SHA_RE.fullmatch(expected_current_build_sha):
        return "INVALID_EXPECTED_CURRENT_BUILD_SHA"
    return None


def _repo_state(repository: str, commit_sha: str) -> tuple[str, str]:
    gh = _get_gh()
    repo = gh.get_repo(repository)
    main_sha = repo.get_branch("main").commit.sha
    tree_sha = repo.get_commit(commit_sha).commit.tree.sha
    return main_sha, tree_sha


def _ci_gate(
    repository: str,
    job_id: str,
    commit_sha: str,
    tree_sha: str,
) -> tuple[str | None, dict | None]:
    spec = _spec(repository)
    job = get_job(job_id)
    if not job:
        return "PRIVATE_CI_JOB_NOT_FOUND", None
    checks = (
        (job.get("repository") == repository, "PRIVATE_CI_REPOSITORY_MISMATCH"),
        (job.get("branch") == "main", "PRIVATE_CI_BRANCH_MISMATCH"),
        (job.get("commit_sha") == commit_sha, "PRIVATE_CI_SHA_MISMATCH"),
        (
            not job.get("tree_sha") or job.get("tree_sha") == tree_sha,
            "PRIVATE_CI_TREE_MISMATCH",
        ),
        (job.get("profile") == spec.get("profile", PROFILE), "PRIVATE_CI_PROFILE_MISMATCH"),
        (
            job.get("status") == "passed" and int(job.get("exit_code") or 0) == 0,
            "PRIVATE_CI_NOT_PASSED",
        ),
        (not job.get("superseded_by_job_id"), "PRIVATE_CI_SUPERSEDED"),
    )
    for passed, code in checks:
        if not passed:
            return code, job
    return None, job


def register_infrastructure_executor_heartbeat(
    executor_id: str,
    state: str = "idle",
    current_deployment_id: str = "",
) -> dict:
    spec = _spec(REPOSITORY)
    if executor_id != spec.get("executor_id"):
        return _error_response("EXECUTOR_NOT_ALLOWED", "executor identity is not allowed")
    if state not in {"idle", "running"}:
        return _error_response("EXECUTOR_STATE_INVALID", "executor state must be idle or running")
    store.write_executor_heartbeat(
        executor_id,
        state,
        str(current_deployment_id or "") or None,
    )
    return {"ok": True, "executor": _executor()}


def plan_infrastructure_deployment(
    repository: str,
    environment: str,
    commit_sha: str,
    private_ci_job_id: str,
    expected_current_build_sha: str,
    scope: str = SCOPE,
    recovery_of_deployment_id: str = "",
) -> dict:
    reason = _validate_common(
        repository,
        environment,
        scope,
        commit_sha,
        expected_current_build_sha,
    )
    if reason:
        return {"ok": True, "ready": False, "reasons": [reason]}

    try:
        main_sha, tree_sha = _repo_state(repository, commit_sha)
    except Exception as exc:
        return {
            "ok": True,
            "ready": False,
            "reasons": ["GITHUB_STATE_UNAVAILABLE"],
            "message": type(exc).__name__,
        }

    recovery_id = str(recovery_of_deployment_id or "").strip()
    execution_mode = (
        EXECUTION_MODE_POST_SWITCH_RECOVERY if recovery_id else EXECUTION_MODE_FULL
    )
    recovery_source = store.get_deployment(recovery_id) if recovery_id else None

    reasons: list[str] = []
    if main_sha != commit_sha:
        reasons.append("COMMIT_NOT_CURRENT_MAIN")
    current_build_sha = runtime_build_sha()
    if current_build_sha != expected_current_build_sha:
        reasons.append("CURRENT_BUILD_SHA_MISMATCH")
    if recovery_id:
        recovery_reason = _validate_recovery_source(
            recovery_source, repository, environment, scope, commit_sha, tree_sha
        )
        if recovery_reason:
            reasons.append(recovery_reason)
        if current_build_sha != commit_sha:
            reasons.append("INFRASTRUCTURE_RECOVERY_RUNTIME_NOT_TARGET")
    elif current_build_sha == commit_sha:
        reasons.append("ALREADY_DEPLOYED")

    ci_reason, ci_job = _ci_gate(repository, private_ci_job_id, commit_sha, tree_sha)
    if ci_reason:
        reasons.append(ci_reason)
    active = store.active_deployment(repository, environment)
    if active:
        reasons.append("INFRASTRUCTURE_DEPLOYMENT_ALREADY_ACTIVE")
    executor = _executor(repository)
    if not executor.get("online"):
        reasons.append("INFRASTRUCTURE_EXECUTOR_OFFLINE")
    elif executor.get("state") != "idle":
        reasons.append("INFRASTRUCTURE_EXECUTOR_BUSY")

    return {
        "ok": True,
        "ready": not reasons,
        "reasons": reasons,
        "repository": repository,
        "environment": environment,
        "scope": scope,
        "commit_sha": commit_sha,
        "tree_sha": tree_sha,
        "exact_main_sha": main_sha,
        "private_ci": {
            "job_id": ci_job.get("job_id") if ci_job else private_ci_job_id,
            "repository": ci_job.get("repository") if ci_job else None,
            "branch": ci_job.get("branch") if ci_job else None,
            "commit_sha": ci_job.get("commit_sha") if ci_job else None,
            "tree_sha": ci_job.get("tree_sha") if ci_job else None,
            "profile": ci_job.get("profile") if ci_job else None,
            "status": ci_job.get("status") if ci_job else None,
            "exit_code": ci_job.get("exit_code") if ci_job else None,
            "superseded": bool(ci_job and ci_job.get("superseded_by_job_id")),
        },
        "expected_current_build_sha": expected_current_build_sha,
        "current_build_sha": current_build_sha,
        "execution_mode": execution_mode,
        "recovery_of_deployment_id": recovery_id or None,
        "recovery_source": _recovery_source_summary(recovery_source),
        "executor": executor,
        "execution_contract": (
            "fixed-executor/post-switch-recovery/fail-stop/no-auto-rollback"
            if recovery_id
            else "fixed-executor/fail-stop/no-auto-rollback"
        ),
    }


def start_infrastructure_deployment(
    repository: str,
    environment: str,
    commit_sha: str,
    private_ci_job_id: str,
    expected_current_build_sha: str,
    scope: str = SCOPE,
    confirm: bool = False,
    requested_by: str = "mcp",
    recovery_of_deployment_id: str = "",
) -> dict:
    if not confirm:
        return _error_response("CONFIRM_REQUIRED", "confirm must be true")
    recovery_id = str(recovery_of_deployment_id or "").strip()
    plan = plan_infrastructure_deployment(
        repository,
        environment,
        commit_sha,
        private_ci_job_id,
        expected_current_build_sha,
        scope,
        recovery_id,
    )
    if not plan.get("ready"):
        return {
            **plan,
            "error": {
                "code": (plan.get("reasons") or ["NOT_READY"])[0],
                "message": "infrastructure deployment is not ready",
                "details": {},
            },
        }

    store.init_db()
    db = store.get_db()
    with _lock:
        db.execute("BEGIN IMMEDIATE")
        active = db.execute(
            "SELECT deployment_id FROM infrastructure_deployments WHERE repository=? AND environment=? "
            "AND status IN ('queued','claimed','running') LIMIT 1",
            (repository, environment),
        ).fetchone()
        if active:
            db.rollback()
            return _error_response(
                "INFRASTRUCTURE_DEPLOYMENT_ALREADY_ACTIVE",
                "another infrastructure deployment is active",
                details={"deployment_id": active[0]},
            )
        if runtime_build_sha() != expected_current_build_sha:
            db.rollback()
            return _error_response(
                "CURRENT_BUILD_SHA_CHANGED",
                "current MyGithut12 build changed after planning",
            )
        deployment_id = "infra_dep_" + uuid.uuid4().hex
        timestamp = store.now()
        db.execute(
            """
            INSERT INTO infrastructure_deployments(
              deployment_id,repository,environment,requested_scope,commit_sha,tree_sha,
              private_ci_job_id,expected_current_build_sha,requested_by,execution_mode,
              recovery_of_deployment_id,status,current_step,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,'queued','queued',?,?)
            """,
            (
                deployment_id,
                repository,
                environment,
                scope,
                commit_sha,
                plan["tree_sha"],
                private_ci_job_id,
                expected_current_build_sha,
                requested_by,
                plan["execution_mode"],
                recovery_id or None,
                timestamp,
                timestamp,
            ),
        )
        db.commit()
    return {
        "ok": True,
        "deployment_id": deployment_id,
        "status": "queued",
        "repository": repository,
        "environment": environment,
        "scope": scope,
        "commit_sha": commit_sha,
        "tree_sha": plan["tree_sha"],
        "expected_current_build_sha": expected_current_build_sha,
        "execution_mode": plan["execution_mode"],
        "recovery_of_deployment_id": recovery_id or None,
        "execution_contract": plan["execution_contract"],
    }


def _deployment_phase(row) -> str:
    status = str(row["status"] or "")
    step = str(row["current_step"] or "")
    if status == "passed":
        return "completed"
    if status == "failed":
        return "failed"
    if step in STRUCTURED_PHASES:
        return step
    return LEGACY_PHASE_MAP.get(step, "validation")


def get_infrastructure_deployment(
    deployment_id: str,
    wait_seconds: int = 0,
    last_known_revision: int = 0,
    last_known_status: str = "",
    last_known_step: str = "",
    include_log_tail: bool = False,
    log_tail_lines: int = DEFAULT_LOG_TAIL_LINES,
) -> dict:
    """Read deployment state, optionally long-polling the shared SQLite record.

    The default one-argument call keeps the original compact response. Waiting
    never holds a SQLite transaction, so Controller blue/green replacement can
    keep updating the same durable deployment row.
    """
    timeout = min(max(int(wait_seconds or 0), 0), MAX_WAIT_SECONDS)
    known_revision = max(int(last_known_revision or 0), 0)
    known_status = str(last_known_status or "")
    known_step = str(last_known_step or "")
    started = time.monotonic()
    changed = False
    terminal = False
    timed_out = False

    while True:
        row = store.get_deployment(deployment_id)
        if not row:
            return _error_response(
                "INFRASTRUCTURE_DEPLOYMENT_NOT_FOUND",
                "infrastructure deployment not found",
            )
        status = str(row["status"] or "")
        step = str(row["current_step"] or "")
        revision = int(row["log_revision"] or 0)
        terminal = status in TERMINAL_STATUSES
        changed = (
            revision != known_revision
            or bool(known_status and status != known_status)
            or bool(known_step and step != known_step)
        )
        elapsed = time.monotonic() - started
        if timeout <= 0 or changed or terminal:
            break
        if elapsed >= timeout:
            timed_out = True
            break
        time.sleep(min(WAIT_POLL_SECONDS, max(0.01, timeout - elapsed)))

    result = {
        "ok": True,
        "deployment": store.public_deployment(row),
        "executor": _executor(row["repository"]),
    }
    diagnostics_requested = bool(
        timeout
        or known_revision
        or known_status
        or known_step
        or include_log_tail
        or int(log_tail_lines or DEFAULT_LOG_TAIL_LINES) != DEFAULT_LOG_TAIL_LINES
    )
    if diagnostics_requested:
        diagnostics = {
            "changed": changed,
            "timed_out": timed_out,
            "terminal": terminal,
            "revision": int(row["log_revision"] or 0),
            "status": str(row["status"] or ""),
            "current_step": str(row["current_step"] or ""),
            "phase": _deployment_phase(row),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "max_wait_seconds": MAX_WAIT_SECONDS,
        }
        if include_log_tail:
            diagnostics["log_tail"] = store.redacted_log_tail(row, log_tail_lines)
        result["diagnostics"] = diagnostics
    return result


def claim_infrastructure_deployment(executor_id: str) -> dict:
    spec = _spec(REPOSITORY)
    if executor_id != spec.get("executor_id"):
        return _error_response("EXECUTOR_NOT_ALLOWED", "executor identity is not allowed")
    store.init_db()
    db = store.get_db()
    with _lock:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT * FROM infrastructure_deployments WHERE repository=? AND environment=? "
            "AND status='queued' ORDER BY created_at LIMIT 1",
            (REPOSITORY, spec["environment"]),
        ).fetchone()
        if not row:
            db.commit()
            store.write_executor_heartbeat(executor_id, "idle", None)
            return {"ok": True, "deployment": None}

        try:
            main_sha, tree_sha = _repo_state(row["repository"], row["commit_sha"])
        except Exception as exc:
            db.rollback()
            return _error_response(
                "GITHUB_STATE_UNAVAILABLE",
                "current GitHub state is unavailable during executor claim",
                details={"type": type(exc).__name__},
            )
        ci_reason, _ = _ci_gate(
            row["repository"],
            row["private_ci_job_id"],
            row["commit_sha"],
            row["tree_sha"],
        )
        current_build_sha = runtime_build_sha()
        stale_code = None
        stale_message = None
        if main_sha != row["commit_sha"] or tree_sha != row["tree_sha"]:
            stale_code = "COMMIT_NOT_CURRENT_MAIN"
            stale_message = "deployment target is no longer the exact current main"
        elif ci_reason:
            stale_code = ci_reason
            stale_message = "private CI gate changed before executor claim"
        elif current_build_sha != row["expected_current_build_sha"]:
            stale_code = "CURRENT_BUILD_SHA_CHANGED"
            stale_message = "current build changed before executor claim"
        elif row["execution_mode"] == EXECUTION_MODE_POST_SWITCH_RECOVERY:
            source_id = str(row["recovery_of_deployment_id"] or "")
            source = (
                db.execute(
                    "SELECT * FROM infrastructure_deployments WHERE deployment_id=?",
                    (source_id,),
                ).fetchone()
                if source_id
                else None
            )
            recovery_reason = _validate_recovery_source(
                source,
                row["repository"],
                row["environment"],
                row["requested_scope"],
                row["commit_sha"],
                row["tree_sha"],
            )
            if recovery_reason:
                stale_code = recovery_reason
                stale_message = "recovery source changed or is no longer eligible"
            elif current_build_sha != row["commit_sha"]:
                stale_code = "INFRASTRUCTURE_RECOVERY_RUNTIME_NOT_TARGET"
                stale_message = "recovery requires the current runtime to already match the target commit"
        if stale_code:
            timestamp = store.now()
            db.execute(
                "UPDATE infrastructure_deployments SET status='failed',current_step='preflight',exit_code=1,"
                "error_code=?,error_message=?,finished_at=?,updated_at=?,log_revision=log_revision+1 "
                "WHERE deployment_id=?",
                (stale_code, stale_message, timestamp, timestamp, row["deployment_id"]),
            )
            db.commit()
            store.write_executor_heartbeat(executor_id, "idle", None)
            return {
                "ok": True,
                "deployment": None,
                "failed_deployment_id": row["deployment_id"],
                "error_code": stale_code,
            }

        timestamp = store.now()
        db.execute(
            "UPDATE infrastructure_deployments SET status='claimed',current_step='claimed',"
            "started_at=COALESCE(started_at,?),updated_at=?,log_revision=log_revision+1 "
            "WHERE deployment_id=? AND status='queued'",
            (timestamp, timestamp, row["deployment_id"]),
        )
        db.commit()
        store.write_executor_heartbeat(executor_id, "running", row["deployment_id"])
        claimed = store.get_deployment(row["deployment_id"])
    return {"ok": True, "deployment": store.public_deployment(claimed)}


def update_infrastructure_deployment_progress(
    deployment_id: str,
    current_step: str,
    message: str = "",
) -> dict:
    row = store.get_deployment(deployment_id)
    if not row:
        return _error_response("INFRASTRUCTURE_DEPLOYMENT_NOT_FOUND", "infrastructure deployment not found")
    if row["status"] in TERMINAL_STATUSES:
        return {"ok": True, "idempotent": True, "deployment": store.public_deployment(row)}
    if row["status"] not in {"claimed", "running"}:
        return _error_response("INFRASTRUCTURE_DEPLOYMENT_STATE_INVALID", "deployment is not claimed")
    log_text, revision = store.append_log(row, message)
    timestamp = store.now()
    db = store.get_db()
    db.execute(
        "UPDATE infrastructure_deployments SET status='running',current_step=?,updated_at=?,log_revision=?,log_text=? WHERE deployment_id=?",
        (str(current_step or "running")[:80], timestamp, revision, log_text, deployment_id),
    )
    db.commit()
    return {
        "ok": True,
        "idempotent": False,
        "deployment": store.public_deployment(store.get_deployment(deployment_id)),
    }


def complete_infrastructure_deployment(
    deployment_id: str,
    exit_code: int,
    controller_healthy: bool,
    private_ci_agent_healthy: bool,
    message: str = "",
) -> dict:
    if exit_code != 0:
        return fail_infrastructure_deployment(
            deployment_id,
            exit_code,
            "INFRASTRUCTURE_DEPLOYMENT_EXIT_NONZERO",
            message or "infrastructure deployment exited non-zero",
        )
    row = store.get_deployment(deployment_id)
    if not row:
        return _error_response("INFRASTRUCTURE_DEPLOYMENT_NOT_FOUND", "infrastructure deployment not found")
    if row["status"] in TERMINAL_STATUSES:
        return {"ok": True, "idempotent": True, "deployment": store.public_deployment(row)}
    if row["status"] not in {"claimed", "running"}:
        return _error_response("INFRASTRUCTURE_DEPLOYMENT_STATE_INVALID", "deployment is not running")
    if not controller_healthy or not private_ci_agent_healthy:
        return _error_response(
            "INFRASTRUCTURE_HEALTH_EVIDENCE_REQUIRED",
            "terminal success requires Controller and private CI agent health evidence",
        )
    current_build = runtime_build_sha()
    if current_build != row["commit_sha"]:
        return _error_response(
            "RUNTIME_BUILD_SHA_MISMATCH",
            "runtime build SHA does not match deployment target",
            details={"expected": row["commit_sha"], "actual": current_build},
        )
    timestamp = store.now()
    log_text, _ = store.append_log(row, message)
    db = store.get_db()
    db.execute(
        "UPDATE infrastructure_deployments SET status='passed',current_step='completed',exit_code=0,"
        "error_code=NULL,error_message=NULL,finished_at=?,updated_at=?,log_revision=log_revision+1,log_text=? WHERE deployment_id=?",
        (timestamp, timestamp, log_text, deployment_id),
    )
    db.commit()
    spec = _spec(row["repository"])
    store.write_executor_heartbeat(spec["executor_id"], "idle", None)
    return {
        "ok": True,
        "idempotent": False,
        "deployment": store.public_deployment(store.get_deployment(deployment_id)),
    }


def fail_infrastructure_deployment(
    deployment_id: str,
    exit_code: int,
    error_code: str,
    error_message: str = "",
) -> dict:
    row = store.get_deployment(deployment_id)
    if not row:
        return _error_response("INFRASTRUCTURE_DEPLOYMENT_NOT_FOUND", "infrastructure deployment not found")
    if row["status"] in TERMINAL_STATUSES:
        return {"ok": True, "idempotent": True, "deployment": store.public_deployment(row)}
    code = str(error_code or "INFRASTRUCTURE_DEPLOYMENT_FAILED")
    if not ERROR_CODE_RE.fullmatch(code):
        code = "INFRASTRUCTURE_DEPLOYMENT_FAILED"
    message = store.sanitize_log(error_message)
    log_text, _ = store.append_log(row, message)
    timestamp = store.now()
    db = store.get_db()
    db.execute(
        "UPDATE infrastructure_deployments SET status='failed',current_step='failed',exit_code=?,error_code=?,"
        "error_message=?,finished_at=?,updated_at=?,log_revision=log_revision+1,log_text=? WHERE deployment_id=?",
        (int(exit_code or 1), code, message[:1000], timestamp, timestamp, log_text, deployment_id),
    )
    db.commit()
    spec = _spec(row["repository"])
    store.write_executor_heartbeat(spec["executor_id"], "idle", None)
    return {
        "ok": True,
        "idempotent": False,
        "deployment": store.public_deployment(store.get_deployment(deployment_id)),
    }
