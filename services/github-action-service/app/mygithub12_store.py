"""MyGithut12 immutable index store and workspace coordination primitives."""
from __future__ import annotations

import ast
import base64
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from pathlib import PurePosixPath
from typing import Any

INDEX_VERSION = "12.0.0-1"
DEFAULT_LEASE_SECONDS = 1800
MAX_LEASE_SECONDS = 14400
MAX_INDEX_FILES = int(os.getenv("MYGITHUB12_INDEX_MAX_FILES", "5000"))
MAX_INDEX_BYTES = int(os.getenv("MYGITHUB12_INDEX_MAX_BYTES", str(50 * 1024 * 1024)))
MAX_FILE_BYTES = int(os.getenv("MYGITHUB12_INDEX_MAX_FILE_BYTES", str(512 * 1024)))
MAX_BATCH_BYTES = 4 * 1024 * 1024
EXCLUDED_PARTS = {".git", "node_modules", "vendor", "dist", "build", "target", ".next", ".venv", "venv", "__pycache__"}
SENSITIVE_NAMES = {".env", "id_rsa", "id_ed25519", "credentials", "credentials.json", "secrets.json", "private.key", "server.key"}
LOCK = threading.RLock()


class MyGithub12Error(Exception):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.trace_id = str(uuid.uuid4())


def now() -> float:
    return time.time()


def db_path() -> str:
    configured = os.getenv("MYGITHUB12_DB_PATH", "").strip()
    if configured:
        return configured
    legacy = os.getenv("IDEMPOTENCY_DB_PATH", "/data/idempotency.db")
    return os.path.join(os.path.dirname(legacy) or "/data", "mygithub12.db")


def connect() -> sqlite3.Connection:
    path = db_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=30000")
    return db


def init_db() -> None:
    with LOCK, connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS repository_indexes(
              repository TEXT NOT NULL, commit_sha TEXT NOT NULL, tree_sha TEXT NOT NULL,
              index_version TEXT NOT NULL, status TEXT NOT NULL, build_strategy TEXT NOT NULL,
              base_commit_sha TEXT, file_count INTEGER NOT NULL, symbol_count INTEGER NOT NULL,
              size_bytes INTEGER NOT NULL, manifest_json TEXT NOT NULL,
              created_at REAL NOT NULL, last_accessed_at REAL NOT NULL,
              PRIMARY KEY(repository,commit_sha,tree_sha,index_version));
            CREATE TABLE IF NOT EXISTS repository_index_files(
              repository TEXT NOT NULL, commit_sha TEXT NOT NULL, path TEXT NOT NULL,
              blob_sha TEXT NOT NULL, size_bytes INTEGER NOT NULL, language TEXT NOT NULL,
              content_sha256 TEXT NOT NULL, line_count INTEGER NOT NULL, content TEXT NOT NULL,
              PRIMARY KEY(repository,commit_sha,path));
            CREATE INDEX IF NOT EXISTS idx_index_files_blob
              ON repository_index_files(repository,commit_sha,blob_sha);
            CREATE TABLE IF NOT EXISTS repository_index_symbols(
              repository TEXT NOT NULL, commit_sha TEXT NOT NULL, symbol_id TEXT NOT NULL,
              name TEXT NOT NULL, qualified_name TEXT NOT NULL, kind TEXT NOT NULL,
              language TEXT NOT NULL, path TEXT NOT NULL, blob_sha TEXT NOT NULL,
              start_line INTEGER NOT NULL, end_line INTEGER NOT NULL, signature TEXT NOT NULL,
              parent_name TEXT, bases_json TEXT NOT NULL,
              PRIMARY KEY(repository,commit_sha,symbol_id));
            CREATE INDEX IF NOT EXISTS idx_index_symbols_name
              ON repository_index_symbols(repository,commit_sha,name);
            CREATE TABLE IF NOT EXISTS repository_index_jobs(
              job_id TEXT PRIMARY KEY, repository TEXT NOT NULL, commit_sha TEXT NOT NULL,
              tree_sha TEXT NOT NULL, index_version TEXT NOT NULL, strategy TEXT NOT NULL,
              base_commit_sha TEXT, status TEXT NOT NULL, step TEXT NOT NULL,
              revision INTEGER NOT NULL, progress_current INTEGER NOT NULL,
              progress_total INTEGER NOT NULL, reused_file_count INTEGER NOT NULL,
              reindexed_file_count INTEGER NOT NULL, cancel_requested INTEGER NOT NULL,
              error_code TEXT, error_message TEXT, created_at REAL NOT NULL,
              started_at REAL, finished_at REAL, idempotency_key TEXT);
            CREATE INDEX IF NOT EXISTS idx_index_jobs_target
              ON repository_index_jobs(repository,commit_sha,status);
            CREATE TABLE IF NOT EXISTS development_workspaces(
              workspace_id TEXT PRIMARY KEY, repository TEXT NOT NULL, branch TEXT NOT NULL,
              base_branch TEXT NOT NULL, base_commit_sha TEXT NOT NULL, head_sha TEXT NOT NULL,
              tree_sha TEXT NOT NULL, status TEXT NOT NULL, revision INTEGER NOT NULL,
              owner TEXT NOT NULL, lease_expires_at REAL NOT NULL, index_commit_sha TEXT,
              scope_json TEXT NOT NULL, drift_reason TEXT, pr_number INTEGER,
              created_at REAL NOT NULL, updated_at REAL NOT NULL);
            CREATE UNIQUE INDEX IF NOT EXISTS uq_active_workspace_branch
              ON development_workspaces(repository,branch)
              WHERE status IN ('active','drifted');
            """
        )


def parse_json(value: str, expected: type, name: str, default: Any) -> Any:
    if not value:
        return default
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise MyGithub12Error("SEARCH_QUERY_INVALID", f"{name} must be valid JSON", {"position": exc.pos}) from exc
    if not isinstance(parsed, expected):
        raise MyGithub12Error("SEARCH_QUERY_INVALID", f"{name} has the wrong JSON type")
    return parsed


def safe_path(path: str, allow_empty: bool = False) -> str:
    if not path and allow_empty:
        return ""
    if not path or path.startswith("/") or "\\" in path:
        raise MyGithub12Error("INVALID_REPOSITORY_PATH", "path must be repository-relative")
    if any(part in {"", ".", ".."} for part in path.split("/")):
        raise MyGithub12Error("INVALID_REPOSITORY_PATH", "path contains unsafe segments")
    if any(ord(ch) < 32 for ch in path):
        raise MyGithub12Error("INVALID_REPOSITORY_PATH", "path contains control characters")
    return path


def get_repo(service: Any, repository: str):
    service._check_repository_allowed(repository)
    raw = service.client.client if hasattr(service.client, "client") else service.client
    github = getattr(raw, "_pygithub", None) or getattr(service.client, "_pygithub", None)
    if github is None:
        raise MyGithub12Error("GITHUB_NOT_CONFIGURED", "GitHub client is unavailable")
    return github.get_repo(repository)


def commit_tree_sha(commit: Any) -> str:
    values = (
        getattr(getattr(getattr(commit, "commit", None), "tree", None), "sha", None),
        getattr(getattr(commit, "tree", None), "sha", None),
    )
    for value in values:
        if value:
            return str(value)
    raise MyGithub12Error("INDEX_SNAPSHOT_UNAVAILABLE", "commit tree SHA is unavailable")


def resolve_identity(service: Any, repository: str, commit_sha: str = "", ref: str = "") -> dict[str, str]:
    repo = get_repo(service, repository)
    target = commit_sha or ref or repo.default_branch
    try:
        commit = repo.get_commit(target)
    except Exception as exc:
        raise MyGithub12Error("REF_NOT_FOUND", f"could not resolve Git ref: {target}") from exc
    resolved = str(commit.sha)
    if commit_sha and resolved != commit_sha:
        raise MyGithub12Error(
            "INDEX_COMMIT_MISMATCH",
            "resolved commit differs from requested commit",
            {"requested": commit_sha, "resolved": resolved},
        )
    if ref and commit_sha and str(repo.get_commit(ref).sha) != commit_sha:
        raise MyGithub12Error(
            "INDEX_COMMIT_MISMATCH",
            "ref no longer points to requested commit",
            {"ref": ref, "requested": commit_sha},
        )
    return {"repository": repository, "commit_sha": resolved, "tree_sha": commit_tree_sha(commit)}


def language_for_path(path: str) -> str:
    return {
        ".py": "python", ".go": "go", ".ts": "typescript", ".tsx": "typescript",
        ".js": "javascript", ".jsx": "javascript", ".vue": "vue", ".java": "java",
        ".rs": "rust", ".cs": "csharp", ".sql": "sql", ".json": "json",
        ".md": "markdown", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
        ".sh": "shell",
    }.get(PurePosixPath(path).suffix.lower(), "text")


def excluded_path(path: str) -> bool:
    parts = path.split("/")
    base = parts[-1].lower()
    return (
        any(part in EXCLUDED_PARTS for part in parts)
        or base in SENSITIVE_NAMES
        or (base.startswith(".env") and base != ".env.example")
        or base.endswith((".pem", ".p12", ".pfx", ".key", ".crt"))
    )


def decode_blob(repo: Any, blob_sha: str) -> bytes:
    blob = repo.get_git_blob(blob_sha)
    content = getattr(blob, "content", "") or ""
    if getattr(blob, "encoding", "base64") == "base64":
        return base64.b64decode(content)
    return content.encode("utf-8")


def symbol_id(repository: str, commit_sha: str, path: str, name: str, kind: str, line: int) -> str:
    raw = f"{repository}\0{commit_sha}\0{path}\0{name}\0{kind}\0{line}".encode()
    return "sym_" + hashlib.sha256(raw).hexdigest()[:24]


def extract_symbols(
    repository: str,
    commit_sha: str,
    path: str,
    blob_sha: str,
    language: str,
    content: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    lines = content.splitlines()
    if language == "python":
        try:
            tree = ast.parse(content)
        except SyntaxError:
            tree = None
        if tree is not None:
            parents: list[str] = []

            def visit(node: ast.AST) -> None:
                name = getattr(node, "name", "")
                is_class = isinstance(node, ast.ClassDef)
                is_function = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                if not (is_class or is_function):
                    return
                kind = "class" if is_class else ("method" if parents else "function")
                line = int(getattr(node, "lineno", 1))
                end = int(getattr(node, "end_lineno", line))
                bases = [ast.unparse(base) for base in node.bases] if is_class else []
                rows.append(
                    {
                        "symbol_id": symbol_id(repository, commit_sha, path, name, kind, line),
                        "name": name,
                        "qualified_name": ".".join([*parents, name]),
                        "kind": kind,
                        "language": language,
                        "path": path,
                        "blob_sha": blob_sha,
                        "start_line": line,
                        "end_line": end,
                        "signature": lines[line - 1].strip()[:1000] if lines else name,
                        "parent_name": parents[-1] if parents else None,
                        "bases_json": json.dumps(bases),
                    }
                )
                old = list(parents)
                if is_class:
                    parents.append(name)
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                        visit(child)
                parents[:] = old

            for child in ast.iter_child_nodes(tree):
                if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    visit(child)
            return rows

    patterns: list[tuple[str, re.Pattern[str]]] = []
    if language == "go":
        patterns = [
            ("function", re.compile(r"(?m)^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*\(")),
            ("type", re.compile(r"(?m)^\s*type\s+([A-Za-z_]\w*)\s+(?:struct|interface)\b")),
        ]
    elif language in {"typescript", "javascript", "vue"}:
        patterns = [
            ("function", re.compile(r"(?m)^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(")),
            ("class", re.compile(r"(?m)^\s*(?:export\s+)?class\s+([A-Za-z_$][\w$]*)(?:\s+extends\s+([\w.$]+))?")),
            ("interface", re.compile(r"(?m)^\s*(?:export\s+)?interface\s+([A-Za-z_$][\w$]*)(?:\s+extends\s+([^\{]+))?")),
        ]
    elif language == "java":
        patterns = [
            ("class", re.compile(r"(?m)^\s*(?:public\s+)?(?:abstract\s+)?class\s+([A-Za-z_]\w*)(?:\s+extends\s+([\w.$]+))?")),
            ("interface", re.compile(r"(?m)^\s*(?:public\s+)?interface\s+([A-Za-z_]\w*)")),
        ]
    elif language == "rust":
        patterns = [
            ("function", re.compile(r"(?m)^\s*(?:pub\s+)?(?:async\s+)?fn\s+([A-Za-z_]\w*)\s*\(")),
            ("type", re.compile(r"(?m)^\s*(?:pub\s+)?(?:struct|enum|trait)\s+([A-Za-z_]\w*)")),
        ]
    for kind, pattern in patterns:
        for match in pattern.finditer(content):
            name = match.group(1)
            line = content.count("\n", 0, match.start()) + 1
            bases: list[str] = []
            if match.lastindex and match.lastindex >= 2 and match.group(2):
                bases = [item for item in re.split(r"[,\s]+", match.group(2).strip()) if item]
            rows.append(
                {
                    "symbol_id": symbol_id(repository, commit_sha, path, name, kind, line),
                    "name": name,
                    "qualified_name": name,
                    "kind": kind,
                    "language": language,
                    "path": path,
                    "blob_sha": blob_sha,
                    "start_line": line,
                    "end_line": line,
                    "signature": lines[line - 1].strip()[:1000] if lines else name,
                    "parent_name": None,
                    "bases_json": json.dumps(bases),
                }
            )
    return rows


def public_job(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    return {
        key: item.get(key)
        for key in (
            "job_id", "repository", "commit_sha", "tree_sha", "index_version",
            "strategy", "base_commit_sha", "status", "step", "revision",
            "progress_current", "progress_total", "reused_file_count",
            "reindexed_file_count", "error_code", "error_message", "created_at",
            "started_at", "finished_at",
        )
    }


def public_workspace(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["scope"] = json.loads(item.pop("scope_json") or "{}")
    item["lease_valid"] = item["status"] == "active" and item["lease_expires_at"] > now()
    return item
