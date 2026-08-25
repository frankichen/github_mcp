"""Allowlist-bound local bare mirrors for immutable Git object reads."""
from __future__ import annotations

import fcntl
import hashlib
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from app import mygithub12 as core
from app.github_auth import credential_provider

MyGithub12Error=core.MyGithub12Error


def _root() -> Path:
    path = Path(os.getenv("MYGITHUB12_MIRROR_ROOT", "/data/mygithub12/mirrors"))
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path, 0o700)
    except OSError as exc:
        raise MyGithub12Error(
            "MIRROR_UNAVAILABLE",
            "local Git mirror storage is unavailable",
            {"retryable": True, "error_type": type(exc).__name__},
        ) from exc
    return path


def _repo_slug(repository: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository or ""):
        raise MyGithub12Error("MIRROR_UNAVAILABLE", "invalid allowlisted repository identity")
    owner, name = repository.split("/", 1)
    if owner in {".", ".."} or name in {".", ".."}:
        raise MyGithub12Error("MIRROR_UNAVAILABLE", "invalid allowlisted repository identity")
    return repository.replace("/", "-")


def authoritative_remote(repository: str) -> str:
    # Repository authorization MUST be checked by the caller/service before this
    # function is used. No caller-supplied host or URL is accepted.
    base=os.getenv("MYGITHUB12_MIRROR_GITHUB_BASE","https://github.com").rstrip("/")
    if base != "https://github.com":
        raise MyGithub12Error("MIRROR_ORIGIN_MISMATCH","mirror GitHub base is not authoritative")
    return f"{base}/{repository}.git"


def mirror_path(repository: str) -> Path:
    return _root()/f"{_repo_slug(repository)}.git"


def _git_env() -> dict[str, str]:
    root = _root()
    askpass = root / ".git-askpass"
    if not askpass.exists():
        askpass.write_text(
            "#!/bin/sh\ncase \"$1\" in *Username*) printf '%s\\n' x-access-token ;; *) printf '%s\\n' \"$GITHUB_TOKEN\" ;; esac\n",
            encoding="utf-8",
        )
        os.chmod(askpass, 0o700)
    env = {
        "PATH": os.getenv("PATH", "/usr/bin:/bin"),
        "HOME": str(root),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": str(askpass),
        "LC_ALL": "C",
    }
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    try:
        token = credential_provider.token()
    except Exception:
        token = ""
    if token:
        env["GITHUB_TOKEN"] = token
    return env


def _run(args: list[str], *, timeout: int = 30, check: bool = True) -> subprocess.CompletedProcess[str]:
    cp=subprocess.run(args,capture_output=True,text=True,timeout=timeout,env=_git_env())
    if check and cp.returncode!=0:
        raise MyGithub12Error("MIRROR_FETCH_FAILED","local Git mirror operation failed",{"stage":args[1] if len(args)>1 else "git","exit_code":cp.returncode})
    return cp


def _lock(repository: str):
    lock_dir=_root()/".locks"; lock_dir.mkdir(mode=0o700,exist_ok=True); fh=(lock_dir/f"{_repo_slug(repository)}.lock").open("a+"); fcntl.flock(fh.fileno(),fcntl.LOCK_EX); return fh


def _quarantine(path: Path) -> None:
    if not path.exists(): return
    target=path.with_name(path.name+f".corrupt-{int(time.time())}")
    try: os.replace(path,target)
    except OSError: shutil.rmtree(path,ignore_errors=True)


def ensure_mirror(repository: str, *, fetch: bool = True) -> dict[str, Any]:
    path=mirror_path(repository); remote=authoritative_remote(repository); lock=_lock(repository)
    try:
        if path.exists():
            origin=_run(["git","--git-dir",str(path),"remote","get-url","origin"],check=False)
            if origin.returncode!=0 or origin.stdout.strip()!=remote:
                _quarantine(path); raise MyGithub12Error("MIRROR_ORIGIN_MISMATCH","local mirror origin does not match authoritative repository")
            fsck=_run(["git","--git-dir",str(path),"fsck","--connectivity-only"],timeout=60,check=False)
            if fsck.returncode!=0:
                _quarantine(path)
        if not path.exists():
            _run(["git","clone","--mirror","--",remote,str(path)],timeout=120)
        if fetch:
            _run(["git","--git-dir",str(path),"fetch","--prune","--no-tags","origin","+refs/heads/*:refs/heads/*"],timeout=120)
        generation=hashlib.sha256((repository+str(path.stat().st_mtime_ns)).encode()).hexdigest()[:16]
        return {"path":str(path),"generation":generation,"remote_identity_sha256":hashlib.sha256(remote.encode()).hexdigest(),"fetched":fetch}
    finally:
        fcntl.flock(lock.fileno(),fcntl.LOCK_UN); lock.close()


def _cat(repository: str, commit_sha: str, spec: str) -> tuple[bytes, dict[str, Any]]:
    if not __import__("re").fullmatch(r"[0-9a-f]{40}",commit_sha):
        raise MyGithub12Error("MIRROR_IDENTITY_MISMATCH","exact lowercase 40-character commit SHA is required")
    meta=ensure_mirror(repository,fetch=False); path=meta["path"]
    exists=_run(["git","--git-dir",path,"cat-file","-e",f"{commit_sha}^{{commit}}"],check=False)
    if exists.returncode!=0:
        meta=ensure_mirror(repository,fetch=True); path=meta["path"]
        if _run(["git","--git-dir",path,"cat-file","-e",f"{commit_sha}^{{commit}}"],check=False).returncode!=0:
            raise MyGithub12Error("MIRROR_OBJECT_MISSING","commit object is unavailable from authoritative mirror",{"commit_sha":commit_sha})
    cp=subprocess.run(["git","--git-dir",path,"show",f"{commit_sha}:{spec}"],capture_output=True,timeout=30)
    if cp.returncode!=0:
        raise MyGithub12Error("MIRROR_OBJECT_MISSING","file object is unavailable from exact commit",{"commit_sha":commit_sha,"path":spec})
    return cp.stdout,{"source":"mirror","mirror_generation":meta["generation"],"commit_sha":commit_sha}


def read_blob(repository: str, commit_sha: str, path: str) -> tuple[bytes, dict[str, Any]]:
    path = core._safe_path(path)
    data, evidence = _cat(repository, commit_sha, path)
    blob = _run(
        ["git", "--git-dir", str(mirror_path(repository)), "rev-parse", f"{commit_sha}:{path}"]
    ).stdout.strip()
    return data, {
        "repository": repository,
        "path": path,
        "blob_sha": blob,
        "size_bytes": len(data),
        **evidence,
    }


def read_file(repository: str, commit_sha: str, path: str) -> dict[str, Any]:
    data, evidence = read_blob(repository, commit_sha, path)
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MyGithub12Error(
            "BINARY_FILE_UNSUPPORTED", "mirror file is not UTF-8 text", {"path": path}
        ) from exc
    return {"ok": True, **evidence, "content": content}


def diff_names(repository: str, base_sha: str, head_sha: str) -> dict[str, Any]:
    meta=ensure_mirror(repository,fetch=True); path=meta["path"]
    for sha in (base_sha,head_sha):
        if not __import__("re").fullmatch(r"[0-9a-f]{40}",sha): raise MyGithub12Error("MIRROR_IDENTITY_MISMATCH","exact commit SHA required")
    cp=_run(["git","--git-dir",path,"diff","--name-status",base_sha,head_sha,"--"])
    items=[]
    for line in cp.stdout.splitlines():
        parts=line.split("\t"); items.append({"status":parts[0],"path":parts[-1]})
    return {"ok":True,"repository":repository,"base_commit_sha":base_sha,"head_commit_sha":head_sha,"files":items,"source":"mirror","mirror_generation":meta["generation"]}
