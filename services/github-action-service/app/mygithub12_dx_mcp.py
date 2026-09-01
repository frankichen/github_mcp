"""Register stable development orchestration and drift-recovery tools."""
from __future__ import annotations

import json
import hashlib
import logging
import re
import uuid
from typing import Any, Callable, Awaitable, NotRequired, TypedDict

from mcp.types import ToolAnnotations

from app import artifact_store
from app import development_drift_recovery as drift_recovery
from app import development_converge as converge
from app import development_orchestrator as dx
from app import development_resume as resume
from app import development_session_store as sessions
from app import development_change_set_store as prepared_store
from app import github_utils, mygithub12
from app import mygithub10

logger=logging.getLogger(__name__)
_ORCHESTRATION=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=True)


class ChangeSetFile(TypedDict):
    download_url: str
    file_id: str
    mime_type: NotRequired[str]
    file_name: NotRequired[str]


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


def register_dx_tools(
    mcp,
    github_call: Callable[..., Awaitable[Any]],
    service: Any,
    finalize_write: Callable[..., Awaitable[dict]],
    ingest_runtime_artifact: Callable[..., Awaitable[artifact_store.ArtifactRef]] | None = None,
) -> None:
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

    @mcp.tool(
        name="recover_drifted_development_task",
        description="Explicitly adopt a freshly verified forward-only externally advanced branch into drifted Workspace/Development Session control-plane state; never moves Git refs or writes repository files.",
        annotations=_ORCHESTRATION,
    )
    async def recover_drifted_development_task(
        repository: str,
        branch: str,
        workspace_id: str,
        development_session_id: str,
        expected_workspace_revision: int,
        expected_session_revision: int,
        expected_current_head_sha: str,
        expected_current_tree_sha: str,
        expected_base_branch: str,
        expected_base_sha: str,
        idempotency_key: str,
        lease_seconds: int=mygithub12.DEFAULT_LEASE_SECONDS,
    ) -> str:
        try:
            result=await github_call(
                drift_recovery.recover_drifted_task,service,repository,branch,workspace_id,development_session_id,
                expected_workspace_revision,expected_session_revision,expected_current_head_sha,expected_current_tree_sha,
                expected_base_branch,expected_base_sha,idempotency_key,lease_seconds,
            )
            return json.dumps(result,ensure_ascii=False)
        except Exception as exc: return _error(exc)

    @mcp.tool(
        name="apply_development_change_set",
        description=(
            "Strictly validate/apply a versioned patch/range/upload ChangeSet with Session, Workspace, "
            "HEAD/blob CAS and durable GitHub read-back. Small payloads may use change_set_json; large or "
            "exact-byte-sensitive payloads must use change_set_file with raw size/SHA identity. A raw "
            "strict dry-run freezes a short-lived prepared_change_set_id; the real write should use that ID "
            "so the Candidate is not transported again. Exactly one raw/prepared source is accepted."
        ),
        meta={"openai/fileParams":["change_set_file"]},
        annotations=_ORCHESTRATION,
    )
    async def apply_development_change_set(
        development_session_id: str, expected_session_revision: int, expected_workspace_revision: int, expected_head_sha: str,
        commit_message: str, change_set_json: str="", change_set_file: ChangeSetFile | None=None,
        expected_change_set_size_bytes: int=0, expected_change_set_sha256: str="",
        expected_change_set_git_blob_sha: str="", prepared_change_set_id: str="",
        dry_run: bool=True, idempotency_key: str="", create_pull_request: bool=False, pull_request_json: str="{}",
    ) -> str:
        claimed_prepared_id=""
        owned_artifact_id=""
        try:
            inline_source=isinstance(change_set_json,str) and bool(change_set_json)
            file_source=change_set_file is not None
            prepared_source=bool(prepared_change_set_id)
            source_count=sum((inline_source,file_source,prepared_source))
            if source_count==0:
                raise mygithub12.MyGithub12Error(
                    "CHANGE_SET_FILE_REQUIRED",
                    "provide exactly one of change_set_json, change_set_file, or prepared_change_set_id",
                )
            if source_count!=1:
                raise mygithub12.MyGithub12Error(
                    "CHANGE_SET_SOURCE_CONFLICT",
                    "change_set_json, change_set_file, and prepared_change_set_id are mutually exclusive",
                )
            if not file_source and (
                expected_change_set_size_bytes or expected_change_set_sha256 or expected_change_set_git_blob_sha
            ):
                raise mygithub12.MyGithub12Error(
                    "CHANGE_SET_SOURCE_CONFLICT",
                    "expected raw file identities are valid only with change_set_file",
                )
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
            if not prepared_source and not dry_run:
                raise mygithub12.MyGithub12Error(
                    "CHANGE_SET_SOURCE_CONFLICT",
                    "real write requires prepared_change_set_id; raw ChangeSet sources are prepare-only",
                )

            raw_identity={}; payload_source="prepared_change_set"; source_artifact=None
            effective_idempotency_key=idempotency_key
            if prepared_source:
                if dry_run:
                    raise mygithub12.MyGithub12Error(
                        "CHANGE_SET_SOURCE_CONFLICT",
                        "prepared_change_set_id is a frozen write source and requires dry_run=false",
                    )
                prepared=await github_call(prepared_store.get_prepared_change_set,prepared_change_set_id)
                if prepared["development_session_id"]!=development_session_id:
                    raise mygithub12.MyGithub12Error(
                        "PREPARED_CHANGE_SET_SCOPE_MISMATCH","prepared change set belongs to another Development Session"
                    )
                if int(prepared["session_revision"])!=int(expected_session_revision):
                    raise mygithub12.MyGithub12Error(
                        "DEVELOPMENT_SESSION_REVISION_MISMATCH","prepared change set is bound to another Session revision",
                        {"expected":prepared["session_revision"],"actual":expected_session_revision},
                    )
                if int(prepared["workspace_revision"])!=int(expected_workspace_revision):
                    raise mygithub12.MyGithub12Error(
                        "WORKSPACE_REVISION_MISMATCH","prepared change set is bound to another Workspace revision",
                        {"expected":prepared["workspace_revision"],"actual":expected_workspace_revision},
                    )
                if prepared["expected_head_sha"]!=expected_head_sha:
                    raise mygithub12.MyGithub12Error(
                        "HEAD_CHANGED","prepared change set is bound to another HEAD",
                        {"expected":prepared["expected_head_sha"],"actual":expected_head_sha},
                    )
                effective_idempotency_key=idempotency_key or f"prepared-write:{prepared_change_set_id}"
                fingerprint=hashlib.sha256(json.dumps({
                    "prepared_change_set_id":prepared_change_set_id,
                    "development_session_id":development_session_id,
                    "expected_session_revision":expected_session_revision,
                    "expected_workspace_revision":expected_workspace_revision,
                    "expected_head_sha":expected_head_sha,
                    "commit_message":commit_message,
                    "create_pull_request":create_pull_request,
                    "pull_request_json":pull_request_json,
                },ensure_ascii=False,sort_keys=True,separators=(",",":")).encode("utf-8")).hexdigest()
                recovery=await github_call(
                    prepared_store.recover_executing_write,prepared_change_set_id,
                    idempotency_key=effective_idempotency_key,request_fingerprint=fingerprint,
                )
                if recovery and recovery.get("action") in {"resume_git_verified","resume_success_verified"}:
                    recovered_result=dict(recovery.get("result") or {})
                    recovered_result.update({
                        "payload_source":"prepared_change_set",
                        "received_size_bytes":int(prepared["raw_size_bytes"]),
                        "received_sha256":prepared["raw_sha256"],
                        "received_git_blob_sha":prepared["raw_git_blob_sha"],
                        "change_set_canonical_hash":prepared["canonical_change_set_hash"],
                    })
                    if recovery["action"]=="resume_git_verified":
                        recovered_result["_operation_id"]=recovery.get("operation_id","")
                        try:
                            finalized=await finalize_write(
                                recovered_result,prepared["workspace_id"],int(prepared["workspace_revision"])
                            )
                        except Exception as finalize_exc:
                            finalize_error=_error_payload(finalize_exc)
                            committed_response={
                                "ok":False,**recovered_result,
                                "prepared_change_set_id":prepared_change_set_id,
                                "development_session_id":development_session_id,
                                "recovery_required":True,
                                "failed_stage":"workspace_finalize",
                                "orchestration_error":finalize_error["error"],
                                "recovered_from_interrupted_execution":True,
                            }
                            await github_call(
                                prepared_store.mark_committed,prepared_change_set_id,committed_response
                            )
                            return json.dumps(committed_response,ensure_ascii=False)
                    else:
                        finalized=recovered_result
                    try:
                        state=await github_call(
                            dx.recover_after_verified_change,service,development_session_id,
                            expected_session_revision,finalized,
                        )
                    except Exception as state_exc:
                        state_error=_error_payload(state_exc)
                        committed_response={
                            "ok":False,**finalized,"write_verified":True,
                            "prepared_change_set_id":prepared_change_set_id,
                            "development_session_id":development_session_id,
                            "recovery_required":True,
                            "failed_stage":"development_session_finalize",
                            "orchestration_error":state_error["error"],
                            "recovered_from_interrupted_execution":True,
                        }
                        await github_call(
                            prepared_store.mark_committed,prepared_change_set_id,committed_response
                        )
                        return json.dumps(committed_response,ensure_ascii=False)
                    response={
                        "ok":True,**finalized,
                        "prepared_change_set_id":prepared_change_set_id,
                        "development_session":state["session"],
                        "index":state["index"],"index_error":state["index_error"],
                        "recovered_from_interrupted_execution":True,
                        "lease_maintenance":{"renewed":False,"remaining_seconds":None,"audit":None,"recovery":None},
                    }
                    if create_pull_request:
                        response["pull_request"]=None
                        response["pull_request_error"]={
                            "code":"PULL_REQUEST_RECOVERY_REQUIRED",
                            "message":"verified commit was recovered; prepare or update the pull request from the recovered Development Session",
                            "details":{"commit_already_verified":True,"session_recovery_required":False},
                        }
                        response["partial_success"]=True
                    await github_call(
                        prepared_store.mark_committed,prepared_change_set_id,response
                    )
                    return json.dumps(response,ensure_ascii=False)
                replay=await github_call(
                    prepared_store.replay_write,prepared_change_set_id,
                    idempotency_key=effective_idempotency_key,request_fingerprint=fingerprint,
                )
                if replay is not None:
                    return json.dumps(replay,ensure_ascii=False)
                prepared,raw_bytes=await github_call(prepared_store.load_prepared_bytes,prepared_change_set_id)
                parsed=dx.parse_change_set_bytes(raw_bytes)
                if parsed["canonical_hash"]!=prepared["canonical_change_set_hash"]:
                    raise mygithub12.MyGithub12Error(
                        "PREPARED_CHANGE_SET_NOT_FOUND","prepared ChangeSet canonical identity is invalid"
                    )
                session,workspace=await github_call(
                    dx.require_session_workspace,service,development_session_id,expected_session_revision,
                    expected_workspace_revision,expected_head_sha,
                )
                if (
                    session["repository"]!=prepared["repository"]
                    or session["branch"]!=prepared["branch"]
                    or workspace["workspace_id"]!=prepared["workspace_id"]
                ):
                    raise mygithub12.MyGithub12Error(
                        "PREPARED_CHANGE_SET_SCOPE_MISMATCH",
                        "prepared change set repository/branch/workspace scope changed",
                    )
                effective_session_revision=expected_session_revision
                effective_workspace_revision=expected_workspace_revision
                lease_maintenance={"renewed":False,"remaining_seconds":None,"audit":None,"recovery":None}
                audit={"development_session_id":development_session_id,"session_revision":effective_session_revision,"workspace_id":workspace["workspace_id"],"workspace_revision":effective_workspace_revision,"prepared_change_set_id":prepared_change_set_id}
                strict_recheck=await github_call(
                    dx.execute_change_set,service,session,workspace,parsed,expected_head_sha,
                    effective_workspace_revision,commit_message,True,effective_idempotency_key,audit,
                )
                current_paths=sorted(
                    str(item.get("path")) for item in strict_recheck.get("changed_files",[])
                    if isinstance(item,dict) and item.get("path")
                )
                if current_paths!=prepared["affected_paths"]:
                    raise mygithub12.MyGithub12Error(
                        "PREPARED_CHANGE_SET_SCOPE_MISMATCH","prepared affected paths changed during write revalidation"
                    )
                observed_blobs={
                    str(item["path"]):item.get("old_blob_sha")
                    for item in strict_recheck.get("changed_files",[])
                    if isinstance(item,dict) and item.get("path") and "old_blob_sha" in item
                }
                for path,expected_blob in prepared["expected_blob_identities"].items():
                    if path in observed_blobs and observed_blobs[path]!=expected_blob:
                        raise mygithub12.MyGithub12Error(
                            "BLOB_CHANGED","prepared blob identity changed before write",
                            {"path":path,"expected":expected_blob,"actual":observed_blobs[path]},
                        )
                replay=await github_call(
                    prepared_store.claim_for_write,prepared_change_set_id,
                    idempotency_key=effective_idempotency_key,request_fingerprint=fingerprint,
                )
                if replay is not None:
                    return json.dumps(replay,ensure_ascii=False)
                claimed_prepared_id=prepared_change_set_id
                raw_identity={
                    "received_size_bytes":prepared["raw_size_bytes"],
                    "received_sha256":prepared["raw_sha256"],
                    "received_git_blob_sha":prepared["raw_git_blob_sha"],
                }
            else:
                if file_source:
                    if ingest_runtime_artifact is None:
                        raise mygithub12.MyGithub12Error(
                            "CHANGE_SET_FILE_INVALID","runtime-file ingress is unavailable"
                        )
                    try:
                        source_artifact=await ingest_runtime_artifact(
                            change_set_file,
                            kind="development_change_set",
                            max_bytes=mygithub10.MAX_DEVELOPMENT_CHANGE_SET_FILE_BYTES,
                            label="change_set_file",
                            session_scope=development_session_id,
                            ttl_seconds=mygithub10.PREPARED_CHANGE_SET_TTL_SECONDS,
                        )
                    except Exception as exc:
                        ingress_code=str(getattr(exc,"code",""))
                        code=(
                            "CHANGE_SET_FILE_TOO_LARGE"
                            if ingress_code in {"TOO_LARGE","ARTIFACT_TOO_LARGE"}
                            else "CHANGE_SET_FILE_INVALID"
                        )
                        raise mygithub12.MyGithub12Error(
                            code,
                            str(getattr(exc,"message","change_set_file ingress failed")),
                            dict(getattr(exc,"details",{}) or {}),
                        ) from exc
                    owned_artifact_id=source_artifact.artifact_id
                    raw_identity={
                        "received_size_bytes":source_artifact.size_bytes,
                        "received_sha256":source_artifact.sha256,
                        "received_git_blob_sha":source_artifact.git_blob_sha,
                    }
                    if isinstance(expected_change_set_size_bytes,bool) or expected_change_set_size_bytes<=0:
                        raise mygithub12.MyGithub12Error(
                            "CHANGE_SET_SIZE_MISMATCH","expected_change_set_size_bytes is required for change_set_file"
                        )
                    if raw_identity["received_size_bytes"]!=expected_change_set_size_bytes:
                        raise mygithub12.MyGithub12Error(
                            "CHANGE_SET_SIZE_MISMATCH","downloaded ChangeSet byte count differs from expected",
                            {"expected":expected_change_set_size_bytes,"actual":raw_identity["received_size_bytes"]},
                        )
                    if not re.fullmatch(r"[0-9a-f]{64}",expected_change_set_sha256 or "") or raw_identity["received_sha256"]!=expected_change_set_sha256:
                        raise mygithub12.MyGithub12Error(
                            "CHANGE_SET_SHA256_MISMATCH","downloaded ChangeSet SHA-256 differs from expected",
                            {"expected":expected_change_set_sha256,"actual":raw_identity["received_sha256"]},
                        )
                    if expected_change_set_git_blob_sha and (
                        not re.fullmatch(r"[0-9a-f]{40}",expected_change_set_git_blob_sha)
                        or raw_identity["received_git_blob_sha"]!=expected_change_set_git_blob_sha
                    ):
                        raise mygithub12.MyGithub12Error(
                            "CHANGE_SET_GIT_BLOB_SHA_MISMATCH","downloaded ChangeSet Git Blob SHA differs from expected",
                            {"expected":expected_change_set_git_blob_sha,"actual":raw_identity["received_git_blob_sha"]},
                        )
                    raw_bytes=await github_call(
                        artifact_store.read_artifact_bytes,source_artifact.artifact_id,
                        session_scope=development_session_id,
                    )
                    parsed=dx.parse_change_set_bytes(raw_bytes)
                    payload_source="change_set_file"
                else:
                    try:
                        raw_bytes=change_set_json.encode("utf-8",errors="strict")
                    except UnicodeEncodeError as exc:
                        raise mygithub12.MyGithub12Error(
                            "CHANGE_SET_INVALID_UTF8","change_set_json must be valid UTF-8 text"
                        ) from exc
                    if len(raw_bytes)>mygithub10.MAX_DEVELOPMENT_CHANGE_SET_INLINE_BYTES:
                        raise mygithub12.MyGithub12Error(
                            "CHANGE_SET_FILE_REQUIRED",
                            "large ChangeSet must use change_set_file instead of inline text",
                            {"size_bytes":len(raw_bytes),"inline_limit_bytes":mygithub10.MAX_DEVELOPMENT_CHANGE_SET_INLINE_BYTES},
                        )
                    parsed=dx.parse_change_set(change_set_json)
                    source_artifact=await github_call(
                        artifact_store.store_bytes,raw_bytes,
                        kind="development_change_set",
                        max_bytes=mygithub10.MAX_DEVELOPMENT_CHANGE_SET_FILE_BYTES,
                        source_transport="inline_tool_argument",
                        file_name="change-set.json",
                        mime_type="application/json",
                        session_scope=development_session_id,
                        ttl_seconds=mygithub10.PREPARED_CHANGE_SET_TTL_SECONDS,
                    )
                    owned_artifact_id=source_artifact.artifact_id
                    raw_identity={
                        "received_size_bytes":source_artifact.size_bytes,
                        "received_sha256":source_artifact.sha256,
                        "received_git_blob_sha":source_artifact.git_blob_sha,
                    }
                    payload_source="change_set_json"
                maintenance=await github_call(dx.maybe_auto_renew_session_workspace,service,development_session_id,expected_session_revision,expected_workspace_revision,expected_head_sha,idempotency_key)
                session=maintenance["session"]; workspace=maintenance["workspace"]
                effective_session_revision=int(session["session_revision"]); effective_workspace_revision=int(workspace["revision"])
                lease_maintenance={"renewed":bool(maintenance.get("renewed")),"remaining_seconds":maintenance.get("remaining_seconds"),"audit":maintenance.get("audit"),"recovery":maintenance.get("recovery")}
                if expected_head_sha!=session["head_commit_sha"]:
                    raise mygithub12.MyGithub12Error("HEAD_CHANGED","expected HEAD differs from the recovered Development Session HEAD",{"expected":expected_head_sha,"actual":session["head_commit_sha"]})
                session,workspace=await github_call(dx.require_session_workspace,service,development_session_id,effective_session_revision,effective_workspace_revision,session["head_commit_sha"])
                audit={"development_session_id":development_session_id,"session_revision":effective_session_revision,"workspace_id":workspace["workspace_id"],"workspace_revision":effective_workspace_revision}

            result=await github_call(dx.execute_change_set,service,session,workspace,parsed,session["head_commit_sha"],effective_workspace_revision,commit_message,dry_run,effective_idempotency_key,audit)
            result.update({"payload_source":payload_source,**raw_identity})
            if dry_run:
                if source_artifact is None:
                    raise mygithub12.MyGithub12Error(
                        "PREPARED_CHANGE_SET_NOT_FOUND","source artifact is unavailable"
                    )
                prepared=await github_call(
                    prepared_store.create_prepared_change_set,source_artifact,parsed,result,session,workspace,
                    expected_head_sha=session["head_commit_sha"],
                )
                owned_artifact_id=""
                result.update({
                    "prepared_change_set_id":prepared["prepared_change_set_id"],
                    "expires_at":prepared["expires_at"],
                    "development_session_id":development_session_id,
                    "session_revision":effective_session_revision,
                    "workspace_id":workspace["workspace_id"],
                    "workspace_revision":effective_workspace_revision,
                    "expected_head_sha":session["head_commit_sha"],
                    "lease_maintenance":lease_maintenance,
                })
                return json.dumps(result,ensure_ascii=False)
            try:
                finalized=await finalize_write(
                    result,workspace["workspace_id"],effective_workspace_revision
                )
            except Exception as finalize_exc:
                if claimed_prepared_id and result.get("write_verified") and result.get("commit_sha"):
                    finalize_error=_error_payload(finalize_exc)
                    committed_response={
                        "ok":False,**result,
                        "development_session_id":development_session_id,
                        "recovery_required":True,
                        "failed_stage":"workspace_finalize",
                        "orchestration_error":finalize_error["error"],
                    }
                    await github_call(
                        prepared_store.mark_committed,claimed_prepared_id,committed_response
                    )
                    claimed_prepared_id=""
                    return json.dumps(committed_response,ensure_ascii=False)
                raise
            try:
                state=await github_call(dx.after_verified_change,service,development_session_id,effective_session_revision,finalized)
            except Exception as state_exc:
                state_error=_error_payload(state_exc)
                failed_response={
                    "ok":False,**finalized,"write_verified":True,
                    "development_session_id":development_session_id,
                    "recovery_required":True,"failed_stage":"development_session_finalize",
                    "orchestration_error":state_error["error"],
                }
                if claimed_prepared_id:
                    await github_call(prepared_store.mark_committed,claimed_prepared_id,failed_response)
                    claimed_prepared_id=""
                return json.dumps(failed_response,ensure_ascii=False)
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
            if claimed_prepared_id:
                response["prepared_change_set_id"]=claimed_prepared_id
                await github_call(prepared_store.mark_committed,claimed_prepared_id,response)
                claimed_prepared_id=""
            return json.dumps(response,ensure_ascii=False)
        except Exception as exc:
            if claimed_prepared_id:
                try:
                    await github_call(
                        prepared_store.mark_failed_terminal,claimed_prepared_id,
                        str(getattr(exc,"code","INTERNAL_ERROR")),
                    )
                except Exception:
                    logger.exception("prepared ChangeSet failure state could not be persisted")
            if owned_artifact_id:
                try:
                    await github_call(artifact_store.consume_artifact,owned_artifact_id)
                except Exception:
                    logger.exception("unprepared ChangeSet artifact could not be invalidated")
            return _error(exc)

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

    @mcp.tool(name="converge_development_task",description="Converge exact-head Index, Change Context, Impact, Contract, Affected Tests and fast/full Private CI; never merges, deploys or rolls back.",annotations=_ORCHESTRATION)
    async def converge_development_task(
        development_session_id: str, expected_session_revision: int, mode: str="full", base_sha: str="",
        index_wait_seconds: int=55, wait_seconds: int=55, force_rerun: bool=False,
        supersede_previous: bool=True, include_failure_pack: bool=True, idempotency_key: str="",
    ) -> str:
        try:
            result=await converge.converge_task(
                github_call,service,development_session_id,expected_session_revision,mode,base_sha,
                index_wait_seconds,wait_seconds,force_rerun,supersede_previous,include_failure_pack,idempotency_key,
            )
            return json.dumps(result,ensure_ascii=False)
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
