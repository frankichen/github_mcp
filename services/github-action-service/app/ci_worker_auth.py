"""CI worker authentication module."""

import hmac
import hashlib
import logging
from fastapi import Request, HTTPException

from app.ci_database import verify_worker_token

logger = logging.getLogger(__name__)


async def verify_ci_worker(request: Request) -> str:
    """Verify CI Worker token and return worker_id."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail={"error": "unauthorized", "message": "Missing CI Worker token"})

    token = auth_header[7:]
    if not token:
        raise HTTPException(status_code=401, detail={"error": "unauthorized", "message": "Empty CI Worker token"})

    worker_id = request.headers.get("X-Worker-ID", "")
    if not worker_id:
        raise HTTPException(status_code=400, detail={"error": "bad_request", "message": "Missing X-Worker-ID header"})

    if not verify_worker_token(worker_id, token):
        logger.warning(
            "CI worker auth failed: worker_id=%s request_id=%s",
            worker_id,
            request.headers.get("X-Request-ID", "unknown"),
        )
        raise HTTPException(status_code=403, detail={"error": "forbidden", "message": "Invalid CI Worker token"})

    return worker_id
