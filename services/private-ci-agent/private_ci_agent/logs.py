"""Log upload and management."""

import logging
import time
import uuid
import threading
from typing import Optional

logger = logging.getLogger(__name__)


class LogManager:
    def __init__(self, controller_client, max_log_bytes: int = 10485760):
        self.client = controller_client
        self.max_log_bytes = max_log_bytes
        self._total_uploaded = {}
        self._truncated = {}
        self._buffers = {}
        self._buffer_bytes = {}
        self._last_flush = {}
        self.max_batch_bytes = int(__import__("os").environ.get("CI_LOG_BATCH_MAX_BYTES", "32768"))
        self.max_batch_interval = float(__import__("os").environ.get("CI_LOG_BATCH_MAX_INTERVAL_SECONDS", "1"))
        self._lock = threading.RLock()

    def upload(self, job_id: str, content: str):
        """Upload log content in chunks."""
        if not content:
            return
        with self._lock:
            self._upload_locked(job_id, content)

    def _upload_locked(self, job_id: str, content: str):

        current = self._total_uploaded.get(job_id, 0) + self._buffer_bytes.get(job_id, 0)

        if current >= self.max_log_bytes:
            self._truncated[job_id] = True
            return

        remaining = self.max_log_bytes - current
        chunk = content[:remaining]
        if len(chunk) < len(content):
            chunk += f"\n[LOG TRUNCATED at {self.max_log_bytes} bytes]\n"
            self._truncated[job_id] = True

        self._buffers.setdefault(job_id, []).append(chunk)
        self._buffer_bytes[job_id] = self._buffer_bytes.get(job_id, 0) + len(chunk)
        self._last_flush.setdefault(job_id, time.monotonic())
        if self._buffer_bytes[job_id] >= self.max_batch_bytes or time.monotonic() - self._last_flush[job_id] >= self.max_batch_interval:
            self.flush(job_id)

    def flush(self, job_id: str, force: bool = True):
        with self._lock:
            chunks = self._buffers.get(job_id) or []
            if not chunks:
                return True
            content = "".join(chunks)
            batch_id = uuid.uuid4().hex
            if self.client.upload_log_batch(job_id, batch_id, content):
                self._total_uploaded[job_id] = self._total_uploaded.get(job_id, 0) + len(content)
                self._buffers[job_id] = []
                self._buffer_bytes[job_id] = 0
                self._last_flush[job_id] = time.monotonic()
                return True
            return False

    def get_total(self, job_id: str) -> int:
        return self._total_uploaded.get(job_id, 0)

    def is_truncated(self, job_id: str) -> bool:
        return self._truncated.get(job_id, False)

    def reset(self, job_id: str):
        self._total_uploaded.pop(job_id, None)
        self._truncated.pop(job_id, None)
        self._buffers.pop(job_id, None)
        self._buffer_bytes.pop(job_id, None)
        self._last_flush.pop(job_id, None)
