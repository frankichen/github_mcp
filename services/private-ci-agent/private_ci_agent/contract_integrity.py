"""Trusted host-side product contract integrity gate."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


PRODUCT_CONTRACT_DIR = "docs/product-contracts/"
PRODUCT_CONTRACT_README = f"{PRODUCT_CONTRACT_DIR}README.md"
PRODUCT_CONTRACT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
PRODUCT_CONTRACT_VERSION_RE = re.compile(
    r"^docs/product-contracts/(?P<domain>[^/]+)/"
    r"(?P<date>\d{4}-\d{2}-\d{2})-(?P<revision>r[0-9A-Za-z._]+)-"
    r"(?P<slug>[^/]+)\.md$"
)
PRODUCT_CONTRACT_REQUIRED_METADATA = (
    "contract_id",
    "contract_revision",
    "approved_source",
    "supersedes",
)
PRODUCT_CONTRACT_IMPLEMENTATION_PREFIXES = (
    "cmd/",
    "internal/",
    "api/openapi/",
    "db/migrations/",
    "h5/",
    "tests/",
    "tools/",
    "scripts/",
)
PRODUCT_CONTRACT_FILENAME_TOKENS = ("需求", "契约", "协议", "规范", "产品规则")


def _git(source_dir: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", source_dir, *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _git_text(source_dir: str, revision: str, path: str) -> str:
    result = _git(source_dir, "show", f"{revision}:{path}")
    return result.stdout if result.returncode == 0 else ""


def _changed_entries(
    source_dir: str, base_sha: str, head_sha: str
) -> tuple[list[tuple[str, str]], str | None]:
    result = _git(
        source_dir,
        "diff",
        "--name-status",
        "--find-renames",
        "-z",
        f"{base_sha}...{head_sha}",
    )
    if result.returncode != 0:
        return [], (result.stderr or "git diff failed")[-300:]
    entries: list[tuple[str, str]] = []
    fields = iter(result.stdout.rstrip("\0").split("\0"))
    for status in fields:
        if status.startswith("R"):
            entries.extend((("D", next(fields)), ("A", next(fields))))
        elif status.startswith("C"):
            next(fields)
            entries.append(("A", next(fields)))
        else:
            entries.append((status[:1], next(fields)))
    return entries, None


def _is_frozen_contract(path: str, text: str) -> bool:
    if not text or not path.endswith(".md") or path == PRODUCT_CONTRACT_README:
        return False
    header = "\n".join(text.splitlines()[:80])
    if re.search(r"(?mi)^\s*contract_status\s*:\s*frozen\s*$", header):
        return True
    if re.search(r"(?mi)^\s*-?\s*文档状态\s*[：:]\s*.*target_contract", header):
        return True
    if not any(token in Path(path).name for token in PRODUCT_CONTRACT_FILENAME_TOKENS):
        return False
    return bool(
        re.search(
            r"(?mi)^\s*>?\s*(?:文档)?状态\s*[：:]\s*.*"
            r"(?:已确认|已冻结|冻结|已定稿|产品依据|开发和验收依据|开发与验收依据)",
            header,
        )
    )


def _metadata(text: str, key: str) -> str:
    match = re.search(rf"(?mi)^\s*{re.escape(key)}\s*:\s*(.+?)\s*$", text)
    return match.group(1).strip() if match else ""


def _error(code: str, path: str, message: str) -> dict:
    return {"code": code, "path": path, "message": message}


def _validate_new_contract(path: str, text: str) -> list[dict]:
    match = PRODUCT_CONTRACT_VERSION_RE.match(path)
    if not match:
        return [
            _error(
                "PRODUCT_CONTRACT_VERSION_PATH_REQUIRED",
                path,
                "new frozen contract must use the versioned product-contract path",
            )
        ]
    findings = []
    missing = [key for key in PRODUCT_CONTRACT_REQUIRED_METADATA if not _metadata(text, key)]
    if _metadata(text, "contract_status").lower() != "frozen":
        missing.append("contract_status: frozen")
    if missing:
        findings.append(
            _error(
                "PRODUCT_CONTRACT_METADATA_INVALID",
                path,
                "missing/invalid: " + ", ".join(missing),
            )
        )
    revision = _metadata(text, "contract_revision")
    if revision and revision != match.group("revision"):
        findings.append(
            _error(
                "PRODUCT_CONTRACT_REVISION_MISMATCH",
                path,
                f"filename revision {match.group('revision')} != contract_revision {revision}",
            )
        )
    if _metadata(text, "approved_source").lower() in {"tbd", "todo", "unknown", "待确认"}:
        findings.append(
            _error(
                "PRODUCT_CONTRACT_METADATA_INVALID",
                path,
                "approved_source must reference a real product decision/source",
            )
        )
    return findings


def verify_product_contract_integrity(
    repository: str, source_dir: str, base_sha: str, head_sha: str
) -> dict:
    """Reject rewrites of frozen contracts and mixed contract/implementation revisions."""
    del repository  # Reserved for future repository-specific policy.
    if not (Path(source_dir) / PRODUCT_CONTRACT_README).is_file():
        return {"ok": True, "applicable": False, "errors": [], "checked_entries": 0}
    errors = []
    if not PRODUCT_CONTRACT_SHA_RE.fullmatch(base_sha or ""):
        errors.append(
            _error(
                "CONTRACT_BASE_SHA_REQUIRED",
                PRODUCT_CONTRACT_DIR,
                "exact base SHA is required",
            )
        )
        return {"ok": False, "applicable": True, "errors": errors, "checked_entries": 0}
    if not PRODUCT_CONTRACT_SHA_RE.fullmatch(head_sha or ""):
        errors.append(
            _error(
                "CONTRACT_HEAD_SHA_REQUIRED",
                PRODUCT_CONTRACT_DIR,
                "exact head SHA is required",
            )
        )
        return {"ok": False, "applicable": True, "errors": errors, "checked_entries": 0}
    actual_head = _git(source_dir, "rev-parse", "HEAD")
    if actual_head.returncode != 0 or actual_head.stdout.strip() != head_sha:
        errors.append(
            _error(
                "CONTRACT_SOURCE_HEAD_MISMATCH",
                PRODUCT_CONTRACT_DIR,
                "source checkout does not match requested head SHA",
            )
        )
        return {"ok": False, "applicable": True, "errors": errors, "checked_entries": 0}
    if _git(source_dir, "cat-file", "-e", f"{base_sha}^{{commit}}").returncode != 0:
        errors.append(
            _error(
                "CONTRACT_BASE_COMMIT_UNAVAILABLE",
                PRODUCT_CONTRACT_DIR,
                "base commit is unavailable in trusted source mirror",
            )
        )
        return {"ok": False, "applicable": True, "errors": errors, "checked_entries": 0}
    if _git(source_dir, "merge-base", "--is-ancestor", base_sha, head_sha).returncode != 0:
        errors.append(
            _error(
                "CONTRACT_BASE_NOT_ANCESTOR",
                PRODUCT_CONTRACT_DIR,
                "base SHA is not an ancestor of head SHA",
            )
        )
        return {"ok": False, "applicable": True, "errors": errors, "checked_entries": 0}

    entries, diff_error = _changed_entries(source_dir, base_sha, head_sha)
    if diff_error:
        errors.append(_error("CONTRACT_DIFF_UNAVAILABLE", PRODUCT_CONTRACT_DIR, diff_error))
        return {"ok": False, "applicable": True, "errors": errors, "checked_entries": 0}

    new_contracts = set()
    for status, path in entries:
        previous_text = _git_text(source_dir, base_sha, path)
        current_text = "" if status == "D" else _git_text(source_dir, head_sha, path)
        if previous_text and _is_frozen_contract(path, previous_text) and current_text != previous_text:
            errors.append(
                _error(
                    "HISTORICAL_CONTRACT_MUTATION",
                    path,
                    "frozen product contract is immutable; create a new revision",
                )
            )
        if status == "A" and _is_frozen_contract(path, current_text):
            if not path.startswith(PRODUCT_CONTRACT_DIR):
                errors.append(
                    _error(
                        "PRODUCT_CONTRACT_VERSION_PATH_REQUIRED",
                        path,
                        "new frozen contracts must use the versioned product-contract path",
                    )
                )
            elif path != PRODUCT_CONTRACT_README:
                new_contracts.add(path)
                errors.extend(_validate_new_contract(path, current_text))
        elif path.startswith(PRODUCT_CONTRACT_DIR) and path != PRODUCT_CONTRACT_README:
            if status == "D":
                errors.append(
                    _error(
                        "HISTORICAL_CONTRACT_MUTATION",
                        path,
                        "versioned product contract cannot be deleted",
                    )
                )
            elif status in {"A", "M"}:
                new_contracts.add(path)
                errors.extend(_validate_new_contract(path, current_text))

    implementation_paths = sorted(
        {
            path
            for status, path in entries
            if status in {"A", "M", "D"}
            and path.startswith(PRODUCT_CONTRACT_IMPLEMENTATION_PREFIXES)
        }
    )
    if new_contracts and implementation_paths:
        errors.append(
            _error(
                "CONTRACT_AND_IMPLEMENTATION_MIXED",
                sorted(new_contracts)[0],
                "contract-only revision cannot change implementation; first path: "
                + implementation_paths[0],
            )
        )
    return {
        "ok": not errors,
        "applicable": True,
        "errors": errors,
        "checked_entries": len(entries),
        "new_contract_revisions": sorted(new_contracts),
    }
