"""MyGithut12 immutable code index and multi-window development workspaces.

GitHub is always the source of truth. SQLite stores rebuildable exact-commit
indexes and shared workspace coordination state; it never becomes a source
repository and never accepts shell commands, arbitrary Git URLs or host paths.
"""
from __future__ import annotations

import ast
import base64
import fnmatch
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
MAX_INDEX_FILES = int(os.getenv("MYGITHUB12_INDEX_MAX_FILES", "5000"))
MAX_INDEX_BYTES = int(os.getenv("MYGITHUB12_INDEX_MAX_BYTES", str(50 * 1024 * 1024)))
MAX_FILE_BYTES = int(os.getenv("MYGITHUB12_INDEX_MAX_FILE_BYTES", str(512 * 1024)))
MAX_RESULTS = 500
MAX_BATCH_FILES = 100
MAX_BATCH_BYTES = 4 * 1024 * 1024
DEFAULT_LEASE_SECONDS = 1800
MAX_LEASE_SECONDS = 14400
_EXCLUDED = {".git", "node_modules", "vendor", "dist", "build", "target", ".next", ".venv", "venv", "__pycache__"}
_SENSITIVE = {".env", "id_rsa", "id_ed25519", "credentials", "credentials.json", "secrets.json", "private.key", "server.key"}
_LOCK = threading.RLock()


class MyGithub12Error(Exception):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details or {}
        self.trace_id = str(uuid.uuid4())


def _now() -> float:
    return time.time()


def _db_path() -> str:
    configured = os.getenv("MYGITHUB12_DB_PATH", "").strip()
    if configured:
        return configured
    legacy = os.getenv("IDEMPOTENCY_DB_PATH", "/data/idempotency.db")
    return os.path.join(os.path.dirname(legacy) or "/data", "mygithub12.db")


def _db() -> sqlite3.Connection:
    path = _db_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=30000")
    return db


def init_db() -> None:
    with _LOCK, _db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS indexes(
          repository TEXT, commit_sha TEXT, tree_sha TEXT, version TEXT,
          status TEXT, strategy TEXT, base_commit_sha TEXT, file_count INTEGER,
          symbol_count INTEGER, size_bytes INTEGER, created_at REAL, accessed_at REAL,
          manifest_json TEXT, PRIMARY KEY(repository,commit_sha,tree_sha,version));
        CREATE TABLE IF NOT EXISTS files(
          repository TEXT, commit_sha TEXT, path TEXT, blob_sha TEXT, size_bytes INTEGER,
          language TEXT, content_sha256 TEXT, line_count INTEGER, content TEXT,
          PRIMARY KEY(repository,commit_sha,path));
        CREATE INDEX IF NOT EXISTS idx_files_blob ON files(repository,commit_sha,blob_sha);
        CREATE TABLE IF NOT EXISTS symbols(
          repository TEXT, commit_sha TEXT, symbol_id TEXT, name TEXT, qualified_name TEXT,
          kind TEXT, language TEXT, path TEXT, blob_sha TEXT, start_line INTEGER,
          end_line INTEGER, signature TEXT, parent_name TEXT, bases_json TEXT,
          PRIMARY KEY(repository,commit_sha,symbol_id));
        CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(repository,commit_sha,name);
        CREATE TABLE IF NOT EXISTS jobs(
          job_id TEXT PRIMARY KEY, repository TEXT, commit_sha TEXT, tree_sha TEXT,
          version TEXT, strategy TEXT, base_commit_sha TEXT, status TEXT, step TEXT,
          revision INTEGER, progress_current INTEGER, progress_total INTEGER,
          reused_files INTEGER, reindexed_files INTEGER, cancel_requested INTEGER,
          error_code TEXT, error_message TEXT, created_at REAL, started_at REAL,
          finished_at REAL, idempotency_key TEXT);
        CREATE INDEX IF NOT EXISTS idx_jobs_target ON jobs(repository,commit_sha,status);
        CREATE UNIQUE INDEX IF NOT EXISTS uq_active_index_job
          ON jobs(repository,commit_sha,tree_sha,version)
          WHERE status IN ('queued','running');
        CREATE UNIQUE INDEX IF NOT EXISTS uq_index_job_idempotency ON jobs(repository,idempotency_key) WHERE idempotency_key IS NOT NULL;
        CREATE TABLE IF NOT EXISTS workspaces(
          workspace_id TEXT PRIMARY KEY, repository TEXT, branch TEXT, base_branch TEXT,
          base_commit_sha TEXT, head_sha TEXT, tree_sha TEXT, status TEXT, revision INTEGER,
          owner TEXT, lease_expires_at REAL, index_commit_sha TEXT, scope_json TEXT,
          drift_reason TEXT, pr_number INTEGER, created_at REAL, updated_at REAL);
        CREATE UNIQUE INDEX IF NOT EXISTS uq_workspace_branch ON workspaces(repository,branch)
          WHERE status IN ('active','drifted');
        """)


def _safe_path(path: str, empty: bool = False) -> str:
    if not path and empty:
        return ""
    if not path or path.startswith("/") or "\\" in path or any(p in {"", ".", ".."} for p in path.split("/")):
        raise MyGithub12Error("INVALID_REPOSITORY_PATH", "path must be a safe repository-relative path")
    if any(ord(ch) < 32 for ch in path):
        raise MyGithub12Error("INVALID_REPOSITORY_PATH", "path contains control characters")
    return path


def _parse(value: str, expected: type, name: str, default: Any) -> Any:
    if not value:
        return default
    try:
        item = json.loads(value)
    except json.JSONDecodeError as exc:
        raise MyGithub12Error("SEARCH_QUERY_INVALID", f"{name} must be valid JSON", {"position": exc.pos}) from exc
    if not isinstance(item, expected):
        raise MyGithub12Error("SEARCH_QUERY_INVALID", f"{name} has the wrong JSON type")
    return item


def _service_repo(service: Any, repository: str):
    service._check_repository_allowed(repository)
    raw = service.client.client if hasattr(service.client, "client") else service.client
    github = getattr(raw, "_pygithub", None) or getattr(service.client, "_pygithub", None)
    if github is None:
        raise MyGithub12Error("GITHUB_NOT_CONFIGURED", "GitHub client is unavailable")
    return github.get_repo(repository)


def _tree_sha(commit: Any) -> str:
    for value in (
        getattr(getattr(getattr(commit, "commit", None), "tree", None), "sha", None),
        getattr(getattr(commit, "tree", None), "sha", None),
    ):
        if value:
            return str(value)
    raise MyGithub12Error("INDEX_SNAPSHOT_UNAVAILABLE", "commit tree SHA is unavailable")


def resolve_identity(service: Any, repository: str, commit_sha: str = "", ref: str = "") -> dict[str, str]:
    repo = _service_repo(service, repository)
    target = commit_sha or ref or repo.default_branch
    try:
        commit = repo.get_commit(target)
    except Exception as exc:
        raise MyGithub12Error("REF_NOT_FOUND", f"could not resolve Git ref: {target}") from exc
    resolved = str(commit.sha)
    if commit_sha and resolved != commit_sha:
        raise MyGithub12Error("INDEX_COMMIT_MISMATCH", "resolved commit differs from requested commit", {"requested": commit_sha, "resolved": resolved})
    if ref and commit_sha and str(repo.get_commit(ref).sha) != commit_sha:
        raise MyGithub12Error("INDEX_COMMIT_MISMATCH", "ref no longer points to requested commit", {"ref": ref})
    return {"repository": repository, "commit_sha": resolved, "tree_sha": _tree_sha(commit)}


def _lang(path: str) -> str:
    return {".py":"python", ".go":"go", ".ts":"typescript", ".tsx":"typescript", ".js":"javascript", ".jsx":"javascript", ".vue":"vue", ".java":"java", ".rs":"rust", ".cs":"csharp", ".sql":"sql", ".json":"json", ".md":"markdown", ".yaml":"yaml", ".yml":"yaml", ".toml":"toml", ".sh":"shell"}.get(PurePosixPath(path).suffix.lower(), "text")


def _excluded(path: str) -> bool:
    parts = path.split("/")
    base = parts[-1].lower()
    return any(p in _EXCLUDED for p in parts) or base in _SENSITIVE or (base.startswith(".env") and base != ".env.example") or base.endswith((".pem", ".p12", ".pfx", ".key", ".crt"))


def _decode_blob(repo: Any, sha: str) -> bytes:
    blob = repo.get_git_blob(sha)
    content = getattr(blob, "content", "") or ""
    return base64.b64decode(content) if getattr(blob, "encoding", "base64") == "base64" else content.encode()


def _symbol_id(repository: str, commit_sha: str, path: str, name: str, kind: str, line: int) -> str:
    raw = f"{repository}\0{commit_sha}\0{path}\0{name}\0{kind}\0{line}".encode()
    return "sym_" + hashlib.sha256(raw).hexdigest()[:24]


def _symbols(repository: str, commit_sha: str, path: str, blob_sha: str, language: str, content: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    lines = content.splitlines()
    if language == "python":
        try:
            tree = ast.parse(content)
        except SyntaxError:
            tree = None
        if tree:
            parents: list[str] = []
            def visit(node: ast.AST) -> None:
                name = getattr(node, "name", "") if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) else ""
                kind = "class" if isinstance(node, ast.ClassDef) else ("method" if parents and name else "function" if name else "")
                bases = [ast.unparse(b) for b in node.bases] if isinstance(node, ast.ClassDef) else []
                if name:
                    line = int(getattr(node, "lineno", 1)); end = int(getattr(node, "end_lineno", line))
                    out.append({"symbol_id":_symbol_id(repository,commit_sha,path,name,kind,line),"name":name,"qualified_name":".".join([*parents,name]),"kind":kind,"language":language,"path":path,"blob_sha":blob_sha,"start_line":line,"end_line":end,"signature":lines[line-1].strip()[:1000] if lines else name,"parent_name":parents[-1] if parents else None,"bases_json":json.dumps(bases)})
                next_parents = [*parents, name] if isinstance(node, ast.ClassDef) and name else parents
                old = parents[:]; parents[:] = next_parents
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)): visit(child)
                parents[:] = old
            for n in ast.iter_child_nodes(tree):
                if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)): visit(n)
            return out
    patterns: list[tuple[str,re.Pattern[str]]] = []
    if language == "go": patterns=[("function",re.compile(r"(?m)^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*\(")),("type",re.compile(r"(?m)^\s*type\s+([A-Za-z_]\w*)\s+(?:struct|interface)\b"))]
    elif language in {"typescript","javascript","vue"}: patterns=[("function",re.compile(r"(?m)^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(")),("class",re.compile(r"(?m)^\s*(?:export\s+)?class\s+([A-Za-z_$][\w$]*)(?:\s+extends\s+([\w.$]+))?")),("interface",re.compile(r"(?m)^\s*(?:export\s+)?interface\s+([A-Za-z_$][\w$]*)(?:\s+extends\s+([^\{]+))?"))]
    elif language == "java": patterns=[("class",re.compile(r"(?m)^\s*(?:public\s+)?(?:abstract\s+)?class\s+([A-Za-z_]\w*)(?:\s+extends\s+([\w.$]+))?")),("interface",re.compile(r"(?m)^\s*(?:public\s+)?interface\s+([A-Za-z_]\w*)"))]
    elif language == "rust": patterns=[("function",re.compile(r"(?m)^\s*(?:pub\s+)?(?:async\s+)?fn\s+([A-Za-z_]\w*)\s*\(")),("type",re.compile(r"(?m)^\s*(?:pub\s+)?(?:struct|enum|trait)\s+([A-Za-z_]\w*)"))]
    for kind, pattern in patterns:
        for m in pattern.finditer(content):
            name=m.group(1); line=content.count("\n",0,m.start())+1; bases=[]
            if m.lastindex and m.lastindex >= 2 and m.group(2): bases=[x.strip() for x in re.split(r"[,\s]+",m.group(2)) if x.strip()]
            out.append({"symbol_id":_symbol_id(repository,commit_sha,path,name,kind,line),"name":name,"qualified_name":name,"kind":kind,"language":language,"path":path,"blob_sha":blob_sha,"start_line":line,"end_line":line,"signature":lines[line-1].strip()[:1000] if lines else name,"parent_name":None,"bases_json":json.dumps(bases)})
    return out


def _index_row(db: sqlite3.Connection, identity: dict[str,str]) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM indexes WHERE repository=? AND commit_sha=? AND tree_sha=? AND version=?",(identity["repository"],identity["commit_sha"],identity["tree_sha"],INDEX_VERSION)).fetchone()


def request_index_build(service: Any, repository: str, commit_sha: str, strategy: str="auto", base_commit_sha: str="", priority: str="interactive", idempotency_key: str="", force: bool=False) -> dict[str,Any]:
    del priority
    if strategy not in {"auto","incremental","full"}: raise MyGithub12Error("SEARCH_QUERY_INVALID","strategy must be auto, incremental, or full")
    identity=resolve_identity(service,repository,commit_sha=commit_sha); init_db()
    with _LOCK,_db() as db:
        existing=_index_row(db,identity)
        if existing and existing["status"]=="ready" and not force:
            return {"ok":True,"status":"completed","deduplicated":True,"job_id":None,**identity,"index_version":INDEX_VERSION}
        if idempotency_key:
            row=db.execute("SELECT * FROM jobs WHERE repository=? AND idempotency_key=? ORDER BY created_at DESC LIMIT 1",(repository,idempotency_key)).fetchone()
            if row: return {"ok":True,"deduplicated":True,**_public_job(dict(row))}
        running=db.execute("SELECT * FROM jobs WHERE repository=? AND commit_sha=? AND status IN ('queued','running') ORDER BY created_at DESC LIMIT 1",(repository,commit_sha)).fetchone()
        if running: return {"ok":True,"deduplicated":True,**_public_job(dict(running))}
        job_id=str(uuid.uuid4()); now=_now()
        try:
            db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(job_id,repository,identity["commit_sha"],identity["tree_sha"],INDEX_VERSION,strategy,base_commit_sha or None,"queued","queued",1,0,0,0,0,0,None,None,now,None,None,idempotency_key or None))
        except sqlite3.IntegrityError:
            row=db.execute("SELECT * FROM jobs WHERE repository=? AND commit_sha=? AND tree_sha=? AND version=? AND status IN ('queued','running') ORDER BY created_at DESC LIMIT 1",(repository,identity["commit_sha"],identity["tree_sha"],INDEX_VERSION)).fetchone()
            if row: return {"ok":True,"deduplicated":True,**_public_job(dict(row))}
            raise
    worker=threading.Thread(target=_run_index_build,args=(service,identity,job_id,strategy,base_commit_sha),name=f"mygithub12-index-{job_id[:8]}",daemon=True)
    worker.start()
    return {"ok":True,"deduplicated":False,**get_index_job(job_id)}


def _job_cancelled(job_id: str) -> bool:
    with _db() as db:
        row=db.execute("SELECT cancel_requested FROM jobs WHERE job_id=?",(job_id,)).fetchone()
    return bool(row and row["cancel_requested"])


def _finish_cancelled_job(job_id: str) -> None:
    with _LOCK,_db() as db:
        db.execute("UPDATE jobs SET status='cancelled',step='cancelled',finished_at=?,revision=revision+1 WHERE job_id=?",(_now(),job_id))


def _run_index_build(service: Any, identity: dict[str,str], job_id: str, strategy: str, base_commit_sha: str) -> None:
    repository=identity["repository"]; commit_sha=identity["commit_sha"]
    try:
        with _LOCK,_db() as db:
            db.execute("UPDATE jobs SET status='running',step='snapshot',started_at=?,revision=revision+1 WHERE job_id=?",(_now(),job_id))
        repo=_service_repo(service,repository); commit=repo.get_commit(identity["commit_sha"]); tree=repo.get_git_tree(_tree_sha(commit),recursive=True)
        entries=[e for e in tree.tree if e.type=="blob" and not _excluded(str(e.path)) and int(getattr(e,"size",0) or 0)<=MAX_FILE_BYTES]
        if len(entries)>MAX_INDEX_FILES: raise MyGithub12Error("INDEX_QUOTA_EXCEEDED","repository file limit exceeded",{"limit":MAX_INDEX_FILES})
        with _LOCK,_db() as db:
            db.execute("UPDATE jobs SET step='indexing',progress_total=?,revision=revision+1 WHERE job_id=?",(len(entries),job_id))
        total=0; symbol_count=0; languages=set(); reused=0; reindexed=0
        base_files: dict[str,sqlite3.Row]={}
        if base_commit_sha and strategy in {"auto","incremental"}:
            with _db() as db: base_files={r["path"]:r for r in db.execute("SELECT * FROM files WHERE repository=? AND commit_sha=?",(repository,base_commit_sha))}
        file_rows=[]; symbol_rows=[]
        for idx,e in enumerate(entries,1):
            if _job_cancelled(job_id):
                _finish_cancelled_job(job_id)
                return
            size=int(getattr(e,"size",0) or 0)
            if total+size>MAX_INDEX_BYTES: raise MyGithub12Error("INDEX_QUOTA_EXCEEDED","repository byte limit exceeded",{"limit":MAX_INDEX_BYTES})
            base=base_files.get(str(e.path)); language=_lang(str(e.path))
            if base and base["blob_sha"]==e.sha:
                content=base["content"]; digest=base["content_sha256"]; line_count=base["line_count"]; reused+=1
            else:
                data=_decode_blob(repo,e.sha)
                try: content=data.decode("utf-8")
                except UnicodeDecodeError:
                    with _LOCK,_db() as db: db.execute("UPDATE jobs SET progress_current=?,revision=revision+1 WHERE job_id=?",(idx,job_id))
                    continue
                digest=hashlib.sha256(data).hexdigest(); line_count=len(content.splitlines()); reindexed+=1
            content_bytes=len(content.encode()); total+=content_bytes; languages.add(language); syms=_symbols(repository,commit_sha,str(e.path),e.sha,language,content); symbol_count+=len(syms)
            file_rows.append((repository,commit_sha,str(e.path),e.sha,content_bytes,language,digest,line_count,content))
            for s in syms:
                symbol_rows.append((repository,commit_sha,s["symbol_id"],s["name"],s["qualified_name"],s["kind"],s["language"],s["path"],s["blob_sha"],s["start_line"],s["end_line"],s["signature"],s["parent_name"],s["bases_json"]))
            with _LOCK,_db() as db:
                db.execute("UPDATE jobs SET progress_current=?,revision=revision+1 WHERE job_id=?",(idx,job_id))
        if _job_cancelled(job_id):
            _finish_cancelled_job(job_id)
            return
        manifest={**identity,"index_version":INDEX_VERSION,"file_count":len(file_rows),"symbol_count":symbol_count,"size_bytes":total,"languages":sorted(languages),"reused_file_count":reused,"reindexed_file_count":reindexed,"build_strategy":"incremental" if reused else "full","base_commit_sha":base_commit_sha or None}
        with _LOCK,_db() as db:
            now=_now()
            db.execute("DELETE FROM files WHERE repository=? AND commit_sha=?",(repository,commit_sha))
            db.execute("DELETE FROM symbols WHERE repository=? AND commit_sha=?",(repository,commit_sha))
            db.executemany("INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?)",file_rows)
            db.executemany("INSERT INTO symbols VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",symbol_rows)
            db.execute("INSERT OR REPLACE INTO indexes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",(repository,commit_sha,identity["tree_sha"],INDEX_VERSION,"ready",manifest["build_strategy"],base_commit_sha or None,len(file_rows),symbol_count,total,now,now,json.dumps(manifest)))
            db.execute("UPDATE jobs SET status='completed',step='completed',progress_current=progress_total,reused_files=?,reindexed_files=?,finished_at=?,revision=revision+1 WHERE job_id=?",(reused,reindexed,now,job_id))
    except MyGithub12Error as exc:
        with _LOCK,_db() as db: db.execute("UPDATE jobs SET status='failed',step='failed',error_code=?,error_message=?,finished_at=?,revision=revision+1 WHERE job_id=?",(exc.code,exc.message,_now(),job_id))
    except Exception as exc:
        with _LOCK,_db() as db: db.execute("UPDATE jobs SET status='failed',step='failed',error_code='INDEX_BUILD_FAILED',error_message=?,finished_at=?,revision=revision+1 WHERE job_id=?",(str(exc)[:1000],_now(),job_id))


def _public_job(row: dict[str,Any]) -> dict[str,Any]:
    return {k:row.get(k) for k in ("job_id","repository","commit_sha","tree_sha","version","strategy","base_commit_sha","status","step","revision","progress_current","progress_total","reused_files","reindexed_files","error_code","error_message","created_at","started_at","finished_at")}


def get_index_job(job_id: str) -> dict[str,Any]:
    init_db()
    with _db() as db: row=db.execute("SELECT * FROM jobs WHERE job_id=?",(job_id,)).fetchone()
    if not row: raise MyGithub12Error("INDEX_NOT_FOUND","index job was not found",{"job_id":job_id})
    return {"ok":True,**_public_job(dict(row))}


def wait_index_job(job_id: str, timeout_seconds: int=55, last_known_revision: int=0, last_known_status: str="", last_known_step: str="") -> dict[str,Any]:
    deadline=time.monotonic()+max(0,min(timeout_seconds,55))
    while True:
        item=get_index_job(job_id)
        if item["status"] in {"completed","failed","cancelled"} or item["revision"]!=last_known_revision or item["status"]!=last_known_status or item["step"]!=last_known_step: item["timed_out"]=False; return item
        if time.monotonic()>=deadline: item["timed_out"]=True; return item
        time.sleep(.25)


def cancel_index_job(job_id: str) -> dict[str,Any]:
    item=get_index_job(job_id)
    if item["status"] in {"completed","failed","cancelled"}: return {**item,"cancelled":False}
    with _LOCK,_db() as db: db.execute("UPDATE jobs SET cancel_requested=1,revision=revision+1 WHERE job_id=?",(job_id,))
    return {**get_index_job(job_id),"cancelled":True}


def get_index_status(service: Any, repository: str, commit_sha: str="", ref: str="") -> dict[str,Any]:
    identity=resolve_identity(service,repository,commit_sha,ref); init_db()
    with _LOCK,_db() as db:
        row=_index_row(db,identity)
        if row: db.execute("UPDATE indexes SET accessed_at=? WHERE repository=? AND commit_sha=? AND tree_sha=? AND version=?",(_now(),repository,identity["commit_sha"],identity["tree_sha"],INDEX_VERSION))
    if not row: return {"ok":True,**identity,"index_version":INDEX_VERSION,"status":"not_found"}
    return {"ok":True,**json.loads(row["manifest_json"]),"status":row["status"],"created_at":row["created_at"],"last_accessed_at":row["accessed_at"]}


def list_indexes(service: Any, repository: str, limit: int=50, offset: int=0) -> dict[str,Any]:
    _service_repo(service,repository); init_db(); limit=max(1,min(limit,100)); offset=max(0,offset)
    with _db() as db:
        total=db.execute("SELECT COUNT(*) FROM indexes WHERE repository=?",(repository,)).fetchone()[0]
        rows=db.execute("SELECT * FROM indexes WHERE repository=? ORDER BY accessed_at DESC LIMIT ? OFFSET ?",(repository,limit,offset)).fetchall()
        pins={r[0] for r in db.execute("SELECT DISTINCT index_commit_sha FROM workspaces WHERE repository=? AND status IN ('active','drifted') AND index_commit_sha IS NOT NULL",(repository,))}
    return {"ok":True,"repository":repository,"items":[{"commit_sha":r["commit_sha"],"tree_sha":r["tree_sha"],"index_version":r["version"],"status":r["status"],"file_count":r["file_count"],"symbol_count":r["symbol_count"],"size_bytes":r["size_bytes"],"created_at":r["created_at"],"last_accessed_at":r["accessed_at"],"pinned_by_workspace":r["commit_sha"] in pins} for r in rows],"total":total,"limit":limit,"offset":offset}


def _ready(service: Any, repository: str, commit_sha: str) -> dict[str,str]:
    identity=resolve_identity(service,repository,commit_sha=commit_sha); init_db()
    with _db() as db: row=_index_row(db,identity)
    if not row: raise MyGithub12Error("INDEX_NOT_FOUND","repository index is not built",{"commit_sha":commit_sha})
    if row["status"]!="ready": raise MyGithub12Error("INDEX_NOT_READY","repository index is not ready")
    return identity


def _recursive_tree(service: Any, repository: str, commit_sha: str):
    identity=resolve_identity(service,repository,commit_sha=commit_sha); repo=_service_repo(service,repository); commit=repo.get_commit(commit_sha)
    return identity,list(repo.get_git_tree(_tree_sha(commit),recursive=True).tree)


def list_repository_tree(service: Any, repository: str, commit_sha: str, path: str="", max_depth: int=5, include_globs_json: str="[]", exclude_globs_json: str="[]", limit: int=500, cursor: str="") -> dict[str,Any]:
    path=_safe_path(path,True); includes=_parse(include_globs_json,list,"include_globs_json",[]); excludes=_parse(exclude_globs_json,list,"exclude_globs_json",[]); identity,entries=_recursive_tree(service,repository,commit_sha); prefix=path.rstrip("/")+("/" if path else ""); out=[]
    for e in entries:
        p=str(e.path)
        if prefix and p!=path and not p.startswith(prefix): continue
        rel=p[len(prefix):] if prefix else p
        if rel.count("/")>max(0,min(max_depth,20)) or _excluded(p): continue
        if includes and not any(fnmatch.fnmatch(p,x) for x in includes): continue
        if excludes and any(fnmatch.fnmatch(p,x) for x in excludes): continue
        out.append({"path":p,"type":e.type,"sha":e.sha,"size":int(getattr(e,"size",0) or 0),"language":_lang(p) if e.type=="blob" else None})
    out.sort(key=lambda x:x["path"]); off=int(cursor or 0); limit=max(1,min(limit,2000))
    return {"ok":True,**identity,"items":out[off:off+limit],"total":len(out),"next_cursor":str(off+limit) if off+limit<len(out) else None}


def search_repository_files(service: Any, repository: str, commit_sha: str, query: str, path_prefix: str="", extensions_json: str="[]", limit: int=100, cursor: str="") -> dict[str,Any]:
    if not query.strip(): raise MyGithub12Error("SEARCH_QUERY_INVALID","query cannot be empty")
    ext={str(x).lower().lstrip(".") for x in _parse(extensions_json,list,"extensions_json",[])}; prefix=_safe_path(path_prefix,True); identity,entries=_recursive_tree(service,repository,commit_sha); q=query.lower(); out=[]
    for e in entries:
        p=str(e.path)
        if e.type!="blob" or _excluded(p) or (prefix and p!=prefix and not p.startswith(prefix.rstrip("/")+"/")): continue
        suffix=PurePosixPath(p).suffix.lower().lstrip(".")
        if ext and suffix not in ext: continue
        at=p.lower().find(q)
        if at>=0: out.append({"path":p,"blob_sha":e.sha,"size_bytes":int(getattr(e,"size",0) or 0),"language":_lang(p),"match_start":at,"match_end":at+len(query)})
    out.sort(key=lambda x:(x["match_start"],x["path"])); off=int(cursor or 0); limit=max(1,min(limit,500))
    return {"ok":True,**identity,"items":out[off:off+limit],"total":len(out),"next_cursor":str(off+limit) if off+limit<len(out) else None}


def get_files_batch(service: Any, repository: str, commit_sha: str, paths_json: str, include_content: bool=True, max_total_bytes: int=MAX_BATCH_BYTES) -> dict[str,Any]:
    paths=_parse(paths_json,list,"paths_json",[])
    if not paths or len(paths)>MAX_BATCH_FILES: raise MyGithub12Error("BATCH_FILE_LIMIT_EXCEEDED","invalid batch path count",{"max_files":MAX_BATCH_FILES})
    identity=resolve_identity(service,repository,commit_sha=commit_sha); repo=_service_repo(service,repository); used=0; items=[]; max_total_bytes=max(1,min(max_total_bytes,MAX_BATCH_BYTES))
    for raw in paths:
        path=_safe_path(str(raw))
        try:
            e=repo.get_contents(path,ref=commit_sha)
            if isinstance(e,list): raise ValueError("path is a directory")
            data=bytes(e.decoded_content)
            if used+len(data)>max_total_bytes: items.append({"path":path,"ok":False,"error_code":"BATCH_TOTAL_BYTES_EXCEEDED"}); continue
            used+=len(data)
            try: text=data.decode(); binary=False
            except UnicodeDecodeError: text=""; binary=True
            items.append({"path":path,"ok":True,"blob_sha":e.sha,"size_bytes":len(data),"content_sha256":hashlib.sha256(data).hexdigest(),"binary":binary,"content":text if include_content and not binary else None,"truncated":False})
        except Exception as exc: items.append({"path":path,"ok":False,"error_code":"FILE_NOT_FOUND","message":str(exc)[:300]})
    return {"ok":True,**identity,"items":items,"total_bytes":used}


def _file_rows(repository: str, commit_sha: str, globs: list[str] | None=None) -> list[sqlite3.Row]:
    with _db() as db: rows=db.execute("SELECT * FROM files WHERE repository=? AND commit_sha=? ORDER BY path",(repository,commit_sha)).fetchall()
    return [r for r in rows if not globs or any(fnmatch.fnmatch(r["path"],g) for g in globs)]


def search_text(service: Any, repository: str, commit_sha: str, query: str, regex: bool=False, case_sensitive: bool=False, path_globs_json: str="[]", context_lines: int=2, limit: int=100, cursor: str="") -> dict[str,Any]:
    identity=_ready(service,repository,commit_sha); globs=_parse(path_globs_json,list,"path_globs_json",[])
    if not query or len(query)>1000: raise MyGithub12Error("SEARCH_QUERY_INVALID","query length is invalid")
    try: pattern=re.compile(query if regex else re.escape(query),0 if case_sensitive else re.I)
    except re.error as exc: raise MyGithub12Error("SEARCH_REGEX_INVALID","regular expression is invalid",{"error":str(exc)}) from exc
    out=[]; context_lines=max(0,min(context_lines,10))
    for row in _file_rows(repository,commit_sha,globs):
        lines=row["content"].splitlines()
        for line_no,line in enumerate(lines,1):
            for m in pattern.finditer(line):
                start=max(1,line_no-context_lines); end=min(len(lines),line_no+context_lines)
                out.append({"path":row["path"],"blob_sha":row["blob_sha"],"line":line_no,"start_line":start,"end_line":end,"match_start":m.start(),"match_end":m.end(),"snippet":"\n".join(lines[start-1:end])})
                if len(out)>=5000: break
    off=int(cursor or 0); limit=max(1,min(limit,MAX_RESULTS))
    return {"ok":True,**identity,"items":out[off:off+limit],"total":len(out),"next_cursor":str(off+limit) if off+limit<len(out) else None}


def search_semantic(service: Any, repository: str, commit_sha: str, query: str, path_globs_json: str="[]", limit: int=20, cursor: str="") -> dict[str,Any]:
    identity=_ready(service,repository,commit_sha); globs=_parse(path_globs_json,list,"path_globs_json",[]); tokens={t.lower() for t in re.findall(r"[\w\u4e00-\u9fff]+",query) if len(t)>1}
    if not tokens: raise MyGithub12Error("SEARCH_QUERY_INVALID","semantic query has no searchable terms")
    out=[]
    for row in _file_rows(repository,commit_sha,globs):
        lines=row["content"].splitlines()
        for i in range(0,len(lines),20):
            chunk="\n".join(lines[i:i+20]); words={t.lower() for t in re.findall(r"[\w\u4e00-\u9fff]+",chunk)}; overlap=tokens & words
            if overlap: out.append({"path":row["path"],"blob_sha":row["blob_sha"],"start_line":i+1,"end_line":min(len(lines),i+20),"score":round(len(overlap)/len(tokens),4),"matched_terms":sorted(overlap),"snippet":chunk[:4000],"authoritative":False})
    out.sort(key=lambda x:(-x["score"],x["path"],x["start_line"])); off=int(cursor or 0); limit=max(1,min(limit,100))
    return {"ok":True,**identity,"items":out[off:off+limit],"total":len(out),"next_cursor":str(off+limit) if off+limit<len(out) else None,"authoritative":False}


def search_symbols(service: Any, repository: str, commit_sha: str, query: str, kinds_json: str="[]", languages_json: str="[]", path_prefix: str="", limit: int=100, cursor: str="") -> dict[str,Any]:
    identity=_ready(service,repository,commit_sha); kinds=set(_parse(kinds_json,list,"kinds_json",[])); langs=set(_parse(languages_json,list,"languages_json",[])); prefix=_safe_path(path_prefix,True); q=query.lower();
    with _db() as db: rows=[dict(r) for r in db.execute("SELECT * FROM symbols WHERE repository=? AND commit_sha=? ORDER BY name,path,start_line",(repository,commit_sha))]
    rows=[r for r in rows if (not q or q in r["name"].lower() or q in r["qualified_name"].lower()) and (not kinds or r["kind"] in kinds) and (not langs or r["language"] in langs) and (not prefix or r["path"]==prefix or r["path"].startswith(prefix.rstrip("/")+"/"))]
    for r in rows: r["bases"]=json.loads(r.pop("bases_json") or "[]")
    off=int(cursor or 0); limit=max(1,min(limit,MAX_RESULTS))
    return {"ok":True,**identity,"items":rows[off:off+limit],"total":len(rows),"next_cursor":str(off+limit) if off+limit<len(rows) else None}


def _get_symbol(repository: str, commit_sha: str, symbol_id: str="", path: str="", line: int=0) -> dict[str,Any]:
    with _db() as db:
        if symbol_id: rows=db.execute("SELECT * FROM symbols WHERE repository=? AND commit_sha=? AND symbol_id=?",(repository,commit_sha,symbol_id)).fetchall()
        else: rows=db.execute("SELECT * FROM symbols WHERE repository=? AND commit_sha=? AND path=? AND start_line<=? AND end_line>=? ORDER BY end_line-start_line",(repository,commit_sha,path,line,line)).fetchall()
    if not rows: raise MyGithub12Error("SYMBOL_NOT_FOUND","symbol was not found")
    if len(rows)>1 and symbol_id: raise MyGithub12Error("SYMBOL_AMBIGUOUS","symbol ID is ambiguous")
    r=dict(rows[0]); r["bases"]=json.loads(r.pop("bases_json") or "[]"); return r


def get_symbol_definition(service: Any, repository: str, commit_sha: str, symbol_id: str="", path: str="", line: int=0, column: int=0) -> dict[str,Any]:
    del column; identity=_ready(service,repository,commit_sha); symbol=_get_symbol(repository,commit_sha,symbol_id,_safe_path(path) if path else "",line)
    return {"ok":True,**identity,"definition":symbol,"authoritative":symbol["language"]=="python"}


def find_references(service: Any, repository: str, commit_sha: str, symbol_id: str, include_definition: bool=False, limit: int=100, cursor: str="") -> dict[str,Any]:
    identity=_ready(service,repository,commit_sha); symbol=_get_symbol(repository,commit_sha,symbol_id); pattern=re.compile(r"\b"+re.escape(symbol["name"])+r"\b"); out=[]
    for row in _file_rows(repository,commit_sha):
        for n,line in enumerate(row["content"].splitlines(),1):
            for m in pattern.finditer(line):
                is_def=row["path"]==symbol["path"] and symbol["start_line"]<=n<=symbol["end_line"]
                if is_def and not include_definition: continue
                tail=line[m.end():].lstrip(); kind="call" if tail.startswith("(") else "definition" if is_def else "unknown"
                out.append({"path":row["path"],"blob_sha":row["blob_sha"],"line":n,"column":m.start()+1,"reference_kind":kind,"snippet":line[:1000],"reliability":"exact_lexical","authoritative":False})
    off=int(cursor or 0); limit=max(1,min(limit,MAX_RESULTS))
    return {"ok":True,**identity,"symbol":{"symbol_id":symbol_id,"name":symbol["name"]},"items":out[off:off+limit],"total":len(out),"next_cursor":str(off+limit) if off+limit<len(out) else None,"authoritative":False}


def call_hierarchy(service: Any, repository: str, commit_sha: str, symbol_id: str, direction: str="both", depth: int=2, limit: int=200) -> dict[str,Any]:
    if direction not in {"callers","callees","both"}: raise MyGithub12Error("SEARCH_QUERY_INVALID","direction must be callers, callees, or both")
    identity=_ready(service,repository,commit_sha); root=_get_symbol(repository,commit_sha,symbol_id); nodes={symbol_id:{"symbol_id":symbol_id,"name":root["name"],"path":root["path"],"start_line":root["start_line"]}}; edges=[]
    if direction in {"callers","both"}:
        refs=find_references(service,repository,commit_sha,symbol_id,False,limit,"")
        with _db() as db: all_syms=[dict(r) for r in db.execute("SELECT * FROM symbols WHERE repository=? AND commit_sha=?",(repository,commit_sha))]
        for r in refs["items"]:
            if r["reference_kind"]!="call": continue
            candidates=[s for s in all_syms if s["path"]==r["path"] and s["start_line"]<=r["line"]<=s["end_line"]]
            if candidates:
                caller=min(candidates,key=lambda s:s["end_line"]-s["start_line"]); nodes[caller["symbol_id"]]={"symbol_id":caller["symbol_id"],"name":caller["name"],"path":caller["path"],"start_line":caller["start_line"]}; edges.append({"from":caller["symbol_id"],"to":symbol_id,"kind":"lexical_call","line":r["line"],"reliability":"heuristic"})
    if direction in {"callees","both"}:
        with _db() as db: row=db.execute("SELECT content FROM files WHERE repository=? AND commit_sha=? AND path=?",(repository,commit_sha,root["path"])).fetchone(); syms=[dict(r) for r in db.execute("SELECT * FROM symbols WHERE repository=? AND commit_sha=?",(repository,commit_sha))]
        if row:
            segment="\n".join(row["content"].splitlines()[root["start_line"]-1:root["end_line"]])
            for name in set(re.findall(r"\b([A-Za-z_]\w*)\s*\(",segment)):
                target=next((s for s in syms if s["name"]==name and s["symbol_id"]!=symbol_id),None)
                if target: nodes[target["symbol_id"]]={"symbol_id":target["symbol_id"],"name":target["name"],"path":target["path"],"start_line":target["start_line"]}; edges.append({"from":symbol_id,"to":target["symbol_id"],"kind":"lexical_call","reliability":"heuristic"})
                if len(edges)>=limit: break
    return {"ok":True,**identity,"root_symbol_id":symbol_id,"direction":direction,"depth_requested":depth,"nodes":list(nodes.values()),"edges":edges[:limit],"truncated":len(edges)>limit,"authoritative":False}


def symbol_implementations(service: Any, repository: str, commit_sha: str, symbol_id: str) -> dict[str,Any]:
    identity=_ready(service,repository,commit_sha); root=_get_symbol(repository,commit_sha,symbol_id)
    with _db() as db: rows=[dict(r) for r in db.execute("SELECT * FROM symbols WHERE repository=? AND commit_sha=?",(repository,commit_sha))]
    items=[]
    for r in rows:
        bases=json.loads(r["bases_json"] or "[]")
        if root["name"] in bases or any(b.endswith("."+root["name"]) for b in bases): r.pop("bases_json",None); r["bases"]=bases; r["evidence"]="declared_base"; items.append(r)
    return {"ok":True,**identity,"symbol":{"symbol_id":symbol_id,"name":root["name"]},"items":items,"authoritative":root["language"]=="python"}


def symbol_type_hierarchy(service: Any, repository: str, commit_sha: str, symbol_id: str, direction: str="both") -> dict[str,Any]:
    if direction not in {"parents","children","both"}: raise MyGithub12Error("SEARCH_QUERY_INVALID","direction must be parents, children, or both")
    identity=_ready(service,repository,commit_sha); root=_get_symbol(repository,commit_sha,symbol_id)
    with _db() as db: rows=[dict(r) for r in db.execute("SELECT * FROM symbols WHERE repository=? AND commit_sha=? AND kind IN ('class','interface','type')",(repository,commit_sha))]
    nodes=[{"symbol_id":symbol_id,"name":root["name"],"path":root["path"]}]; edges=[]
    if direction in {"parents","both"}:
        for base in root["bases"]:
            target=next((r for r in rows if r["name"]==base or r["qualified_name"]==base),None); nodes.append({"symbol_id":target["symbol_id"] if target else None,"name":base,"path":target["path"] if target else None}); edges.append({"from":symbol_id,"to":target["symbol_id"] if target else base,"kind":"extends"})
    if direction in {"children","both"}:
        for r in rows:
            bases=json.loads(r["bases_json"] or "[]")
            if root["name"] in bases: nodes.append({"symbol_id":r["symbol_id"],"name":r["name"],"path":r["path"]}); edges.append({"from":r["symbol_id"],"to":symbol_id,"kind":"extends"})
    return {"ok":True,**identity,"nodes":nodes,"edges":edges}


def symbol_diagnostics(service: Any, repository: str, commit_sha: str, symbol_id: str="", path: str="") -> dict[str,Any]:
    identity=_ready(service,repository,commit_sha)
    if symbol_id: path=_get_symbol(repository,commit_sha,symbol_id)["path"]
    path=_safe_path(path)
    with _db() as db: row=db.execute("SELECT * FROM files WHERE repository=? AND commit_sha=? AND path=?",(repository,commit_sha,path)).fetchone()
    if not row: raise MyGithub12Error("FILE_NOT_FOUND","indexed file was not found")
    diagnostics=[]; lang=row["language"]
    if lang=="python":
        try: ast.parse(row["content"])
        except SyntaxError as exc: diagnostics.append({"severity":"error","code":"PYTHON_SYNTAX_ERROR","message":exc.msg,"line":exc.lineno,"column":exc.offset})
    elif lang=="json":
        try: json.loads(row["content"])
        except json.JSONDecodeError as exc: diagnostics.append({"severity":"error","code":"JSON_PARSE_ERROR","message":exc.msg,"line":exc.lineno,"column":exc.colno})
    return {"ok":True,**identity,"path":path,"blob_sha":row["blob_sha"],"symbol_id":symbol_id or None,"diagnostics":diagnostics,"authoritative":lang in {"python","json"}}


def symbol_history(service: Any, repository: str, commit_sha: str, symbol_id: str, limit: int=30) -> dict[str,Any]:
    identity=_ready(service,repository,commit_sha); symbol=_get_symbol(repository,commit_sha,symbol_id); repo=_service_repo(service,repository); limit=max(1,min(limit,100)); events=[]
    try:
        for c in list(repo.get_commits(sha=commit_sha,path=symbol["path"]))[:limit]: events.append({"commit_sha":c.sha,"path":symbol["path"],"message":getattr(getattr(c,"commit",None),"message","")[:500],"event":"file_changed","evidence":"git_path_history"})
    except Exception as exc: raise MyGithub12Error("SYMBOL_HISTORY_LIMIT_EXCEEDED","symbol history could not be read",{"cause":type(exc).__name__}) from exc
    return {"ok":True,**identity,"symbol":{"symbol_id":symbol_id,"name":symbol["name"],"path":symbol["path"]},"events":events,"authoritative":False}


def dependency_graph(service: Any, repository: str, commit_sha: str, path_prefix: str="", symbol_id: str="", depth: int=2, limit: int=500) -> dict[str,Any]:
    del depth; identity=_ready(service,repository,commit_sha); prefix=_safe_path(path_prefix,True)
    if symbol_id: prefix=str(PurePosixPath(_get_symbol(repository,commit_sha,symbol_id)["path"]).parent); prefix="" if prefix=="." else prefix
    nodes={}; edges=[]
    for row in _file_rows(repository,commit_sha):
        if prefix and row["path"]!=prefix and not row["path"].startswith(prefix.rstrip("/")+"/"): continue
        nodes[row["path"]]={"id":row["path"],"kind":"file","language":row["language"]}
        patterns=[r"(?m)^\s*(?:from|import)\s+([\w./-]+)", r"(?m)^\s*import\s+.*?from\s+['\"]([^'\"]+)"]
        for pat in patterns:
            for m in re.finditer(pat,row["content"]):
                target=m.group(1); edges.append({"from":row["path"],"to":target,"kind":"import","evidence":{"line":row["content"].count("\n",0,m.start())+1},"authoritative":False})
                if len(edges)>=limit: break
    return {"ok":True,**identity,"nodes":list(nodes.values()),"edges":edges[:limit],"truncated":len(edges)>limit,"authoritative":False}


def agent_instructions(service: Any, repository: str, commit_sha: str, target_paths_json: str="[]") -> dict[str,Any]:
    identity=_ready(service,repository,commit_sha); targets=[_safe_path(str(x)) for x in _parse(target_paths_json,list,"target_paths_json",[])]; known={"AGENTS.md","CLAUDE.md","CONTRIBUTING.md","README.md"}; items=[]
    for row in _file_rows(repository,commit_sha):
        base=PurePosixPath(row["path"]).name
        if base not in known: continue
        parent=str(PurePosixPath(row["path"]).parent); parent="" if parent=="." else parent
        applies=not targets or any(not parent or t==parent or t.startswith(parent+"/") for t in targets)
        if applies: items.append({"path":row["path"],"blob_sha":row["blob_sha"],"scope":parent or "repository","priority":row["path"].count("/")+1,"content":row["content"][:20000]})
    items.sort(key=lambda x:(x["priority"],x["path"])); return {"ok":True,**identity,"target_paths":targets,"instructions":items}


def repository_context_pack(service: Any, repository: str, commit_sha: str, task: str, seed_paths_json: str="[]", seed_symbols_json: str="[]", max_files: int=30, max_total_bytes: int=512000, include_tests: bool=True, include_docs: bool=True) -> dict[str,Any]:
    identity=_ready(service,repository,commit_sha); seeds=[_safe_path(str(x)) for x in _parse(seed_paths_json,list,"seed_paths_json",[])]; sym_ids=_parse(seed_symbols_json,list,"seed_symbols_json",[]); reasons={p:"explicit_seed" for p in seeds}
    for sid in sym_ids:
        try: reasons[_get_symbol(repository,commit_sha,str(sid))["path"]]="symbol_definition"
        except MyGithub12Error: pass
    terms={t.lower() for t in re.findall(r"[\w\u4e00-\u9fff]+",task) if len(t)>2}
    for row in _file_rows(repository,commit_sha):
        p=row["path"]; lower=(p+" "+row["content"][:5000]).lower()
        if terms and any(t in lower for t in terms): reasons.setdefault(p,"task_term_match")
        if include_tests and ("test" in p.lower() or p.endswith("_test.go")): reasons.setdefault(p,"test_candidate")
        if include_docs and p.lower().endswith(".md"): reasons.setdefault(p,"documentation")
    chosen=[]; used=0; max_files=max(1,min(max_files,100)); max_total_bytes=max(1,min(max_total_bytes,4*1024*1024)); rows={r["path"]:r for r in _file_rows(repository,commit_sha)}
    for p,reason in reasons.items():
        row=rows.get(p)
        if not row: continue
        data=row["content"].encode()
        if len(chosen)>=max_files or used+len(data)>max_total_bytes: continue
        chosen.append({"path":p,"blob_sha":row["blob_sha"],"reason":reason,"content":row["content"],"size_bytes":len(data)}); used+=len(data)
    return {"ok":True,**identity,"task":task,"items":chosen,"total_files":len(chosen),"total_bytes":used,"omitted_count":max(0,len(reasons)-len(chosen))}


def _compare(service: Any, repository: str, base: str, head: str) -> dict[str,Any]:
    repo=_service_repo(service,repository)
    try: cmp=repo.compare(base,head)
    except Exception as exc: raise MyGithub12Error("IMPACT_ANALYSIS_INCOMPLETE","commit comparison failed") from exc
    files=[]
    for f in cmp.files: files.append({"path":f.filename,"status":f.status,"additions":f.additions,"deletions":f.deletions,"changes":f.changes,"patch":getattr(f,"patch",None)})
    return {"base_commit_sha":str(cmp.merge_base_commit.sha) if getattr(cmp,"merge_base_commit",None) else base,"head_commit_sha":head,"ahead_by":getattr(cmp,"ahead_by",None),"behind_by":getattr(cmp,"behind_by",None),"files":files}


def affected_tests(service: Any, repository: str, head_commit_sha: str, base_commit_sha: str="", paths_json: str="[]", symbol_ids_json: str="[]", patch: str="") -> dict[str,Any]:
    del symbol_ids_json; changed=[_safe_path(str(x)) for x in _parse(paths_json,list,"paths_json",[])]
    if base_commit_sha: changed.extend(f["path"] for f in _compare(service,repository,base_commit_sha,head_commit_sha)["files"])
    changed.extend(re.findall(r"(?m)^\+\+\+ b/(.+)$",patch)); changed=sorted(set(changed)); candidates=[]
    for row in _file_rows(repository,head_commit_sha):
        p=row["path"]; is_test=("/tests/" in "/"+p.lower() or PurePosixPath(p).name.startswith("test_") or p.endswith("_test.go") or p.endswith((".spec.ts",".test.ts",".spec.js",".test.js")))
        if not is_test: continue
        score=0; reasons=[]
        for c in changed:
            if str(PurePosixPath(c).parent)==str(PurePosixPath(p).parent): score+=3; reasons.append("same_directory")
            if PurePosixPath(c).stem.replace("_test","") in p: score+=2; reasons.append("name_relation")
        if score: candidates.append({"path":p,"score":score,"reasons":sorted(set(reasons))})
    candidates.sort(key=lambda x:(-x["score"],x["path"])); return {"ok":True,"repository":repository,"head_commit_sha":head_commit_sha,"base_commit_sha":base_commit_sha or None,"changed_paths":changed,"tests":candidates,"authoritative":False}


def contract_changes(service: Any, repository: str, base_commit_sha: str, head_commit_sha: str) -> dict[str,Any]:
    comparison=_compare(service,repository,base_commit_sha,head_commit_sha); items=[]
    for f in comparison["files"]:
        p=f["path"].lower(); kind=None
        if "openapi" in p or p.endswith(("swagger.yaml","swagger.json")): kind="api"
        elif "migration" in p or p.endswith(".sql"): kind="database"
        elif p.endswith((".env.example","config.py","config.go","application.yml","application.yaml")): kind="configuration"
        elif "permission" in p or "auth" in p: kind="authorization"
        elif p.endswith(("schema.json",".proto")): kind="schema"
        if kind: items.append({"path":f["path"],"kind":kind,"classification":"unknown","status":f["status"],"evidence":"changed_contract_file"})
    return {"ok":True,"repository":repository,**comparison,"changes":items,"summary":{"breaking":0,"compatible":0,"unknown":len(items)}}


def change_impact(service: Any, repository: str, base_commit_sha: str, head_commit_sha: str) -> dict[str,Any]:
    comparison=_compare(service,repository,base_commit_sha,head_commit_sha); paths=[f["path"] for f in comparison["files"]]; tests=affected_tests(service,repository,head_commit_sha,base_commit_sha); contracts=contract_changes(service,repository,base_commit_sha,head_commit_sha)
    modules=sorted({p.split("/",1)[0] for p in paths}); return {"ok":True,"repository":repository,**comparison,"changed_paths":paths,"affected_modules":modules,"affected_tests":tests["tests"],"contract_changes":contracts["changes"],"complete":True}


def change_context_pack(service: Any, repository: str, base_commit_sha: str, head_commit_sha: str, task: str="", max_files: int=50, max_total_bytes: int=1048576) -> dict[str,Any]:
    impact=change_impact(service,repository,base_commit_sha,head_commit_sha); paths=impact["changed_paths"]+[x["path"] for x in impact["affected_tests"]]
    pack=repository_context_pack(service,repository,head_commit_sha,task,json.dumps(paths),"[]",max_files,max_total_bytes,True,True); pack["base_commit_sha"]=base_commit_sha; pack["impact_summary"]={"changed_files":len(impact["changed_paths"]),"affected_tests":len(impact["affected_tests"]),"contract_changes":len(impact["contract_changes"])}; return pack


def analyze_patch(service: Any, repository: str, base_commit_sha: str, patch: str) -> dict[str,Any]:
    resolve_identity(service,repository,commit_sha=base_commit_sha)
    if not patch or len(patch.encode())>262144: raise MyGithub12Error("PATCH_ANALYSIS_INVALID","patch is empty or too large")
    paths=re.findall(r"(?m)^\+\+\+ (?:b/)?(.+)$",patch); paths=[p for p in paths if p!="/dev/null"]
    tests=affected_tests(service,repository,base_commit_sha,paths_json=json.dumps(paths),patch=patch); contract_candidates=[]
    for p in paths:
        lp=p.lower()
        if "migration" in lp or lp.endswith(".sql"): contract_candidates.append({"path":p,"kind":"database","classification":"unknown"})
        elif "openapi" in lp or "swagger" in lp: contract_candidates.append({"path":p,"kind":"api","classification":"unknown"})
    return {"ok":True,"repository":repository,"base_commit_sha":base_commit_sha,"applicable":"unknown","changed_paths":paths,"affected_tests":tests["tests"],"contract_changes":contract_candidates,"diagnostics":[],"authoritative":False}




from app.mygithub12_workspace import (
    create_workspace, get_workspace, list_workspaces, renew_workspace_lease,
    refresh_workspace, close_workspace, declare_workspace_scope, workspace_overlap,
    workspace_sync_plan, workspace_write_preflight, workspace_write_complete,
)
