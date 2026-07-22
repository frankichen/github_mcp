import hmac
import hashlib
import os
from fastapi import Request, HTTPException
from app.config import settings


def verify_api_key(request: Request) -> None:
    endpoint = request.url.path
    public_endpoints = {
        "/health",
        "/actions-openapi.json",
        "/privacy",
        "/docs",
        "/openapi.json",
    }
    if endpoint in public_endpoints or endpoint.startswith("/docs") or endpoint.startswith("/openapi.json"):
        return

    key = request.headers.get("Authorization", "")
    if not key.startswith("Bearer "):
        raise HTTPException(status_code=401, detail={
            "error": "unauthorized",
            "message": "Invalid or missing API key",
        })

    token = key[7:]
    if not _constant_time_compare(token, settings.ACTION_API_KEY.get_secret_value()):
        raise HTTPException(status_code=401, detail={
            "error": "unauthorized",
            "message": "Invalid or missing API key",
        })


def _constant_time_compare(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
