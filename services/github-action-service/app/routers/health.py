import asyncio
import os
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from app.auth import verify_api_key
from app.config import settings
from app.github_client import GitHubClient
from app.observability import prometheus_metrics
from app.version import SERVICE_NAME, SERVICE_VERSION

router = APIRouter()

_client = GitHubClient()


@router.get("/health")
async def health():
    return {
        "status": "ok",
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "github_configured": _client.configured,
    }


def _sqlite_readable(path: str) -> bool:
    database = Path(path)
    if not database.is_file():
        return False
    try:
        db = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=2)
        db.execute("SELECT 1").fetchone()
        db.close()
        return True
    except sqlite3.Error:
        return False


@router.get("/ready")
async def readiness():
    paths = {
        "idempotency_db": settings.IDEMPOTENCY_DB_PATH,
        "ci_db": os.environ.get("CI_DB_PATH", "/data/ci.db"),
        "deployment_db": os.environ.get("DEPLOYMENT_DB_PATH", "/data/deployments.db"),
    }
    results = await asyncio.gather(
        *(asyncio.to_thread(_sqlite_readable, path) for path in paths.values())
    )
    checks = {"github_configured": _client.configured}
    checks.update(dict(zip(paths, results)))
    ready = all(checks.values())
    payload = {
        "status": "ready" if ready else "not_ready",
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "checks": checks,
    }
    return payload if ready else JSONResponse(payload, status_code=503)


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics(request: Request):
    verify_api_key(request)
    return prometheus_metrics()
