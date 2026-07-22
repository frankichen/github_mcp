import hashlib
import json
import logging
import time
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings

logger = logging.getLogger(__name__)

_RESERVED_CHARS = frozenset({0x00, 0x0A, 0x0D})

_IDEMPOTENCY_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_MAX_KEY_LENGTH = 256


def _validate_idempotency_key(key: str) -> bool:
    if not key or len(key) > _MAX_KEY_LENGTH:
        return False
    for ch in key:
        if ord(ch) in _RESERVED_CHARS:
            return False
    return True


class IdempotencyMiddleware(BaseHTTPMiddleware):
    def __init__(self, app):
        super().__init__(app)
        self._db = None

    async def _get_db(self):
        if self._db is None:
            import aiosqlite
            self._db = await aiosqlite.connect(settings.IDEMPOTENCY_DB_PATH)
            self._db.row_factory = aiosqlite.Row
            await self._db.execute(
                """CREATE TABLE IF NOT EXISTS idempotency (
                    key TEXT PRIMARY KEY,
                    request_hash TEXT NOT NULL,
                    status_code INTEGER NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                )"""
            )
            await self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_idempotency_created_at ON idempotency(created_at)"
            )
            await self._db.commit()
        return self._db

    async def dispatch(self, request: Request, call_next):
        if request.method not in _IDEMPOTENCY_METHODS:
            return await call_next(request)

        idempotency_key = request.headers.get("Idempotency-Key", "")
        if not idempotency_key:
            return await call_next(request)

        if not _validate_idempotency_key(idempotency_key):
            return Response(
                content=json.dumps({"error": "invalid_idempotency_key", "message": "Invalid Idempotency-Key header"}),
                status_code=400,
                media_type="application/json",
            )

        body_bytes = await request.body()
        request_hash = hashlib.sha256(
            f"{request.method}:{request.url.path}:{body_bytes.decode('utf-8', errors='replace')}".encode()
        ).hexdigest()

        db = await self._get_db()

        async with db.execute(
            "SELECT status_code, response_json, request_hash FROM idempotency WHERE key = ?",
            (idempotency_key,),
        ) as cursor:
            row = await cursor.fetchone()

        if row is not None:
            if row["request_hash"] != request_hash:
                return Response(
                    content=json.dumps({"error": "idempotency_key_reuse", "message": "Idempotency-Key reused with different request content"}),
                    status_code=422,
                    media_type="application/json",
                )
            return Response(
                content=row["response_json"],
                status_code=row["status_code"],
                media_type="application/json",
            )

        async def receive() -> dict:
            return {"type": "http.request", "body": body_bytes, "more_body": False}

        request._receive = receive

        response = await call_next(request)

        if 200 <= response.status_code < 600:
            response_body = b""
            async for chunk in response.body_iterator:
                response_body += chunk

            response_json = response_body.decode("utf-8", errors="replace")

            now = time.time()
            await db.execute(
                "INSERT INTO idempotency (key, request_hash, status_code, response_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (idempotency_key, request_hash, response.status_code, response_json, now),
            )
            await db.commit()

            return Response(
                content=response_body,
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type=response.media_type,
            )

        return response

    async def cleanup(self):
        db = await self._get_db()
        cutoff = time.time() - (settings.IDEMPOTENCY_TTL_HOURS * 3600)
        await db.execute("DELETE FROM idempotency WHERE created_at < ?", (cutoff,))
        await db.commit()
