"""Authenticated callbacks from the WSL deployment executor."""

import hashlib
import hmac
import logging

from fastapi import APIRouter, HTTPException, Request

from app.config import settings
from app.deployment_service import append_deployment_log_batch, complete_test_deployment, fail_test_deployment, list_delegated_deployments, update_test_deployment_progress

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/internal/deployments", tags=["Deployments"])


async def _authorize(request: Request) -> None:
    configured = settings.DEPLOY_CALLBACK_API_KEY.get_secret_value()
    supplied = request.headers.get("X-Deployment-Callback-Key", "")
    if not configured:
        raise HTTPException(status_code=503, detail={"error": "deployment_callback_not_configured"})
    if not supplied or not hmac.compare_digest(supplied, configured):
        raise HTTPException(status_code=401, detail={"error": "deployment_callback_unauthorized"})


@router.get("/assigned")
async def deployment_assigned(request: Request):
    await _authorize(request)
    return {"ok": True, "items": list_delegated_deployments()}


@router.post("/{deployment_id}/progress")
async def deployment_progress(deployment_id: str, request: Request):
    await _authorize(request)
    body = await request.json()
    result = update_test_deployment_progress(
        deployment_id,
        str(body.get("current_step") or "running"),
        str(body.get("message") or ""),
        body.get("status"),
        body.get("release") if isinstance(body.get("release"), dict) else None,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error"))
    logger.info("Deployment progress callback deployment_id=%s step=%s idempotent=%s", deployment_id, body.get("current_step"), result.get("idempotent"))
    return result


@router.post("/{deployment_id}/logs/batch")
async def deployment_log_batch(deployment_id: str, request: Request):
    await _authorize(request)
    body = await request.json()
    result = append_deployment_log_batch(deployment_id, str(body.get("batch_id") or ""), str(body.get("content") or ""))
    if not result.get("ok"):
        raise HTTPException(status_code=409 if result.get("error", {}).get("code") == "INVALID_LOG_BATCH" else 404, detail=result.get("error"))
    return result


@router.post("/{deployment_id}/complete")
async def deployment_complete(deployment_id: str, request: Request):
    await _authorize(request)
    body = await request.json()
    result = complete_test_deployment(
        deployment_id,
        int(body.get("exit_code", 0)),
        str(body.get("message") or "deployment completed"),
        body.get("release") if isinstance(body.get("release"), dict) else None,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error"))
    logger.info("Deployment complete callback deployment_id=%s idempotent=%s", deployment_id, result.get("idempotent"))
    return result


@router.post("/{deployment_id}/fail")
async def deployment_fail(deployment_id: str, request: Request):
    await _authorize(request)
    body = await request.json()
    result = fail_test_deployment(
        deployment_id,
        int(body.get("exit_code", 1)),
        str(body.get("error_code") or "DEPLOYMENT_FAILED"),
        str(body.get("error_message") or "deployment failed"),
    )
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error"))
    logger.info("Deployment fail callback deployment_id=%s idempotent=%s", deployment_id, result.get("idempotent"))
    return result
