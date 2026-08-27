"""Register the four stable DX-1 orchestration tools."""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Callable, Awaitable

from mcp.types import ToolAnnotations

from app import development_orchestrator as dx
from app import development_resume as resume
from app import development_session_store as sessions
from app import github_utils, mygithub12

logger=logging.getLogger(__name__)
_ORCHESTRATION=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=True)


def _error_payload(exc: Exception, *, log_unexpected: bool = True) -> dict[str, Any]:
    if isinstance(exc,mygithub12.MyGithub12Error):
        return {"ok":False,"error":{"code":exc.code,"message":exc.message,"details":exc.details,"trace_id":exc.trace_id,"retryable":bool(exc.details.get("retryable",False))}}
    if log_unexpected:
        logger.exception("DX orchestration failed")
    return {"ok":False,"error":{"code":"INTERNAL_ERROR","message":"development orchestration failed","details":{"type":type(exc).__name__},"trace_id":str(uuid.uuid4()),"retryable":False}}


def _error(exc: Exception) -> str:
    return json.dumps(_error_payload(exc),ensure_ascii=False)


def _require_ok_result(result: Any, default_code: str, default_message: str) -> None:
    if not isinstance(result,dict) or result.get("ok") is not False:
        return
    error=result.get("error") if isinstance(result.get("error"),dict) else {}
    raise mygithub12.MyGithub12Error(
        str(error.get("code") or default_code),
        str(error.get("message") or default_message),
        dict(error.get("details") or {}),
    )


def register_dx_tools(mcp, github_call: Callable[..., Awaitable[Any]], service: Any, finalize_write: Callable[..., Awaitable[dict]]) -> None:
    @mcp.tool(name="prepare_development_task",description="Prepare one durable development session: policy, exact base, workspace/branch lease, index, instructions, compact context and overlap.",annotations=_ORCHESTRATION)
    async def prepare_development_task(
        repository: str, task_name: str, base_ref: str="main", branch: str="", owner: str="chatgpt", create_branch: bool=True,
        seed_paths_json: str="[]", seed_symbols_json: str="[]", include_tests: bool=True, include_docs: bool=True,
        lease_seconds: int=mygithub12.DEFAULT_LEASE_SECONDS, context_budget_bytes: int=262144, idempotency_key: str="",
    ) -> str:
        try:
            result=await github_call(dx.prepare_task,service,repository,task_name,base_ref,branch,owner,create_branch,seed_paths_json,seed_symbols_json,include_tests,include_docs,lease_seconds,context_budget_bytes,idempotency_key)
            return json.dumps(result,ensure_ascii=False)
        except Exception as exc: return _error(exc)

    @mcp.tool(name="resume_development_task",description="Resume a branch or PR development context with fresh repository, Workspace, Session, Index, CI, PR and overlap evidence; safely recovers stale Sessions only under DX2 guards.",annotations=_ORCHESTRATION)
    async def resume_development_task(
        repository: str, branch: str="", pull_number: int=0, recover_stale_session: bool=True, renew_lease: bool=False,
        expected_workspace_revision: int=0, expected_session_revision: int=0, lease_seconds: int=mygithub12.DEFAULT_LEASE_SECONDS,
        idempotency_key: str="",
    ) -> str:
        try:
            result=await github_call(
                resume.resume_task,service,repository,branch,pull_number,recover_stale_session,renew_lease,
                expected_workspace_revision,expected_session_revision,lease_seconds,idempotency_key,
            )
            return json.dumps(result,ensure_ascii=False)
        except Exception as exc: return _error(exc)

    @mcp.tool(name="apply_development_change_set",description="Apply one versioned patch/range/upload change set with Session, Workspace, HEAD/blob CAS and durable GitHub read-back.",annotations=_ORCHESTRATION)
    async def apply_development_change_set(
        development_session_id: str, expected_session_revision: int, expected_workspace_revision: int, expected_head_sha: str,
        change_set_json: str, commit_message: str, dry_run: bool=True, idempotency_key: str="", create_pull_request: bool=False,
        pull_request_json: str="{}",
    ) -> str:
        try:
            parsed=dx.parse_change_set(change_set_json)
            pr_cfg=None
            if create_pull_request:
                try:
                    pr_cfg=json.loads(pull_request_json or "{}")
                except json.JSONDecodeError as exc:
                    raise mygithub12.MyGithub12Error(
                        "SEARCH_QUERY_INVALID","pull_request_json must be valid JSON",{"position":exc.pos}
                    ) from exc
                if not isinstance(pr_cfg,dict):
                    raise mygithub12.MyGithub12Error("SEARCH_QUERY_INVALID","pull_request_json must be an object")
            maintenance=await github_call(dx.maybe_auto_renew_session_workspace,service,development_session_id,expected_session_revision,expected_workspace_revision,expected_head_sha,idempotency_key)
            session=maintenance["session"]; workspace=maintenance["workspace"]
            effective_session_revision=int(session["session_revision"]); effective_workspace_revision=int(workspace["revision"])
            lease_maintenance={"renewed":bool(maintenance.get("renewed")),"remaining_seconds":maintenance.get("remaining_seconds"),"audit":maintenance.get("audit"),"recovery":maintenance.get("recovery")}
            if expected_head_sha!=session["head_commit_sha"]:
                raise mygithub12.MyGithub12Error("HEAD_CHANGED","expected HEAD differs from the recovered Development Session HEAD",{"expected":expected_head_sha,"actual":session["head_commit_sha"]})
            session,workspace=await github_call(dx.require_session_workspace,service,development_session_id,effective_session_revision,effective_workspace_revision,session["head_commit_sha"])
            audit={"development_session_id":development_session_id,"session_revision":effective_session_revision,"workspace_id":workspace["workspace_id"],"workspace_revision":effective_workspace_revision}
            result=await github_call(dx.execute_change_set,service,session,workspace,parsed,session["head_commit_sha"],effective_workspace_revision,commit_message,dry_run,idempotency_key,audit)
            if dry_run:
                result["development_session_id"]=development_session_id; result["session_revision"]=effective_session_revision; result["workspace_revision"]=effective_workspace_revision; result["lease_maintenance"]=lease_maintenance
                return json.dumps(result,ensure_ascii=False)
            finalized=await finalize_write(result,workspace["workspace_id"],effective_workspace_revision)
            try:
                state=await github_call(dx.after_verified_change,service,development_session_id,effective_session_revision,finalized)
            except Exception as state_exc:
                state_error=_error_payload(state_exc)
                return json.dumps({
                    "ok":False,**finalized,"write_verified":True,
                    "development_session_id":development_session_id,
                    "recovery_required":True,"failed_stage":"development_session_finalize",
                    "orchestration_error":state_error["error"],
                },ensure_ascii=False)
            pr=None; pr_error=None
            if create_pull_request:
                try:
                    cfg=pr_cfg or {}; current_session=state["session"]
                    if current_session.get("pull_number"):
                        number=int(current_session["pull_number"])
                        pr=await github_call(
                            github_utils.update_github_pull_request,current_session["repository"],number,
                            cfg.get("title"),cfg.get("body"),None,cfg.get("base_branch"),current_session["head_commit_sha"],
                        )
                        _require_ok_result(pr,"PULL_REQUEST_UPDATE_FAILED","pull request update failed after verified commit")
                    else:
                        pr=await github_call(service.create_pull_request,current_session["repository"],current_session["branch"],str(cfg.get("base_branch") or current_session["base_branch"]),str(cfg.get("title") or commit_message),str(cfg.get("body") or ""),bool(cfg.get("draft",True)))
                        _require_ok_result(pr,"PULL_REQUEST_CREATE_FAILED","pull request creation failed after verified commit")
                        number=int(((pr or {}).get("pull_request") or {}).get("number") or 0)
                    if not number:
                        raise mygithub12.MyGithub12Error("DEVELOPMENT_SESSION_RECOVERY_REQUIRED","pull request creation/update was not confirmed after verified commit")
                    state["session"]=await github_call(sessions.transition,development_session_id,current_session["session_revision"],"pr_ready",event_type="pull_request_prepared",allowed_from={"active","pr_ready"},fields={"pull_number":int(number)})
                except Exception as pr_exc:
                    pr_error=_error_payload(pr_exc)
                    if pr and ((pr or {}).get("pull_request") or {}).get("number"):
                        pr_error["error"]["details"]={
                            **(pr_error["error"].get("details") or {}),
                            "pull_number":int(pr["pull_request"]["number"]),
                            "commit_already_verified":True,
                            "session_recovery_required":True,
                        }
            response={"ok":True,**finalized,"development_session":state["session"],"index":state["index"],"index_error":state["index_error"],"pull_request":pr,"lease_maintenance":lease_maintenance}
            if pr_error:
                response["pull_request_error"]=pr_error["error"]
                response["partial_success"]=True
            return json.dumps(response,ensure_ascii=False)
        except Exception as exc: return _error(exc)

    @mcp.tool(name="validate_development_task",description="Run or reuse fast/full private CI for an exact Session head; fast feedback never becomes merge-eligible, full success yields attestation.",annotations=_ORCHESTRATION)
    async def validate_development_task(
        development_session_id: str, expected_session_revision: int, mode: str="fast", base_sha: str="", force_rerun: bool=False,
        supersede_previous: bool=True, wait_seconds: int=55, include_failure_pack: bool=True, idempotency_key: str="",
    ) -> str:
        try:
            session=sessions.get_session(development_session_id)
            preflight_head=session["head_commit_sha"]
            resolved_base=base_sha or session["base_commit_sha"]
            prepared=await github_call(dx.validation_preflight,service,session,mode,resolved_base)
            maintenance=await github_call(dx.maybe_auto_renew_session_workspace,service,development_session_id,expected_session_revision,int(session["workspace_revision"]),session["head_commit_sha"],idempotency_key)
            session=maintenance["session"]; ws=maintenance["workspace"]; effective_session_revision=int(session["session_revision"])
            if session["head_commit_sha"]!=preflight_head:
                resolved_base=base_sha or session["base_commit_sha"]
                prepared=await github_call(dx.validation_preflight,service,session,mode,resolved_base)
            lease_maintenance={"renewed":bool(maintenance.get("renewed")),"remaining_seconds":maintenance.get("remaining_seconds"),"audit":maintenance.get("audit"),"recovery":maintenance.get("recovery")}
            await github_call(mygithub12.workspace_write_preflight,service,session["repository"],session["branch"],session["head_commit_sha"],session["workspace_id"],ws["revision"])
            phase="validating_fast" if mode=="fast" else "validating_full"
            phase_session=await github_call(sessions.transition,development_session_id,effective_session_revision,phase,event_type="validation_started",allowed_from={"active","pr_ready","validating_fast","validating_full"})
            try:
                job,selection=await github_call(dx.start_validation_job,service,phase_session,mode,resolved_base,force_rerun,supersede_previous,prepared)
            except Exception as start_exc:
                rollback=None; rollback_error=None
                try:
                    rollback=await github_call(
                        sessions.transition,development_session_id,phase_session["session_revision"],session["status"],
                        event_type="validation_start_failed",allowed_from={phase},
                    )
                except Exception as rollback_exc:
                    rollback_error=type(rollback_exc).__name__
                start_error=_error_payload(start_exc)
                start_error["error"]["details"]={
                    **(start_error["error"].get("details") or {}),
                    "validation_state_rolled_back":bool(rollback),
                    "rollback_error_type":rollback_error,
                }
                return json.dumps({
                    "ok":False,"development_session":rollback or phase_session,
                    "validation_started":False,"recovery_required":not bool(rollback),
                    "error":start_error["error"],
                },ensure_ascii=False)
            result=None
            try:
                job=await github_call(dx.wait_validation,job["job_id"],wait_seconds)
                result=await github_call(dx.validation_result,development_session_id,phase_session["session_revision"],mode,job,selection,include_failure_pack)
                fields={"last_fast_ci_job_id" if mode=="fast" else "last_full_ci_job_id":job["job_id"]}
                if isinstance(result.get("attestation"),dict) and result["attestation"].get("attestation_id"): fields["last_attestation_id"]=result["attestation"]["attestation_id"]
                if isinstance(result.get("failure_pack"),dict) and result["failure_pack"].get("resource_uri"): fields["last_failure_resource_uri"]=result["failure_pack"]["resource_uri"]
                next_status=("pr_ready" if result.get("merge_eligible") else "active") if result.get("terminal") else phase
                final_session=await github_call(sessions.transition,development_session_id,phase_session["session_revision"],next_status,event_type="validation_observed",allowed_from={phase},fields=fields)
            except Exception as observe_exc:
                observe_error=_error_payload(observe_exc)
                return json.dumps({
                    "ok":False,"development_session":phase_session,"mode":mode,
                    "validation_started":True,"recovery_required":True,
                    "job":{"job_id":job.get("job_id"),"status":job.get("status"),"profile":job.get("profile"),"commit_sha":job.get("commit_sha")},
                    "validation_result":result,"failed_stage":"validation_observe",
                    "orchestration_error":observe_error["error"],
                },ensure_ascii=False)
            return json.dumps({"ok":True,"development_session":final_session,"mode":mode,"lease_maintenance":lease_maintenance,**result},ensure_ascii=False)
        except Exception as exc: return _error(exc)

    @mcp.tool(name="finalize_development_task",description="Prepare/update Draft PR, read readiness, safely merge with explicit confirmation, or close a development session/workspace.",annotations=_ORCHESTRATION)
    async def finalize_development_task(
        development_session_id: str, expected_session_revision: int, action: str="readiness", pull_request_json: str="{}",
        merge_method: str="squash", required_private_ci_job_id: str="", confirm: bool=False, delete_head_branch: bool=False,
        idempotency_key: str="",
    ) -> str:
        try:
            session=sessions.get_session(development_session_id); sessions._require_revision(development_session_id,expected_session_revision,writable=action!="readiness")
            if action not in {"prepare_pr","readiness","merge","close"}: raise mygithub12.MyGithub12Error("DEVELOPMENT_SESSION_STATE_INVALID","unsupported finalize action",{"action":action})
            cfg=None
            if action=="prepare_pr":
                try:
                    cfg=json.loads(pull_request_json or "{}")
                except json.JSONDecodeError as exc:
                    raise mygithub12.MyGithub12Error(
                        "SEARCH_QUERY_INVALID","pull_request_json must be valid JSON",{"position":exc.pos}
                    ) from exc
                if not isinstance(cfg,dict): raise mygithub12.MyGithub12Error("SEARCH_QUERY_INVALID","pull_request_json must be an object")
            if action=="merge":
                if not confirm: raise mygithub12.MyGithub12Error("CONFIRM_REQUIRED","confirm must be true")
                if not session.get("pull_number"): raise mygithub12.MyGithub12Error("DEVELOPMENT_SESSION_STATE_INVALID","pull request is required before merge")
            effective_session_revision=expected_session_revision; lease_maintenance={"renewed":False,"remaining_seconds":None,"audit":None}
            if action!="close" and session["status"] in dx.AUTO_RENEW_SESSION_STATES:
                maintenance=await github_call(dx.maybe_auto_renew_session_workspace,service,development_session_id,expected_session_revision,int(session["workspace_revision"]),session["head_commit_sha"],idempotency_key)
                session=maintenance["session"]; effective_session_revision=int(session["session_revision"]); lease_maintenance={"renewed":bool(maintenance.get("renewed")),"remaining_seconds":maintenance.get("remaining_seconds"),"audit":maintenance.get("audit")}
            if action=="prepare_pr":
                if session.get("pull_number"):
                    pr=await github_call(github_utils.update_github_pull_request,session["repository"],int(session["pull_number"]),cfg.get("title"),cfg.get("body"),None,cfg.get("base_branch"),session["head_commit_sha"])
                    _require_ok_result(pr,"PULL_REQUEST_UPDATE_FAILED","pull request update failed")
                    number=int(session["pull_number"])
                else:
                    pr=await github_call(service.create_pull_request,session["repository"],session["branch"],str(cfg.get("base_branch") or session["base_branch"]),str(cfg.get("title") or session["metadata"].get("task_name") or "Development task"),str(cfg.get("body") or ""),bool(cfg.get("draft",True)))
                    _require_ok_result(pr,"PULL_REQUEST_CREATE_FAILED","pull request creation failed")
                    number=int(((pr or {}).get("pull_request") or {}).get("number") or 0)
                if not number: raise mygithub12.MyGithub12Error("DEVELOPMENT_SESSION_RECOVERY_REQUIRED","pull request creation/update was not confirmed")
                try:
                    updated=await github_call(sessions.transition,development_session_id,effective_session_revision,"pr_ready",event_type="pull_request_prepared",allowed_from={"active","pr_ready"},fields={"pull_number":number})
                except Exception as state_exc:
                    state_error=_error_payload(state_exc)
                    state_error["error"]["details"]={
                        **(state_error["error"].get("details") or {}),
                        "pull_number":number,"pull_request_already_prepared":True,
                    }
                    return json.dumps({
                        "ok":False,"development_session":session,"pull_request":pr,
                        "pull_number":number,"recovery_required":True,
                        "failed_stage":"development_session_pr_finalize",
                        "orchestration_error":state_error["error"],
                    },ensure_ascii=False)
                return json.dumps({"ok":True,"development_session":updated,"pull_request":pr,"lease_maintenance":lease_maintenance},ensure_ascii=False)
            if action=="readiness":
                if not session.get("pull_number"): return json.dumps({"ok":True,"ready":False,"blocking_reasons":["PULL_REQUEST_REQUIRED"],"development_session":session,"lease_maintenance":lease_maintenance},ensure_ascii=False)
                ready=await github_call(github_utils.get_github_pull_request_merge_readiness,session["repository"],int(session["pull_number"]),session["head_commit_sha"],required_private_ci_job_id,session["base_branch"])
                return json.dumps({"ok":True,"development_session":session,"readiness":ready,"lease_maintenance":lease_maintenance},ensure_ascii=False)
            if action=="merge":
                merged=await github_call(github_utils.merge_github_pull_request,session["repository"],int(session["pull_number"]),merge_method,session["head_commit_sha"],required_private_ci_job_id,session["base_branch"],"","",delete_head_branch,True)
                if not merged.get("ok") or not merged.get("merged"): return json.dumps({"ok":False,"development_session":session,"merge":merged},ensure_ascii=False)
                try:
                    updated=await github_call(sessions.transition,development_session_id,effective_session_revision,"merged",event_type="pull_request_merged",allowed_from={"active","pr_ready"})
                except Exception as state_exc:
                    state_error=_error_payload(state_exc)
                    return json.dumps({
                        "ok":False,"development_session":session,"merge":merged,
                        "merge_completed":True,"recovery_required":True,
                        "failed_stage":"development_session_merge_finalize",
                        "orchestration_error":state_error["error"],
                    },ensure_ascii=False)
                return json.dumps({"ok":True,"development_session":updated,"merge":merged,"lease_maintenance":lease_maintenance},ensure_ascii=False)
            # close
            closing=await github_call(sessions.transition,development_session_id,effective_session_revision,"closing",event_type="session_closing",allowed_from={"active","pr_ready","drifted","blocked","validating_fast","validating_full"})
            try:
                ws=await github_call(mygithub12.get_workspace,service,closing["workspace_id"])
                closed_ws=await github_call(mygithub12.close_workspace,service,closing["workspace_id"],ws["revision"])
            except Exception as close_exc:
                close_error=_error_payload(close_exc)
                return json.dumps({
                    "ok":False,"development_session":closing,"workspace":None,
                    "recovery_required":True,"failed_stage":"workspace_close",
                    "orchestration_error":close_error["error"],
                },ensure_ascii=False)
            try:
                closed=await github_call(sessions.transition,development_session_id,closing["session_revision"],"closed",event_type="session_closed",allowed_from={"closing"},fields={"workspace_revision":closed_ws["revision"],"lease_expires_at":0})
            except Exception as state_exc:
                state_error=_error_payload(state_exc)
                return json.dumps({
                    "ok":False,"development_session":closing,"workspace":closed_ws,
                    "workspace_closed":True,"recovery_required":True,
                    "failed_stage":"development_session_close_finalize",
                    "orchestration_error":state_error["error"],
                },ensure_ascii=False)
            return json.dumps({"ok":True,"development_session":closed,"workspace":closed_ws,"lease_maintenance":lease_maintenance},ensure_ascii=False)
        except Exception as exc: return _error(exc)
