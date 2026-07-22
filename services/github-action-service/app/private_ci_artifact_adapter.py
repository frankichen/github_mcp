"""Bridge from the deploy agent to the fixed artifact-only executor."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def execute_verified_artifact(row, artifact: dict) -> dict:
    from importlib.util import module_from_spec, spec_from_file_location
    script = Path(__file__).resolve().parents[2] / "private-ci-deploy-executor" / "scripts" / "artifact_deployment.py"
    spec = spec_from_file_location("private_artifact_deployment", script)
    if not spec or not spec.loader: return {"ok": False, "error_code": "ARTIFACT_EXECUTOR_NOT_CONFIGURED"}
    module = module_from_spec(spec); spec.loader.exec_module(module)
    incoming = Path(os.environ.get("DEPLOY_ARTIFACT_INCOMING_ROOT", "/srv/private-ci/deploy-incoming")) / row["deployment_id"]
    current = Path(os.environ.get("DEPLOY_CURRENT_LINK", "/srv/private-ci/current"))
    migration_script = Path(__file__).resolve().parents[2] / "private-ci-deploy-executor" / "scripts" / "migrate_with_timeout.sh"
    def run_migration(release_dir):
        if not migration_script.is_file(): return False
        result = subprocess.run(["bash", str(migration_script), "--", "make", "migrate-up"], cwd=release_dir, capture_output=True, text=True, timeout=660)
        return result.returncode == 0
    def restart_services():
        services = ("lenshub-api", "lenshub-worker", "lenshub-scheduler", "lenshub-web")
        result = subprocess.run(["systemctl", "restart", *services], capture_output=True, text=True, timeout=60)
        return result.returncode == 0
    def healthcheck(release_dir):
        release_dir = release_dir.resolve() if release_dir.is_symlink() else release_dir
        script = release_dir / "scripts" / "healthcheck_gongshi_test.sh"
        if not script.is_file(): return False
        result = subprocess.run(["bash", str(script), "--expected-release", release_dir.name], capture_output=True, text=True, timeout=120)
        return result.returncode == 0
    return module.deploy_artifact(artifact["storage_dir"], incoming, current, migration_required=artifact["migration_required"], migration_runner=run_migration, healthcheck=healthcheck, restart_services=restart_services)
