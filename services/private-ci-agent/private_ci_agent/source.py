"""Source code downloading and archive extraction.

Downloads source archives from the German CI controller (which proxies GitHub).
"""

import hashlib
import logging
import os
import socket
import time
import urllib.request
import urllib.error
import subprocess
import fcntl
import shutil
import re

from private_ci_agent.security import safe_extract_tar

logger = logging.getLogger(__name__)

DOWNLOAD_CONNECT_TIMEOUT = 20
DOWNLOAD_TOTAL_TIMEOUT = 300
DOWNLOAD_MAX_RETRIES = 2
DOWNLOAD_RETRY_BACKOFF = [5, 15]
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _authoritative_repository_url(repository: str) -> str | None:
    """Return the fixed GitHub URL for a repository already authorized by Controller."""
    if not REPOSITORY_RE.fullmatch(repository):
        return None
    owner, name = repository.split("/", 1)
    if owner in {".", ".."} or name in {".", ".."}:
        return None
    return f"https://github.com/{repository}.git"


def _error_code(error: Exception) -> str:
    """Map source download exceptions to stable, non-sensitive codes."""
    if isinstance(error, SourceDownloadTimeout):
        return "SOURCE_DOWNLOAD_TIMEOUT"
    if isinstance(error, ProxyUnavailableError):
        return "PROXY_UNAVAILABLE"
    if isinstance(error, urllib.error.HTTPError):
        return f"SOURCE_HTTP_{error.code}"
    if isinstance(error, DownloadError):
        return "SOURCE_DOWNLOAD_FAILED"
    return "SOURCE_DOWNLOAD_FAILED"


def prepare_source_from_mirror(repository: str, commit_sha: str, dest_dir: str, mirror_root: str) -> dict:
    """Refresh an independent bare mirror and create an exact detached worktree."""
    authoritative_url = _authoritative_repository_url(repository)
    if not authoritative_url:
        return {"ok": False, "error_code": "SOURCE_REPOSITORY_NOT_ALLOWED"}
    mirror = os.path.join(mirror_root, repository.replace("/", "-") + ".git")
    os.makedirs(mirror_root, mode=0o700, exist_ok=True)
    lock_path = mirror + ".lock"
    try:
        with open(lock_path, "a+") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"ok": False, "error_code": "SOURCE_MIRROR_LOCK_TIMEOUT"}
            if not os.path.isdir(os.path.join(mirror, "objects")):
                result = subprocess.run(["git", "clone", "--mirror", authoritative_url, mirror], capture_output=True, text=True, timeout=180)
                if result.returncode:
                    return {"ok": False, "error_code": "SOURCE_MIRROR_FETCH_FAILED", "message": result.stderr[-500:]}
            origin = subprocess.run(["git", "-C", mirror, "remote", "get-url", "origin"], capture_output=True, text=True, timeout=20)
            if origin.returncode or origin.stdout.strip() != authoritative_url:
                return {"ok": False, "error_code": "SOURCE_ORIGIN_MISMATCH"}
            fetched = subprocess.run(["git", "-C", mirror, "remote", "update", "--prune"], capture_output=True, text=True, timeout=180)
            if fetched.returncode:
                return {"ok": False, "error_code": "SOURCE_MIRROR_FETCH_FAILED", "message": fetched.stderr[-500:]}
            exists = subprocess.run(["git", "-C", mirror, "cat-file", "-e", f"{commit_sha}^{{commit}}"], capture_output=True, text=True, timeout=20)
            if exists.returncode:
                return {"ok": False, "error_code": "SOURCE_COMMIT_NOT_FOUND"}
            head = subprocess.run(["git", "-C", mirror, "rev-parse", commit_sha], capture_output=True, text=True, timeout=20).stdout.strip()
            if head != commit_sha:
                return {"ok": False, "error_code": "SOURCE_HEAD_MISMATCH", "head": head}
            if os.path.exists(dest_dir):
                shutil.rmtree(dest_dir)
            os.makedirs(os.path.dirname(dest_dir), mode=0o700, exist_ok=True)
            subprocess.run(["git", "-C", mirror, "worktree", "prune"], capture_output=True, text=True, timeout=20)
            checked = subprocess.run(["git", "-C", mirror, "worktree", "add", "--detach", dest_dir, commit_sha], capture_output=True, text=True, timeout=120)
            if checked.returncode:
                return {"ok": False, "error_code": "SOURCE_WORKTREE_CREATE_FAILED", "message": checked.stderr[-500:]}
            actual = subprocess.run(["git", "-C", dest_dir, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=20).stdout.strip()
            dirty = subprocess.run(["git", "-C", dest_dir, "status", "--porcelain"], capture_output=True, text=True, timeout=20).stdout.strip()
            if actual != commit_sha:
                return {"ok": False, "error_code": "SOURCE_HEAD_MISMATCH", "head": actual}
            if dirty:
                return {"ok": False, "error_code": "SOURCE_WORKTREE_DIRTY"}
            return {"ok": True, "mirror": mirror, "source_hit": True, "head": actual, "clean": True}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error_code": "SOURCE_MIRROR_FETCH_FAILED"}
    except OSError as exc:
        return {"ok": False, "error_code": "SOURCE_MIRROR_FETCH_FAILED", "message": type(exc).__name__}


def remove_source_worktree(dest_dir: str, mirror_root: str):
    repository = ""
    try:
        git_common = subprocess.run(["git", "-C", dest_dir, "rev-parse", "--git-common-dir"], capture_output=True, text=True, timeout=20)
        if git_common.returncode == 0:
            common_dir = os.path.realpath(os.path.join(dest_dir, git_common.stdout.strip()))
            if common_dir.endswith(".git"):
                repository = os.path.basename(common_dir)[:-4]
    except Exception:
        repository = ""
    mirror = os.path.join(mirror_root, (repository or "frankichen-sxt") + ".git")
    if not os.path.isdir(mirror):
        return
    subprocess.run(["git", "-C", mirror, "worktree", "remove", "--force", dest_dir], capture_output=True, text=True, timeout=30)


class DownloadError(Exception):
    pass


class SourceDownloadTimeout(DownloadError):
    pass


class ProxyUnavailableError(DownloadError):
    pass


def _is_proxy_error(error: Exception) -> bool:
    if isinstance(error, urllib.error.HTTPError) and error.code in (502, 503, 504):
        return True
    if "ProxyError" in str(type(error).__name__):
        return True
    msg = str(error).lower()
    return any(k in msg for k in ["proxy", "connection refused", "cannot connect", "tunnel", "407"])


def download_source_archive(controller_url: str, worker_id: str, worker_token: str,
                            job_id: str, dest_file: str, max_bytes: int,
                            connect_timeout: int = DOWNLOAD_CONNECT_TIMEOUT,
                            total_timeout: int = DOWNLOAD_TOTAL_TIMEOUT) -> tuple:
    """Download source tarball from the CI controller and return (sha256, file_size).

    Implements connect timeout, total timeout, and retry with backoff.
    Raises SourceDownloadTimeout or ProxyUnavailableError for specific failure modes.
    """
    url = f"{controller_url}/internal/ci/jobs/{job_id}/source/download"

    last_error = None

    for attempt in range(DOWNLOAD_MAX_RETRIES + 1):
        try:
            return _attempt_download(url, worker_id, worker_token, dest_file,
                                     max_bytes, connect_timeout, total_timeout,
                                     attempt)
        except DownloadError:
            raise
        except Exception as e:
            last_error = e
            if attempt < DOWNLOAD_MAX_RETRIES:
                delay = DOWNLOAD_RETRY_BACKOFF[min(attempt, len(DOWNLOAD_RETRY_BACKOFF) - 1)]
                logger.warning("Download attempt %d/%d failed: %s - retrying in %ds",
                               attempt + 1, DOWNLOAD_MAX_RETRIES + 1, e, delay)
                time.sleep(delay)
            else:
                break

    if _is_proxy_error(last_error):
        raise ProxyUnavailableError(f"Proxy unavailable after {DOWNLOAD_MAX_RETRIES + 1} attempts: {last_error}")

    if isinstance(last_error, urllib.error.HTTPError):
        raise DownloadError(f"Controller download failed: HTTP {last_error.code}: {last_error}")

    if isinstance(last_error, TimeoutError):
        raise SourceDownloadTimeout(f"Source download timed out after {total_timeout}s")

    if isinstance(last_error, socket.timeout):
        raise SourceDownloadTimeout(f"Source download timed out: {last_error}")

    raise DownloadError(f"Source download failed: {last_error}")


def _attempt_download(url: str, worker_id: str, worker_token: str, dest_file: str,
                      max_bytes: int, connect_timeout: int, total_timeout: int,
                      attempt: int) -> tuple:
    logger.info("Download attempt %d: %s", attempt + 1, url)

    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {worker_token}")
    req.add_header("X-Worker-ID", worker_id)
    req.add_header("User-Agent", "private-ci-agent/1.0")

    try:
        with urllib.request.urlopen(req, timeout=connect_timeout) as resp:
            sha256_expected = resp.headers.get("X-SHA256", "")

            sha256_actual = hashlib.sha256()
            total = 0
            start_time = time.time()

            with open(dest_file, "wb") as f:
                while True:
                    elapsed = time.time() - start_time
                    if elapsed > total_timeout:
                        raise SourceDownloadTimeout(
                            f"Download exceeded total timeout of {total_timeout}s at {total} bytes"
                        )

                    try:
                        chunk = resp.read(65536)
                    except socket.timeout:
                        raise SourceDownloadTimeout(f"Download read timeout after {elapsed:.1f}s")

                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError(f"Source archive exceeds max size of {max_bytes} bytes")
                    sha256_actual.update(chunk)
                    f.write(chunk)

            actual_hex = sha256_actual.hexdigest()
            if sha256_expected and actual_hex != sha256_expected:
                raise ValueError(
                    f"SHA256 mismatch: expected {sha256_expected[:16]}..., got {actual_hex[:16]}..."
                )

            logger.info("Downloaded %d bytes in %.1fs", total, time.time() - start_time)
            return actual_hex, total

    except urllib.error.HTTPError as e:
        body = e.read().decode()[:200]
        logger.error("HTTP %d on download: %s", e.code, body)
        if e.code in (502, 503, 504):
            raise ProxyUnavailableError(f"Controller gateway error HTTP {e.code}")
        if e.code == 407:
            raise ProxyUnavailableError("Proxy authentication required (HTTP 407)")
        raise
    except (SourceDownloadTimeout, ProxyUnavailableError, DownloadError):
        raise
    except socket.timeout:
        raise SourceDownloadTimeout("Connection timed out during source download")
    except Exception as e:
        logger.error("Download error: %s", e)
        raise


def extract_source(archive_path: str, dest_dir: str, sha256_expected: str, max_bytes: int) -> tuple:
    """Validate SHA256 and extract the source archive."""
    actual_sha256 = hashlib.sha256()
    with open(archive_path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            actual_sha256.update(chunk)

    actual_hex = actual_sha256.hexdigest()
    if actual_hex != sha256_expected:
        raise ValueError(f"SHA256 mismatch: expected {sha256_expected[:16]}..., got {actual_hex[:16]}...")

    total_bytes, file_count = safe_extract_tar(archive_path, dest_dir, max_bytes)
    logger.info("Extracted %d bytes, %d files to %s", total_bytes, file_count, dest_dir)
    return total_bytes, file_count
