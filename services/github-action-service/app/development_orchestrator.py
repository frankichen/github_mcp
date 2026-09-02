"""High-level MyGithut12 DX-1 development orchestration primitives."""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from app import attestation_registry, github_utils, mygithub10, mygithub12
from app import development_session_store as sessions
from app.ci_database import create_or_get_job, get_job, wait_for_job_change, _job_snapshot
from app.ci_models import ALLOWED_PRIORITIES, effective_priority
from app.ci_repository_config import (
    get_allowed_profiles, get_max_timeout, is_private_ci_enabled, is_profile_allowed,
    is_self_deploy_enabled, is_test_deploy_enabled,
)
from app.development_failure_pack import build_failure_pack
from app.mcp_response import store_response_resource

MyGithub12Error=mygithub12.MyGithub12Error
AUTO_RENEW_THRESHOLD_SECONDS=1800
AUTO_RENEW_SESSION_STATES=frozenset({"active","validating_fast","validating_full","pr_ready"})


def _json(value: str, expected: type, name: str, default: Any):
    if not value:
        return default
    try: parsed=json.loads(value)
    except json.JSONDecodeError as exc: raise MyGithub12Error("SEARCH_QUERY_INVALID",f"{name} must be valid JSON",{"position":exc.pos}) from exc
    if not isinstance(parsed,expected): raise MyGithub12Error("SEARCH_QUERY_INVALID",f"{name} has the wrong JSON type")
    return parsed


def operation_policy(service: Any, repository: str) -> dict[str, bool]:
    service._check_repository_allowed(repository)
    return {
        "github":True,"private_ci":is_private_ci_enabled(repository),
        "test_deploy":is_test_deploy_enabled(repository),"self_deploy":is_self_deploy_enabled(repository),
    }


def context_pack_v2(
    service: Any, repository: str, commit_sha: str, task: str, seed_paths_json: str="[]", seed_symbols_json: str="[]",
    *, include_tests: bool=True, include_docs: bool=True, max_total_bytes: int=262144,
) -> dict[str, Any]:
    raw=mygithub12.repository_context_pack(service,repository,commit_sha,task,seed_paths_json,seed_symbols_json,50,min(max_total_bytes,1024*1024),include_tests,include_docs)
    terms={t.lower() for t in __import__("re").findall(r"[\w\u4e00-\u9fff]+",task) if len(t)>2}
    ranked=[]; full=[]
    for item in raw.get("items",[]):
        content=item.get("content",""); path=item["path"]; reason=item.get("reason","")
        score={"explicit_seed":1.0,"symbol_definition":0.98,"task_term_match":0.86,"test_candidate":0.58,"documentation":0.42}.get(reason,0.4)
        lower=(path+" "+content[:4000]).lower(); score+=min(0.08,0.02*sum(1 for t in terms if t in lower))
        lines=content.splitlines(); start=1; end=min(len(lines),80); snippet="\n".join(lines[:end])
        compact={"kind":"file","path":path,"blob_sha":item["blob_sha"],"start_line":start,"end_line":end,"score":round(min(score,1.0),3),"reason":reason,"authoritative":True,"size_bytes":item.get("size_bytes",len(content.encode()))}
        if len(snippet.encode())<=8192: compact["snippet"]=snippet
        ranked.append(compact); full.append({**compact,"content":content})
    ranked.sort(key=lambda x:(-x["score"],x["path"])); full.sort(key=lambda x:(-x["score"],x["path"]))
    resource=store_response_resource({"task":task,"identity":{"repository":repository,"commit_sha":raw["commit_sha"],"tree_sha":raw["tree_sha"]},"items":full,"omitted_count":raw.get("omitted_count",0)})
    return {"ok":True,"schema_version":2,"task_summary":task[:500],"identity":{"repository":repository,"commit_sha":raw["commit_sha"],"tree_sha":raw["tree_sha"]},"items":ranked[:20],"items_total":len(ranked),"items_truncated":len(ranked)>20,"omitted_count":raw.get("omitted_count",0),"resource_uri":resource["resource_uri"],"content_sha256":resource["sha256"]}


def prepare_task(
    service: Any, repository: str, task_name: str, base_ref: str="main", branch: str="", owner: str="chatgpt",
    create_branch: bool=True, seed_paths_json: str="[]", seed_symbols_json: str="[]", include_tests: bool=True,
    include_docs: bool=True, lease_seconds: int=mygithub12.DEFAULT_LEASE_SECONDS, context_budget_bytes: int=262144, idempotency_key: str="",
) -> dict[str, Any]:
    policy=operation_policy(service,repository)
    existing=sessions.find_session_by_idempotency(repository,idempotency_key)
    if existing:
        ws=mygithub12.get_workspace(service,existing["workspace_id"])
        if existing.get("status")=="prepare_failed":
            raise MyGithub12Error(
                "DEVELOPMENT_TASK_PREPARE_FAILED","previous idempotent preparation failed",
                {"development_session_id":existing["session_id"],"workspace_id":existing["workspace_id"],"replayed":True,"recovery_required":False},
            )
        return {"ok":True,"replayed":True,"development_session":existing,"workspace":ws,"policy":policy}
    identity=mygithub12.resolve_identity(service,repository,ref=base_ref)
    workspace=mygithub12.create_workspace(service,repository,task_name,base_ref,branch,owner,create_branch,lease_seconds)
    session=None
    try:
        session=sessions.create_session(
            workspace,owner=owner,idempotency_key=idempotency_key,
            metadata={"task_name":task_name,"prepared_base_identity":identity},status="preparing",
        )
        index_status=mygithub12.get_index_status(service,repository,workspace["head_sha"])
        index_request=None
        if index_status.get("status")!="ready":
            index_request=mygithub12.request_index_build(service,repository,workspace["head_sha"],"auto",identity["commit_sha"],"interactive",f"prepare:{workspace['workspace_id']}",False)
        instructions=mygithub12.agent_instructions(service,repository,workspace["head_sha"],seed_paths_json)
        context=context_pack_v2(service,repository,workspace["head_sha"],task_name,seed_paths_json,seed_symbols_json,include_tests=include_tests,include_docs=include_docs,max_total_bytes=context_budget_bytes)
        overlap=mygithub12.workspace_overlap(service,workspace["workspace_id"],"[]")
        session=sessions.transition(
            session["session_id"],session["session_revision"],"active",
            event_type="preparation_completed",allowed_from={"preparing"},
        )
        return {"ok":True,"development_session":session,"development_session_id":session["session_id"],"workspace":workspace,"policy":policy,"index":{"status":index_status,"request":index_request},"instructions":{"count":len(instructions.get("instructions",[])),"items":instructions.get("instructions",[])[:5]},"context":context,"overlap":overlap}
    except Exception as exc:
        closed_workspace=None; cleanup_error=None; failed_session=None
        try:
            closed_workspace=mygithub12.close_workspace(service,workspace["workspace_id"],workspace["revision"])
        except Exception as cleanup_exc:
            cleanup_error=type(cleanup_exc).__name__
        if session:
            fields={}
            if closed_workspace:
                fields={"workspace_revision":closed_workspace["revision"],"lease_expires_at":0}
            try:
                failed_session=sessions.transition(
                    session["session_id"],session["session_revision"],"prepare_failed",
                    event_type="preparation_failed",allowed_from={"preparing"},fields=fields,
                )
            except Exception:
                failed_session=None
        details={
            "repository":repository,"workspace_id":workspace["workspace_id"],
            "development_session_id":session["session_id"] if session else None,
            "prepare_failed":True,"workspace_closed":bool(closed_workspace),
            "cleanup_error_type":cleanup_error,
            "recovery_required":not bool(closed_workspace) or (bool(session) and not bool(failed_session)),
        }
        if isinstance(exc,MyGithub12Error):
            exc.details.update({k:v for k,v in details.items() if v is not None})
            raise
        raise MyGithub12Error("DEVELOPMENT_TASK_PREPARE_FAILED","development task preparation failed",{**details,"cause_type":type(exc).__name__}) from exc


def _append_recovery_event_best_effort(
    session_id: str, expected_revision: int, event_type: str, data: dict[str,Any],
) -> bool:
    """Persist recovery audit evidence when possible without masking the primary fail-stop error."""
    try:
        sessions.append_recovery_event(session_id,expected_revision,event_type,data)
        return True
    except Exception:
        return False


def recover_stale_session(
    service: Any, session_id: str, expected_session_revision: int, expected_workspace_revision: int=0,
    expected_head_sha: str="", idempotency_key: str="",
) -> dict[str,Any]:
    """Recover only when fresh GitHub and Workspace identity prove that Session alone is stale."""
    session=sessions.get_session(session_id)
    ws=mygithub12.get_workspace(service,session["workspace_id"])
    current_revision=int(session["session_revision"]); workspace_revision=int(ws["revision"])
    last=(session.get("metadata") or {}).get("last_session_recovery")
    replay_candidate=bool(
        idempotency_key and isinstance(last,dict)
        and last.get("idempotency_key")==idempotency_key
        and int(((last.get("after") or {}).get("session_revision") or -1))==current_revision
    )

    def refuse(event_type: str, code: str, message: str, details: dict[str,Any]) -> None:
        payload={
            "idempotency_key":idempotency_key or None, "reason_code":code,
            "session_head":session["head_commit_sha"], "session_tree":session["tree_sha"],
            "workspace_head":ws["head_sha"], "workspace_tree":ws["tree_sha"],
            **details,
        }
        _append_recovery_event_best_effort(session_id,current_revision,event_type,payload)
        raise MyGithub12Error(code,message,details)

    if expected_workspace_revision and int(expected_workspace_revision) not in {int(session["workspace_revision"]),workspace_revision}:
        if not replay_candidate:
            refuse("recovery_refused","WORKSPACE_REVISION_MISMATCH","workspace revision changed outside the recoverable Session-stale window",{"expected":expected_workspace_revision,"actual":workspace_revision})
    if expected_head_sha and expected_head_sha not in {session["head_commit_sha"],ws["head_sha"]}:
        if not replay_candidate:
            refuse("recovery_refused","DEVELOPMENT_SESSION_RECOVERY_REQUIRED","expected HEAD is neither the Session nor Workspace identity",{"expected_head":expected_head_sha})
    if ws["status"]=="drifted" or ws.get("drift_reason"):
        refuse("external_drift_detected","WORKSPACE_BRANCH_DRIFTED","drifted Workspace cannot recover a Development Session",{"workspace_id":ws["workspace_id"],"drift_reason":ws.get("drift_reason")})
    if ws["status"]=="expired" or not ws.get("lease_valid"):
        refuse("recovery_refused","WORKSPACE_LEASE_REQUIRED","expired Workspace requires explicit resume before Session recovery",{"workspace_id":ws["workspace_id"],"requires_resume":True})
    if ws["status"]!="active":
        refuse("recovery_refused","WORKSPACE_CLOSED","Workspace is not active",{"workspace_id":ws["workspace_id"]})

    branch_state=service.client.get_branch(session["repository"],session["branch"]); actual_head=str(branch_state.commit.sha) if branch_state else ""
    repo=mygithub12._service_repo(service,session["repository"])
    actual_tree=mygithub12._tree_sha(repo.get_commit(actual_head)) if actual_head else ""
    if actual_head!=ws["head_sha"] or actual_tree!=ws["tree_sha"]:
        refuse("external_drift_detected","WORKSPACE_BRANCH_DRIFTED","GitHub branch identity differs from Workspace during Session recovery",{"actual_head":actual_head,"actual_tree":actual_tree})

    ancestry={"required":session["head_commit_sha"]!=ws["head_sha"],"verified":True}
    if session["head_commit_sha"]!=ws["head_sha"]:
        try:
            old_commit=repo.get_commit(session["head_commit_sha"]); old_tree=mygithub12._tree_sha(old_commit)
            comparison=repo.compare(session["head_commit_sha"],ws["head_sha"])
            merge_base=str(comparison.merge_base_commit.sha) if getattr(comparison,"merge_base_commit",None) else ""
            ahead_by=int(getattr(comparison,"ahead_by",0) or 0); behind_by=int(getattr(comparison,"behind_by",0) or 0)
        except Exception as exc:
            refuse("recovery_refused","DEVELOPMENT_SESSION_RECOVERY_REQUIRED","Session ancestry could not be verified",{"cause_type":type(exc).__name__})
        ancestry={"required":True,"verified":old_tree==session["tree_sha"] and merge_base==session["head_commit_sha"] and ahead_by>0 and behind_by==0,"merge_base":merge_base,"ahead_by":ahead_by,"behind_by":behind_by,"session_commit_tree":old_tree}
        if not ancestry["verified"]:
            refuse("recovery_refused","DEVELOPMENT_SESSION_RECOVERY_REQUIRED","Session HEAD is not a provable ancestor of the Workspace HEAD",{"ancestry":ancestry})

    index_status=None; index_request=None; index_error=None
    index_commit_sha=ws["head_sha"] if session.get("index_commit_sha")==ws["head_sha"] and ws.get("index_commit_sha")==ws["head_sha"] else None
    try:
        index_status=mygithub12.get_index_status(service,session["repository"],ws["head_sha"])
        if index_status.get("status")=="ready" and index_status.get("commit_sha")==ws["head_sha"] and index_status.get("tree_sha")==ws["tree_sha"]:
            index_commit_sha=ws["head_sha"]
        else:
            index_request=mygithub12.request_index_build(service,session["repository"],ws["head_sha"],"auto",session["head_commit_sha"] if ancestry["required"] else session["base_commit_sha"],"interactive",f"session-recovery-index:{session_id}:{ws['head_sha']}",False)
            if session["head_commit_sha"]!=ws["head_sha"]:
                index_commit_sha=None
    except Exception as exc:
        index_error={"code":getattr(exc,"code","INDEX_BUILD_FAILED"),"message":getattr(exc,"message","exact recovered HEAD Index request failed")}
        if session["head_commit_sha"]!=ws["head_sha"]:
            index_commit_sha=None

    evidence={"github_head_sha":actual_head,"github_tree_sha":actual_tree,"ancestry":ancestry,"index_status":index_status,"index_request":index_request,"index_error":index_error}
    recovered=sessions.recover_stale_session_from_workspace(
        session_id,expected_session_revision,ws,idempotency_key=idempotency_key,
        index_commit_sha=index_commit_sha,recovery_evidence=evidence,
    )
    return {**recovered,"workspace":ws,"index":{"status":index_status,"request":index_request,"error":index_error}}


def maybe_auto_renew_session_workspace(
    service: Any, session_id: str, expected_session_revision: int, expected_workspace_revision: int=0,
    expected_head_sha: str="", idempotency_key: str="",
) -> dict[str,Any]:
    session=sessions.get_session(session_id); ws=mygithub12.get_workspace(service,session["workspace_id"])
    current_revision=int(session["session_revision"]); workspace_revision=int(ws["revision"])
    last=(session.get("metadata") or {}).get("last_session_recovery")
    replay_candidate=bool(
        idempotency_key and isinstance(last,dict) and last.get("idempotency_key")==idempotency_key
        and int(((last.get("after") or {}).get("session_revision") or -1))==current_revision
    )
    if current_revision!=int(expected_session_revision) and not replay_candidate:
        sessions._require_revision(session_id,expected_session_revision)
    local_stale=(
        int(session["workspace_revision"])!=workspace_revision
        or session["head_commit_sha"]!=ws["head_sha"] or session["tree_sha"]!=ws["tree_sha"]
        or abs(float(session["lease_expires_at"])-float(ws["lease_expires_at"]))>0.001
    )
    recovery=None
    if local_stale or replay_candidate:
        recovery=recover_stale_session(service,session_id,expected_session_revision,expected_workspace_revision,expected_head_sha,idempotency_key)
        session=recovery["session"]; ws=recovery["workspace"]; workspace_revision=int(ws["revision"]); current_revision=int(session["session_revision"])
    else:
        sessions._require_revision(session_id,expected_session_revision)
        if expected_workspace_revision and workspace_revision!=int(expected_workspace_revision): raise MyGithub12Error("WORKSPACE_REVISION_MISMATCH","workspace revision changed",{"expected":expected_workspace_revision,"actual":workspace_revision})
        if expected_head_sha and session["head_commit_sha"]!=expected_head_sha: raise MyGithub12Error("DEVELOPMENT_SESSION_RECOVERY_REQUIRED","session HEAD identity differs from expected HEAD",{"session_head":session["head_commit_sha"],"expected_head":expected_head_sha})
    if session["status"] not in AUTO_RENEW_SESSION_STATES: raise MyGithub12Error("DEVELOPMENT_SESSION_STATE_INVALID","development session state does not permit workspace auto-renew",{"status":session["status"]})
    if int(session["workspace_revision"])!=workspace_revision: raise MyGithub12Error("DEVELOPMENT_SESSION_WORKSPACE_MISMATCH","session does not reference the current workspace revision",{"session_workspace_revision":session["workspace_revision"],"workspace_revision":workspace_revision})
    if ws["status"]=="drifted" or ws.get("drift_reason"): raise MyGithub12Error("WORKSPACE_BRANCH_DRIFTED","drifted workspace cannot be auto-renewed",{"workspace_id":ws["workspace_id"],"drift_reason":ws.get("drift_reason")})
    if ws["status"]=="expired" or not ws.get("lease_valid") or not session.get("lease_valid"): raise MyGithub12Error("WORKSPACE_LEASE_REQUIRED","expired workspace cannot be auto-renewed",{"workspace_id":ws["workspace_id"],"requires_resume":True})
    if ws["status"]!="active": raise MyGithub12Error("WORKSPACE_CLOSED","workspace is not active")
    if session["head_commit_sha"]!=ws["head_sha"] or session["tree_sha"]!=ws["tree_sha"]: raise MyGithub12Error("DEVELOPMENT_SESSION_WORKSPACE_MISMATCH","session and workspace Git identities differ")
    now=mygithub12._now(); remaining=min(float(session["lease_expires_at"]),float(ws["lease_expires_at"]))-now
    if remaining>AUTO_RENEW_THRESHOLD_SECONDS:
        return {"renewed":False,"session":session,"workspace":ws,"remaining_seconds":remaining,"recovery":recovery}
    branch_state=service.client.get_branch(session["repository"],session["branch"]); actual_head=str(branch_state.commit.sha) if branch_state else ""
    if actual_head!=session["head_commit_sha"]:
        _append_recovery_event_best_effort(session_id,current_revision,"external_drift_detected",{"reason_code":"WORKSPACE_BRANCH_DRIFTED","workspace_head":ws["head_sha"],"session_head":session["head_commit_sha"],"actual_head":actual_head})
        raise MyGithub12Error("WORKSPACE_BRANCH_DRIFTED","GitHub branch HEAD changed before auto-renew",{"workspace_head":ws["head_sha"],"session_head":session["head_commit_sha"],"actual_head":actual_head})
    repo=mygithub12._service_repo(service,session["repository"]); actual_tree=mygithub12._tree_sha(repo.get_commit(actual_head))
    if actual_tree!=session["tree_sha"] or actual_tree!=ws["tree_sha"]:
        _append_recovery_event_best_effort(session_id,current_revision,"external_drift_detected",{"reason_code":"WORKSPACE_BRANCH_DRIFTED","workspace_tree":ws["tree_sha"],"session_tree":session["tree_sha"],"actual_tree":actual_tree})
        raise MyGithub12Error("WORKSPACE_BRANCH_DRIFTED","GitHub branch Tree changed before auto-renew",{"workspace_tree":ws["tree_sha"],"session_tree":session["tree_sha"],"actual_tree":actual_tree})
    renewed=sessions.auto_renew_session_workspace_lease(
        session_id,current_revision,ws["workspace_id"],workspace_revision,
        lease_seconds=mygithub12.DEFAULT_LEASE_SECONDS,
        event_data={"idempotency_key":idempotency_key or None,"remaining_seconds":remaining,"threshold_seconds":AUTO_RENEW_THRESHOLD_SECONDS,"github_head_sha":actual_head,"github_tree_sha":actual_tree},
    )
    renewed_ws=mygithub12.get_workspace(service,ws["workspace_id"])
    return {"renewed":True,"session":renewed["session"],"workspace":renewed_ws,"remaining_seconds":remaining,"audit":renewed["audit"],"recovery":recovery}


def require_session_workspace(service: Any, session_id: str, expected_session_revision: int, expected_workspace_revision: int, expected_head_sha: str) -> tuple[dict[str,Any],dict[str,Any]]:
    session=sessions._require_revision(session_id,expected_session_revision)
    public=sessions.get_session(session_id)
    ws=mygithub12.workspace_write_preflight(service,public["repository"],public["branch"],expected_head_sha,public["workspace_id"],expected_workspace_revision)
    if int(public["workspace_revision"])!=int(expected_workspace_revision):
        raise MyGithub12Error("DEVELOPMENT_SESSION_WORKSPACE_MISMATCH","session does not reference the requested workspace revision",{"session_workspace_revision":public["workspace_revision"],"expected_workspace_revision":expected_workspace_revision})
    if public["head_commit_sha"]!=expected_head_sha or ws["head_sha"]!=expected_head_sha:
        raise MyGithub12Error("DEVELOPMENT_SESSION_RECOVERY_REQUIRED","session/workspace HEAD identity differs from expected HEAD",{"session_head":public["head_commit_sha"],"workspace_head":ws["head_sha"],"expected_head":expected_head_sha})
    return public,ws


def resolve_generated_write_context(service: Any, repository: str, branch: str, expected_head_sha: str) -> dict[str,Any]:
    """Bind the friendly generated-file facade to the branch's collaboration lock."""
    listed=mygithub12.list_workspaces(service,repository=repository,branch=branch,limit=100)
    items=list(listed.get("items") or [])
    active=[item for item in items if item.get("status")=="active" and item.get("lease_valid")]
    if len(active)>1:
        raise MyGithub12Error("WORKSPACE_LEASE_CONFLICT","multiple active workspaces own the requested branch",{"repository":repository,"branch":branch})
    if not active:
        drifted=next((item for item in items if item.get("status")=="drifted"),None)
        if drifted:
            raise MyGithub12Error("WORKSPACE_BRANCH_DRIFTED","branch is owned by a drifted workspace",{"workspace_id":drifted["workspace_id"],"repository":repository,"branch":branch})
        expired=next((item for item in items if item.get("status")=="expired"),None)
        if expired:
            raise MyGithub12Error("WORKSPACE_LEASE_REQUIRED","branch workspace lease expired",{"workspace_id":expired["workspace_id"],"repository":repository,"branch":branch,"requires_resume":True})
        return {"managed":False,"workspace":None,"session":None}

    workspace=active[0]
    session=sessions.find_active_session_for_workspace(workspace["workspace_id"])
    if session is None:
        checked=mygithub12.workspace_write_preflight(
            service,repository,branch,expected_head_sha,workspace["workspace_id"],int(workspace["revision"])
        )
        return {"managed":True,"workspace":checked,"session":None}
    if session["status"] not in AUTO_RENEW_SESSION_STATES:
        raise MyGithub12Error("DEVELOPMENT_SESSION_STATE_INVALID","development session state does not permit generated-file writes",{"development_session_id":session["session_id"],"status":session["status"]})
    if not session.get("lease_valid"):
        raise MyGithub12Error("WORKSPACE_LEASE_REQUIRED","development session lease expired",{"development_session_id":session["session_id"],"workspace_id":workspace["workspace_id"],"requires_resume":True})
    checked_session,checked_workspace=require_session_workspace(
        service,session["session_id"],int(session["session_revision"]),int(workspace["revision"]),expected_head_sha
    )
    return {"managed":True,"workspace":checked_workspace,"session":checked_session}


def _validate_change_set(change: dict[str,Any]) -> dict[str,Any]:
    if change.get("schema_version")!=1: raise MyGithub12Error("PATCH_INVALID_FORMAT","change_set_json schema_version must be 1")
    mode=change.get("mode")
    if mode not in {"patch","range","upload"}: raise MyGithub12Error("PATCH_INVALID_FORMAT","change set mode must be patch, range, or upload")
    if mode=="patch" and not isinstance(change.get("patch"),str): raise MyGithub12Error("PATCH_INVALID_FORMAT","patch mode requires patch text")
    if mode=="range" and not isinstance(change.get("range_operations"),list): raise MyGithub12Error("PATCH_INVALID_FORMAT","range mode requires range_operations")
    if mode=="upload":
        uploads=change.get("uploaded_files")
        if not isinstance(uploads,list) or not uploads or not all(isinstance(item,dict) for item in uploads): raise MyGithub12Error("PATCH_INVALID_FORMAT","upload mode requires one or more finalized uploaded files")
        if len(uploads)>mygithub10.MAX_UPLOAD_CHANGE_SET_FILES: raise MyGithub12Error("PATCH_INVALID_FORMAT",f"upload mode exceeds file limit: {mygithub10.MAX_UPLOAD_CHANGE_SET_FILES}")
        paths=[]; upload_ids=[]
        for item in uploads:
            path=item.get("path"); upload_id=item.get("upload_id")
            if not isinstance(path,str) or not path: raise MyGithub12Error("PATCH_INVALID_FORMAT","upload item requires path")
            if not isinstance(upload_id,str) or not upload_id: raise MyGithub12Error("PATCH_INVALID_FORMAT","upload item requires upload_id")
            if path in paths: raise MyGithub12Error("PATCH_INVALID_FORMAT",f"duplicate upload target path: {path}")
            if upload_id in upload_ids: raise MyGithub12Error("PATCH_INVALID_FORMAT",f"duplicate upload_id: {upload_id}")
            paths.append(path); upload_ids.append(upload_id)
    canonical=json.dumps(change,ensure_ascii=False,sort_keys=True,separators=(",",":"))
    return {"change":change,"mode":mode,"canonical_hash":hashlib.sha256(canonical.encode()).hexdigest()}


def parse_change_set(change_set_json: str) -> dict[str,Any]:
    return _validate_change_set(_json(change_set_json,dict,"change_set_json",{}))


def parse_change_set_bytes(raw_bytes: bytes) -> dict[str,Any]:
    """Parse exact downloaded bytes without newline, BOM, or Unicode normalization."""
    try:
        text=raw_bytes.decode("utf-8",errors="strict")
    except UnicodeDecodeError as exc:
        raise MyGithub12Error(
            "CHANGE_SET_INVALID_UTF8","change_set_file must contain strict UTF-8 JSON",
            {"start":exc.start,"end":exc.end},
        ) from exc
    try:
        change=json.loads(text)
    except json.JSONDecodeError as exc:
        raise MyGithub12Error(
            "CHANGE_SET_INVALID_JSON","change_set_file must contain valid JSON",
            {"position":exc.pos},
        ) from exc
    if not isinstance(change,dict):
        raise MyGithub12Error("CHANGE_SET_INVALID_JSON","change_set_file JSON must be an object")
    return _validate_change_set(change)


def change_set_raw_identity(raw_bytes: bytes) -> dict[str,Any]:
    return {
        "received_size_bytes":len(raw_bytes),
        "received_sha256":hashlib.sha256(raw_bytes).hexdigest(),
        "received_git_blob_sha":hashlib.sha1(
            f"blob {len(raw_bytes)}\0".encode("ascii")+raw_bytes
        ).hexdigest(),
    }


def execute_change_set(service: Any, session: dict[str,Any], workspace: dict[str,Any], parsed: dict[str,Any], expected_head_sha: str, expected_workspace_revision: int, commit_message: str, dry_run: bool, idempotency_key: str, audit_context: dict[str,Any]) -> dict[str,Any]:
    change=parsed["change"]; mode=parsed["mode"]
    if mode=="patch":
        expected=change.get("expected_blob_shas") or {}
        if not isinstance(expected,dict): raise MyGithub12Error("PATCH_INVALID_FORMAT","expected_blob_shas must be an object")
        result=mygithub10.apply_patch(service,session["repository"],session["branch"],expected_head_sha,json.dumps(expected),change["patch"],commit_message,dry_run,idempotency_key,audit_context)
    elif mode=="range":
        result=mygithub10.edit_ranges(service,session["repository"],session["branch"],expected_head_sha,json.dumps(change["range_operations"]),commit_message,dry_run,idempotency_key,audit_context)
    else:
        items=change["uploaded_files"]
        for item in items: mygithub10._safe_path(item["path"])
        if dry_run:
            # Reuse the exact HEAD/blob CAS preflight used by the real commit, then verify every finalized upload without consuming any body.
            mygithub10.preflight_upload_targets(service,session["repository"],session["branch"],expected_head_sha,items)
            changed_files=[]; total_size=0
            for item in items:
                _,_,meta=mygithub10._load_upload(item["upload_id"])
                if not meta.get("finalized"): raise MyGithub12Error("UPLOAD_NOT_FINALIZED",f"finalize upload before change-set commit: {item['upload_id']}")
                total_size+=int(meta["size"])
                if total_size>mygithub10.MAX_UPLOAD_CHANGE_SET_BYTES: raise MyGithub12Error("UPLOAD_SIZE_EXCEEDED",f"upload mode exceeds aggregate size limit: {mygithub10.MAX_UPLOAD_CHANGE_SET_BYTES}")
                changed_files.append({"path":item["path"],"operation":"upsert","size_bytes":meta["size"],"content_sha256":meta["sha256"]})
            result={"ok":True,"dry_run":True,"repository":session["repository"],"branch":session["branch"],"expected_head_sha":expected_head_sha,"changed_files":changed_files}
        elif len(items)==1:
            item=items[0]
            result=mygithub10.commit_upload(service,session["repository"],session["branch"],expected_head_sha,item["path"],str(item.get("expected_blob_sha") or ""),item["upload_id"],commit_message,idempotency_key,audit_context)
        else:
            result=mygithub10.commit_uploads(service,session["repository"],session["branch"],expected_head_sha,items,commit_message,idempotency_key,audit_context)
    result["change_set_canonical_hash"]=parsed["canonical_hash"]
    return result


def after_verified_change(service: Any, session_id: str, expected_session_revision: int, finalized_write: dict[str,Any], event_type: str="change_set_committed") -> dict[str,Any]:
    ws=finalized_write.get("workspace")
    if not isinstance(ws,dict): raise MyGithub12Error("DEVELOPMENT_SESSION_RECOVERY_REQUIRED","verified write did not return finalized workspace evidence")
    session=sessions.sync_from_workspace(session_id,expected_session_revision,ws,event_type=event_type,status="active",metadata_patch={"last_commit_operation_id":finalized_write.get("operation_id","")})
    try:
        index=mygithub12.request_index_build(service,session["repository"],session["head_commit_sha"],"auto",finalized_write.get("old_head_sha",session["base_commit_sha"]),"interactive",f"session-index:{session_id}:{session['head_commit_sha']}",False)
        index_error=None
    except Exception as exc:
        index=None; index_error={"code":getattr(exc,"code","INDEX_BUILD_FAILED"),"message":getattr(exc,"message","incremental index request failed")}
    return {"session":session,"index":index,"index_error":index_error}


def recover_after_verified_change(service: Any, session_id: str, expected_session_revision: int, finalized_write: dict[str,Any], event_type: str="change_set_committed") -> dict[str,Any]:
    """Resume Session/index finalization after a verified Git/Workspace write.

    A process can disappear after the Workspace CAS but before the Development
    Session response is persisted. Accept exactly the two safe states: the
    original Session revision (finish it now), or the immediately advanced
    revision already synchronized to the verified Workspace. Anything else is
    concurrent drift and must fail-stop.
    """
    ws=finalized_write.get("workspace")
    if not isinstance(ws,dict):
        raise MyGithub12Error("DEVELOPMENT_SESSION_RECOVERY_REQUIRED","verified write did not return finalized workspace evidence")
    current=sessions.get_session(session_id)
    current_revision=int(current.get("session_revision") or 0)
    if current_revision==int(expected_session_revision):
        return after_verified_change(service,session_id,expected_session_revision,finalized_write,event_type)
    if (
        current_revision==int(expected_session_revision)+1
        and current.get("workspace_id")==ws.get("workspace_id")
        and current.get("repository")==ws.get("repository")
        and current.get("branch")==ws.get("branch")
        and current.get("head_commit_sha")==ws.get("head_sha")
        and current.get("tree_sha")==ws.get("tree_sha")
        and int(current.get("workspace_revision") or 0)==int(ws.get("revision") or 0)
    ):
        try:
            index=mygithub12.request_index_build(service,current["repository"],current["head_commit_sha"],"auto",finalized_write.get("old_head_sha",current["base_commit_sha"]),"interactive",f"session-index:{session_id}:{current['head_commit_sha']}",False)
            index_error=None
        except Exception as exc:
            index=None; index_error={"code":getattr(exc,"code","INDEX_BUILD_FAILED"),"message":getattr(exc,"message","incremental index request failed")}
        return {"session":current,"index":index,"index_error":index_error,"recovered":True}
    raise MyGithub12Error(
        "DEVELOPMENT_SESSION_RECOVERY_REQUIRED",
        "Development Session changed after the verified Git write",
        {
            "development_session_id":session_id,
            "expected_session_revision":expected_session_revision,
            "actual_session_revision":current_revision,
            "verified_head_sha":ws.get("head_sha"),
            "actual_head_sha":current.get("head_commit_sha"),
            "recovery_required":True,
        },
    )


def affected_selection(service: Any, session: dict[str,Any], base_sha: str) -> dict[str,Any]:
    base=base_sha or session["base_commit_sha"]
    try:
        impact=mygithub12.change_impact(service,session["repository"],base,session["head_commit_sha"])
        changed=impact.get("changed_paths",[]); tests=impact.get("affected_tests",[]); contracts=impact.get("contract_changes",[])
        # Conservative top-level workspace approximation. Empty/incomplete never
        # narrows the gate to zero.
        workspaces=sorted({p.split("/",1)[0] if "/" in p else "." for p in changed}) or ["."]
        complete=bool(changed) or base==session["head_commit_sha"]
        return {"complete":complete,"changed_paths":changed,"selected_workspaces":workspaces,"selected_tests":tests,"contract_changes":contracts,"reasons":["exact_commit_change_impact"]}
    except Exception as exc:
        return {"complete":False,"changed_paths":[],"selected_workspaces":["."],"selected_tests":[],"contract_changes":[],"reasons":["analysis_unavailable_fallback_full"],"error_type":type(exc).__name__}


def validation_preflight(service: Any, session: dict[str,Any], mode: str, base_sha: str) -> dict[str,Any]:
    if mode not in {"fast","full","reuse_or_full"}:
        raise MyGithub12Error("DEVELOPMENT_SESSION_STATE_INVALID","validation mode must be fast, full, or reuse_or_full")
    profile="repo-fast-check" if mode=="fast" else "repo-auto-check"
    if not is_profile_allowed(session["repository"],profile):
        raise MyGithub12Error(
            "CI_PROFILE_DISCOVERY_MISMATCH",f"{profile} is not discoverable for repository",
            {"allowed_profiles":get_allowed_profiles(session["repository"])},
        )
    resolved_base=base_sha or session["base_commit_sha"]
    selection=affected_selection(service,session,resolved_base)
    compare=github_utils.get_github_changed_files_result(session["repository"],resolved_base,session["head_commit_sha"])
    if not compare.get("ok"):
        raise MyGithub12Error(
            compare.get("error_code","CHANGED_FILES_COMPARE_FAILED"),
            compare.get("message","changed file comparison failed"),compare.get("details",{}),
        )
    return {
        "profile":profile,"base_sha":resolved_base,"selection":selection,"compare":compare,
        "priority":effective_priority(session["branch"],profile,ALLOWED_PRIORITIES["normal"]),
        "timeout_seconds":get_max_timeout(session["repository"]),
    }


def start_validation_job(
    service: Any, session: dict[str,Any], mode: str, base_sha: str,
    force_rerun: bool, supersede_previous: bool, prepared: dict[str,Any] | None=None,
) -> tuple[dict[str,Any],dict[str,Any]]:
    prepared=prepared or validation_preflight(service,session,mode,base_sha)
    compare=prepared["compare"]; profile=prepared["profile"]
    job=create_or_get_job(
        session["repository"],session["branch"],session["head_commit_sha"],profile,
        prepared["priority"],prepared["timeout_seconds"],force_rerun,supersede_previous,
        prepared["base_sha"],compare["changed_files"],compare["total_count"],compare["truncated"],
    )
    return job,prepared["selection"]


def wait_validation(job_id: str, wait_seconds: int) -> dict[str,Any]:
    wait=max(0,min(int(wait_seconds),55))
    if wait:
        snap=_job_snapshot(job_id)
        wait_for_job_change(job_id,wait,snap.get("status", ""),snap.get("current_step") or "",int(snap.get("revision",0)))
    job=get_job(job_id)
    if not job: raise MyGithub12Error("PRIVATE_CI_JOB_NOT_FOUND","private CI job disappeared",{"job_id":job_id})
    return job


VALIDATION_TERMINAL_STATUSES = {"passed", "failed", "timed_out", "cancelled", "superseded", "worker_lost", "internal_error"}


def validation_result(session_id: str, session_revision: int, mode: str, job: dict[str,Any], selection: dict[str,Any], include_failure_pack: bool=True) -> dict[str,Any]:
    status=job.get("status"); terminal=status in VALIDATION_TERMINAL_STATUSES; merge_eligible=bool(mode!="fast" and status=="passed")
    attestation=None; failure=None
    if merge_eligible:
        try: attestation=attestation_registry.create_attestation_for_passed_job(job_id=job["job_id"])
        except ValueError as exc: attestation={"ok":False,"error_code":str(exc)}; merge_eligible=False
    elif terminal and status!="passed" and include_failure_pack:
        try: failure=build_failure_pack(job,affected=selection)
        except Exception: failure={"summary":{"job_id":job.get("job_id"),"status":status},"error_code":"FAILURE_PACK_UNAVAILABLE"}
    sessions.record_validation(session_id,session_revision,mode,job.get("commit_sha",""),(job.get("summary") or {}).get("git_tree_sha","") if isinstance(job.get("summary"),dict) else "",job_id=job["job_id"],status=status,merge_eligible=merge_eligible,attestation_id=(attestation or {}).get("attestation_id","") if isinstance(attestation,dict) else "",evidence={"selection":selection},finished=terminal)
    return {"job":{"job_id":job["job_id"],"status":status,"profile":job.get("profile"),"commit_sha":job.get("commit_sha"),"current_step":job.get("current_step"),"exit_code":job.get("exit_code")},"affected":selection,"merge_eligible":merge_eligible,"attestation":attestation,"failure_pack":failure,"terminal":terminal}
