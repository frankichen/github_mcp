import json
import os
import shlex
import subprocess

import pytest

from private_ci_agent.profiles import (
    NODE_CHROMIUM_IMAGE,
    NODE_IMAGE,
    discover_workspaces,
    go_commands_for_workspace,
    go_image_for_version,
    go_version_requirements,
    node_browser_image,
    node_commands_for_workspace,
    select_node_scripts,
)


def write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def node_package(path, scripts=None, deps=None):
    path.mkdir(parents=True, exist_ok=True)
    write_json(path / "package.json", {"scripts": scripts or {}, "dependencies": deps or {"vue": "^3.0.0"}})


def test_root_package_json_is_node(tmp_path):
    node_package(tmp_path)
    result = discover_workspaces(str(tmp_path))
    assert any(w["path"] == "." and w["stack"] == "node" for w in result["workspaces"])


def test_h5_workspace_is_found_with_vue_and_npm(tmp_path):
    node_package(tmp_path / "h5" / "lenshub-console")
    (tmp_path / "h5" / "lenshub-console" / "package-lock.json").write_text("{}")
    result = discover_workspaces(str(tmp_path))
    workspace = next(w for w in result["workspaces"] if w["path"] == "h5/lenshub-console")
    assert workspace["stack"] == "node"
    assert workspace["framework"] == "vue"
    assert workspace["package_manager"] == "npm"


def test_depth_limit_discovers_supported_path(tmp_path):
    node_package(tmp_path / "h5" / "lenshub-console")
    (tmp_path / "h5" / "lenshub-console" / "package-lock.json").write_text("{}")
    assert any(w["path"] == "h5/lenshub-console" for w in discover_workspaces(str(tmp_path))["workspaces"])


@pytest.mark.parametrize("directory", ["node_modules", "dist", "coverage", "docs", "examples"])
def test_excluded_directories_are_not_scanned(tmp_path, directory):
    node_package(tmp_path / directory / "fake")
    assert not discover_workspaces(str(tmp_path))["workspaces"]


def test_python_file_alone_does_not_trigger_python(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "gen_i18n.py").write_text("print('x')")
    assert "python" not in discover_workspaces(str(tmp_path))["detected_stacks"]


def test_python_manifest_triggers_python(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    assert "python" in discover_workspaces(str(tmp_path))["detected_stacks"]


def test_go_and_node_workspaces_are_combined(tmp_path):
    (tmp_path / "go.mod").write_text("module example\n")
    node_package(tmp_path / "client", deps={"vite": "^6.0.0"})
    (tmp_path / "client" / "package-lock.json").write_text("{}")
    result = discover_workspaces(str(tmp_path))
    assert "go" in result["detected_stacks"] and "node" in result["detected_stacks"]


def test_multiple_lock_files_are_configuration_error(tmp_path):
    node_package(tmp_path)
    for name in ("package-lock.json", "pnpm-lock.yaml"):
        (tmp_path / name).write_text("{}")
    workspace = discover_workspaces(str(tmp_path))["workspaces"][0]
    assert "multiple lock files" in workspace["configuration_error"]


def test_no_manifest_is_unsupported(tmp_path):
    result = discover_workspaces(str(tmp_path))
    assert result["workspaces"] == []
    assert result["detected_stacks"] == []


def test_node_script_priority_and_safe_install(tmp_path):
    node_package(tmp_path, {"test": "vitest run", "test:ci": "vitest run", "test:run": "vitest run", "dev": "vite"})
    (tmp_path / "package-lock.json").write_text("{}")
    workspace = discover_workspaces(str(tmp_path))["workspaces"][0]
    selected, skipped = select_node_scripts(workspace, ["test:run"])
    assert [item["name"] for item in selected] == ["test:run"]
    assert node_commands_for_workspace(workspace, ["test:run"])["setup"] == ["npm ci"]
    assert "dev" not in [item["name"] for item in selected + skipped]


def test_watch_script_is_not_executed(tmp_path):
    node_package(tmp_path, {"test": "vitest --watch"})
    (tmp_path / "package-lock.json").write_text("{}")
    workspace = discover_workspaces(str(tmp_path))["workspaces"][0]
    selected, _ = select_node_scripts(workspace, ["test"])
    assert selected[0]["status"] == "configuration_error"


def test_missing_required_script_is_configuration_error(tmp_path):
    node_package(tmp_path, {"build": "vite build"})
    (tmp_path / "package-lock.json").write_text("{}")
    workspace = discover_workspaces(str(tmp_path))["workspaces"][0]
    commands = node_commands_for_workspace(workspace, ["test:run"])
    assert commands["check"] == []
    assert commands["skipped"][0]["status"] == "configuration_error"


def test_optional_missing_script_is_skipped(tmp_path):
    node_package(tmp_path, {"build": "vite build"})
    (tmp_path / "package-lock.json").write_text("{}")
    workspace = discover_workspaces(str(tmp_path))["workspaces"][0]
    commands = node_commands_for_workspace(workspace)
    assert commands["skipped"] == []
    _, skipped = select_node_scripts(workspace, [], ["lint"])
    assert skipped[0]["status"] == "skipped"


def test_node_profile_uses_controlled_cache_path(tmp_path):
    node_package(tmp_path, {"test:run": "vitest run"})
    (tmp_path / "package-lock.json").write_text("{}")
    workspace = discover_workspaces(str(tmp_path))["workspaces"][0]
    commands = node_commands_for_workspace(workspace, ["test:run"])

    assert commands["setup"] == ["npm ci"]
    assert commands["cache_dirs"] == {"npm": "/ci-cache/npm"}


def test_plain_node_workspace_uses_lightweight_node_image(tmp_path):
    node_package(tmp_path, {"test:run": "vitest run"})
    (tmp_path / "package-lock.json").write_text("{}")
    workspace = discover_workspaces(str(tmp_path))["workspaces"][0]

    commands = node_commands_for_workspace(workspace, ["test:run"])

    assert commands["image"] == NODE_IMAGE


def test_browser_smoke_workspace_uses_controlled_chromium_image(tmp_path):
    node_package(
        tmp_path,
        {"test:run": "npm run test:browser-smoke:storage-plan", "test:browser-smoke:storage-plan": "node scripts/cloud-storage-plan-browser-smoke.mjs"},
    )
    (tmp_path / "package-lock.json").write_text("{}")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "cloud-storage-plan-browser-smoke.mjs").write_text(
        "npx --yes playwright@1.62.0 install chromium\n", encoding="utf-8"
    )
    workspace = discover_workspaces(str(tmp_path))["workspaces"][0]

    commands = node_commands_for_workspace(workspace, source_dir=str(tmp_path))

    assert commands["image"] == NODE_CHROMIUM_IMAGE
    assert NODE_CHROMIUM_IMAGE != NODE_IMAGE


def test_node_chromium_image_is_restricted_to_approved_prefix(monkeypatch):
    import private_ci_agent.profiles as profiles_module

    monkeypatch.setattr(profiles_module, "NODE_CHROMIUM_IMAGE", "docker.io/attacker/node-chromium:22")
    with pytest.raises(ValueError):
        node_browser_image()

    monkeypatch.setattr(profiles_module, "NODE_CHROMIUM_IMAGE", "localhost/node-chromium:22")
    assert node_browser_image() == "localhost/node-chromium:22"

    monkeypatch.setattr(profiles_module, "NODE_CHROMIUM_IMAGE", "100.118.124.97:5555/library/node-chromium:22")
    with pytest.raises(ValueError):
        node_browser_image()


def test_vue_workspace_without_browser_smoke_does_not_mount_playwright_cache(tmp_path):
    node_package(tmp_path, {"test:run": "vitest run"})
    (tmp_path / "package-lock.json").write_text("{}")
    workspace = discover_workspaces(str(tmp_path))["workspaces"][0]

    commands = node_commands_for_workspace(workspace, ["test:run"])

    assert commands["cache_dirs"] == {"npm": "/ci-cache/npm"}


def test_browser_preheat_uses_pinned_workspace_playwright(tmp_path):
    node_package(
        tmp_path,
        {"test:run": "npm run test:browser-smoke:storage-plan", "test:browser-smoke:storage-plan": "node scripts/cloud-storage-plan-browser-smoke.mjs"},
    )
    (tmp_path / "package-lock.json").write_text("{}")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "cloud-storage-plan-browser-smoke.mjs").write_text(
        "npx --yes playwright@1.62.0 install --with-deps chromium\n",
        encoding="utf-8",
    )
    workspace = discover_workspaces(str(tmp_path))["workspaces"][0]
    commands = node_commands_for_workspace(workspace, source_dir=str(tmp_path))

    preheat = commands["setup"][1]
    assert preheat["name"] == "playwright_preheat"
    assert "npx --yes playwright@1.62.0 install chromium --no-shell" in preheat["command"]
    assert "PLAYWRIGHT_BROWSERS_PATH" in preheat["command"]
    assert "/ci-cache/ms-playwright" in preheat["command"]
    assert "--with-deps" not in preheat["command"]


def test_browser_preheat_uses_workspace_local_playwright(tmp_path):
    node_package(tmp_path, {"test:run": "node scripts/browser-smoke.mjs"}, deps={"playwright": "^1.62.0"})
    (tmp_path / "package-lock.json").write_text("{}")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "browser-smoke.mjs").write_text("console.log('browser-smoke')\n", encoding="utf-8")
    workspace = discover_workspaces(str(tmp_path))["workspaces"][0]
    commands = node_commands_for_workspace(workspace, source_dir=str(tmp_path))

    preheat = commands["setup"][1]
    assert "node_modules/.bin/playwright install chromium --no-shell" in preheat["command"]
    assert "npx --yes playwright@" not in preheat["command"]


def test_browser_preheat_is_idempotent_on_cache_hit_and_installs_on_miss(tmp_path):
    node_package(
        tmp_path,
        {"test:run": "node scripts/cloud-storage-plan-browser-smoke.mjs"},
    )
    (tmp_path / "package-lock.json").write_text("{}")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "cloud-storage-plan-browser-smoke.mjs").write_text(
        "npx --yes playwright@1.62.0 install chromium\n", encoding="utf-8"
    )
    workspace = discover_workspaces(str(tmp_path))["workspaces"][0]
    command = node_commands_for_workspace(workspace, source_dir=str(tmp_path))["setup"][1]["command"]
    cache = tmp_path / "browser-cache"
    cache.mkdir()
    marker = tmp_path / "npx-called"
    fake_install = (
        f"touch {shlex.quote(str(marker))}; "
        'touch "$PLAYWRIGHT_BROWSERS_PATH/chrome"; '
        'chmod 700 "$PLAYWRIGHT_BROWSERS_PATH/chrome"'
    )
    command = command.replace(
        "npx --yes playwright@1.62.0 install chromium --no-shell",
        fake_install,
    )
    assert "npx --yes playwright@1.62.0" not in command
    (cache / "chrome").write_text("cached", encoding="utf-8")
    (cache / "chrome").chmod(0o700)
    env = dict(os.environ, PLAYWRIGHT_BROWSERS_PATH=str(cache))

    hit = subprocess.run(["/bin/sh", "-c", command], env=env, capture_output=True, text=True)
    assert hit.returncode == 0
    assert "cache_hit=true" in hit.stdout
    assert not marker.exists()

    (cache / "chrome").unlink()
    miss = subprocess.run(["/bin/sh", "-c", command], env=env, capture_output=True, text=True)
    assert miss.returncode == 0
    assert "cache_hit=false" in miss.stdout
    assert marker.exists()


def test_go_mod_requirement_selects_compatible_version_and_build(tmp_path):
    (tmp_path / "go.mod").write_text("module example\ngo 1.26.4\n", encoding="utf-8")
    assert go_version_requirements(tmp_path / "go.mod") == ("1.26.4", None)
    commands = go_commands_for_workspace(str(tmp_path))
    assert commands["selected_go_version"] == "1.26.4"
    assert commands["selected_image"] == "docker.io/library/golang:1.26.4"
    assert any(item["name"] == "gobuild" and item["command"] == "go build ./... 2>&1" for item in commands["check"])
    assert commands["cache_dirs"] == {"go": "/ci-cache"}
    assert "GOMODCACHE" in commands["setup"][0]["command"]
    migration = next(item for item in commands["setup"] if item["name"] == "migrate")
    assert "/ci-cache/.tool-bin/goose" in migration["command"]
    assert "CI_MIGRATION_TIMEOUT_SECONDS" in migration["command"]


def test_go_toolchain_is_the_effective_minimum(tmp_path):
    (tmp_path / "go.mod").write_text("module example\ngo 1.26.4\ntoolchain go1.27.1\n", encoding="utf-8")
    assert go_version_requirements(tmp_path / "go.mod") == ("1.27.1", "1.27.1")


def test_old_go_version_is_not_selected_for_new_go_mod(tmp_path):
    (tmp_path / "go.mod").write_text("module example\ngo 1.26.4\n", encoding="utf-8")
    commands = go_commands_for_workspace(str(tmp_path))
    assert commands["selected_go_version"] != "1.23.12"
    assert not commands["selected_image"].endswith(":1.23.12")


def test_arbitrary_image_name_is_rejected():
    with pytest.raises(ValueError):
        go_image_for_version("docker.io/attacker/golang:1.26.4")
