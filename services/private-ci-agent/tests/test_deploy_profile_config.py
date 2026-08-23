import os
import subprocess
import sys
from pathlib import Path

import yaml

from private_ci_agent.config import DEFAULT_CONFIG
from private_ci_agent.profiles import PROFILE_COMMANDS, get_repository_overrides


DEPLOY_DIR = Path(__file__).parents[1] / "deploy"


def test_worker_registers_every_builtin_profile():
    assert set(PROFILE_COMMANDS).issubset(set(DEFAULT_CONFIG["supported_profiles"]))


def test_deploy_profiles_include_repo_fast_check():
    data = yaml.safe_load((DEPLOY_DIR / "profiles.yml").read_text(encoding="utf-8"))

    assert "repo-fast-check" in data["profiles"]
    assert data["profiles"]["repo-fast-check"]["merge_eligible"] is False


def test_deploy_profiles_include_fixed_openapi_check():
    data = yaml.safe_load((DEPLOY_DIR / "profiles.yml").read_text(encoding="utf-8"))
    profile = data["profiles"]["openapi-check"]

    assert profile["merge_eligible"] is False
    assert profile["language"] == "node"
    assert profile["base_image"] == "docker.io/library/node:22"
    assert profile["command"] == "OPENAPI_PARALLELISM=1 bash scripts/validate-openapi.sh"


def test_only_sxt_allows_openapi_check_on_agent_side():
    data = yaml.safe_load((DEPLOY_DIR / "repositories.yml").read_text(encoding="utf-8"))
    repositories = data["repositories"]

    assert "openapi-check" in repositories["frankichen/sxt"]["allowed_profiles"]
    for repository, config in repositories.items():
        if repository != "frankichen/sxt":
            assert "openapi-check" not in (config.get("allowed_profiles") or [])


def test_sxt_runtime_overrides_request_only_lenshub_services_and_hooks():
    data = yaml.safe_load((DEPLOY_DIR / "repositories.yml").read_text(encoding="utf-8"))
    root = next(item for item in data["repositories"]["frankichen/sxt"]["workspaces"] if item["path"] == ".")

    assert root["services"] == ["postgres", "redis", "rabbitmq"]
    assert root["hooks"] == ["go-migrate", "ai-integrity"]

    controller = yaml.safe_load(
        (Path(__file__).parents[2] / "github-action-service" / "config" / "ci_repositories.yml").read_text(encoding="utf-8")
    )
    controller_root = next(
        item for item in controller["repositories"]["frankichen/sxt"]["workspaces"] if item["path"] == "."
    )
    assert controller_root["services"] == root["services"]
    assert controller_root["hooks"] == root["hooks"]


def test_sxt_allows_repo_fast_check_on_agent_side():
    data = yaml.safe_load((DEPLOY_DIR / "repositories.yml").read_text(encoding="utf-8"))
    allowed = data["repositories"]["frankichen/sxt"]["allowed_profiles"]

    assert "repo-fast-check" in allowed


def test_deploy_repositories_keep_runtime_overrides():
    data = yaml.safe_load((DEPLOY_DIR / "repositories.yml").read_text(encoding="utf-8"))

    assert {"frankichen/ai_war", "frankichen/lenshub-diag-mcp", "frankichen/sxt", "frankichen/github_mcp", "frankichen/auto_gupiao"}.issubset(
        set(data["repositories"])
    )


def test_auto_gupiao_legacy_entry_is_preserved():
    data = yaml.safe_load((DEPLOY_DIR / "repositories.yml").read_text(encoding="utf-8"))
    allowed = data["repositories"]["frankichen/auto_gupiao"]["allowed_profiles"]

    assert data["repositories"]["frankichen/auto_gupiao"]["enabled"] is True
    assert "repo-auto-check" in allowed
    assert "go-check" in allowed


def test_repository_overrides_expose_only_workspace_configuration(tmp_path, monkeypatch):
    path = tmp_path / "repositories.yml"
    path.write_text(
        """
repositories:
  frankichen/example:
    enabled: false
    private_ci: false
    allowed_profiles:
      - repo-auto-check
    deployment:
      enabled: true
    workspaces:
      - path: web
        type: node
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("PRIVATE_CI_REPOSITORY_OVERRIDES_PATH", str(path))

    assert get_repository_overrides("frankichen/example", "repo-auto-check") == {
        "workspaces": [{"path": "web", "type": "node"}]
    }


def test_apply_fixes_syncs_source_module():
    script = (DEPLOY_DIR / "apply-fixes.sh").read_text(encoding="utf-8")

    assert "profiles.py source.py controller_client.py" in script


def test_playwright_cache_maintenance_is_pinned_and_not_a_job_step():
    script = (DEPLOY_DIR / "prepare-playwright-cache").read_text(encoding="utf-8")

    assert "/srv/private-ci/cache/ms-playwright" in script
    assert "/ci-cache/ms-playwright" in script
    assert "playwright@1.62.0 install chromium --no-shell" in script
    assert "pass_proxy=True" in script
    assert "run as ciworker" in script


def test_controller_build_passes_host_proxy():
    script = (DEPLOY_DIR / "apply-fixes.sh").read_text(encoding="utf-8")

    assert "PRIVATE_CI_DOCKER_BUILD_PROXY:-http://127.0.0.1:10808" in script
    assert "--network host" in script
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        assert f'--build-arg "{name}=${{DOCKER_BUILD_PROXY}}"' in script


def test_node_chromium_dockerfile_uses_inherited_proxy():
    dockerfile = (DEPLOY_DIR / "Dockerfile.node-chromium").read_text(encoding="utf-8")
    preheat = (DEPLOY_DIR / "prepare-node-chromium").read_text(encoding="utf-8")

    assert "Acquire::http::Proxy=${http_apt_proxy}" in dockerfile
    assert "Acquire::https::Proxy=${https_apt_proxy}" in dockerfile
    assert "proxy.runtime.conf" in preheat
    assert "PRIVATE_CI_CONTAINER_PROXY_HOST:-10.0.2.2" in preheat


APPLY_FIXES_SCRIPT = DEPLOY_DIR / "apply-fixes.sh"
ROLLBACK_CONTAINER = "github-action-service-rollback-aaaaaaaaaaaa"

FAKE_TOOL_SOURCE = r'''#!/usr/bin/env python3
import os
import sys
from pathlib import Path

name = sys.argv[1]
args = sys.argv[2:]
call_log = Path(os.environ["FAKE_CALL_LOG"])
state_path = Path(os.environ["FAKE_STATE"])

def log_call():
    with call_log.open("a", encoding="utf-8") as handle:
        handle.write(name + (" " + " ".join(args) if args else "") + "\n")

def load_state():
    if not state_path.exists():
        return set()
    return {line for line in state_path.read_text(encoding="utf-8").splitlines() if line}

def save_state(state):
    state_path.write_text("".join(f"{item}\n" for item in sorted(state)), encoding="utf-8")

log_call()

if name == "docker":
    state = load_state()
    command = args[0] if args else ""
    if command == "build":
        sys.exit(0)
    if command == "inspect":
        target = args[1]
        if target not in state:
            sys.exit(1)
        if "--format" in args:
            print("EXISTING_ENV=1")
        else:
            print("[]")
        sys.exit(0)
    if command == "stop":
        sys.exit(0 if args[-1] in state else 1)
    if command == "rename":
        old_name, new_name = args[1], args[2]
        if old_name not in state:
            sys.exit(1)
        state.remove(old_name)
        state.add(new_name)
        save_state(state)
        sys.exit(0)
    if command == "update":
        sys.exit(0)
    if command == "run":
        target = args[args.index("--name") + 1]
        if os.environ.get("FAKE_DOCKER_RUN_FAIL") == "1":
            if os.environ.get("FAKE_DOCKER_RUN_CREATES_CONTAINER") == "1":
                state.add(target)
                save_state(state)
            sys.exit(125)
        state.add(target)
        save_state(state)
        print("fake-controller-id")
        sys.exit(0)
    if command == "rm":
        state.discard(args[-1])
        save_state(state)
        sys.exit(0)
    if command == "start":
        sys.exit(0 if args[-1] in state else 1)
    if command == "logs":
        sys.exit(0)
    sys.exit(0)

if name == "jq":
    sys.stdin.read()
    sys.exit(0)

if name == "curl":
    sys.exit(0 if os.environ.get("FAKE_HEALTH_MODE", "success") == "success" else 22)

if name == "git":
    if "rev-parse" in args:
        print("a" * 40)
        sys.exit(0)
    sys.exit(1)

if name == "systemctl":
    sys.exit(0)

if name in {"touch", "mount", "rm", "install", "sleep", "runuser", "mkdir", "chown", "chmod"}:
    sys.exit(0)

sys.exit(0)
'''

def _stage_apply_fixes_repo(tmp_path):
    repo_root = tmp_path / "repo"
    agent_root = repo_root / "services/private-ci-agent"
    deploy_root = agent_root / "deploy"
    source_root = agent_root / "private_ci_agent"
    controller_app = repo_root / "services/github-action-service/app"
    deploy_root.mkdir(parents=True)
    source_root.mkdir(parents=True)
    controller_app.mkdir(parents=True)

    staged_script = deploy_root / "apply-fixes.sh"
    staged_script.write_bytes(APPLY_FIXES_SCRIPT.read_bytes())
    staged_script.chmod(0o755)
    for name in (
        "config.py", "executor.py", "main.py", "podman.py",
        "profiles.py", "source.py", "controller_client.py",
    ):
        (source_root / name).write_text("# test fixture\n", encoding="utf-8")
    (controller_app / "version.py").write_text(
        'SERVICE_VERSION = "12.0.5"\n', encoding="utf-8"
    )
    return repo_root, staged_script


def _run_apply_fixes(
    tmp_path,
    *,
    failure_mode=None,
    docker_run_fail=False,
    docker_run_creates_container=False,
    health_mode="success",
):
    repo_root, staged_script = _stage_apply_fixes_repo(tmp_path)
    fake_tool = tmp_path / "fake-tool.py"
    fake_tool.write_text(FAKE_TOOL_SOURCE, encoding="utf-8")
    commands = (
        "docker", "jq", "curl", "git", "systemctl", "touch", "mount", "rm",
        "install", "sleep", "runuser", "mkdir", "chown", "chmod",
    )
    bash_env = tmp_path / "bash_env"
    functions = ['_fake_tool() { "$FAKE_PYTHON" "$FAKE_TOOL_SCRIPT" "$@"; }']
    functions.extend(f'{command}() {{ _fake_tool {command} "$@"; }}' for command in commands)
    bash_env.write_text("\n".join(functions) + "\n", encoding="utf-8")

    call_log = tmp_path / "calls.log"
    state_path = tmp_path / "containers.state"
    state_path.write_text("github-action-service\n", encoding="utf-8")
    env = os.environ.copy()
    env.update({
        "BASH_ENV": str(bash_env),
        "FAKE_PYTHON": sys.executable,
        "FAKE_TOOL_SCRIPT": str(fake_tool),
        "FAKE_CALL_LOG": str(call_log),
        "FAKE_STATE": str(state_path),
        "FAKE_DOCKER_RUN_FAIL": "1" if docker_run_fail else "0",
        "FAKE_DOCKER_RUN_CREATES_CONTAINER": "1" if docker_run_creates_container else "0",
        "FAKE_HEALTH_MODE": health_mode,
    })
    if failure_mode is None:
        env.pop("MYGITHUB12_DEPLOY_FAILURE_MODE", None)
    else:
        env["MYGITHUB12_DEPLOY_FAILURE_MODE"] = failure_mode

    result = subprocess.run(
        ["/bin/bash", str(staged_script)],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    calls = call_log.read_text(encoding="utf-8").splitlines() if call_log.exists() else []
    state = {line for line in state_path.read_text(encoding="utf-8").splitlines() if line}
    return result, calls, state

def _output(result):
    return result.stdout + result.stderr

def test_apply_fixes_default_mode_auto_rolls_back_start_failure(tmp_path):
    result, calls, state = _run_apply_fixes(
        tmp_path, docker_run_fail=True, docker_run_creates_container=True
    )
    assert result.returncode != 0
    assert "Controller failure mode: auto-rollback" in _output(result)
    assert "docker rm -f github-action-service" in calls
    assert f"docker rename {ROLLBACK_CONTAINER} github-action-service" in calls
    assert "docker start github-action-service" in calls
    assert state == {"github-action-service"}

def test_apply_fixes_explicit_auto_rollback_restores_start_failure(tmp_path):
    result, calls, state = _run_apply_fixes(
        tmp_path, failure_mode="auto-rollback", docker_run_fail=True, docker_run_creates_container=True
    )
    assert result.returncode != 0
    assert "controller start failed; rollback started" in _output(result)
    assert f"docker rename {ROLLBACK_CONTAINER} github-action-service" in calls
    assert "docker start github-action-service" in calls
    assert state == {"github-action-service"}

def test_apply_fixes_explicit_auto_rollback_restores_health_failure(tmp_path):
    result, calls, state = _run_apply_fixes(
        tmp_path, failure_mode="auto-rollback", health_mode="failure"
    )
    assert result.returncode != 0
    assert "controller health failed; rollback started" in _output(result)
    assert "docker rm -f github-action-service" in calls
    assert f"docker rename {ROLLBACK_CONTAINER} github-action-service" in calls
    assert "docker start github-action-service" in calls
    assert state == {"github-action-service"}

def test_apply_fixes_fail_stop_start_failure_preserves_candidates(tmp_path):
    result, calls, state = _run_apply_fixes(
        tmp_path, failure_mode="fail-stop", docker_run_fail=True, docker_run_creates_container=True
    )
    output = _output(result)
    assert result.returncode != 0
    assert "AUTO_ROLLBACK_DISABLED" in output
    assert "FAILURE_STAGE=controller_start" in output
    assert f"ROLLBACK_CONTAINER={ROLLBACK_CONTAINER}" in output
    assert "FAILED_CONTROLLER_CONTAINER=github-action-service (preserved for diagnostics)" in output
    assert "MANUAL_RECOVERY_REQUIRES_AUTHORIZATION: 人工恢复需要另行授权" in output
    assert "docker rm -f github-action-service" not in calls
    assert f"docker rename {ROLLBACK_CONTAINER} github-action-service" not in calls
    assert "docker start github-action-service" not in calls
    assert state == {ROLLBACK_CONTAINER, "github-action-service"}

def test_apply_fixes_fail_stop_reports_start_failure_without_new_container(tmp_path):
    result, calls, state = _run_apply_fixes(
        tmp_path, failure_mode="fail-stop", docker_run_fail=True, docker_run_creates_container=False
    )
    assert result.returncode != 0
    assert "FAILED_CONTROLLER_CONTAINER=not-created" in _output(result)
    assert f"docker rename {ROLLBACK_CONTAINER} github-action-service" not in calls
    assert "docker start github-action-service" not in calls
    assert state == {ROLLBACK_CONTAINER}

def test_apply_fixes_fail_stop_health_failure_preserves_candidates(tmp_path):
    result, calls, state = _run_apply_fixes(
        tmp_path, failure_mode="fail-stop", health_mode="failure"
    )
    output = _output(result)
    assert result.returncode != 0
    assert "AUTO_ROLLBACK_DISABLED" in output
    assert "FAILURE_STAGE=controller_health" in output
    assert "docker rm -f github-action-service" not in calls
    assert f"docker rename {ROLLBACK_CONTAINER} github-action-service" not in calls
    assert "docker start github-action-service" not in calls
    assert state == {ROLLBACK_CONTAINER, "github-action-service"}

def test_apply_fixes_rejects_invalid_mode_before_controller_switch(tmp_path):
    result, calls, state = _run_apply_fixes(tmp_path, failure_mode="unsafe-value")
    assert result.returncode != 0
    assert "allowed values: auto-rollback, fail-stop" in _output(result)
    assert not any(call.startswith("docker ") for call in calls)
    assert state == {"github-action-service"}

def test_apply_fixes_success_path_continues_in_fail_stop_mode(tmp_path):
    result, calls, state = _run_apply_fixes(tmp_path, failure_mode="fail-stop")
    assert result.returncode == 0, _output(result)
    assert "DONE. Worker restarted with local shared image and caches preheated." in _output(result)
    assert len([call for call in calls if call.startswith("runuser ")]) == 3
    assert "systemctl restart private-ci-agent.service" in calls
    assert "systemctl is-active --quiet private-ci-agent.service" in calls
    assert f"docker rename {ROLLBACK_CONTAINER} github-action-service" not in calls
    assert state == {ROLLBACK_CONTAINER, "github-action-service"}
