"""HTTP client for communicating with the German CI Controller."""

import logging
import json
import time
import urllib.request
import urllib.error
from typing import Optional

logger = logging.getLogger(__name__)


class ControllerClient:
    def __init__(self, base_url: str, worker_id: str, worker_token: str):
        self.base_url = base_url.rstrip("/")
        self.worker_id = worker_id
        self.worker_token = worker_token
        self._job_leases: dict[str, str] = {}

    def _request(
        self, method: str, path: str, body: Optional[dict] = None, timeout: int = 30,
        job_id: Optional[str] = None,
    ) -> dict:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body is not None else None

        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.worker_token}")
        req.add_header("X-Worker-ID", self.worker_id)
        req.add_header("Content-Type", "application/json")
        req.add_header("X-Request-ID", f"{self.worker_id}-{int(time.time())}")
        if job_id:
            lease_token = self._job_leases.get(job_id)
            if lease_token:
                req.add_header("X-CI-Lease-Token", lease_token)

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body_text = e.read().decode()[:200]
            logger.error("HTTP %d on %s %s: %s", e.code, method, path, body_text)
            raise
        except Exception as e:
            logger.error("Request failed %s %s: %s", method, path, e)
            raise

    def register(self, profiles: list[str], max_concurrent: int) -> bool:
        try:
            self._request("POST", "/internal/ci/workers/register", {
                "worker_id": self.worker_id,
                "token": self.worker_token,
                "profiles": profiles,
                "max_concurrent": max_concurrent,
            })
            return True
        except Exception:
            return False

    def heartbeat(self, current_job_id: Optional[str] = None, lease_token: Optional[str] = None) -> dict:
        body = {"current_job_id": current_job_id} if current_job_id else {}
        if lease_token:
            body["lease_token"] = lease_token
        return self._request("POST", "/internal/ci/workers/heartbeat", body)

    def lease_job(self) -> Optional[dict]:
        try:
            result = self._request("POST", "/internal/ci/jobs/lease", {})
            job_id = result.get("job_id")
            lease_token = result.get("lease_token")
            if job_id:
                if lease_token:
                    self._job_leases[job_id] = lease_token
                return result
            return None
        except Exception:
            return None

    def upload_log(self, job_id: str, content: str) -> bool:
        try:
            self._request("POST", f"/internal/ci/jobs/{job_id}/logs", {"content": content}, job_id=job_id)
            return True
        except Exception:
            return False

    def upload_log_batch(self, job_id: str, batch_id: str, content: str) -> bool:
        try:
            self._request("POST", f"/internal/ci/jobs/{job_id}/logs/batch", {"batch_id": batch_id, "content": content}, job_id=job_id)
            return True
        except Exception as e:
            logger.warning("Failed to upload log batch: %s", e)
            return False

    def start_step(self, job_id: str, step_name: str) -> Optional[int]:
        try:
            result = self._request("POST", f"/internal/ci/jobs/{job_id}/steps", {
                "step_name": step_name,
                "action": "start",
            }, job_id=job_id)
            return result.get("step_id")
        except Exception:
            return None

    def finish_step(self, job_id: str, step_id: int, status: str, exit_code: Optional[int] = None, log_end_offset: Optional[int] = None):
        try:
            self._request("POST", f"/internal/ci/jobs/{job_id}/steps", {
                "step_id": step_id,
                "step_name": "",
                "action": "finish",
                "status": status,
                "exit_code": exit_code,
                "log_end_offset": log_end_offset,
            }, job_id=job_id)
        except Exception as e:
            logger.warning("Failed to finish step: %s", e)

    def update_job_status(self, job_id: str, status: str):
        try:
            self._request("POST", f"/internal/ci/jobs/{job_id}/steps", {
                "action": "update_status",
                "job_status": status,
                "step_name": "",
            }, job_id=job_id)
        except Exception as e:
            logger.warning("Failed to update job status: %s", e)

    def finish_job(self, job_id: str, exit_code: int, status: str, summary: Optional[dict] = None, error_code: Optional[str] = None, error_message: Optional[str] = None):
        try:
            self._request("POST", f"/internal/ci/jobs/{job_id}/finish", {
                "exit_code": exit_code,
                "status": status,
                "summary": summary,
                "error_code": error_code,
                "error_message": error_message,
            }, job_id=job_id)
            self._job_leases.pop(job_id, None)
        except Exception as e:
            logger.error("Failed to finish job: %s", e)

    def release_job(self, job_id: str):
        try:
            self._request("POST", f"/internal/ci/jobs/{job_id}/release", {}, job_id=job_id)
            self._job_leases.pop(job_id, None)
        except Exception as e:
            logger.warning("Failed to release job: %s", e)
