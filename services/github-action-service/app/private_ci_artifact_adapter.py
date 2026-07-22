"""Bridge from the deploy agent to the fixed artifact-only executor."""

from __future__ import annotations

import os
from pathlib import Path


def execute_verified_artifact(row, artifact: dict) -> dict:
    from importlib.util import module_from_spec, spec_from_file_location
    script = Path(__file__).resolve().parents[2] / "private-ci-deploy-executor" / "scripts" / "artifact_deployment.py"
    spec = spec_from_file_location("private_artifact_deployment", script)
    if not spec or not spec.loader: return {"ok": False, "error_code": "ARTIFACT_EXECUTOR_NOT_CONFIGURED"}
    module = module_from_spec(spec); spec.loader.exec_module(module)
    incoming = Path(os.environ.get("DEPLOY_ARTIFACT_INCOMING_ROOT", "/srv/private-ci/deploy-incoming")) / row["deployment_id"]
    current = Path(os.environ.get("DEPLOY_CURRENT_LINK", "/srv/private-ci/current"))
    def unavailable():
        return False
    return module.deploy_artifact(artifact["storage_dir"], incoming, current, migration_required=artifact["migration_required"], migration_runner=unavailable, healthcheck=unavailable, restart_services=unavailable)
