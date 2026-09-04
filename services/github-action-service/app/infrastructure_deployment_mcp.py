"""Register the fixed-contract MyGithut12 infrastructure deployment MCP tools."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Awaitable, Callable

from mcp.types import ToolAnnotations

from app import infrastructure_deployment_service as infrastructure

logger = logging.getLogger(__name__)
_READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
_CONSEQUENTIAL = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=True,
)


def _error(exc: Exception) -> str:
    logger.exception("Infrastructure deployment MCP operation failed")
    return json.dumps(
        {
            "ok": False,
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "infrastructure deployment operation failed",
                "details": {"type": type(exc).__name__},
                "trace_id": str(uuid.uuid4()),
            },
        },
        ensure_ascii=False,
    )


async def _call(
    github_call: Callable[..., Awaitable[Any]],
    function: Callable[..., Any],
    *args: Any,
) -> str:
    try:
        return json.dumps(await github_call(function, *args), ensure_ascii=False)
    except Exception as exc:
        return _error(exc)


def register_infrastructure_deployment_tools(mcp, github_call) -> None:
    @mcp.tool(
        name="plan_infrastructure_deployment",
        description=(
            "Plan the fixed MyGithut12 production control-plane deployment. "
            "Requires exact current main, exact repo-auto-check evidence, current-build CAS, "
            "and a fresh fixed executor heartbeat. A failed post-switch deployment may be supplied "
            "as recovery_of_deployment_id for same-SHA health/preheat recovery. Never accepts host, "
            "shell, script, rollback, or failure-mode input."
        ),
        annotations=_READ_ONLY,
    )
    async def plan_infrastructure_deployment(
        repository: str,
        environment: str,
        commit_sha: str,
        private_ci_job_id: str,
        expected_current_build_sha: str,
        scope: str = infrastructure.SCOPE,
        recovery_of_deployment_id: str = "",
    ) -> str:
        return await _call(
            github_call,
            infrastructure.plan_infrastructure_deployment,
            repository,
            environment,
            commit_sha,
            private_ci_job_id,
            expected_current_build_sha,
            scope,
            recovery_of_deployment_id,
        )

    @mcp.tool(
        name="start_infrastructure_deployment",
        description=(
            "Queue the fixed MyGithut12 production control-plane deployment after all plan gates. "
            "Requires confirm=true. A recovery_of_deployment_id is accepted only for a failed, exact-target, "
            "post-switch deployment while the current runtime already matches that target. The executor contract "
            "is always fail-stop and never auto-rolls back."
        ),
        annotations=_CONSEQUENTIAL,
    )
    async def start_infrastructure_deployment(
        repository: str,
        environment: str,
        commit_sha: str,
        private_ci_job_id: str,
        expected_current_build_sha: str,
        scope: str = infrastructure.SCOPE,
        confirm: bool = False,
        recovery_of_deployment_id: str = "",
    ) -> str:
        return await _call(
            github_call,
            infrastructure.start_infrastructure_deployment,
            repository,
            environment,
            commit_sha,
            private_ci_job_id,
            expected_current_build_sha,
            scope,
            confirm,
            "mcp",
            recovery_of_deployment_id,
        )

    @mcp.tool(
        name="get_infrastructure_deployment",
        description=(
            "Get one MyGithut12 infrastructure deployment. The legacy deployment_id-only call stays compact; "
            "optional wait fields long-poll durable status/step/revision for at most 55 seconds, and an explicit "
            "include_log_tail opt-in returns only a bounded redacted tail."
        ),
        annotations=_READ_ONLY,
    )
    async def get_infrastructure_deployment(
        deployment_id: str,
        wait_seconds: int = 0,
        last_known_revision: int = 0,
        last_known_status: str = "",
        last_known_step: str = "",
        include_log_tail: bool = False,
        log_tail_lines: int = infrastructure.DEFAULT_LOG_TAIL_LINES,
    ) -> str:
        return await _call(
            github_call,
            infrastructure.get_infrastructure_deployment,
            deployment_id,
            wait_seconds,
            last_known_revision,
            last_known_status,
            last_known_step,
            include_log_tail,
            log_tail_lines,
        )
