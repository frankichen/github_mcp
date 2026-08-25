"""Conservative changed-file selection for repo-fast-check evidence."""
from __future__ import annotations

from pathlib import PurePosixPath


_GLOBAL_FILES = {
    "go.mod", "go.sum", "go.work", "go.work.sum", "package.json", "package-lock.json",
    "pnpm-lock.yaml", "yarn.lock", "pyproject.toml", "requirements.txt",
    "requirements-dev.txt", "Cargo.toml", "Cargo.lock", "pom.xml", "build.gradle",
    "build.gradle.kts", "settings.gradle", "settings.gradle.kts", "global.json",
}
_GLOBAL_PREFIXES = (".github/", "scripts/", "ci/", "docker/", "deploy/")


def select_affected(changed_files: list[str], workspaces: list[dict], *, truncated: bool = False) -> dict:
    normalized = sorted({str(path).strip("/") for path in changed_files if str(path).strip("/")})
    public = [
        {"path": str(item.get("path") or "."), "stack": str(item.get("stack") or "")}
        for item in workspaces if isinstance(item, dict)
    ]
    if truncated or not normalized:
        return {
            "complete": False,
            "changed_files": normalized,
            "selected_workspaces": public,
            "selected_tests": [],
            "reasons": ["changed_files_truncated" if truncated else "changed_files_empty_fallback_all"],
        }

    global_change = any(
        PurePosixPath(path).name in _GLOBAL_FILES
        or path.startswith(_GLOBAL_PREFIXES)
        or "/migrations/" in f"/{path}"
        or path.startswith("db/migrations/")
        for path in normalized
    )
    selected = []
    if global_change:
        selected = public
    else:
        for workspace in public:
            root = workspace["path"].strip("/")
            if root in {"", "."}:
                # Root workspaces are relevant to any repository change, but do
                # not prevent selecting more specific workspaces as evidence.
                selected.append(workspace)
                continue
            if any(path == root or path.startswith(root + "/") for path in normalized):
                selected.append(workspace)

    if not selected:
        # Unknown path-to-workspace relationships must widen, never narrow.
        selected = public
        complete = False
        reasons = ["no_workspace_match_fallback_all"]
    else:
        complete = True
        reasons = ["global_dependency_or_ci_change"] if global_change else ["path_prefix_match"]

    tests = sorted(
        path for path in normalized
        if "/tests/" in f"/{path.lower()}" or PurePosixPath(path).name.startswith("test_")
        or path.endswith(("_test.go", ".spec.ts", ".test.ts", ".spec.js", ".test.js"))
    )
    return {
        "complete": complete,
        "changed_files": normalized,
        "selected_workspaces": selected,
        "selected_tests": tests,
        "reasons": reasons,
    }
