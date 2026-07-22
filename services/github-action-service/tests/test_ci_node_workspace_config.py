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
                    "allowed_profiles": ["repo-auto-check", "go-check", "node-check"],
                    "workspaces": [{"path": ".", "type": "auto"}, {"path": "h5/lenshub-console", "type": "node"}],
                }
            }
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "CONFIG_PATH", str(path))
    config.reload_config()
    assert config.get_allowed_profiles("frankichen/sxt") == ["repo-auto-check", "go-check", "node-check"]
    assert config.is_profile_allowed("frankichen/sxt", "node-check")
    assert not config.is_profile_allowed("frankichen/sxt", "python-check")
