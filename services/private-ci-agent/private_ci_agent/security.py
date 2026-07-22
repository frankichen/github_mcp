"""Security validations for source archives and workspace isolation."""

import os
import re
import tarfile
import logging

logger = logging.getLogger(__name__)

MAX_FILE_COUNT = 10000
MAX_PATH_LENGTH = 256
DANGEROUS_PATTERNS = [
    re.compile(r"\.\.[/\\]"),
    re.compile(r"^[/\\]"),
]


def validate_tar_path(member_name: str) -> bool:
    if len(member_name) > MAX_PATH_LENGTH:
        return False
    for pattern in DANGEROUS_PATTERNS:
        if pattern.search(member_name):
            logger.warning("Dangerous tar path rejected: %s", member_name)
            return False
    return True


def safe_extract_tar(tar_path: str, dest_dir: str, max_bytes: int) -> tuple:
    total_bytes = 0
    file_count = 0
    os.makedirs(dest_dir, exist_ok=True)

    with tarfile.open(tar_path, "r:gz") as tar:
        for member in tar:
            if not validate_tar_path(member.name):
                raise ValueError(f"Unsafe path in archive: {member.name}")

            if member.isdir():
                continue

            if member.isfile():
                total_bytes += member.size
                if total_bytes > max_bytes:
                    raise ValueError(f"Archive exceeds max size of {max_bytes} bytes")

                file_count += 1
                if file_count > MAX_FILE_COUNT:
                    raise ValueError(f"Too many files in archive (max {MAX_FILE_COUNT})")

            parts = member.name.split("/", 1)
            if len(parts) > 1:
                member.name = parts[1]
            else:
                continue

            if member.isdir() or member.isfile():
                tar.extract(member, dest_dir)

    return total_bytes, file_count


def validate_no_sensitive_mounts(mounts: list) -> bool:
    forbidden = ["/home", "/root", "/mnt/c", "/mnt/d", "/etc", "/var/run",
                 "docker.sock", "podman.sock", ".ssh"]
    for m in mounts:
        for f in forbidden:
            if f in m:
                return False
    return True


def validate_no_token_leak(env: dict) -> bool:
    forbidden_keys = ["GITHUB_TOKEN", "ACTION_API_KEY", "CI_WORKER_TOKEN",
                      "SSH_AUTH_SOCK", "AWS", "PASSWORD", "SECRET", "TOKEN"]
    for key in env:
        for fk in forbidden_keys:
            if fk.lower() in key.lower():
                return False
    return True
