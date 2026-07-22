"""Worker startup state reconciliation.

On each agent startup, reconciles stale worker state with the Controller.
Handles: stale current_job, worker_lost marking, idle recovery.
"""

import logging
import time
import urllib.request
import urllib.error
import json

logger = logging.getLogger(__name__)


class Reconciler:
    def __init__(self, controller_url: str, worker_id: str, worker_token: str):
        self.controller_url = controller_url.rstrip("/")
        self.worker_id = worker_id
        self.worker_token = worker_token

    def _request(self, method: str, path: str, body: dict = None, timeout: int = 30) -> dict:
        url = f"{self.controller_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.worker_token}")
        req.add_header("X-Worker-ID", self.worker_id)
        req.add_header("Content-Type", "application/json")
        req.add_header("X-Request-ID", f"{self.worker_id}-reconcile-{int(time.time())}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body_text = e.read().decode()[:200]
            logger.warning("Reconcile HTTP %d on %s %s: %s", e.code, method, path, body_text)
            return {"error": f"HTTP {e.code}", "detail": body_text}
        except Exception as e:
            logger.warning("Reconcile request failed %s %s: %s", method, path, e)
            return {"error": str(e)}

    def reconcile(self):
        """Execute safe startup state reconciliation.

        Rules:
        1. current_job empty -> normal
        2. current_job points to terminal state -> clear it
        3. current_job points to running with valid lease -> do not re-execute
        4. current_job points to running with expired lease -> mark worker_lost
        5. Same job must not be executed by two workers
        """
        logger.info("Starting startup state reconciliation for worker %s", self.worker_id)

        terminal_states = {"passed", "failed", "cancelled", "timed_out", "internal_error", "superseded"}

        try:
            result = self._request("POST", "/internal/ci/workers/reconcile", {
                "worker_id": self.worker_id,
                "action": "startup_reconcile",
                "terminal_states": list(terminal_states),
            })

            if result.get("error"):
                logger.warning("Reconcile returned error: %s", result["error"])
                return

            current_job_id = result.get("current_job_id")
            current_job_status = result.get("current_job_status")
            lease_expired = result.get("lease_expired", False)
            action_taken = result.get("action", "none")

            if not current_job_id:
                logger.info("Reconcile: no current_job - worker is idle")
                return

            logger.info(
                "Reconcile: current_job=%s status=%s lease_expired=%s action=%s",
                current_job_id, current_job_status, lease_expired, action_taken
            )

            if action_taken == "cleared":
                logger.info("Reconcile: cleared stale job %s (was %s)", current_job_id, current_job_status)
            elif action_taken == "worker_lost":
                logger.info("Reconcile: marked worker_lost for job %s (lease expired)", current_job_id)
            elif action_taken == "resume":
                logger.info("Reconcile: job %s lease valid, not re-executing", current_job_id)
            else:
                logger.info("Reconcile: no action needed for job %s", current_job_id)

        except Exception as e:
            logger.warning("Reconcile failed (non-fatal): %s", e)

        logger.info("Startup reconciliation complete")
