#!/usr/bin/env python3
"""Export the already-authenticated private CI mirror as a credential-free Git bundle.

This fixed root-owned service has no GitHub network access and never copies CI
credentials or a .git/config file. Only the P2P main Git ref is exported.
"""
import fcntl
import os
import pwd
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

SOURCE = Path("/srv/private-ci/cache/git/frankichen-p2p-server.git")
DESTINATION = Path("/var/lib/mygithub12-p2p-source")
BUNDLE = DESTINATION / "main.bundle"
AUTHORITATIVE_URL = "https://github.com/frankichen/p2p-server.git"
COMMIT_RE = re.compile(r"[0-9a-f]{40}")


def _git(*argv, timeout=75):
    result = subprocess.run(["git", *map(str, argv)], capture_output=True,
                            text=True, check=False, timeout=timeout)
    if result.returncode:
        raise RuntimeError("P2P_SOURCE_GIT_OPERATION_FAILED")
    return result.stdout.strip()


def source_main_sha(source=SOURCE):
    if source.is_symlink() or not source.is_dir() or not (source / "objects").is_dir():
        raise ValueError("P2P_CI_SOURCE_MIRROR_UNAVAILABLE")
    if _git("--git-dir=" + str(source), "remote", "get-url", "origin") != AUTHORITATIVE_URL:
        raise ValueError("P2P_CI_SOURCE_ORIGIN_MISMATCH")
    sha = _git("--git-dir=" + str(source), "rev-parse", "--verify", "refs/heads/main")
    if not COMMIT_RE.fullmatch(sha):
        raise ValueError("P2P_CI_SOURCE_MAIN_INVALID")
    if _git("--git-dir=" + str(source), "cat-file", "-t", sha) != "commit":
        raise ValueError("P2P_CI_SOURCE_COMMIT_INVALID")
    return sha


def bundle_main_sha(path):
    if path.is_symlink() or not path.is_file():
        return None
    try:
        lines = _git("bundle", "list-heads", str(path), timeout=15).splitlines()
    except (RuntimeError, subprocess.TimeoutExpired):
        return None
    if len(lines) != 1:
        return None
    parts = lines[0].split()
    if len(parts) != 2 or parts[1] != "refs/heads/main":
        return None
    return parts[0] if COMMIT_RE.fullmatch(parts[0]) else None


def create_checked_bundle(source, dest):
    expected = source_main_sha(source)
    _git("--git-dir=" + str(source), "bundle", "create",
         str(dest), "refs/heads/main", timeout=90)
    if bundle_main_sha(dest) != expected:
        raise ValueError("P2P_SOURCE_BUNDLE_SHA_MISMATCH")
    return expected


def main():
    if os.geteuid() != 0:
        raise SystemExit("P2P_EXPORT_ROOT_REQUIRED")
    group = pwd.getpwnam("xiaowu").pw_gid
    DESTINATION.mkdir(mode=0o750, parents=True, exist_ok=True)
    if DESTINATION.is_symlink() or not stat.S_ISDIR(DESTINATION.lstat().st_mode):
        raise SystemExit("P2P_EXPORT_PATH_UNSAFE")
    os.chown(DESTINATION, 0, group)
    os.chmod(DESTINATION, 0o750)
    with (DESTINATION / ".export.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        sha = source_main_sha()
        current = bundle_main_sha(BUNDLE)
        if current == sha and BUNDLE.stat().st_uid == 0 and BUNDLE.stat().st_gid == group:
            print("P2P_SOURCE_BUNDLE=already_current")
            return
        staging = Path(tempfile.mkdtemp(prefix=".p2p-source-", dir=DESTINATION))
        try:
            candidate = staging / "main.bundle"
            if create_checked_bundle(SOURCE, candidate) != sha:
                raise ValueError("P2P_CI_SOURCE_CHANGED_DURING_EXPORT")
            os.chown(candidate, 0, group)
            os.chmod(candidate, 0o640)
            os.replace(candidate, BUNDLE)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        print("P2P_SOURCE_BUNDLE=verified")


if __name__ == "__main__":
    main()
