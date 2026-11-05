from pathlib import Path

import yaml

from app import ci_repository_config, mygithub12


SERVICE_ROOT = Path(__file__).parents[1]
CONFIG_PATH = SERVICE_ROOT / "config" / "ci_repositories.yml"


class _TreeEntry:
    type = "blob"

    def __init__(self, path: str):
        self.path = path


class _PlanService:
    pass


def _sxt_policy(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))["repositories"]["frankichen/sxt"]


def test_sxt_repo_auto_check_ignores_nested_node_manifests(monkeypatch):
    identity = {
        "repository": "frankichen/sxt",
        "commit_sha": "a" * 40,
        "tree_sha": "b" * 40,
    }
    paths = [
        "go.mod",
        "h5/lenshub-console-react/package.json",
        "h5/lenshub-console-react/package-lock.json",
        "h5/lenshub-storage-mall/package.json",
        "h5/lenshub-storage-mall/package-lock.json",
    ]
    monkeypatch.setattr(mygithub12, "_recursive_tree", lambda *args, **kwargs: (identity, [_TreeEntry(path) for path in paths]))
    monkeypatch.setattr(ci_repository_config, "CONFIG_PATH", str(CONFIG_PATH))
    monkeypatch.setattr(ci_repository_config, "_config_cache", None)

    result = mygithub12.plan_private_ci_job(_PlanService(), "frankichen/sxt", "a" * 40, "repo-auto-check")

    assert result["applicable"] is True
    assert result["allowed_profiles"] == ["repo-auto-check", "repo-fast-check", "go-check"]
    assert result["detected_stacks"] == ["go"]
    assert result["selected_profiles"] == ["go-check"]
    assert result["workspaces"] == [{"path": ".", "stack": "go"}]


def test_sxt_fallback_policy_is_go_only(monkeypatch, tmp_path):
    monkeypatch.setattr(ci_repository_config, "CONFIG_PATH", str(tmp_path / "missing.yml"))
    monkeypatch.setattr(ci_repository_config, "_config_cache", None)

    fallback = ci_repository_config.get_repository_config("frankichen/sxt")

    assert fallback["allowed_profiles"] == ["repo-auto-check", "repo-fast-check", "go-check"]
    assert fallback["workspaces"] == _sxt_policy(CONFIG_PATH)["workspaces"]
    assert fallback["merge_policy"] == _sxt_policy(CONFIG_PATH)["merge_policy"]
