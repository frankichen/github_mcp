"""P2P deploy plan must be bounded and fail closed before enqueue."""
from pathlib import Path
from types import SimpleNamespace

import yaml
from app import ci_repository_config as policy
from app import deployment_service as deployment


def _policy(monkeypatch):
    path = Path(__file__).parents[1] / "config" / "ci_repositories.yml"
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    monkeypatch.setattr(policy, "_config_cache", cfg)


def test_p2p_only_exact_deployment_scope(monkeypatch):
    _policy(monkeypatch)
    repo = "frankichen/p2p-server"
    assert policy.is_test_deploy_enabled(repo)
    assert not policy.is_self_deploy_enabled(repo)
    assert not policy.is_test_deploy_enabled("frankichen/unknown")
    assert deployment._validate_common(repo, "gongshi-p2p-lab", "p2p-lab", "a" * 40) is None
    assert deployment._validate_common(repo, "gongshi-test", "p2p-lab", "a" * 40) == "ENVIRONMENT_NOT_ALLOWED"
    assert deployment._validate_common(repo, "gongshi-p2p-lab", "fullstack", "a" * 40) == "SCOPE_NOT_ALLOWED"


def test_missing_reviewed_script_prevents_queued_job(monkeypatch):
    _policy(monkeypatch)
    sha = "a" * 40
    monkeypatch.setattr(deployment, "_repo_state", lambda repo, commit: (sha, []))
    monkeypatch.setattr(deployment, "_ci_gate", lambda *args: (None, {"status": "passed"}))
    monkeypatch.setattr(deployment, "_status_snapshot", lambda *args: {})
    class NoScript:
        def get_contents(self, *args, **kwargs):
            raise FileNotFoundError("script not present at exact SHA")
    monkeypatch.setattr(deployment, "_get_gh", lambda: SimpleNamespace(get_repo=lambda *args: NoScript()))
    plan = deployment.plan_test_deployment("frankichen/p2p-server", "gongshi-p2p-lab",
                                           sha, "ci-placeholder", "p2p-lab")
    assert not plan["ready"]
    assert "P2P_DEPLOY_SCRIPT_UNAVAILABLE" in plan["reasons"]
    assert plan["backend_components"] == ["p2p-gateway", "p2p-signaling"]
    assert plan["frontend_components"] == []


def test_p2p_rollback_cannot_call_sxt_rollback():
    result = deployment.rollback_test_deployment(
        "frankichen/p2p-server", "gongshi-p2p-lab", "any", "", confirm=True)
    assert result["error"]["code"] == "P2P_LAB_ROLLBACK_NOT_IMPLEMENTED"
