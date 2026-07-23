"""Bridge from the deploy agent to the fixed artifact-only executor."""

from __future__ import annotations

import os
import importlib


def execute_verified_artifact(row, artifact: dict) -> dict:
    """Submit to the isolated executor; never execute deployment commands here."""
    module_name = os.environ.get("ARTIFACT_EXECUTOR_TRANSPORT", "").strip()
    if not module_name:
        return {"ok": False, "error_code": "ARTIFACT_EXECUTOR_NOT_CONFIGURED"}
    try:
        transport = importlib.import_module(module_name)
        submit = getattr(transport, "submit_artifact_deployment")
    except (ImportError, AttributeError):
        return {"ok": False, "error_code": "ARTIFACT_EXECUTOR_NOT_CONFIGURED"}
    return submit({
        "deployment_id": row["deployment_id"],
        "artifact_id": artifact["artifact_id"],
        "expected_current_release_id": row.get("current_release_before") if hasattr(row, "get") else row["current_release_before"],
    })
