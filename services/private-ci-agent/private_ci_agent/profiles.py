"""受控 CI Profile、Manifest 和多工作区发现。

这里的输入只来自仓库文件和本机白名单配置，远端任务不能注入任意 shell。
"""

import json
import logging
import os
import re
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

MAX_MANIFEST_DEPTH = 4
EXCLUDED_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "vendor", "dist", "build",
    "coverage", ".cache", "tmp", ".tmp", ".ci", "docs", "examples",
}
LOCK_FILES = {
    "package-lock.json": "npm",
    "pnpm-lock.yaml": "pnpm",
    "yarn.lock": "yarn",
    "bun.lock": "bun",
    "bun.lockb": "bun",
}
SAFE_NODE_SCRIPTS = ("lint", "typecheck", "test:run", "test:ci", "test", "build")
UNSAFE_SCRIPT_WORDS = ("dev", "serve", "start", "preview", "watch")

GO_COMMANDS = {
    "setup": [
        {
            "name": "prepare_cache",
            "command": "echo '[go:.:setup] preparing writable cache'; mkdir -p \"$HOME\" \"$GOPATH\" \"$GOMODCACHE\" \"$GOCACHE\" \"$(dirname \"$GOENV\")\" \"$GOTMPDIR\" \"$XDG_CACHE_HOME\" \"$XDG_CONFIG_HOME\"; test -w /ci-cache; test -w \"$GOMODCACHE\"; test -w \"$GOCACHE\"; test -w \"$GOTMPDIR\"",
        },
        {"name": "version", "command": "go version"},
        {"name": "env", "command": "go env"},
        {"name": "mod_download", "command": "go mod download 2>&1"},
        {"name": "migrate", "command": "make migrate-up 2>&1"},
    ],
    "check": [
        {"name": "gofmt", "command": 'UNFORMATTED=$(gofmt -l . 2>&1); if [ -n "$UNFORMATTED" ]; then echo "UNFORMATTED FILES:"; echo "$UNFORMATTED"; exit 1; fi; echo "All Go files properly formatted"'},
        {"name": "govet", "command": "go vet ./... 2>&1"},
        {"name": "gotest", "command": "go test -p 6 -count=1 ./... 2>&1"},
        {"name": "gobuild", "command": "go build ./... 2>&1"},
    ],
    "image": "docker.io/library/golang:1.26.4",
    "cache_dirs": {"go": "/ci-cache"},
}

APPROVED_GO_IMAGE_PREFIXES = (
    "docker.io/library/golang:",
    "100.118.124.97:5555/library/golang:",
)

GO_DIRECTIVE_RE = re.compile(r"^\s*go\s+(?P<version>\d+\.\d+(?:\.\d+)?)\s*$", re.MULTILINE)
GO_TOOLCHAIN_RE = re.compile(r"^\s*toolchain\s+go(?P<version>\d+\.\d+(?:\.\d+)?)\s*$", re.MULTILINE)


def _go_version_tuple(version: str) -> tuple[int, int, int]:
    parts = version.split(".")
    if len(parts) not in (2, 3) or parts[0] != "1" or not all(part.isdigit() for part in parts):
        raise ValueError(f"unsupported Go version: {version}")
    return int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) == 3 else 0


def _format_go_version(version_tuple: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version_tuple)


def go_image_for_version(image: str) -> str:
    """仅允许受控 Registry 的 Go 镜像，拒绝仓库输入注入任意镜像。"""
    if not any(image.startswith(prefix) for prefix in APPROVED_GO_IMAGE_PREFIXES):
        raise ValueError(f"unapproved Go image: {image}")
    version = image.rsplit(":", 1)[-1]
    if not re.fullmatch(r"1\.\d+(?:\.\d+)?", version):
        raise ValueError(f"invalid Go image version: {image}")
    return image


def go_version_requirements(go_mod_path: Path) -> tuple[str, str | None]:
    try:
        content = go_mod_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read go.mod: {exc}") from exc
    directive = GO_DIRECTIVE_RE.search(content)
    if not directive:
        raise ValueError("go.mod is missing a supported go directive")
    required = _go_version_tuple(directive.group("version"))
    toolchain = GO_TOOLCHAIN_RE.search(content)
    toolchain_version = None
    if toolchain:
        toolchain_tuple = _go_version_tuple(toolchain.group("version"))
        required = max(required, toolchain_tuple)
        toolchain_version = _format_go_version(toolchain_tuple)
    return _format_go_version(required), toolchain_version


def go_commands_for_workspace(source_dir: str, workspace_path: str = ".") -> dict:
    go_mod = Path(source_dir) / ("" if workspace_path in ("", ".") else workspace_path) / "go.mod"
    try:
        selected_version, toolchain_version = go_version_requirements(go_mod)
    except ValueError as exc:
        return {"error": "configuration_error", "message": str(exc)}
    source_image = f"docker.io/library/golang:{selected_version}"
    image_prefix = os.environ.get(
        "PRIVATE_CI_GO_IMAGE_PREFIX", "docker.io/library/golang:"
    )
    if not image_prefix.endswith(":"):
        image_prefix = f"{image_prefix}:"
    image = go_image_for_version(f"{image_prefix}{selected_version}")
    return {
        "setup": GO_COMMANDS["setup"],
        "check": GO_COMMANDS["check"],
        "image": image,
        "cache_dirs": GO_COMMANDS["cache_dirs"],
        "requested_go_version": selected_version,
        "selected_go_version": selected_version,
        "source_image": source_image,
        "selected_image": image,
        "toolchain": toolchain_version,
    }

PYTHON_COMMANDS = {
    "setup": [
        "pip install --no-input ruff pytest 2>&1",
        '[ -f requirements.txt ] && pip install --no-input -r requirements.txt 2>&1 || true',
        '[ -f pyproject.toml ] && pip install --no-input -e ".[dev,test]" 2>&1 || true',
    ],
    "check": [
        {"name": "ruff", "command": "python -m ruff check app 2>&1"},
        {"name": "compileall", "command": "python -m compileall -q . 2>&1"},
        {"name": "pytest", "command": "python -m pytest -q -p no:warnings 2>&1"},
    ],
    "image": "docker.io/library/python:3.12-slim",
    "cache_dirs": {"pip": "/root/.cache/pip"},
}

PROFILE_COMMANDS = {
    "go-check": GO_COMMANDS,
    "python-check": PYTHON_COMMANDS,
    "node-check": None,
    "repo-auto-check": None,
    "repo-fast-check": None,
}


def _relative_depth(path: str) -> int:
    return 0 if path in ("", ".") else len(Path(path).parts)


def _walk_manifest_dirs(source_dir: str):
    """只遍历受控深度，并在遍历前剪枝生成物和示例目录。"""
    root = Path(source_dir)
    for current, dirs, files in os.walk(root, topdown=True):
        rel = os.path.relpath(current, root).replace(os.sep, "/")
        if rel == ".":
            rel = ""
        depth = _relative_depth(rel)
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDED_DIRS and not d.startswith("."))
        if depth >= MAX_MANIFEST_DEPTH:
            dirs[:] = []
        yield Path(current), rel, set(files)


def _load_json(path: Path) -> dict:
    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        return {"_configuration_error": f"invalid package.json: {exc}"}


def _node_workspace(source_dir: str, rel: str, files: set[str], explicit: dict | None = None) -> dict:
    directory = Path(source_dir) / rel
    package = _load_json(directory / "package.json")
    lock_names = sorted(name for name in LOCK_FILES if name in files)
    if len(lock_names) > 1:
        return {"path": rel or ".", "stack": "node", "configuration_error": f"multiple lock files: {lock_names}"}
    package_manager = LOCK_FILES[lock_names[0]] if lock_names else None
    dependencies = {}
    dependencies.update(package.get("dependencies") or {})
    dependencies.update(package.get("devDependencies") or {})
    framework = None
    if any(name == "vue" or name == "vite" or name == "nuxt" or name.startswith("@vue/") for name in dependencies):
        framework = "vue"
    scripts = package.get("scripts") or {}
    result = {
        "path": rel or ".",
        "stack": "node",
        "framework": framework,
        "package_manager": package_manager,
        "scripts": scripts,
        "package_engines": package.get("engines") or {},
    }
    if package.get("_configuration_error"):
        result["configuration_error"] = package["_configuration_error"]
    if explicit:
        result["required_scripts"] = list(explicit.get("required_scripts") or [])
        result["optional_scripts"] = list(explicit.get("optional_scripts") or [])
    return result


def discover_workspaces(source_dir: str, repository_config: dict | None = None) -> dict:
    """发现 Go/Node/Python Manifest，返回可审计的工作区计划。"""
    configured = (repository_config or {}).get("workspaces") or []
    found: dict[tuple[str, str], dict] = {}

    def add_workspace(workspace: dict):
        key = (workspace.get("path", "."), workspace["stack"])
        if key not in found:
            found[key] = workspace

    # 显式工作区优先；auto 入口仍会用受控扫描补齐其它 Manifest。
    for item in configured:
        rel = (item.get("path") or ".").strip("/") or "."
        directory = Path(source_dir) / ("" if rel == "." else rel)
        if not directory.is_dir():
            continue
        files = {p.name for p in directory.iterdir() if p.is_file()}
        kind = item.get("type", "auto")
        if kind in ("auto", "go") and "go.mod" in files:
            add_workspace({"path": rel, "stack": "go"})
        if kind in ("auto", "node") and "package.json" in files and not (
            kind == "auto" and rel == "." and "go.mod" in files and not any(lock in files for lock in LOCK_FILES)
        ):
            node = _node_workspace(source_dir, rel, files, item)
            if item.get("package_manager"):
                node["package_manager"] = item["package_manager"]
            add_workspace(node)
        if kind in ("auto", "python") and any(name in files for name in ("pyproject.toml", "requirements.txt", "setup.py", "setup.cfg", "Pipfile")):
            add_workspace({"path": rel, "stack": "python"})

    for directory, rel, files in _walk_manifest_dirs(source_dir):
        if "go.mod" in files:
            add_workspace({"path": rel or ".", "stack": "go"})
        if "package.json" in files:
            # 根目录既是 Go 模块又是无锁的 npm 元数据时，不创建一个会触发 npm install 的伪工作区。
            if not (rel in ("", ".") and "go.mod" in files and not any(lock in files for lock in LOCK_FILES)):
                add_workspace(_node_workspace(source_dir, rel, files))
        if any(name in files for name in ("pyproject.toml", "requirements.txt", "setup.py", "setup.cfg", "Pipfile")):
            add_workspace({"path": rel or ".", "stack": "python"})

    workspaces = sorted(found.values(), key=lambda value: (value["path"], value["stack"]))
    stacks = []
    for workspace in workspaces:
        if workspace["stack"] not in stacks:
            stacks.append(workspace["stack"])
        if workspace.get("framework") and workspace["framework"] not in stacks:
            stacks.append(workspace["framework"])
    return {"workspaces": workspaces, "detected_stacks": stacks}


def detect_languages(source_dir: str) -> list[str]:
    result = discover_workspaces(source_dir)
    return [stack for stack in result["detected_stacks"] if stack in ("go", "python", "node")]


def _script_is_one_shot(name: str, command: str) -> bool:
    lowered = f"{name} {command}".lower()
    return not any(word in lowered for word in UNSAFE_SCRIPT_WORDS)


def select_node_scripts(workspace: dict, required_default: list[str] | None = None, optional_default: list[str] | None = None) -> tuple[list[dict], list[dict]]:
    scripts = workspace.get("scripts") or {}
    required = list(workspace.get("required_scripts") or (required_default or []))
    optional = list(workspace.get("optional_scripts") or (optional_default or []))
    selected = []
    skipped = []

    def choose(name: str, required_flag: bool):
        if name not in scripts:
            item = {"name": name, "status": "configuration_error" if required_flag else "skipped", "reason": "script_not_defined"}
            (selected if required_flag else skipped).append(item)
            return
        command = f"npm run {name}"
        if not _script_is_one_shot(name, scripts[name]):
            item = {"name": name, "status": "configuration_error", "reason": "non_ci_script"}
            (selected if required_flag else skipped).append(item)
            return
        selected.append({"name": name, "command": command, "status": "planned"})

    for name in required:
        choose(name, True)
    for name in optional:
        if name not in required:
            choose(name, False)

    # Generic node-check: choose only scripts that really exist.
    if not required and not optional:
        if "lint" in scripts:
            choose("lint", False)
        test_name = next((name for name in ("test:run", "test:ci", "test") if name in scripts), None)
        if test_name:
            choose(test_name, True)
        if "typecheck" in scripts:
            choose("typecheck", False)
        if "build" in scripts:
            choose("build", False)
    return selected, skipped


def node_commands_for_workspace(workspace: dict, required_default: list[str] | None = None) -> dict:
    selected, skipped = select_node_scripts(workspace, required_default)
    invalid = [item for item in selected if not item.get("command")]
    selected = [item for item in selected if item.get("command")]
    skipped = invalid + skipped
    package_manager = workspace.get("package_manager")
    if workspace.get("configuration_error"):
        return {"error": "configuration_error", "message": workspace["configuration_error"], "selected_scripts": [], "skipped": []}
    if not package_manager:
        return {"error": "configuration_error", "message": "Node workspace requires exactly one supported lock file", "selected_scripts": selected, "skipped_scripts": skipped}
    install = {"npm": "npm ci", "pnpm": "pnpm install --frozen-lockfile", "yarn": "yarn install --immutable", "bun": "bun install --frozen-lockfile"}[package_manager]
    return {
        "setup": [install],
        "check": [item for item in selected if item.get("command")],
        "skipped": skipped,
        "selected_scripts": [item["name"] for item in selected if item.get("command")],
        "image": "docker.io/library/node:22",
        "cache_dirs": {"npm": "/root/.npm"} if package_manager == "npm" else {},
    }


def get_commands_for_profile(profile: str, source_dir: str = "") -> dict:
    if profile not in PROFILE_COMMANDS:
        logger.error("Unknown profile: %s", profile)
        return None
    if profile in {"repo-auto-check", "repo-fast-check"}:
        detected = discover_workspaces(source_dir)
        if not detected["workspaces"]:
            return {"error": "unsupported", "message": "No supported project Manifest detected", **detected}
        return {"error": "use_workspace_plan", **detected}
    if profile == "node-check":
        detected = discover_workspaces(source_dir, {"workspaces": [{"path": ".", "type": "node"}]})
        node = next((item for item in detected["workspaces"] if item["stack"] == "node"), None)
        return node_commands_for_workspace(node or {"path": ".", "stack": "node"})
    if profile == "go-check" and source_dir:
        return go_commands_for_workspace(source_dir)
    return PROFILE_COMMANDS[profile]


def get_repository_overrides(repository: str, profile: str) -> dict:
    path = "/etc/private-ci/repositories.yml"
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            repos = yaml.safe_load(handle) or {}
        return (repos.get("repositories", {}).get(repository) or {}).copy()
    except Exception as exc:
        logger.warning("Failed to load repository overrides: %s", exc)
        return {}
