"""Compact, non-lossy-on-primary-error CI failure evidence for DX sessions."""
from __future__ import annotations

from typing import Any

from app.mcp_response import store_response_resource


def build_failure_pack(job: dict[str, Any], *, affected: dict[str, Any] | None = None, log_tail: str = "") -> dict[str, Any]:
    summary=job.get("summary") if isinstance(job.get("summary"),dict) else {}
    steps=summary.get("steps") if isinstance(summary.get("steps"),list) else []
    failed=[s for s in steps if isinstance(s,dict) and s.get("status") in {"failed","timed_out","configuration_error","blocked_by_setup"}]
    payload={
        "job_id":job.get("job_id"),"repository":job.get("repository"),"branch":job.get("branch"),
        "commit_sha":job.get("commit_sha"),"profile":job.get("profile"),"status":job.get("status"),
        "exit_code":job.get("exit_code"),"error_code":job.get("error_code"),"error_message":job.get("error_message"),
        "failed_steps":failed[:20],"changed_files":job.get("changed_files",[])[:100],
        "affected":affected or {},"environment_cache":summary.get("environment_cache",{}),
        "log_tail":log_tail[-12000:] if log_tail else "",
    }
    resource=store_response_resource(payload)
    return {
        "summary":{
            "job_id":job.get("job_id"),"status":job.get("status"),"exit_code":job.get("exit_code"),
            "error_code":job.get("error_code"),"failed_steps":[{"step_name":s.get("step_name"),"status":s.get("status"),"exit_code":s.get("exit_code")} for s in failed[:5]],
        },
        "resource_uri":resource["resource_uri"],"content_sha256":resource["sha256"],"total_bytes":resource["total_bytes"],
    }
