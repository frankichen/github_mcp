"""Authenticated callbacks for the fixed MyGithut12 infrastructure executor."""

from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, HTTPException, Request

from app.config import settings
from app.infrastructure_deployment_service import (
    claim_infrastructure_deployment,
    complete_infrastructure_deployment,
    fail_infrastructure_deployment,
    register_infrastructure_executor_heartbeat,
    update_infrastructure_deployment_progress,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/internal/infrastructure-deployments", tags=["Infrastructure Deployments"])


async def _authorize(request: Request) -> None:
    configured = settings.INFRASTRUCTURE_DEPLOY_CALLBACK_API_KEY.get_secret_value()
    supplied = request.headers.get("X-Infrastructure-Deployment-Callback-Key", "")
    if not configured:
        raise HTTPException(
            status_code=503,
            detail={"error": "infrastructure_deployment_callback_not_configured"},
        )
    if not supplied or not hmac.compare_digest(supplied, configured):
        raise HTTPException(
            status_code=401,
            detail={"error": "infrastructure_deployment_callback_unauthorized"},
        )


@router.post("/heartbeat")
async def infrastructure_deployment_heartbeat(request: Request):
    await _authorize(request)
    body = await request.json()
    result = register_infrastructure_executor_heartbeat(
        str(body.get("executor_id") or ""),
        str(body.get("state") or "idle"),
        str(body.get("current_deployment_id") or ""),
    )
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error"))
    return result


@router.post("/claim")
async def infrastructure_deployment_claim(request: Request):
    await _authorize(request)
    body = await request.json()
    result = claim_infrastructure_deployment(str(body.get("executor_id") or ""))
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error"))
    return result


@router.post("/{deployment_id}/progress")
async def infrastructure_deployment_progress(deployment_id: str, request: Request):
    await _authorize(request)
    body = await request.json()
    result = update_infrastructure_deployment_progress(
        deployment_id,
        str(body.get("current_step") or "running"),
        str(body.get("message") or ""),
    )
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error"))
    logger.info(
        "Infrastructure deployment progress deployment_id=%s step=%s idempotent=%s",
        deployment_id,
        body.get("current_step"),
        result.get("idempotent"),
    )
    return result


@router.post("/{deployment_id}/complete")
async def infrastructure_deployment_complete(deployment_id: str, request: Request):
    await _authorize(request)
    body = await request.json()
    result = complete_infrastructure_deployment(
        deployment_id,
        int(body.get("exit_code", 0)),
        body.get("controller_healthy") is True,
        body.get("private_ci_agent_healthy") is True,
        str(body.get("message") or "infrastructure deployment completed"),
    )
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error"))
    return result


@router.post("/{deployment_id}/fail")
async def infrastructure_deployment_fail(deployment_id: str, request: Request):
    await _authorize(request)
    body = await request.json()
    result = fail_infrastructure_deployment(
        deployment_id,
        int(body.get("exit_code", 1)),
        str(body.get("error_code") or "INFRASTRUCTURE_DEPLOYMENT_FAILED"),
        str(body.get("error_message") or "infrastructure deployment failed"),
    )
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error"))
    return result
