from pathlib import Path

import yaml

from app import ci_repository_config as config


def test_sxt_profile_allowlist_and_workspaces(tmp_path, monkeypatch):
    path = tmp_path / "ci_repositories.yml"
    path.write_text(
        yaml.safe_dump({
            "repositories": {
                "frankichen/sxt": {
                    "enabled": True,
                    "auto_detect": True,
                    "allowed_profiles": ["repo-auto-check", "go-check", "node-check", "openapi-check"],
                    "workspaces": [{"path": ".", "type": "auto"}, {"path": "h5/lenshub-console", "type": "node"}],
                }
            }
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "CONFIG_PATH", str(path))
    config.reload_config()
    assert config.get_allowed_profiles("frankichen/sxt") == ["repo-auto-check", "go-check", "node-check", "openapi-check"]
    assert config.is_profile_allowed("frankichen/sxt", "node-check")
    assert config.is_profile_allowed("frankichen/sxt", "openapi-check")
    assert not config.is_profile_allowed("frankichen/sxt", "python-check")
    assert not config.is_profile_allowed("frankichen/other", "openapi-check")


def test_checked_in_sxt_config_keeps_runtime_services_and_hooks_explicit():
    path = Path(__file__).parents[1] / "config" / "ci_repositories.yml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    root = next(
        item
        for item in data["repositories"]["frankichen/sxt"]["workspaces"]
        if item["path"] == "."
    )

    assert root["services"] == ["postgres", "redis", "rabbitmq"]
    assert root["hooks"] == ["go-migrate", "ai-integrity"]
