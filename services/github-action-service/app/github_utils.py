#!/usr/bin/env python3
"""Extended GitHub utility functions for MCP tools.

Uses PyGithub to provide additional GitHub API capabilities beyond basic file operations.
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Any
import re
import hashlib
import time
import os
import subprocess
import tempfile
import yaml
from functools import lru_cache

import requests

from github import Github
from github.GithubException import GithubException, RateLimitExceededException
from github.Repository import Repository
from github.PullRequest import PullRequest

from app.config import settings
from app.exceptions import GitHubApiError, RateLimitError

logger = logging.getLogger(__name__)

def _error_response(code: str, message: str, retryable: bool = False, details: dict = None) -> dict:
    return {"ok": False, "error": {"code": code, "message": message, "retryable": retryable, "details": details or {}}}

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SEMANTIC_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")

_CACHE_TTL = 300  # 5 min default cache

GITHUB_API_VERSION = "2022-11-28"

_MARK_READY_MUTATION = """
mutation MarkReady($pullRequestId: ID!) {
  markPullRequestReadyForReview(input: {pullRequestId: $pullRequestId}) {
    pullRequest { id number isDraft headRefOid state }
  }
}
"""

_CONVERT_DRAFT_MUTATION = """
mutation ConvertToDraft($pullRequestId: ID!) {
  convertPullRequestToDraft(input: {pullRequestId: $pullRequestId}) {
    pullRequest { id number isDraft headRefOid state }
  }
}
"""


def _classify_github_credential_type(token: str) -> str:
    """Classify a GitHub credential without returning any token characters."""
    if token.startswith("github_pat_"):
        return "fine_grained_pat"
    if token.startswith("ghp_") or token.startswith("github_"):
        return "classic_pat"
    if token.startswith("ghs_"):
        return "github_app_installation"
    if token.startswith("ghu_"):
        return "github_app_user_access_token"
    if token.startswith("gho_"):
        return "oauth_token"
    return "other_or_unknown"


def _parse_oauth_scopes(headers: dict) -> list[str]:
    """Parse GitHub's scope header without retaining any credential material."""
    value = headers.get("oauth_scopes") or headers.get("X-OAuth-Scopes") or ""
    return sorted({item.strip() for item in value.split(",") if item.strip()})


def _github_auth_capabilities(credential_type: str, oauth_scopes: list[str], checks_status: int,
                              statuses_status: int) -> dict:
    """Build capabilities from declared mode, scopes, and read-only API probes."""
    repo_scope = "repo" in oauth_scopes
    return {
        "credential_type": credential_type,
        "declared_auth_mode": settings.GITHUB_AUTH_MODE,
        "oauth_scopes": oauth_scopes,
        "checks_supported": checks_status == 200,
        "statuses_supported": statuses_status == 200,
        "contents_write_supported": repo_scope,
        "pull_requests_write_supported": repo_scope,
    }

def _get_gh() -> Github:
    token = settings.GITHUB_TOKEN.get_secret_value()
    if not token or token == "REPLACE_WITH_FINE_GRAINED_GITHUB_TOKEN":
        raise GitHubApiError(401, "GitHub token not configured")
    return Github(token)


def _github_response_headers(response) -> dict:
    """Return GitHub diagnostics without ever returning authentication headers."""
    return {
        "content_type": response.headers.get("Content-Type"),
        "github_request_id": response.headers.get("X-GitHub-Request-Id"),
        "accepted_github_permissions": response.headers.get("X-Accepted-GitHub-Permissions"),
        "accepted_oauth_scopes": response.headers.get("X-Accepted-OAuth-Scopes"),
        "oauth_scopes": response.headers.get("X-OAuth-Scopes"),
        "rate_limit": response.headers.get("X-RateLimit-Limit"),
        "rate_remaining": response.headers.get("X-RateLimit-Remaining"),
    }


def _github_endpoint_error_code(kind: str, status: int, headers) -> str:
    """Map one GitHub endpoint response to a stable, non-ambiguous code."""
    rate_remaining = headers.get("X-RateLimit-Remaining", headers.get("rate_remaining"))
    if status == 401:
        suffix = "AUTHENTICATION_FAILED"
    elif status in (403, 429):
        suffix = "RATE_LIMITED" if status == 429 or rate_remaining == "0" else "PERMISSION_DENIED"
    elif status == 404:
        suffix = "NOT_FOUND"
    elif status >= 500:
        suffix = "API_FAILED"
    else:
        suffix = "API_FAILED"
    return f"{kind}_{suffix}"


def _github_get_json(path: str) -> tuple[int, object, dict]:
    """Make a read-only GitHub request with the current supported headers."""
    token = settings.GITHUB_TOKEN.get_secret_value()
    try:
        response = requests.get(
            f"{settings.GITHUB_API_URL.rstrip('/')}{path}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": GITHUB_API_VERSION,
            },
            timeout=20,
        )
    except requests.RequestException:
        return 0, None, {"request_error": "github_request_failed"}
    try:
        payload = response.json()
    except ValueError:
        payload = None
    return response.status_code, payload, _github_response_headers(response)


def github_graphql_request(query: str, variables: dict, operation_name: str) -> dict:
    """Execute a redacted GitHub GraphQL request and preserve request diagnostics."""
    token = settings.GITHUB_TOKEN.get_secret_value()
    try:
        response = requests.post(
            f"{settings.GITHUB_API_URL.rstrip('/')}/graphql",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": GITHUB_API_VERSION,
            },
            json={"query": query, "variables": variables, "operationName": operation_name},
            timeout=20,
        )
    except requests.Timeout:
        return _error_response("GITHUB_GRAPHQL_FAILED", "GitHub GraphQL request timed out", retryable=True,
                               details={"operation_name": operation_name, "request_error": "timeout"})
    except requests.RequestException:
        return _error_response("GITHUB_GRAPHQL_FAILED", "GitHub GraphQL request failed", retryable=True,
                               details={"operation_name": operation_name, "request_error": "request_failed"})

    headers = _github_response_headers(response)
    diagnostics = {"operation_name": operation_name, "github_request_id": headers.get("github_request_id")}
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if response.status_code == 401:
        return _error_response("GITHUB_AUTH_FAILED", "GitHub authentication failed", details=diagnostics)
    if response.status_code == 403:
        return _error_response("GITHUB_PERMISSION_DENIED", "GitHub GraphQL permission denied", details=diagnostics)
    if response.status_code == 429:
        return _error_response("GITHUB_RATE_LIMITED", "GitHub GraphQL rate limit exceeded", retryable=True, details=diagnostics)
    if response.status_code >= 500:
        return _error_response("GITHUB_GRAPHQL_FAILED", "GitHub GraphQL service failed", retryable=True, details=diagnostics)
    if response.status_code >= 400:
        return _error_response("GITHUB_GRAPHQL_FAILED", "GitHub GraphQL request rejected", details={**diagnostics, "github_status": response.status_code})
    if not isinstance(payload, dict):
        return _error_response("GITHUB_GRAPHQL_INVALID_RESPONSE", "GitHub GraphQL response is not an object", details=diagnostics)
    errors = payload.get("errors")
    if errors:
        safe_errors = [{"type": item.get("type"), "message": item.get("message")} for item in errors if isinstance(item, dict)]
        return _error_response("GITHUB_GRAPHQL_FAILED", "GitHub GraphQL returned errors", details={**diagnostics, "errors": safe_errors})
    if not isinstance(payload.get("data"), dict):
        return _error_response("GITHUB_GRAPHQL_INVALID_RESPONSE", "GitHub GraphQL response has no data", details=diagnostics)
    return {"ok": True, "data": payload["data"], "github_request_id": headers.get("github_request_id")}


def _parse_repo(repository: str) -> tuple:
    parts = repository.split("/")
    if len(parts) != 2:
        raise ValueError(f"Invalid repository format: {repository}")
    return parts[0], parts[1]


def _rate_limit_info(gh: Github) -> dict:
    try:
        rl = gh.get_rate_limit()
        core = rl.core
        return {"rate_limit_remaining": core.remaining, "rate_limit_limit": core.limit,
                "rate_limit_reset_at": core.reset.isoformat() if core.reset else None}
    except Exception:
        return {"rate_limit_remaining": None, "rate_limit_reset_at": None}


def get_github_repository(repository: str) -> dict:
    gh = _get_gh()
    try:
        repo = gh.get_repo(repository)
        return {
            "ok": True,
            "full_name": repo.full_name,
            "name": repo.name,
            "owner": repo.owner.login,
            "private": repo.private,
            "archived": repo.archived,
            "disabled": getattr(repo, "disabled", False),
            "default_branch": repo.default_branch,
            "description": repo.description,
            "language": repo.language,
            "size": repo.size,
            "open_issues_count": repo.open_issues_count,
            "forks_count": repo.forks_count,
            "stargazers_count": repo.stargazers_count,
            "created_at": repo.created_at.isoformat() if repo.created_at else None,
            "updated_at": repo.updated_at.isoformat() if repo.updated_at else None,
            "pushed_at": repo.pushed_at.isoformat() if repo.pushed_at else None,
            "html_url": repo.html_url,
            "allow_merge_commit": getattr(repo, "allow_merge_commit", None),
            "allow_squash_merge": getattr(repo, "allow_squash_merge", None),
            "allow_rebase_merge": getattr(repo, "allow_rebase_merge", None),
        }
    except GithubException as e:
        if e.status == 404:
            return {"ok": False, "error": {"code": "REPOSITORY_NOT_FOUND", "message": str(e), "retryable": False}}
        raise GitHubApiError(e.status, str(e))


def list_github_branches(repository: str, protected_only: bool = False, limit: int = 100, page: int = 1) -> dict:
    gh = _get_gh()
    try:
        repo = gh.get_repo(repository)
        branches = list(repo.get_branches())
        total = len(branches)

        if protected_only:
            branches = [b for b in branches if b.protected]
            total = len(branches)

        start = (page - 1) * limit
        end = start + limit
        page_branches = branches[start:end]

        return {
            "ok": True,
            "repository": repository,
            "total_count": total,
            "page": page,
            "limit": limit,
            "has_more": end < total,
            "branches": [
                {
                    "name": b.name,
                    "commit_sha": b.commit.sha,
                    "protected": b.protected,
                }
                for b in page_branches
            ],
        }
    except GithubException as e:
        if e.status == 404:
            return {"ok": False, "error": {"code": "REPOSITORY_NOT_FOUND", "message": str(e), "retryable": False}}
        raise GitHubApiError(e.status, str(e))


def get_github_branch(repository: str, branch: str, base_branch: str = "") -> dict:
    gh = _get_gh()
    try:
        repo = gh.get_repo(repository)
        try:
            b = repo.get_branch(branch)
        except GithubException:
            return {"ok": False, "error": {"code": "BRANCH_NOT_FOUND", "message": f"Branch '{branch}' not found", "retryable": False}}

        result = {
            "ok": True,
            "repository": repository,
            "branch": b.name,
            "commit_sha": b.commit.sha,
            "protected": b.protected,
            "commit_url": b.commit.html_url,
            "html_url": f"https://github.com/{repository}/tree/{branch}",
        }

        if base_branch:
            try:
                base = repo.get_branch(base_branch)
                comparison = repo.compare(base.commit.sha, b.commit.sha)
                result["base_branch"] = base_branch
                result["ahead_by"] = comparison.ahead_by
                result["behind_by"] = comparison.behind_by
            except GithubException:
                result["base_branch"] = base_branch
                result["ahead_by"] = None
                result["behind_by"] = None

        return result
    except GithubException as e:
        if e.status == 404:
            return {"ok": False, "error": {"code": "REPOSITORY_NOT_FOUND", "message": str(e), "retryable": False}}
        raise GitHubApiError(e.status, str(e))


def get_github_commit(repository: str, commit_sha: str, file_limit: int = 100) -> dict:
    gh = _get_gh()
    try:
        repo = gh.get_repo(repository)
        try:
            commit = repo.get_commit(commit_sha)
        except GithubException:
            return {"ok": False, "error": {"code": "COMMIT_NOT_FOUND", "message": f"Commit '{commit_sha}' not found", "retryable": False}}

        files = list(commit.files)
        total_files = len(files)
        truncated = total_files > file_limit
        display_files = files[:file_limit]

        return {
            "ok": True,
            "repository": repository,
            "sha": commit.sha,
            "message": commit.commit.message,
            "author": commit.commit.author.name if commit.commit.author else None,
            "author_login": commit.author.login if commit.author else None,
            "committer": commit.commit.committer.name if commit.commit.committer else None,
            "authored_at": commit.commit.author.date.isoformat() if commit.commit.author and commit.commit.author.date else None,
            "committed_at": commit.commit.committer.date.isoformat() if commit.commit.committer and commit.commit.committer.date else None,
            "parents": [{"sha": p.sha} for p in commit.parents],
            "files_changed": total_files,
            "additions": commit.stats.additions if commit.stats else 0,
            "deletions": commit.stats.deletions if commit.stats else 0,
            "total_changes": commit.stats.total if commit.stats else 0,
            "html_url": commit.html_url,
            "verification": {"verified": None},
            "changed_files": [
                {
                    "filename": f.filename,
                    "status": f.status,
                    "additions": f.additions,
                    "deletions": f.deletions,
                    "changes": f.changes,
                    "patch": (f.patch[:2000] if f.patch and len(f.patch) > 2000 else f.patch),
                    "patch_truncated": len(f.patch) > 2000 if f.patch else False,
                }
                for f in display_files
            ],
            "truncated": truncated,
        }
    except GithubException as e:
        if e.status == 404:
            return {"ok": False, "error": {"code": "COMMIT_NOT_FOUND", "message": str(e), "retryable": False}}
        raise GitHubApiError(e.status, str(e))


def compare_github_commits(repository: str, base: str, head: str, file_limit: int = 100) -> dict:
    gh = _get_gh()
    try:
        repo = gh.get_repo(repository)
        comparison = repo.compare(base, head)

        commits = list(comparison.commits[:50])
        files = list(comparison.files)
        total_files = len(files)
        truncated = total_files > file_limit
        display_files = files[:file_limit]

        return {
            "ok": True,
            "repository": repository,
            "base": base,
            "head": head,
            "status": comparison.status,
            "ahead_by": comparison.ahead_by,
            "behind_by": comparison.behind_by,
            "total_commits": comparison.total_commits,
            "merge_base_sha": comparison.merge_base_commit.sha if comparison.merge_base_commit else None,
            "commits": [
                {"sha": c.sha, "message": c.commit.message.split("\n")[0][:200]}
                for c in commits
            ],
            "changed_files_count": total_files,
            "additions": sum(f.additions for f in display_files),
            "deletions": sum(f.deletions for f in display_files),
            "files": [
                {
                    "filename": f.filename,
                    "status": f.status,
                    "additions": f.additions,
                    "deletions": f.deletions,
                    "changes": f.changes,
                }
                for f in display_files
            ],
            "truncated": truncated,
        }
    except GithubException as e:
        if e.status == 404:
            return {"ok": False, "error": {"code": "COMMIT_NOT_FOUND", "message": str(e), "retryable": False}}
        raise GitHubApiError(e.status, str(e))


def list_github_pull_requests(
    repository: str,
    state: str = "open",
    head_branch: str = "",
    base_branch: str = "",
    sort: str = "updated",
    direction: str = "desc",
    limit: int = 30,
    page: int = 1,
) -> dict:
    gh = _get_gh()
    try:
        repo = gh.get_repo(repository)
        kwargs = {"state": state, "sort": sort, "direction": direction}
        if head_branch:
            kwargs["head"] = head_branch
        if base_branch:
            kwargs["base"] = base_branch

        all_prs = list(repo.get_pulls(**kwargs))
        total = len(all_prs)
        start = (page - 1) * limit
        end = start + limit
        page_prs = all_prs[start:end]

        return {
            "ok": True,
            "repository": repository,
            "total_count": total,
            "page": page,
            "limit": limit,
            "has_more": end < total,
            "pull_requests": [
                {
                    "pull_number": pr.number,
                    "title": pr.title,
                    "state": pr.state,
                    "draft": pr.draft,
                    "head_branch": pr.head.ref,
                    "head_sha": pr.head.sha,
                    "base_branch": pr.base.ref,
                    "author": pr.user.login if pr.user else None,
                    "created_at": pr.created_at.isoformat() if pr.created_at else None,
                    "updated_at": pr.updated_at.isoformat() if pr.updated_at else None,
                    "html_url": pr.html_url,
                }
                for pr in page_prs
            ],
        }
    except GithubException as e:
        if e.status == 404:
            return {"ok": False, "error": {"code": "REPOSITORY_NOT_FOUND", "message": str(e), "retryable": False}}
        raise GitHubApiError(e.status, str(e))


def get_github_pull_request(repository: str, pull_number: int) -> dict:
    gh = _get_gh()
    try:
        repo = gh.get_repo(repository)
        try:
            pr = repo.get_pull(pull_number)
        except GithubException:
            return {"ok": False, "error": {"code": "PULL_REQUEST_NOT_FOUND", "message": f"PR #{pull_number} not found", "retryable": False}}

        # GitHub 异步计算 mergeable；最多重读三次，避免把 unknown 当成可合并。
        mergeable = pr.mergeable
        for _ in range(2):
            if mergeable is not None:
                break
            time.sleep(0.2)
            pr = repo.get_pull(pull_number)
            mergeable = pr.mergeable

        requested_reviewers, requested_teams = _get_requested_reviewers(repo, pr)
        reviews = _get_submitted_reviews(pr)
        review_decision = _review_decision(reviews)

        return {
            "ok": True,
            "repository": repository,
            "pull_number": pr.number,
            "title": pr.title,
            "body": pr.body,
            "state": pr.state,
            "draft": pr.draft,
            "merged": pr.merged,
            "mergeable": mergeable,
            "mergeable_state": getattr(pr, "mergeable_state", None),
            "head_branch": pr.head.ref,
            "head_sha": pr.head.sha,
            "base_branch": pr.base.ref,
            "base_sha": pr.base.sha,
            "author": pr.user.login if pr.user else None,
            "requested_reviewers": requested_reviewers,
            "requested_teams": requested_teams,
            "reviews": reviews,
            "review_decision": review_decision,
            "labels": [lbl.name for lbl in pr.labels],
            "commits": pr.commits,
            "changed_files": pr.changed_files,
            "additions": pr.additions,
            "deletions": pr.deletions,
            "comments": pr.comments,
            "review_comments": pr.review_comments,
            "created_at": pr.created_at.isoformat() if pr.created_at else None,
            "updated_at": pr.updated_at.isoformat() if pr.updated_at else None,
            "closed_at": pr.closed_at.isoformat() if pr.closed_at else None,
            "merged_at": pr.merged_at.isoformat() if pr.merged_at else None,
            "html_url": pr.html_url,
        }
    except GithubException as e:
        if e.status == 404:
            return {"ok": False, "error": {"code": "REPOSITORY_NOT_FOUND", "message": str(e), "retryable": False}}
        raise GitHubApiError(e.status, str(e))


def _get_requested_reviewers(repo, pr) -> tuple[list[str], list[str]]:
    """读取尚未完成的 reviewer 请求；兼容 SDK 缺少 get_review_requests 的版本。"""
    try:
        if hasattr(pr, "get_review_requests"):
            users, teams = pr.get_review_requests()
            return ([u.login for u in users], [t.slug for t in teams])
    except (AttributeError, TypeError, GithubException):
        logger.info("PyGithub reviewer request API unavailable; using REST fallback")
    try:
        owner, name = _parse_repo(repo.full_name)
        payload, _ = repo._requester.requestJsonAndCheck(
            "GET", f"/repos/{owner}/{name}/pulls/{pr.number}/requested_reviewers"
        )
        return ([u.get("login") for u in payload.get("users", []) if u.get("login")],
                [t.get("slug") or t.get("name") for t in payload.get("teams", []) if t.get("slug") or t.get("name")])
    except GithubException:
        return [], []


def _get_submitted_reviews(pr) -> list[dict]:
    try:
        reviews = []
        for review in pr.get_reviews():
            reviews.append({
                "id": review.id,
                "user": review.user.login if review.user else None,
                "state": review.state,
                "body": review.body,
                "commit_id": review.commit_id,
                "submitted_at": review.submitted_at.isoformat() if review.submitted_at else None,
                "html_url": review.html_url,
            })
        return reviews
    except (AttributeError, GithubException):
        return []


def _review_decision(reviews: list[dict]) -> str:
    latest_by_user = {}
    for review in reviews:
        if review.get("user"):
            latest_by_user[review["user"]] = review.get("state", "").upper()
    if any(state == "CHANGES_REQUESTED" for state in latest_by_user.values()):
        return "CHANGES_REQUESTED"
    if any(state == "APPROVED" for state in latest_by_user.values()):
        return "APPROVED"
    return "REVIEW_REQUIRED"


def _private_ci_job(job_id: str) -> Optional[dict]:
    try:
        from app.ci_database import get_job
        return get_job(job_id)
    except Exception:
        return None


def _review_policy(repo, base_branch: str) -> dict:
    """Read protection policy without changing it; unavailable is explicit."""
    result = {"required_approvals": 0, "current_approvals": 0,
              "changes_requested": False, "code_owner_review_required": False,
              "last_push_approval_required": False, "conversation_resolution_required": False,
              "source": "unavailable"}
    try:
        owner, name = _parse_repo(repo.full_name)
        payload, _ = repo._requester.requestJsonAndCheck(
            "GET", f"/repos/{owner}/{name}/branches/{base_branch}/protection"
        )
        reviews = payload.get("required_pull_request_reviews") or {}
        result.update({
            "required_approvals": reviews.get("required_approving_review_count", 0),
            "code_owner_review_required": bool(reviews.get("require_code_owner_reviews")),
            "last_push_approval_required": bool(reviews.get("require_last_push_approval")),
            "conversation_resolution_required": bool((payload.get("required_conversation_resolution") or {}).get("enabled")),
            "source": "branch_protection",
        })
    except GithubException as exc:
        if exc.status == 404:
            result["source"] = "none"
            result["error_code"] = None
        else:
            result["error_code"] = "BRANCH_PROTECTION_PERMISSION_DENIED" if exc.status == 403 else "BRANCH_PROTECTION_UNAVAILABLE"
    except Exception:
        result["error_code"] = "BRANCH_PROTECTION_UNAVAILABLE"
    return result


def _repository_merge_policy(repository: str) -> dict:
    """Load only the explicitly configured MyGithub09 merge policy."""
    path = os.environ.get("CI_REPOSITORIES_PATH", "/app/config/ci_repositories.yml")
    if not os.path.exists(path):
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "ci_repositories.yml")
    try:
        with open(path, encoding="utf-8") as handle:
            entry = (yaml.safe_load(handle) or {}).get("repositories", {}).get(repository, {})
    except (OSError, yaml.YAMLError):
        entry = {}
    policy = entry.get("merge_policy") or {}
    return {
        "private_ci_authoritative": bool(policy.get("private_ci_authoritative", False)),
        "required_private_ci_profile": policy.get("required_private_ci_profile", "repo-auto-check"),
        "github_checks_mode": policy.get("github_checks_mode", "required_only"),
        "allow_non_required_check_failures": bool(policy.get("allow_non_required_check_failures", False)),
        "allow_quota_or_infrastructure_failures": bool(policy.get("allow_quota_or_infrastructure_failures", False)),
        "required_workflows": list(policy.get("required_workflows") or []),
    }


def _required_check_sources(repo, base_branch: str, policy: dict) -> dict:
    """Resolve required checks from GitHub protection/rulesets and explicit policy only."""
    if not getattr(repo, "full_name", None) or not getattr(repo, "_requester", None):
        return {"contexts": [], "checks": list(policy.get("required_workflows") or []),
                "sources": ["repository_policy"] if policy.get("required_workflows") else [], "errors": []}
    owner, name = _parse_repo(repo.full_name)
    contexts, checks = set(), set()
    sources = []
    errors = []

    try:
        payload, _ = repo._requester.requestJsonAndCheck("GET", f"/repos/{owner}/{name}/branches/{base_branch}/protection")
        required = payload.get("required_status_checks") or {}
        contexts.update(required.get("contexts") or [])
        checks.update(item.get("context") for item in (required.get("checks") or []) if item.get("context"))
        if contexts or checks:
            sources.append("branch_protection")
    except GithubException as exc:
        if exc.status != 404:
            errors.append("BRANCH_PROTECTION_PERMISSION_DENIED" if exc.status == 403 else "BRANCH_PROTECTION_UNAVAILABLE")
    except Exception:
        errors.append("BRANCH_PROTECTION_UNAVAILABLE")

    try:
        payload, _ = repo._requester.requestJsonAndCheck("GET", f"/repos/{owner}/{name}/rulesets?includes_parents=true")
        rulesets = payload if isinstance(payload, list) else []
        for ruleset in rulesets:
            if ruleset.get("enforcement") not in ("active", "evaluate"):
                continue
            condition = ruleset.get("conditions", {}).get("ref_name", {})
            refs = condition.get("include") or []
            if refs and not any(ref in ("~ALL", f"refs/heads/{base_branch}", base_branch) for ref in refs):
                continue
            detail = repo._requester.requestJsonAndCheck("GET", f"/repos/{owner}/{name}/rulesets/{ruleset.get('id')}")[0]
            for rule in detail.get("rules", []):
                rule_type = rule.get("type")
                parameters = rule.get("parameters") or {}
                if rule_type == "required_status_checks":
                    for item in parameters.get("required_status_checks", []):
                        context = item.get("context")
                        if context: contexts.add(context)
                    sources.append("ruleset")
                elif rule_type == "required_workflows":
                    for workflow in parameters.get("workflows", []):
                        value = workflow.get("path") or workflow.get("workflow_file") or workflow.get("name")
                        if value: checks.add(value)
                    sources.append("ruleset")
    except GithubException as exc:
        if exc.status not in (404,):
            errors.append("RULESETS_PERMISSION_DENIED" if exc.status == 403 else "RULESETS_UNAVAILABLE")
    except Exception:
        errors.append("RULESETS_UNAVAILABLE")

    explicit = set(policy.get("required_workflows") or [])
    checks.update(explicit)
    if explicit: sources.append("repository_policy")
    return {"contexts": sorted(contexts), "checks": sorted(checks), "sources": sorted(set(sources)), "errors": sorted(set(errors))}


def _is_actions_infrastructure_failure(check: dict) -> bool:
    text = " ".join(str(check.get(key) or "") for key in ("name", "conclusion", "output_summary", "failure_message")).lower()
    duration = check.get("duration_seconds")
    short_job = duration is not None and duration <= 10
    no_steps = check.get("steps") == []
    runner_zero = check.get("runner_id") in (0, "0")
    logs_missing = check.get("logs_http_status") == 404
    quota_text = any(token in text for token in ("quota", "billing", "spending limit", "runner unavailable", "no hosted runner"))
    return (short_job and no_steps) or runner_zero or logs_missing or quota_text


def _classify_check_run(check: dict, required: bool = False, required_source: str = "") -> dict:
    status = check.get("status")
    conclusion = check.get("conclusion")
    # GitHub may report a failed check even though the job never obtained a
    # runner.  That is an Actions service/quota problem, not a project failure;
    # classify it before applying the required-check gate so it remains a
    # warning even when the check name is listed by protection/rulesets.
    infrastructure_failure = (
        status == "completed"
        and conclusion not in ("success", "neutral", "skipped")
        and _is_actions_infrastructure_failure(check)
    )
    if infrastructure_failure:
        classification = "GITHUB_ACTIONS_QUOTA_OR_INFRA_FAILURE"
    elif required and status != "completed":
        classification = "REQUIRED_CHECK_PENDING"
    elif required and conclusion not in ("success", "neutral", "skipped"):
        classification = "REQUIRED_CHECK_FAILED"
    elif status != "completed":
        classification = "NON_REQUIRED_CHECK_PENDING"
    elif conclusion not in ("success", "neutral", "skipped"):
        classification = ("GITHUB_ACTIONS_QUOTA_OR_INFRA_FAILURE" if _is_actions_infrastructure_failure(check)
                         else "GITHUB_ACTIONS_CODE_FAILURE")
    else:
        classification = "PASS"
    return {**check, "is_required": required, "required_source": required_source, "classification": classification,
            "blocking": classification in ("REQUIRED_CHECK_FAILED", "REQUIRED_CHECK_PENDING", "REQUIRED_CHECK_MISSING")}


def _readiness(repository: str, pull_number: int, expected_head_sha: str = "",
               required_private_ci_job_id: str = "", expected_base_branch: str = "main") -> dict:
    pr_result = get_github_pull_request(repository, pull_number)
    if not pr_result.get("ok"):
        return {"ok": True, "ready": False, "reasons": ["PR_NOT_FOUND"], "repository": repository, "pull_number": pull_number}
    if pr_result.get("merged"):
        return {"ok": True, "ready": False, "reasons": ["ALREADY_MERGED"], "blocking": ["ALREADY_MERGED"],
                "warnings": [], "repository": repository, "pull_number": pull_number,
                "state": pr_result["state"], "merged": True, "draft": pr_result["draft"],
                "base_branch": pr_result["base_branch"], "base_sha": pr_result["base_sha"],
                "head_branch": pr_result["head_branch"], "head_sha": pr_result["head_sha"],
                "review_policy": {"source": "not_evaluated"}, "github_checks": {"checks": []}, "private_ci": None}
    if pr_result.get("state") != "open":
        return {"ok": True, "ready": False, "reasons": ["PR_NOT_OPEN"], "blocking": ["PR_NOT_OPEN"],
                "warnings": [], "repository": repository, "pull_number": pull_number,
                "state": pr_result["state"], "merged": False, "draft": pr_result["draft"],
                "base_branch": pr_result["base_branch"], "base_sha": pr_result["base_sha"],
                "head_branch": pr_result["head_branch"], "head_sha": pr_result["head_sha"],
                "review_policy": {"source": "not_evaluated"}, "github_checks": {"checks": []}, "private_ci": None}
    reasons = []
    if pr_result["draft"]: reasons.append("PR_IS_DRAFT")
    if pr_result["base_branch"] != expected_base_branch: reasons.append("BASE_BRANCH_MISMATCH")
    if expected_head_sha and pr_result["head_sha"] != expected_head_sha: reasons.append("HEAD_CHANGED")
    mergeable_state = pr_result["mergeable_state"]
    if mergeable_state == "dirty": reasons.append("MERGE_CONFLICT")
    elif pr_result["mergeable"] is None or mergeable_state in (None, "unknown"):
        reasons.append("MERGEABILITY_PENDING")
    elif pr_result["mergeable"] is not True:
        reasons.append("UPDATE_BRANCH_REQUIRED" if mergeable_state == "behind" else "MERGEABLE_STATE_BLOCKED")
    if pr_result["review_decision"] == "CHANGES_REQUESTED": reasons.append("CHANGES_REQUESTED")

    gh = _get_gh()
    repo_obj = gh.get_repo(repository)
    review_policy = _review_policy(repo_obj, expected_base_branch)
    latest_approvals = {r.get("user") for r in pr_result["reviews"] if r.get("state") == "APPROVED"}
    review_policy["current_approvals"] = len(latest_approvals)
    review_policy["changes_requested"] = pr_result["review_decision"] == "CHANGES_REQUESTED"
    if review_policy["changes_requested"]:
        reasons.append("CHANGES_REQUESTED")
    elif review_policy["required_approvals"] > review_policy["current_approvals"]:
        reasons.append("REVIEW_REQUIRED")

    try:
        checks = get_github_pull_request_checks(repository, pull_number)
    except GitHubApiError as exc:
        checks = _error_response("CHECKS_PERMISSION_DENIED" if getattr(exc, "status", None) == 403 else "CHECKS_UNAVAILABLE", "GitHub checks could not be read")
    warnings = []
    if not checks.get("ok"):
        reasons.append(checks.get("error", {}).get("code", "GITHUB_CHECKS_UNAVAILABLE"))
    else:
        for item in checks.get("checks", []) + checks.get("statuses", []):
            classification = item.get("classification")
            if classification in ("REQUIRED_CHECK_FAILED", "REQUIRED_CHECK_PENDING", "REQUIRED_CHECK_MISSING"):
                reasons.append(classification)
            elif classification in ("GITHUB_ACTIONS_QUOTA_OR_INFRA_FAILURE", "GITHUB_ACTIONS_CODE_FAILURE", "NON_REQUIRED_CHECK_FAILED", "NON_REQUIRED_CHECK_PENDING"):
                warnings.append(classification)
        warnings.extend(checks.get("required_check_sources", {}).get("errors", []))

    private_ci = None
    if required_private_ci_job_id:
        private_ci = _private_ci_job(required_private_ci_job_id)
        if not private_ci:
            reasons.append("PRIVATE_CI_JOB_NOT_FOUND")
        else:
            private_ci = {**private_ci, "valid": False}
            if private_ci.get("repository") != repository: reasons.append("PRIVATE_CI_REPOSITORY_MISMATCH")
            if private_ci.get("branch") != pr_result["head_branch"]: reasons.append("PRIVATE_CI_BRANCH_MISMATCH")
            if private_ci.get("commit_sha") != pr_result["head_sha"]: reasons.append("PRIVATE_CI_SHA_MISMATCH")
            required_profile = _repository_merge_policy(repository).get("required_private_ci_profile", "repo-auto-check")
            if private_ci.get("profile") != required_profile: reasons.append("PRIVATE_CI_PROFILE_MISMATCH")
            if private_ci.get("status") != "passed" or private_ci.get("exit_code") != 0: reasons.append("PRIVATE_CI_NOT_PASSED")
            if private_ci.get("superseded_by_job_id"): reasons.append("PRIVATE_CI_SUPERSEDED")
            private_ci["valid"] = not any(reason.startswith("PRIVATE_CI_") for reason in reasons)
    else:
        reasons.append("PRIVATE_CI_REQUIRED")

    repo_meta = get_github_repository(repository)
    allowed = []
    if repo_meta.get("allow_merge_commit"): allowed.append("merge")
    if repo_meta.get("allow_squash_merge"): allowed.append("squash")
    if repo_meta.get("allow_rebase_merge"): allowed.append("rebase")
    return {
        "ok": True, "ready": not reasons, "reasons": reasons,
        "blocking": reasons,
        "warnings": sorted(set(warnings + ([review_policy.get("error_code")] if review_policy.get("error_code") else []))),
        "actions": (["resolve merge conflict locally and push a normal commit"] if "MERGE_CONFLICT" in reasons else []) + (["obtain required approving review"] if "REVIEW_REQUIRED" in reasons else []),
        "repository": repository, "pull_number": pull_number,
        "state": pr_result["state"], "draft": pr_result["draft"], "merged": pr_result["merged"],
        "base_branch": pr_result["base_branch"], "base_sha": pr_result["base_sha"],
        "head_branch": pr_result["head_branch"], "head_sha": pr_result["head_sha"],
        "expected_head_match": bool(expected_head_sha) and pr_result["head_sha"] == expected_head_sha,
        "mergeable": pr_result["mergeable"], "mergeable_state": pr_result["mergeable_state"],
        "review_decision": pr_result["review_decision"], "review_policy": review_policy,
        "requested_reviewers": pr_result["requested_reviewers"], "change_requests": [r for r in pr_result["reviews"] if r["state"] == "CHANGES_REQUESTED"],
        "github_checks": {"overall": checks.get("overall_conclusion") if checks.get("ok") else "unavailable", "checks": checks.get("checks", []) if checks.get("ok") else []},
        "private_ci": private_ci, "allowed_merge_methods": allowed,
    }


def get_github_pull_request_merge_readiness(repository: str, pull_number: int, expected_head_sha: str = "",
                                            required_private_ci_job_id: str = "", expected_base_branch: str = "main") -> dict:
    return _readiness(repository, pull_number, expected_head_sha, required_private_ci_job_id, expected_base_branch)


def _local_conflict_analysis(repository: str, base_sha: str, head_sha: str) -> dict:
    """Use an ephemeral bare repository for exact-SHA, read-only conflict analysis."""
    owner, name = _parse_repo(repository)
    token = settings.GITHUB_TOKEN.get_secret_value()
    remote = f"https://github.com/{owner}/{name}.git"
    env = os.environ.copy()
    # Keep the credential out of argv and never log the environment. Git's
    # extraheader configuration is not honored consistently by all slim Git
    # builds, so use a short-lived askpass helper instead.

    def run(args: list[str], cwd: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], cwd=cwd, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90,
            check=False,
        )

    try:
        with tempfile.TemporaryDirectory(prefix="mygithub08-conflict-") as work:
            askpass = os.path.join(work, "askpass.sh")
            with open(askpass, "w", encoding="utf-8") as handle:
                handle.write("#!/bin/sh\ncase \"$1\" in *Username*) printf '%s\\n' x-access-token ;; *) printf '%s\\n' \"$GITHUB_TOKEN\" ;; esac\n")
            os.chmod(askpass, 0o700)
            env.update({"GIT_ASKPASS": askpass, "GIT_TERMINAL_PROMPT": "0", "GITHUB_TOKEN": token})
            init = run(["init", "--bare", work], work)
            if init.returncode:
                return {"ok": False, "error_code": "CONFLICT_ANALYSIS_FAILED", "message": "git init failed"}
            # Full history is required for merge-base; the repository is
            # ephemeral and is deleted immediately after this analysis.
            fetch = run(["fetch", "--no-tags", remote, base_sha, head_sha], work)
            if fetch.returncode:
                return {"ok": False, "error_code": "CONFLICT_ANALYSIS_FETCH_FAILED", "message": "exact SHA fetch failed", "stderr": fetch.stderr[-500:]}
            merge_base = run(["merge-base", base_sha, head_sha], work)
            merge_trees = [
                run(["merge-tree", "--write-tree", base_sha, head_sha], work),
                run(["merge-tree", "--write-tree", head_sha, base_sha], work),
            ]
            conflict_files = sorted(set(re.findall(
                r"Merge conflict in (.+)",
                "\n".join(item.stdout + item.stderr for item in merge_trees),
            )))
            return {
                "ok": True,
                "merge_base_sha": merge_base.stdout.strip() if merge_base.returncode == 0 else None,
                "conflicting_files": conflict_files,
                "merge_tree_exit_codes": [item.returncode for item in merge_trees],
            }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error_code": "CONFLICT_ANALYSIS_TIMEOUT", "message": "exact SHA conflict analysis timed out"}
    except Exception:
        logger.exception("exact SHA conflict analysis failed")
        return {"ok": False, "error_code": "CONFLICT_ANALYSIS_FAILED", "message": "exact SHA conflict analysis failed"}


def get_github_pull_request_conflicts(repository: str, pull_number: int, expected_head_sha: str = "") -> dict:
    pr = get_github_pull_request(repository, pull_number)
    if not pr.get("ok"):
        return pr
    if expected_head_sha and pr["head_sha"] != expected_head_sha:
        return _error_response("HEAD_CHANGED", "PR head SHA changed", details={"current_head_sha": pr["head_sha"]})
    analysis = _local_conflict_analysis(repository, pr["base_sha"], pr["head_sha"])
    if not analysis.get("ok"):
        return _error_response(analysis.get("error_code", "CONFLICT_ANALYSIS_FAILED"), analysis.get("message", "conflict analysis failed"), details=analysis)
    return {
        "ok": True, "repository": repository, "pull_number": pull_number,
        "base_sha": pr["base_sha"], "head_sha": pr["head_sha"],
        "merge_base_sha": analysis.get("merge_base_sha"), "conflicting_files": analysis.get("conflicting_files", []),
        "merge_tree_exit_codes": analysis.get("merge_tree_exit_codes", []),
        "can_update_branch": pr["mergeable_state"] not in ("dirty",),
        "requires_manual_resolution": pr["mergeable_state"] == "dirty",
        "diagnostic_note": "Exact base/head SHA were fetched into an ephemeral bare repository; no commit, push, or branch mutation was performed.",
    }


def get_github_changed_files_result(repository: str, base_sha: str, head_sha: str, limit: int = 100) -> dict:
    """Return auditable compare paths; never turn compare errors into an empty diff."""
    if not SHA_RE.fullmatch(base_sha or ""):
        return {"ok": False, "error_code": "CHANGED_FILES_BASE_MISSING", "message": "base_sha is required"}
    if not SHA_RE.fullmatch(head_sha or ""):
        return {"ok": False, "error_code": "CHANGED_FILES_HEAD_MISSING", "message": "head_sha is required"}
    try:
        comparison = _get_gh().get_repo(repository).compare(base_sha, head_sha)
        all_files = list(comparison.files)
        # PyGithub's compare.changed_files can be absent/zero on mocked or
        # partial responses.  The materialized file list is authoritative in
        # that case; never persist a non-empty diff with total_count=0.
        declared_total = int(getattr(comparison, "changed_files", 0) or 0)
        total = max(declared_total, len(all_files))
        files = [item.filename for item in all_files[:limit]]
        truncated = total > len(files)
        return {"ok": True, "changed_files": files, "total_count": total,
                "truncated": truncated,
                "warning_code": "CHANGED_FILES_TRUNCATED" if truncated else None,
                "base_sha": base_sha, "head_sha": head_sha}
    except GithubException as exc:
        status = getattr(exc, "status", None)
        logger.error("GitHub compare failed repository=%s base=%.12s head=%.12s status=%s", repository, base_sha, head_sha, status)
        return {"ok": False, "error_code": "CHANGED_FILES_COMPARE_FAILED",
                "message": "GitHub compare failed", "details": {"github_status": status}}
    except Exception as exc:
        logger.exception("GitHub compare failed repository=%s base=%.12s head=%.12s", repository, base_sha, head_sha)
        return {"ok": False, "error_code": "CHANGED_FILES_COMPARE_FAILED",
                "message": "GitHub compare failed", "details": {"error_type": type(exc).__name__}}


def get_github_changed_files(repository: str, base_sha: str, head_sha: str) -> list[str]:
    """Compatibility wrapper for callers that only need the paths."""
    result = get_github_changed_files_result(repository, base_sha, head_sha)
    if not result.get("ok"):
        raise ValueError(result.get("error_code", "CHANGED_FILES_COMPARE_FAILED"))
    return result["changed_files"]


def plan_github_pull_request_merge(repository: str, pull_number: int, merge_method: str = "squash",
                                   expected_head_sha: str = "", required_private_ci_job_id: str = "",
                                   expected_base_branch: str = "main") -> dict:
    readiness = _readiness(repository, pull_number, expected_head_sha, required_private_ci_job_id, expected_base_branch)
    if readiness.get("reasons") == ["ALREADY_MERGED"] or readiness.get("reasons") == ["PR_NOT_OPEN"]:
        return {**readiness, "blocking_reasons": readiness.get("blocking", readiness.get("reasons", [])),
                "required_actions": readiness.get("actions", []), "merge_method": merge_method,
                "resulting_commit_behavior": {"merge": "merge commit", "squash": "one squashed commit", "rebase": "linear commit replay"}.get(merge_method)}
    if merge_method not in ("merge", "squash", "rebase"):
        readiness.setdefault("blocking", []).append("METHOD_NOT_ALLOWED")
        readiness["ready"] = False
    return {**readiness, "blocking_reasons": readiness.get("blocking", readiness.get("reasons", [])),
            "required_actions": readiness.get("actions", []), "merge_method": merge_method,
            "resulting_commit_behavior": {"merge": "merge commit", "squash": "one squashed commit", "rebase": "linear commit replay"}.get(merge_method)}


def build_merge_kwargs(expected_head_sha: str, merge_method: str, commit_title: str = "", commit_message: str = "", delete_head_branch: bool = False) -> dict:
    """Build arguments for the installed PyGithub PullRequest.merge API.

    PyGithub uses ``commit_title``/``commit_message`` and has no ``title`` or
    ``message`` keyword.  Optional values are omitted entirely when empty.
    """
    kwargs = {"sha": expected_head_sha, "merge_method": merge_method}
    if commit_title:
        kwargs["commit_title"] = commit_title
    if commit_message:
        kwargs["commit_message"] = commit_message
    if delete_head_branch:
        kwargs["delete_branch"] = True
    return kwargs


def merge_github_pull_request(repository: str, pull_number: int, merge_method: str = "squash",
                              expected_head_sha: str = "", required_private_ci_job_id: str = "",
                              expected_base_branch: str = "main", commit_title: str = "",
                              commit_message: str = "", delete_head_branch: bool = False,
                              confirm: bool = False) -> dict:
    # Terminal PR states are read-only and must not be masked by merge input
    # validation.  Fall back to the normal input error when a caller has not
    # confirmed and the preliminary read itself is unavailable.
    try:
        terminal = get_github_pull_request(repository, pull_number)
    except Exception:
        terminal = {"ok": False}
    if terminal.get("ok") and terminal.get("merged"):
        return _readiness(repository, pull_number, expected_head_sha, required_private_ci_job_id, expected_base_branch)
    if terminal.get("ok") and terminal.get("state") != "open":
        return _readiness(repository, pull_number, expected_head_sha, required_private_ci_job_id, expected_base_branch)
    if not confirm: return _error_response("CONFIRM_REQUIRED", "confirm must be true")
    if not SHA_RE.fullmatch(expected_head_sha): return _error_response("EXPECTED_HEAD_SHA_REQUIRED", "expected_head_sha must be a full 40-character SHA")
    if not required_private_ci_job_id: return _error_response("PRIVATE_CI_REQUIRED", "required_private_ci_job_id is required")
    if merge_method not in ("merge", "squash", "rebase"): return _error_response("INVALID_MERGE_METHOD", "merge_method must be merge, squash, or rebase")
    readiness = _readiness(repository, pull_number, expected_head_sha, required_private_ci_job_id, expected_base_branch)
    if not readiness.get("ready"):
        code = readiness.get("reasons", ["NOT_READY"])[0]
        return _error_response(code, "Pull request is not ready to merge", details={"readiness": readiness})
    if merge_method not in readiness.get("allowed_merge_methods", []):
        return _error_response("METHOD_NOT_ALLOWED", "Requested merge method is disabled for this repository", details={"allowed_merge_methods": readiness.get("allowed_merge_methods", [])})
    gh = _get_gh()
    try:
        repo = gh.get_repo(repository)
        pr = repo.get_pull(pull_number)
        merge_kwargs = build_merge_kwargs(expected_head_sha, merge_method, commit_title, commit_message, delete_head_branch)
        logger.info("GitHub merge request repository=%s pull=%s method=%s sha=%s", repository, pull_number, merge_method, expected_head_sha)
        result = pr.merge(**merge_kwargs)
        if not result.merged:
            return _error_response("GITHUB_MERGE_REJECTED", result.message or "GitHub did not merge the pull request")
        merged_pr = repo.get_pull(pull_number)
        if not getattr(merged_pr, "merged", False):
            return _error_response("MERGE_RESULT_NOT_CONFIRMED", "GitHub merge result could not be confirmed")
        base_after = repo.get_branch(expected_base_branch).commit.sha
        merge_sha = getattr(merged_pr, "merge_commit_sha", None) or result.sha
        if not merge_sha or not SHA_RE.fullmatch(merge_sha):
            return _error_response("MERGE_COMMIT_SHA_INVALID", "GitHub did not return a full merge commit SHA")
        return {"ok": True, "merged": True, "repository": repository, "pull_number": pull_number,
                "merge_method": merge_method, "previous_head_sha": expected_head_sha,
                "merge_commit_sha": merge_sha, "base_branch": expected_base_branch,
                "base_head_before": readiness["base_sha"], "base_head_after": base_after,
                "html_url": merged_pr.html_url, "head_branch_deleted": False,
                "message": result.message}
    except TypeError as e:
        logger.exception("GitHub merge adapter rejected supported parameters")
        return _error_response("GITHUB_API_FAILED", "GitHub merge adapter does not support the installed SDK signature", details={"error_type": type(e).__name__}, retryable=False)
    except GithubException as e:
        status = getattr(e, "status", None)
        request_id = None
        headers = getattr(e, "headers", None) or {}
        if headers:
            request_id = headers.get("X-GitHub-Request-Id") or headers.get("x-github-request-id")
        details = {"github_status": status, "github_request_id": request_id}
        logger.error("GitHub merge failed status=%s request_id=%s", status, request_id)
        if status == 401: return _error_response("GITHUB_PERMISSION_DENIED", "GitHub authentication failed", details=details)
        if status == 403: return _error_response("GITHUB_PERMISSION_DENIED", "GitHub identity lacks merge permission", details=details)
        if status == 422: return _error_response("GITHUB_MERGE_REJECTED", "GitHub rejected the merge request", details=details)
        if status == 409: return _error_response("GITHUB_MERGE_REJECTED", "GitHub rejected the merge because the head or mergeability changed", retryable=True, details=details)
        if status and status >= 500: return _error_response("GITHUB_API_FAILED", "GitHub merge API failed", retryable=True, details=details)
        return _error_response("GITHUB_API_FAILED", "GitHub merge API failed", retryable=True, details=details)


def list_github_pull_request_reviews(repository: str, pull_number: int, limit: int = 100, page: int = 1) -> dict:
    result = get_github_pull_request(repository, pull_number)
    if not result.get("ok"): return result
    reviews = result.get("reviews", [])
    start = max(0, page - 1) * limit
    return {"ok": True, "repository": repository, "pull_number": pull_number, "reviews": reviews[start:start + limit], "total": len(reviews), "page": page, "limit": limit}


def _pr_with_sha(repository, pull_number, expected_head_sha):
    repo = _get_gh().get_repo(repository)
    pr = repo.get_pull(pull_number)
    if not SHA_RE.fullmatch(expected_head_sha) or pr.head.sha != expected_head_sha:
        return None, _error_response("HEAD_CHANGED", "PR head SHA does not match expected_head_sha", details={"current_head_sha": pr.head.sha})
    return pr, None


def _pull_request_node_id(pr) -> Optional[str]:
    node_id = getattr(pr, "node_id", None)
    if isinstance(node_id, str) and node_id:
        return node_id
    raw_data = getattr(pr, "raw_data", None)
    if isinstance(raw_data, dict) and isinstance(raw_data.get("node_id"), str):
        return raw_data["node_id"]
    return None


def _draft_state_error_from_github(exc: Exception) -> dict:
    status = getattr(exc, "status", None)
    headers = getattr(exc, "headers", None) or {}
    details = {"github_status": status, "github_request_id": headers.get("X-GitHub-Request-Id")}
    if status == 401:
        return _error_response("GITHUB_AUTH_FAILED", "GitHub authentication failed", details=details)
    if status == 403:
        return _error_response("GITHUB_PERMISSION_DENIED", "GitHub permission denied", details=details)
    if status == 404:
        return _error_response("PR_NOT_FOUND", "Pull request not found", details=details)
    if status == 429:
        return _error_response("GITHUB_RATE_LIMITED", "GitHub rate limit exceeded", retryable=True, details=details)
    return _error_response("GITHUB_API_FAILED", "GitHub pull request API failed", retryable=bool(status and status >= 500), details=details)


def _draft_state_operation(repository: str, pull_number: int, expected_head_sha: str, *, ready: bool) -> dict:
    if not SHA_RE.fullmatch(expected_head_sha or ""):
        return _error_response("EXPECTED_HEAD_SHA_REQUIRED", "expected_head_sha must be a full commit SHA")
    try:
        gh = _get_gh()
        repo = gh.get_repo(repository)
        pr = repo.get_pull(pull_number)
    except GithubException as exc:
        return _draft_state_error_from_github(exc)
    except Exception as exc:
        logger.exception("GitHub pull request read failed repository=%s pull=%s", repository, pull_number)
        return _error_response("GITHUB_API_FAILED", "GitHub pull request read failed", details={"error_type": type(exc).__name__})

    current_head = getattr(getattr(pr, "head", None), "sha", None)
    if getattr(pr, "merged", False):
        return _error_response("ALREADY_MERGED", "Pull request is already merged")
    if getattr(pr, "state", None) != "open":
        return _error_response("PR_NOT_OPEN", "Pull request is not open")
    if current_head != expected_head_sha:
        return _error_response("HEAD_CHANGED", "PR head SHA does not match expected_head_sha", details={"current_head_sha": current_head})
    previous_draft = bool(getattr(pr, "draft", False))
    if ready and not previous_draft:
        return {"ok": True, "repository": repository, "pull_number": pull_number, "draft": False,
                "previous_draft": False, "head_sha": current_head, "state": "open", "changed": False,
                "message": "Pull request is already ready for review"}
    if not ready and previous_draft:
        return {"ok": True, "repository": repository, "pull_number": pull_number, "draft": True,
                "previous_draft": True, "head_sha": current_head, "state": "open", "changed": False,
                "message": "Pull request is already draft"}
    node_id = _pull_request_node_id(pr)
    if not node_id:
        return _error_response("PR_NODE_ID_MISSING", "Pull request GraphQL node ID is missing")

    operation_name = "MarkReady" if ready else "ConvertToDraft"
    mutation = _MARK_READY_MUTATION if ready else _CONVERT_DRAFT_MUTATION
    mutation_field = "markPullRequestReadyForReview" if ready else "convertPullRequestToDraft"
    graphql = github_graphql_request(mutation, {"pullRequestId": node_id}, operation_name)
    if not graphql.get("ok"):
        return graphql
    result = graphql.get("data", {}).get(mutation_field)
    returned_pr = result.get("pullRequest") if isinstance(result, dict) else None
    if not isinstance(returned_pr, dict):
        return _error_response("GITHUB_GRAPHQL_INVALID_RESPONSE", "GitHub GraphQL mutation returned no pull request")
    expected_draft = not ready
    if returned_pr.get("isDraft") is not expected_draft:
        return _error_response("READY_STATE_NOT_CONFIRMED" if ready else "DRAFT_STATE_NOT_CONFIRMED",
                               "GitHub GraphQL mutation did not return the requested draft state")
    if returned_pr.get("headRefOid") != expected_head_sha:
        return _error_response("HEAD_CHANGED", "PR head SHA changed during GraphQL mutation",
                               details={"current_head_sha": returned_pr.get("headRefOid")})
    try:
        confirmed = repo.get_pull(pull_number)
    except GithubException as exc:
        return _draft_state_error_from_github(exc)
    except Exception as exc:
        logger.exception("GitHub pull request confirmation read failed repository=%s pull=%s", repository, pull_number)
        return _error_response("GITHUB_API_FAILED", "GitHub pull request confirmation failed", details={"error_type": type(exc).__name__})
    confirmed_head = getattr(getattr(confirmed, "head", None), "sha", None)
    if getattr(confirmed, "merged", False):
        return _error_response("ALREADY_MERGED", "Pull request was merged during state change")
    if getattr(confirmed, "state", None) != "open" or confirmed_head != expected_head_sha:
        return _error_response("HEAD_CHANGED", "Pull request changed during state confirmation", details={"current_head_sha": confirmed_head})
    if bool(getattr(confirmed, "draft", None)) is not expected_draft:
        return _error_response("READY_STATE_NOT_CONFIRMED" if ready else "DRAFT_STATE_NOT_CONFIRMED",
                               "REST readback did not confirm the requested draft state")
    return {"ok": True, "repository": repository, "pull_number": pull_number,
            "draft": expected_draft, "previous_draft": previous_draft,
            "head_sha": confirmed_head, "state": "open", "changed": True,
            "github_request_id": graphql.get("github_request_id")}


def mark_github_pull_request_ready(repository: str, pull_number: int, expected_head_sha: str) -> dict:
    return _draft_state_operation(repository, pull_number, expected_head_sha, ready=True)


def convert_github_pull_request_to_draft(repository: str, pull_number: int, expected_head_sha: str) -> dict:
    return _draft_state_operation(repository, pull_number, expected_head_sha, ready=False)


def update_github_pull_request_branch(repository: str, pull_number: int, expected_head_sha: str) -> dict:
    pr, error = _pr_with_sha(repository, pull_number, expected_head_sha)
    if error: return error
    result = pr.update_branch()
    return {"ok": True, "message": getattr(result, "message", "branch update requested"), "repository": repository, "pull_number": pull_number}


def request_github_pull_request_reviewers(repository: str, pull_number: int, reviewers_json: str, team_reviewers_json: str, expected_head_sha: str) -> dict:
    pr, error = _pr_with_sha(repository, pull_number, expected_head_sha)
    if error: return error
    users, teams = __import__("json").loads(reviewers_json or "[]"), __import__("json").loads(team_reviewers_json or "[]")
    pr.create_review_request(reviewers=users, team_reviewers=teams)
    return {"ok": True, "requested_reviewers": users, "requested_teams": teams}


def remove_github_pull_request_reviewers(repository: str, pull_number: int, reviewers_json: str, team_reviewers_json: str, expected_head_sha: str) -> dict:
    pr, error = _pr_with_sha(repository, pull_number, expected_head_sha)
    if error: return error
    users, teams = __import__("json").loads(reviewers_json or "[]"), __import__("json").loads(team_reviewers_json or "[]")
    pr.delete_review_request(reviewers=users, team_reviewers=teams)
    return {"ok": True, "removed_reviewers": users, "removed_teams": teams}


def delete_github_branch(repository: str, branch: str, expected_head_sha: str, confirm: bool = False) -> dict:
    if not confirm: return _error_response("CONFIRM_REQUIRED", "confirm must be true")
    if branch in ("main", "master"): return _error_response("BRANCH_DELETE_DENIED", "default branches cannot be deleted")
    if not SHA_RE.fullmatch(expected_head_sha): return _error_response("EXPECTED_HEAD_SHA_REQUIRED", "expected_head_sha must be a full 40-character SHA")
    gh = _get_gh()
    try:
        repo_obj = gh.get_repo(repository)
        if branch == repo_obj.default_branch: return _error_response("BRANCH_DELETE_DENIED", "default branch cannot be deleted")
        branch_obj = repo_obj.get_branch(branch)
        if getattr(branch_obj, "protected", False): return _error_response("PROTECTED_BRANCH", "protected branch cannot be deleted")
        if branch_obj.commit.sha != expected_head_sha: return _error_response("HEAD_CHANGED", "branch head SHA does not match expected_head_sha", details={"current_head_sha": branch_obj.commit.sha})
        # fork 来源分支不属于目标仓库，且不允许由该工具跨仓库删除。
        ref = repo_obj.get_git_ref(f"heads/{branch}")
        ref.delete()
        return {"ok": True, "repository": repository, "branch": branch, "deleted": True}
    except GithubException as e:
        if e.status == 403: return _error_response("BRANCH_DELETE_PERMISSION_DENIED", "GitHub denied branch deletion")
        if e.status == 404: return _error_response("BRANCH_NOT_FOUND", "branch not found")
        raise GitHubApiError(e.status, str(e))


def list_github_pull_request_files(
    repository: str,
    pull_number: int,
    limit: int = 100,
    page: int = 1,
    include_patch: bool = True,
) -> dict:
    gh = _get_gh()
    try:
        repo = gh.get_repo(repository)
        try:
            pr = repo.get_pull(pull_number)
        except GithubException:
            return {"ok": False, "error": {"code": "PULL_REQUEST_NOT_FOUND", "message": f"PR #{pull_number} not found", "retryable": False}}

        files = list(pr.get_files())
        total = len(files)
        start = (page - 1) * limit
        end = start + limit
        page_files = files[start:end]

        MAX_PATCH_LEN = 3000
        MAX_TOTAL_PATCH = 50000
        total_patch_len = 0
        patch_truncated = False

        result_files = []
        for f in page_files:
            patch = f.patch if include_patch else None
            if patch:
                total_patch_len += len(patch)
                if total_patch_len > MAX_TOTAL_PATCH:
                    patch_truncated = True
                    patch = (patch[:MAX_PATCH_LEN] if patch else None)

            result_files.append({
                "filename": f.filename,
                "status": f.status,
                "additions": f.additions,
                "deletions": f.deletions,
                "changes": f.changes,
                "previous_filename": getattr(f, "previous_filename", None),
                "blob_url": f.blob_url,
                "raw_url": f.raw_url,
                "patch": patch,
            })

        return {
            "ok": True,
            "repository": repository,
            "pull_number": pull_number,
            "total_count": total,
            "page": page,
            "limit": limit,
            "has_more": end < total,
            "patch_truncated": patch_truncated,
            "files": result_files,
        }
    except GithubException as e:
        if e.status == 404:
            return {"ok": False, "error": {"code": "REPOSITORY_NOT_FOUND", "message": str(e), "retryable": False}}
        raise GitHubApiError(e.status, str(e))


def update_github_pull_request(
    repository: str,
    pull_number: int,
    title: Optional[str] = None,
    body: Optional[str] = None,
    state: Optional[str] = None,
    base_branch: Optional[str] = None,
    expected_head_sha: str = "",
) -> dict:
    gh = _get_gh()
    try:
        repo = gh.get_repo(repository)
        try:
            pr = repo.get_pull(pull_number)
        except GithubException:
            return {"ok": False, "error": {"code": "PULL_REQUEST_NOT_FOUND", "message": f"PR #{pull_number} not found", "retryable": False}}

        if expected_head_sha:
            current_head = pr.head.sha
            if current_head != expected_head_sha:
                return {
                    "ok": False,
                    "error": {
                        "code": "HEAD_SHA_CHANGED",
                        "message": f"Head SHA changed: expected {expected_head_sha[:12]}, got {current_head[:12]}",
                        "retryable": True,
                        "details": {"current_head_sha": current_head, "expected_head_sha": expected_head_sha},
                    },
                }

        if title is not None:
            pr.edit(title=title)
        if body is not None:
            pr.edit(body=body)
        if state is not None:
            if state == "closed":
                pr.edit(state="closed")
            elif state == "open":
                pr.edit(state="open")
        if base_branch is not None:
            pr.edit(base=base_branch)

        # Force refresh
        pr = repo.get_pull(pull_number)
        return {
            "ok": True,
            "repository": repository,
            "pull_number": pr.number,
            "title": pr.title,
            "body": pr.body,
            "state": pr.state,
            "head_branch": pr.head.ref,
            "head_sha": pr.head.sha,
            "base_branch": pr.base.ref,
            "base_sha": pr.base.sha,
            "updated_at": pr.updated_at.isoformat() if pr.updated_at else None,
            "html_url": pr.html_url,
        }
    except GithubException as e:
        if e.status == 404:
            return {"ok": False, "error": {"code": "PULL_REQUEST_NOT_FOUND", "message": str(e), "retryable": False}}
        raise GitHubApiError(e.status, str(e))


def get_github_pull_request_checks(repository: str, pull_number: int) -> dict:
    gh = _get_gh()
    try:
        repo = gh.get_repo(repository)
        try:
            pr = repo.get_pull(pull_number)
        except GithubException:
            return {"ok": False, "error": {"code": "PULL_REQUEST_NOT_FOUND", "message": f"PR #{pull_number} not found", "retryable": False}}

        head_sha = pr.head.sha
        checks_status, checks_payload, checks_headers = _github_get_json(
            f"/repos/{repository}/commits/{head_sha}/check-runs"
        )
        statuses_status, statuses_payload, statuses_headers = _github_get_json(
            f"/repos/{repository}/commits/{head_sha}/status"
        )

        check_runs = checks_payload.get("check_runs", []) if isinstance(checks_payload, dict) else []
        statuses = statuses_payload if isinstance(statuses_payload, list) else []
        checks_access = "available" if checks_status == 200 else "permission_denied" if checks_status == 403 else "unavailable"
        statuses_access = "available" if statuses_status == 200 else "permission_denied" if statuses_status == 403 else "unavailable"
        checks_error_code = None if checks_status == 200 else _github_endpoint_error_code("CHECKS", checks_status, checks_headers)
        statuses_error_code = None if statuses_status == 200 else _github_endpoint_error_code("STATUSES", statuses_status, statuses_headers)
        checks_result_code = "CHECKS_EMPTY" if checks_status == 200 and not check_runs else "CHECKS_AVAILABLE" if checks_status == 200 else checks_error_code
        statuses_result_code = "STATUSES_EMPTY" if statuses_status == 200 and not statuses else "STATUSES_AVAILABLE" if statuses_status == 200 else statuses_error_code
        oauth_scopes = _parse_oauth_scopes(checks_headers)
        if not oauth_scopes:
            oauth_scopes = _parse_oauth_scopes(statuses_headers)
        credential_type = _classify_github_credential_type(settings.GITHUB_TOKEN.get_secret_value())
        capabilities = _github_auth_capabilities(credential_type, oauth_scopes, checks_status, statuses_status)
        diagnostic_code = None
        if checks_status == 403:
            if credential_type == "fine_grained_pat":
                diagnostic_code = "FINE_GRAINED_PAT_CHECKS_UNAVAILABLE"
            elif credential_type == "classic_pat" and "repo" not in oauth_scopes:
                diagnostic_code = "CLASSIC_PAT_REPO_SCOPE_REQUIRED"
            elif credential_type == "classic_pat":
                diagnostic_code = "CLASSIC_PAT_REPOSITORY_ACCESS_DENIED"

        pending = sum(1 for c in check_runs if c.get("status") in ("queued", "in_progress"))
        passed = sum(1 for c in check_runs if c.get("conclusion") == "success")
        failed = sum(1 for c in check_runs if c.get("conclusion") in ("failure", "timed_out", "cancelled", "action_required"))

        overall_status = "unavailable" if checks_status != 200 else "pending" if pending > 0 else "completed"
        overall_conclusion = None if checks_status != 200 else "failure" if failed > 0 else "success" if passed > 0 else "neutral"

        policy = _repository_merge_policy(repository)
        required_sources = _required_check_sources(repo, getattr(getattr(pr, "base", None), "ref", "main"), policy)
        required_names = set(required_sources["contexts"]) | set(required_sources["checks"])
        raw_checks = []
        for c in check_runs[:50]:
            details_url = c.get("details_url") or c.get("html_url") or ""
            job_match = re.search(r"/jobs?/(\d+)", details_url)
            diagnostic = {}
            if job_match:
                job_id = job_match.group(1)
                job_status, job_payload, _ = _github_get_json(f"/repos/{repository}/actions/jobs/{job_id}")
                if job_status == 200 and isinstance(job_payload, dict):
                    started, completed = job_payload.get("started_at"), job_payload.get("completed_at")
                    try:
                        duration = (datetime.fromisoformat(completed.replace("Z", "+00:00")) - datetime.fromisoformat(started.replace("Z", "+00:00"))).total_seconds()
                    except (AttributeError, TypeError, ValueError):
                        duration = None
                    diagnostic = {"job_id": job_payload.get("id"), "run_id": job_payload.get("run_id"), "runner_id": job_payload.get("runner_id"), "steps": job_payload.get("steps"), "duration_seconds": duration}
                    if c.get("conclusion") not in ("success", "neutral", "skipped"):
                        diagnostic["logs_http_status"] = _github_get_json(f"/repos/{repository}/actions/jobs/{job_id}/logs")[0]
                elif job_status:
                    diagnostic = {"job_id": int(job_id), "logs_http_status": job_status}
            raw_checks.append({"name": c.get("name"), "status": c.get("status"), "conclusion": c.get("conclusion"), "url": c.get("html_url"), "started_at": c.get("started_at"), "completed_at": c.get("completed_at"), **diagnostic})
        classified_checks, seen_required = [], set()
        for check in raw_checks:
            name = check.get("name") or ""
            if name in required_sources["contexts"]:
                required_source = "branch_protection" if "branch_protection" in required_sources["sources"] else "ruleset"
            elif name in required_sources["checks"]:
                required_source = "repository_policy" if name in policy.get("required_workflows", []) else "ruleset"
            else:
                required_source = ""
            if required_source: seen_required.add(name)
            classified_checks.append(_classify_check_run(check, bool(required_source), required_source))
        for missing in sorted(required_names - seen_required):
            classified_checks.append({"name": missing, "status": "missing", "conclusion": None, "is_required": True, "required_source": "branch_protection" if missing in required_sources["contexts"] else "ruleset", "classification": "REQUIRED_CHECK_MISSING", "blocking": True})
        status_records = []
        for status in statuses[:50]:
            required = status.get("context") in required_sources["contexts"]
            state = status.get("state")
            classification = ("REQUIRED_CHECK_FAILED" if required and state not in ("success", "pending") else "REQUIRED_CHECK_PENDING" if required and state == "pending" else "NON_REQUIRED_CHECK_FAILED" if state not in ("success", "pending") else "NON_REQUIRED_CHECK_PENDING" if state == "pending" else "PASS")
            status_records.append({"context": status.get("context"), "state": state, "description": status.get("description"), "url": status.get("target_url"), "is_required": required, "required_source": "branch_protection" if required else "", "classification": classification, "blocking": required and state != "success"})
        all_classified = classified_checks + status_records
        blocking_classifications = [item["classification"] for item in all_classified if item.get("blocking")]
        overall_status = "unavailable" if checks_status != 200 else "pending" if any(item.get("status") in ("queued", "in_progress", "pending") for item in all_classified) else "completed"
        overall_conclusion = None if checks_status != 200 else "failure" if any(item["classification"] == "REQUIRED_CHECK_FAILED" for item in all_classified) else "success" if all_classified and all(item["classification"] in ("PASS", "GITHUB_ACTIONS_QUOTA_OR_INFRA_FAILURE", "GITHUB_ACTIONS_CODE_FAILURE") for item in all_classified) else "neutral"

        return {
            "ok": True,
            "repository": repository,
            "pull_number": pull_number,
            "head_sha": head_sha,
            "checks_access": checks_access,
            "statuses_access": statuses_access,
            "checks_error_code": checks_error_code,
            "statuses_error_code": statuses_error_code,
            "checks_result_code": checks_result_code,
            "statuses_result_code": statuses_result_code,
            "credential_type": credential_type,
            "auth_capabilities": capabilities,
            "diagnostic_code": diagnostic_code,
            "checks_http_status": checks_status,
            "statuses_http_status": statuses_status,
            "checks_response_headers": checks_headers,
            "statuses_response_headers": statuses_headers,
            "overall_status": overall_status,
            "overall_conclusion": overall_conclusion,
            "pending_count": pending,
            "passed_count": passed,
            "failed_count": failed,
            "total_checks": len(check_runs) + len(statuses),
            "required_check_sources": required_sources,
            "checks_policy": policy,
            "blocking_classifications": blocking_classifications,
            "checks": classified_checks,
            "statuses": status_records,
        }
    except requests.RequestException:
        return _error_response("CHECKS_API_FAILED", "GitHub Checks API request failed", retryable=True)
    except GithubException as e:
        if e.status == 404:
            return {"ok": False, "error": {"code": "REPOSITORY_NOT_FOUND", "message": str(e), "retryable": False}}
        raise GitHubApiError(e.status, str(e))


def list_github_pull_request_comments(
    repository: str,
    pull_number: int,
    comment_type: str = "all",
    limit: int = 100,
    page: int = 1,
) -> dict:
    gh = _get_gh()
    try:
        repo = gh.get_repo(repository)
        try:
            pr = repo.get_pull(pull_number)
        except GithubException:
            return {"ok": False, "error": {"code": "PULL_REQUEST_NOT_FOUND", "message": f"PR #{pull_number} not found", "retryable": False}}

        all_comments = []

        if comment_type in ("all", "issue"):
            for c in pr.get_issue_comments():
                all_comments.append({
                    "comment_id": c.id,
                    "type": "issue",
                    "author": c.user.login if c.user else None,
                    "body": c.body[:5000],
                    "body_truncated": len(c.body) > 5000 if c.body else False,
                    "path": None,
                    "line": None,
                    "side": None,
                    "created_at": c.created_at.isoformat() if c.created_at else None,
                    "updated_at": c.updated_at.isoformat() if c.updated_at else None,
                    "html_url": c.html_url,
                })

        if comment_type in ("all", "review"):
            for c in pr.get_review_comments():
                all_comments.append({
                    "comment_id": c.id,
                    "type": "review",
                    "author": c.user.login if c.user else None,
                    "body": c.body[:5000],
                    "body_truncated": len(c.body) > 5000 if c.body else False,
                    "path": c.path,
                    "line": c.original_position if hasattr(c, "original_position") else c.position if hasattr(c, "position") else None,
                    "side": "RIGHT" if hasattr(c, "side") and c.side == "RIGHT" else None,
                    "created_at": c.created_at.isoformat() if c.created_at else None,
                    "updated_at": c.updated_at.isoformat() if c.updated_at else None,
                    "html_url": c.html_url,
                })

        all_comments.sort(key=lambda x: x["created_at"] or "", reverse=True)
        total = len(all_comments)
        start = (page - 1) * limit
        end = start + limit

        return {
            "ok": True,
            "repository": repository,
            "pull_number": pull_number,
            "total_count": total,
            "page": page,
            "limit": limit,
            "has_more": end < total,
            "comments": all_comments[start:end],
        }
    except GithubException as e:
        if e.status == 404:
            return {"ok": False, "error": {"code": "REPOSITORY_NOT_FOUND", "message": str(e), "retryable": False}}
        raise GitHubApiError(e.status, str(e))


def create_github_pull_request_comment(
    repository: str,
    pull_number: int,
    body: str,
    expected_head_sha: str = "",
) -> dict:
    gh = _get_gh()
    try:
        repo = gh.get_repo(repository)
        try:
            pr = repo.get_pull(pull_number)
        except GithubException:
            return {"ok": False, "error": {"code": "PULL_REQUEST_NOT_FOUND", "message": f"PR #{pull_number} not found", "retryable": False}}

        if pr.state != "open":
            return {"ok": False, "error": {"code": "INVALID_ARGUMENT", "message": f"PR #{pull_number} is {pr.state}, must be open", "retryable": False}}

        if expected_head_sha and pr.head.sha != expected_head_sha:
            return {
                "ok": False,
                "error": {
                    "code": "HEAD_SHA_CHANGED",
                    "message": f"Head SHA changed: expected {expected_head_sha[:12]}, got {pr.head.sha[:12]}",
                    "retryable": True,
                    "details": {"current_head_sha": pr.head.sha},
                },
            }

        if not body or not body.strip():
            return {"ok": False, "error": {"code": "INVALID_ARGUMENT", "message": "Comment body cannot be empty", "retryable": False}}

        if len(body) > 65536:
            return {"ok": False, "error": {"code": "INVALID_ARGUMENT", "message": f"Comment body too long ({len(body)} chars, max 65536)", "retryable": False}}

        comment = pr.create_issue_comment(body)

        return {
            "ok": True,
            "comment_id": comment.id,
            "author": comment.user.login if comment.user else None,
            "body": comment.body[:2000],
            "body_truncated": len(comment.body) > 2000,
            "created_at": comment.created_at.isoformat() if comment.created_at else None,
            "html_url": comment.html_url,
            "pull_number": pull_number,
            "head_sha": pr.head.sha,
        }
    except GithubException as e:
        if e.status == 404:
            return {"ok": False, "error": {"code": "REPOSITORY_NOT_FOUND", "message": str(e), "retryable": False}}
        raise GitHubApiError(e.status, str(e))


# ============================================================================
# Development History Tools
# ============================================================================

import yaml
import os
from datetime import datetime, timezone, timedelta

_IDENTITY_CACHE = None

def _load_identities():
    global _IDENTITY_CACHE
    if _IDENTITY_CACHE is not None:
        return _IDENTITY_CACHE
    config_path = os.environ.get("REPORT_IDENTITIES_PATH", "/app/config/github_report_identities.yml")
    if os.path.exists(config_path):
        with open(config_path) as f:
            _IDENTITY_CACHE = yaml.safe_load(f) or {}
    else:
        _IDENTITY_CACHE = {"identities": {}}
    return _IDENTITY_CACHE


def _load_report_rules():
    config_path = os.environ.get("REPORT_RULES_PATH", "/app/config/github_report_rules.yml")
    if os.path.exists(config_path):
        with open(config_path) as f:
            return yaml.safe_load(f) or {}
    return {}


def _resolve_identity(identity: str) -> dict:
    config = _load_identities()
    ident = config.get("identities", {}).get(identity, {})
    return {
        "github_logins": ident.get("github_logins", []),
        "commit_emails": ident.get("commit_emails", []),
        "commit_names": ident.get("commit_names", []),
        "timezone": ident.get("timezone", "UTC"),
        "exclude_logins": ident.get("exclude_logins", []),
    }


def _parse_time(time_str: str, default_tz: str) -> datetime:
    """Parse ISO time string with timezone, default to identity timezone."""
    if not time_str:
        return None
    try:
        dt = datetime.fromisoformat(time_str)
        if dt.tzinfo is None:
            import zoneinfo
            tz = zoneinfo.ZoneInfo(default_tz)
            dt = dt.replace(tzinfo=tz)
        return dt
    except Exception:
        return None


def _is_author_match(author_name: str, author_login: str, author_email: str, identity_info: dict) -> bool:
    logins = identity_info.get("github_logins", [])
    emails = identity_info.get("commit_emails", [])
    names = identity_info.get("commit_names", [])

    if author_login and author_login in logins:
        return True
    if author_email:
        email_lower = author_email.lower()
        for e in emails:
            if e.lower() == email_lower:
                return True
    if author_name:
        for n in names:
            if n.lower() == author_name.lower():
                return True

    return False


def _is_excluded(author_login: str, committer_login: str, identity_info: dict) -> bool:
    exclude = identity_info.get("exclude_logins", [])
    if author_login in exclude:
        return True
    if committer_login in exclude:
        return True
    return False


def list_github_commits(
    repository: str,
    branch: str = "",
    author: str = "",
    identity: str = "",
    since: str = "",
    until: str = "",
    path: str = "",
    include_merge_commits: bool = False,
    limit: int = 100,
    page: int = 1,
) -> dict:
    gh = _get_gh()
    repo = gh.get_repo(repository)

    identity_info = _resolve_identity(identity) if identity else {}
    tz = identity_info.get("timezone", "UTC")

    since_dt = _parse_time(since, tz) if since else None
    until_dt = _parse_time(until, tz) if until else None

    kwargs = {}
    if branch:
        kwargs["sha"] = branch
    if author:
        kwargs["author"] = author
    if path:
        kwargs["path"] = path
    if since_dt:
        kwargs["since"] = since_dt
    if until_dt:
        kwargs["until"] = until_dt

    try:
        commits = []
        for idx, c in enumerate(repo.get_commits(**kwargs)):
            if idx >= 100:  # Hard limit per repo
                break
            commits.append(c)
    except GithubException:
        # Try without time filters if they cause issues
        kwargs.pop("since", None)
        kwargs.pop("until", None)
        commits = []
        for idx, c in enumerate(repo.get_commits(**kwargs)):
            if idx >= 100:  # Hard limit per repo
                break
            commits.append(c)

    rules = _load_report_rules()
    exclude_subjects = rules.get("exclude_commit_subject_patterns", [])
    exclude_authors = rules.get("exclude_authors", [])
    exclude_paths = rules.get("exclude_paths", [])

    seen_sha = set()
    filtered = []
    MAX_FILES_PER_COMMIT = 50

    for c in commits:
        if c.sha in seen_sha:
            continue
        seen_sha.add(c.sha)

        msg = c.commit.message
        subject = msg.split("\n")[0] if msg else ""

        author_name = None
        author_login = None
        author_email = None
        try:
            author_name = c.commit.author.name if c.commit.author else None
            author_email = c.commit.author.email if c.commit.author else None
            author_login = c.author.login if c.author else None
        except Exception:
            pass

        # Time filter (manual)
        commit_date = None
        try:
            if c.commit.author and c.commit.author.date:
                commit_date = c.commit.author.date
            elif c.commit.committer and c.commit.committer.date:
                commit_date = c.commit.committer.date
        except Exception:
            pass

        if since_dt and commit_date and commit_date < since_dt:
            continue
        if until_dt and commit_date and commit_date > until_dt:
            continue

        # Identity filter
        if identity:
            if not _is_author_match(author_name, author_login, author_email, identity_info):
                continue

        # Exclude bots
        author_login_val = author_login or ""
        if author_login_val in exclude_authors:
            continue

        # Exclude merge commits
        is_merge = len(list(c.parents)) > 1
        if is_merge and not include_merge_commits:
            continue

        # Exclude subject patterns
        skip = False
        for pat in exclude_subjects:
            if re.search(pat, subject):
                skip = True
                break
        if skip:
            continue

        # Get file info
        try:
            cfiles = list(c.files)
        except Exception:
            cfiles = []
        file_names = []
        additions = 0
        deletions = 0
        for f in cfiles:
            excluded_path = False
            for ep in exclude_paths:
                import fnmatch
                if fnmatch.fnmatch(f.filename, ep):
                    excluded_path = True
                    break
            if not excluded_path:
                file_names.append(f.filename)
                additions += f.additions
                deletions += f.deletions

        files_truncated = len(file_names) > MAX_FILES_PER_COMMIT

        # Branch names from batch cache (pre-built outside loop)
        branch_names = _branch_cache.get(c.sha, [])
        # PR numbers
        pull_numbers = []
        try:
            for p in c.get_pulls():
                pull_numbers.append(p.number)
        except Exception:
            pass

        filtered.append({
            "sha": c.sha,
            "short_sha": c.sha[:7],
            "message": msg[:2000],
            "subject": subject[:500],
            "author_name": author_name,
            "author_login": author_login,
            "authored_at": commit_date.isoformat() if commit_date else None,
            "committed_at": c.commit.committer.date.isoformat() if c.commit.committer and c.commit.committer.date else None,
            "committer_name": c.commit.committer.name if c.commit.committer else None,
            "html_url": c.html_url,
            "branch_names": branch_names[:20],
            "parent_count": len(list(c.parents)),
            "is_merge_commit": is_merge,
            "additions": additions,
            "deletions": deletions,
            "changed_files": len(file_names),
            "file_names": file_names[:MAX_FILES_PER_COMMIT],
            "files_truncated": files_truncated,
            "pull_numbers": pull_numbers,
            "repository": repository,
        })

        if len(filtered) >= limit * page + limit:
            break

    total = len(filtered)
    start = (page - 1) * limit
    end = start + limit

    return {
        "ok": True,
        "repository": repository,
        "total_count": total,
        "page": page,
        "limit": limit,
        "has_more": end < total,
        "commits": filtered[start:end],
    }


def _pr_to_dict(pr: PullRequest, matched_activities: list = None) -> dict:
    return {
        "repository": pr.base.repo.full_name,
        "pull_number": pr.number,
        "title": pr.title,
        "state": pr.state,
        "draft": pr.draft,
        "author": pr.user.login if pr.user else None,
        "head_branch": pr.head.ref,
        "head_sha": pr.head.sha,
        "base_branch": pr.base.ref,
        "created_at": pr.created_at.isoformat() if pr.created_at else None,
        "updated_at": pr.updated_at.isoformat() if pr.updated_at else None,
        "closed_at": pr.closed_at.isoformat() if pr.closed_at else None,
        "merged_at": pr.merged_at.isoformat() if pr.merged_at else None,
        "merge_commit_sha": pr.merge_commit_sha,
        "commits": 0,
        "changed_files": 0,
        "additions": 0,
        "deletions": 0,
        "review_status": None,
        "html_url": pr.html_url,
        "matched_activities": matched_activities or [],
    }


def search_github_pull_request_history(
    repositories: list,
    identity: str,
    activity: str = "all",
    since: str = "",
    until: str = "",
    state: str = "all",
    include_drafts: bool = True,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """Optimized: no N+1 per-PR API calls. Only scans PR metadata."""
    identity_info = _resolve_identity(identity)
    login = identity_info.get("github_logins", [None])[0] if identity_info.get("github_logins") else None
    tz = identity_info.get("timezone", "UTC")
    since_dt = _parse_time(since, tz) if since else None
    until_dt = _parse_time(until, tz) if until else None

    gh = _get_gh()
    results = []

    for repo_name in repositories[:10]:
        try:
            repo = gh.get_repo(repo_name)
            prs = []
            for _i_pr, _pr in enumerate(repo.get_pulls(state="all", sort="updated", direction="desc")):
                prs.append(_pr)
                if _i_pr >= 59:
                    break
        except Exception:
            continue

        for pr in prs:
            if len(results) >= limit * 3:
                break
            if not include_drafts and pr.draft:
                continue

            author_login = pr.user.login if pr.user else None
            matched = []

            created = pr.created_at
            updated = pr.updated_at

            def _safe_merged_at():
                try:
                    return pr.merged_at
                except Exception:
                    return None

            def _safe_closed_at():
                try:
                    return pr.closed_at
                except Exception:
                    return None

            # authored
            if activity in ("all", "authored"):
                if author_login == login and created:
                    if (not since_dt or created >= since_dt) and (not until_dt or created <= until_dt):
                        matched.append("authored")

            # updated
            if activity in ("all", "updated") and "authored" not in matched:
                if author_login == login and updated:
                    if (not since_dt or updated >= since_dt) and (not until_dt or updated <= until_dt):
                        matched.append("updated")

            # merged
            if activity in ("all", "merged") and pr.merged:
                merged_dt = _safe_merged_at()
                if merged_dt and (not since_dt or merged_dt >= since_dt) and (not until_dt or merged_dt <= until_dt):
                    matched.append("merged")

            # closed (without merge)
            if activity in ("all", "closed") and pr.state == "closed" and not pr.merged:
                closed_dt = _safe_closed_at()
                if closed_dt and (not since_dt or closed_dt >= since_dt) and (not until_dt or closed_dt <= until_dt):
                    matched.append("closed")

            if matched:
                results.append({
                    "repository": repo_name, "pull_number": pr.number, "title": pr.title,
                    "state": pr.state, "draft": pr.draft, "author": author_login,
                    "head_branch": pr.head.ref, "head_sha": pr.head.sha,
                    "base_branch": pr.base.ref,
                    "created_at": created.isoformat() if created else None,
                    "updated_at": updated.isoformat() if updated else None,
                    "closed_at": closed_dt.isoformat() if closed_dt else None,
                    "merged_at": merged_dt.isoformat() if merged_dt else None,
                    "merge_commit_sha": pr.merge_commit_sha,
                    "commits": 0, "changed_files": 0,
                    "additions": 0, "deletions": 0,
                    "html_url": pr.html_url, "matched_activities": matched,
                })

    results.sort(key=lambda x: x["updated_at"] or "", reverse=True)
    total = len(results)
    page_results = results[offset:offset + limit]

    return {
        "ok": True, "identity": identity, "total_count": total,
        "limit": limit, "offset": offset,
        "has_more": (offset + limit) < total,
        "pull_requests": page_results,
    }

def list_github_review_history(
    repositories: list,
    identity: str,
    since: str = "",
    until: str = "",
    states: list = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    if states is None:
        states = ["APPROVED", "CHANGES_REQUESTED", "COMMENTED"]

    identity_info = _resolve_identity(identity)
    login = identity_info.get("github_logins", [None])[0] if identity_info.get("github_logins") else None
    tz = identity_info.get("timezone", "UTC")
    since_dt = _parse_time(since, tz) if since else None
    until_dt = _parse_time(until, tz) if until else None

    gh = _get_gh()
    results = []
    seen_prs = set()

    for repo_name in repositories[:10]:
        try:
            repo = gh.get_repo(repo_name)
            prs = []
            for _i_rpr, _rpr in enumerate(repo.get_pulls(state="all", sort="updated", direction="desc")):
                prs.append(_rpr)
                if _i_rpr >= 9:
                    break
        except GithubException:
            continue

        for pr in prs:
            try:
                reviews = pr.get_reviews()
            except GithubException:
                continue

            for r in reviews:
                if not r.user or r.user.login != login:
                    continue
                if states and r.state not in states:
                    continue
                if since_dt and r.submitted_at and r.submitted_at < since_dt:
                    continue
                if until_dt and r.submitted_at and r.submitted_at > until_dt:
                    continue

                results.append({
                    "repository": repo_name,
                    "pull_number": pr.number,
                    "pull_title": pr.title,
                    "review_id": r.id,
                    "reviewer": r.user.login,
                    "state": r.state,
                    "body": (r.body or "")[:2000],
                    "submitted_at": r.submitted_at.isoformat() if r.submitted_at else None,
                    "commit_sha": r.commit_id,
                    "html_url": r.html_url,
                    "comments_count": 0,
                })
                seen_prs.add(pr.number)
                break

    results.sort(key=lambda x: x["submitted_at"] or "", reverse=True)
    total = len(results)
    page_results = results[offset:offset + limit]

    return {
        "ok": True,
        "identity": identity,
        "total_count": total,
        "review_events_count": total,
        "unique_pull_requests_count": len(seen_prs),
        "limit": limit,
        "offset": offset,
        "has_more": (offset + limit) < total,
        "reviews": page_results,
    }


def list_github_issue_history(
    repositories: list,
    identity: str,
    activity: str = "all",
    since: str = "",
    until: str = "",
    state: str = "all",
    limit: int = 100,
    offset: int = 0,
) -> dict:
    identity_info = _resolve_identity(identity)
    login = identity_info.get("github_logins", [None])[0] if identity_info.get("github_logins") else None
    tz = identity_info.get("timezone", "UTC")
    since_dt = _parse_time(since, tz) if since else None
    until_dt = _parse_time(until, tz) if until else None

    gh = _get_gh()
    results = []

    for repo_name in repositories[:10]:
        try:
            repo = gh.get_repo(repo_name)
            issues = []
            for _i_is, _is in enumerate(repo.get_issues(state=state if state != "all" else "all", sort="updated", direction="desc")):
                issues.append(_is)
                if _i_is >= 49:
                    break
        except GithubException:
            continue

        for issue in issues:
            if issue.pull_request is not None:
                continue

            author_login = issue.user.login if issue.user else None
            matched = []

            if activity in ("all", "authored"):
                if author_login == login:
                    created = issue.created_at
                    if (not since_dt or (created and created >= since_dt)) and \
                       (not until_dt or (created and created <= until_dt)):
                        matched.append("authored")
            if activity in ("all", "updated"):
                updated = issue.updated_at
                if (not since_dt or (updated and updated >= since_dt)) and \
                   (not until_dt or (updated and updated <= until_dt)):
                    if "authored" not in matched:
                        matched.append("updated")
            if activity in ("all", "closed"):
                closed_dt = None
                if issue.state == "closed":
                    try:
                        closed_dt = issue.closed_at
                    except Exception:
                        pass
                    if (not since_dt or closed_dt >= since_dt) and \
                       (not until_dt or closed_dt <= until_dt):
                        matched.append("closed")

            if matched:
                assignees = [a.login for a in issue.assignees] if issue.assignees else []
                labels = [lbl.name for lbl in issue.labels]
                results.append({
                    "repository": repo_name,
                    "issue_number": issue.number,
                    "title": issue.title,
                    "state": issue.state,
                    "author": author_login,
                    "assignees": assignees,
                    "labels": labels,
                    "created_at": issue.created_at.isoformat() if issue.created_at else None,
                    "updated_at": issue.updated_at.isoformat() if issue.updated_at else None,
                    "closed_at": closed_dt.isoformat() if closed_dt else None,
                    "comments": 0,
                    "html_url": issue.html_url,
                    "matched_activities": matched,
                })

    total = len(results)
    page_results = results[offset:offset + limit]

    return {
        "ok": True,
        "identity": identity,
        "total_count": total,
        "limit": limit,
        "offset": offset,
        "has_more": (offset + limit) < total,
        "issues": page_results,
    }


def _fetch_github_actions_runs(repositories: list, since_dt, until_dt, limit: int = 100) -> list:
    gh = _get_gh()
    results = []
    seen = set()
    for repo_name in repositories[:10]:
        try:
            repo = gh.get_repo(repo_name)
            runs = []
            for _i_run, _run in enumerate(repo.get_workflow_runs()):
                runs.append(_run)
                if _i_run >= 99:
                    break
        except GithubException:
            continue
        for r in runs:
            run_id = f"{repo_name}:{r.id}"
            if run_id in seen:
                continue
            seen.add(run_id)

            created = r.created_at
            if since_dt and created and created < since_dt:
                continue
            if until_dt and created and created > until_dt:
                continue

            results.append({
                "repository": repo_name,
                "workflow_name": r.name or "",
                "run_id": r.id,
                "event": r.event or "",
                "branch": r.head_branch,
                "head_sha": r.head_sha,
                "status": r.status or "",
                "conclusion": r.conclusion or "",
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
                "html_url": r.html_url,
            })
    return results


def _fetch_private_ci_jobs(repositories: list, since_dt, until_dt) -> list:
    from app.ci_database import list_jobs
    jobs = list_jobs(limit=500)
    results = []
    for j in jobs:
        repo = j.get("repository", "")
        if repositories and repo not in repositories:
            continue
        finished = j.get("finished_at")
        if finished:
            try:
                dt = datetime.fromisoformat(finished.replace("+00:00", "+00:00"))
                if since_dt and dt < since_dt:
                    continue
                if until_dt and dt > until_dt:
                    continue
            except Exception:
                pass
        results.append(j)
    return results




# ============================================================================
# Cache Implementation
# ============================================================================
import time as _cache_time
import threading

_CACHE = {}
_CACHE_LOCK = threading.Lock()
_CACHE_MAX_ENTRIES = 200
_CACHE_DEFAULT_TTL = 300

def _build_cache_key(tool_name, **kwargs):
    parts = [tool_name]
    for k in sorted(str(kwargs.keys())):
        v = kwargs.get(k, "")
        if isinstance(v, list):
            v = ",".join(sorted(str(x) for x in v))
        parts.append(f"{k}={v}")
    key_raw = "|".join(parts)
    import hashlib
    return hashlib.sha256(key_raw.encode()).hexdigest()

def _cache_get(key):
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry and _cache_time.time() < entry["expires"]:
            return entry["data"], _cache_time.time() - entry["created"]
        if entry:
            del _CACHE[key]
        return None, 0

def _cache_set(key, data, ttl=None):
    ttl_val = ttl if ttl is not None else _CACHE_DEFAULT_TTL
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_MAX_ENTRIES:
            oldest = min(_CACHE.keys(), key=lambda k: _CACHE.get(k, {}).get("expires", 0))
            _CACHE.pop(oldest, None)
        _CACHE[key] = {
            "data": data,
            "expires": _cache_time.time() + ttl_val,
            "created": _cache_time.time(),
        }


# ============================================================================
# Branch cache (ONE-TIME batch per repo)
# ============================================================================
_branch_cache = {}
_branch_cache_lock = threading.Lock()

def _build_branch_map(repo):
    with _branch_cache_lock:
        repo_name = repo.full_name
        if "_repo_" + repo_name in _branch_cache:
            return
        try:
            branches = list(repo.get_branches())
            mapping = {}
            for b in branches:
                sha = b.commit.sha
                if sha not in mapping:
                    mapping[sha] = []
                mapping[sha].append(b.name)
            _branch_cache.update(mapping)
            _branch_cache["_repo_" + repo_name] = True
        except Exception:
            _branch_cache["_repo_" + repo_name] = True


# ============================================================================
# Performance-optimized report function with concurrent execution
# ============================================================================
import concurrent.futures
import time as _perf_time

def get_github_development_history(
    identity: str,
    repositories: list = None,
    since: str = "",
    until: str = "",
    include: list = None,
    include_details: bool = True,
    max_items_per_section: int = 100,
) -> dict:
    import zoneinfo
    timings = {}
    total_start = _perf_time.time()

    if repositories is None:
        from app.ci_repository_config import get_allowed_repositories
        repositories = get_allowed_repositories()

    identity_info = _resolve_identity(identity)
    tz = identity_info.get("timezone", "UTC")
    since_dt = _parse_time(since, tz) if since else datetime.now(timezone.utc) - timedelta(days=7)
    until_dt = _parse_time(until, tz) if until else datetime.now(timezone.utc)

    if include is None:
        include = ["commits", "pull_requests", "reviews", "comments", "issues", "github_actions", "private_ci", "open_work"]

    if (until_dt - since_dt).days > 365:
        return {"ok": False, "error": {"code": "INVALID_ARGUMENT", "message": "Time range exceeds 365 days", "retryable": False}}

    result = {
        "ok": True,
        "identity": identity,
        "timezone": tz,
        "since": since_dt.isoformat(),
        "until": until_dt.isoformat(),
        "repositories": repositories,
        "summary": {},
        "by_repository": [],
        "commits": [],
        "pull_requests": [],
        "reviews": [],
        "issues": [],
        "github_actions": [],
        "private_ci": [],
        "open_work": [],
        "warnings": [],
    }

    max_items = min(max_items_per_section, 100)

    # Pre-build branch cache (ONE-TIME batch)
    if "commits" in include:
        t0 = _perf_time.time()
        gh_pre = _get_gh()
        for repo_name in repositories:
            try:
                _build_branch_map(gh_pre.get_repo(repo_name))
            except Exception:
                pass
        timings["branch_cache"] = _perf_time.time() - t0

    # Sequential execution with per-section timeout protection
    # Each section wraps a github API call and has its own timeout

    if "commits" in include:
        t0 = _perf_time.time()
        try:
            all_commits = []
            seen_sha = set()
            for repo in repositories:
                res = list_github_commits(repo, identity=identity, since=since_dt.isoformat(), until=until_dt.isoformat(), limit=max_items)
                if res.get("ok"):
                    for c in res.get("commits", []):
                        if c["sha"] not in seen_sha:
                            seen_sha.add(c["sha"])
                            all_commits.append(c)
            result["commits"] = all_commits[:max_items_per_section]
            timings["commits"] = _perf_time.time() - t0
        except Exception as e:
            timings["commits"] = _perf_time.time() - t0
            result["warnings"].append(f"commits: {str(e)[:80]}")
            logger.error(f"Commits section failed: {e}")

    if "pull_requests" in include:
        t0 = _perf_time.time()
        try:
            pr_res = search_github_pull_request_history(
                repositories, identity, since=since_dt.isoformat(), until=until_dt.isoformat(), limit=min(max_items, 50))
            result["pull_requests"] = pr_res.get("pull_requests", []) if isinstance(pr_res, dict) else []
            timings["pull_requests"] = _perf_time.time() - t0
        except Exception as e:
            timings["pull_requests"] = _perf_time.time() - t0
            result["warnings"].append(f"pull_requests: {str(e)[:80]}")
            logger.error(f"Pull requests section failed: {e}")

    if "reviews" in include:
        t0 = _perf_time.time()
        try:
            rev_res = list_github_review_history(
                repositories, identity, since=since_dt.isoformat(), until=until_dt.isoformat(), limit=min(max_items, 50))
            result["reviews"] = rev_res.get("reviews", []) if isinstance(rev_res, dict) else []
            timings["reviews"] = _perf_time.time() - t0
        except Exception as e:
            timings["reviews"] = _perf_time.time() - t0
            result["warnings"].append(f"reviews: {str(e)[:80]}")
            logger.error(f"Reviews section failed: {e}")

    if "issues" in include:
        t0 = _perf_time.time()
        try:
            iss_res = list_github_issue_history(repositories, identity, since=since_dt.isoformat(), until=until_dt.isoformat(), limit=max_items)
            result["issues"] = iss_res.get("issues", []) if isinstance(iss_res, dict) else []
            timings["issues"] = _perf_time.time() - t0
        except Exception as e:
            timings["issues"] = _perf_time.time() - t0
            result["warnings"].append(f"issues: {str(e)[:80]}")
            logger.error(f"Issues section failed: {e}")

    if "github_actions" in include:
        t0 = _perf_time.time()
        try:
            result["github_actions"] = _fetch_github_actions_runs(repositories, since_dt, until_dt, max_items)
            timings["github_actions"] = _perf_time.time() - t0
        except Exception as e:
            timings["github_actions"] = _perf_time.time() - t0
            result["warnings"].append(f"github_actions: {str(e)[:80]}")

    if "private_ci" in include:
        t0 = _perf_time.time()
        try:
            result["private_ci"] = _fetch_private_ci_jobs(repositories, since_dt, until_dt)[:max_items]
            timings["private_ci"] = _perf_time.time() - t0
        except Exception as e:
            timings["private_ci"] = _perf_time.time() - t0
            result["warnings"].append(f"private_ci: {str(e)[:80]}")

    if "open_work" in include:
        t0 = _perf_time.time()
        MAX_OPEN = min(max_items, 30)
        open_items = []
        try:
            gh = _get_gh()
            for repo in repositories:
                if len(open_items) >= MAX_OPEN:
                    break
                try:
                    r = gh.get_repo(repo)
                    prs = list(r.get_pulls(state="open", sort="updated", direction="desc"))[:30]
                    for pr in prs:
                        open_items.append({
                            "repository": repo, "branch": pr.head.ref,
                            "head_sha": pr.head.sha, "pull_number": pr.number,
                            "title": pr.title, "state": pr.state, "draft": pr.draft,
                            "updated_at": pr.updated_at.isoformat() if pr.updated_at else None,
                            "html_url": pr.html_url,
                        })
                        if len(open_items) >= MAX_OPEN:
                            break
                except Exception:
                    pass
            result["open_work"] = open_items[:MAX_OPEN]
            result["open_work_total_count"] = len(open_items)
            result["open_work_truncated"] = len(open_items) > MAX_OPEN
            if len(open_items) > MAX_OPEN:
                result["warnings"].append(f"open_work truncated: {len(open_items)} total, showing {MAX_OPEN}")
            timings["open_work"] = _perf_time.time() - t0
        except Exception as e:
            timings["open_work"] = _perf_time.time() - t0
            result["open_work"] = []
            result["warnings"].append(f"open_work: {str(e)[:80]}")

    # Summary
    commits_list = result["commits"]
    prs_list = result["pull_requests"]
    summary = {
        "repositories_touched": len(set(c.get("repository", "") for c in commits_list)),
        "commits": len(commits_list),
        "unique_commits": len(set(c["sha"] for c in commits_list)),
        "pull_requests_created": sum(1 for p in prs_list if "authored" in p.get("matched_activities", [])),
        "pull_requests_updated": sum(1 for p in prs_list if "updated" in p.get("matched_activities", [])),
        "pull_requests_merged": sum(1 for p in prs_list if "merged" in p.get("matched_activities", [])),
        "reviews_submitted": len(result["reviews"]),
        "pull_requests_reviewed": len(set(r["pull_number"] for r in result["reviews"])),
        "issues_created": sum(1 for i in result["issues"] if "authored" in i.get("matched_activities", [])),
        "issues_closed": sum(1 for i in result["issues"] if "closed" in i.get("matched_activities", [])),
        "github_actions_runs": len(result["github_actions"]),
        "private_ci_runs": len(result["private_ci"]),
        "private_ci_passed": sum(1 for j in result["private_ci"] if j.get("status") == "passed"),
        "private_ci_failed": sum(1 for j in result["private_ci"] if j.get("status") in ("failed", "timed_out")),
        "additions": sum(c.get("additions", 0) or 0 for c in commits_list),
        "deletions": sum(c.get("deletions", 0) or 0 for c in commits_list),
        "changed_files": sum(c.get("changed_files", 0) or 0 for c in commits_list),
    }
    result["summary"] = summary

    by_repo = {}
    for c in commits_list:
        r = c.get("repository", "")
        if r not in by_repo:
            by_repo[r] = {"repository": r, "commits": 0, "additions": 0, "deletions": 0}
        by_repo[r]["commits"] += 1
        by_repo[r]["additions"] += c.get("additions", 0) or 0
        by_repo[r]["deletions"] += c.get("deletions", 0) or 0
    result["by_repository"] = list(by_repo.values())

    try:
        gh2 = _get_gh()
        rl = gh2.get_rate_limit()
        result["rate_limit_remaining"] = rl.core.remaining
        result["rate_limit_reset_at"] = rl.core.reset.isoformat() if rl.core.reset else None
    except Exception:
        pass

    timings["total"] = _perf_time.time() - total_start
    result["timings"] = timings
    return {"ok": True, **result}


def get_github_weekly_report_data(
    identity: str,
    repositories: list = None,
    week: str = "current",
    week_start: str = "monday",
    timezone: str = "Asia/Shanghai",
    include_weekend: bool = True,
    include_open_work: bool = True,
    include_ci_failures: bool = True,
    include_code_statistics: bool = True,
    since: str = "",
    until: str = "",
) -> dict:
    import zoneinfo
    try:
        tz_obj = zoneinfo.ZoneInfo(timezone)
    except Exception:
        tz_obj = timezone.utc

    now = datetime.now(tz_obj)

    if since and until:
        since_dt = _parse_time(since, timezone)
        until_dt = _parse_time(until, timezone)
    elif week == "current":
        days_since = (now.weekday() - 0) if week_start == "monday" else (now.weekday() - 6) % 7
        since_dt = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_since)
        until_dt = now
    elif week == "previous":
        days_since = (now.weekday() - 0) if week_start == "monday" else (now.weekday() - 6) % 7
        prev_start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_since + 7)
        until_dt = prev_start + timedelta(days=7)
        since_dt = prev_start
    else:
        since_dt = now - timedelta(days=7)
        until_dt = now

    # Cache key
    repos_key = sorted(repositories) if repositories else ["all"]
    cache_kwargs = {
        "identity": identity, "repositories": repos_key,
        "since": since_dt.isoformat(), "until": until_dt.isoformat(),
        "week": week, "timezone": timezone,
        "include_weekend": include_weekend, "include_open_work": include_open_work,
        "include_ci_failures": include_ci_failures, "include_code_statistics": include_code_statistics,
    }
    cache_key = _build_cache_key("weekly_report", **cache_kwargs)
    cached, cache_age = _cache_get(cache_key)
    if cached is not None:
        cached["cache_hit"] = True
        cached["cache_age_seconds"] = round(cache_age, 1)
        return cached

    dev = get_github_development_history(
        identity=identity,
        repositories=repositories,
        since=since_dt.isoformat(),
        until=until_dt.isoformat(),
        max_items_per_section=200,
    )

    year, week_num, _ = now.isocalendar()
    period_label = f"{year}年第{week_num}周"

    result = {
        "ok": True,
        "partial_result": bool(dev.get("warnings")),
        "period": {
            "timezone": timezone,
            "start": since_dt.isoformat(),
            "end": until_dt.isoformat(),
            "label": period_label,
        },
        "executive_summary_data": dev.get("summary", {}),
        "completed_work": [],
        "work_in_progress": [],
        "code_reviews": dev.get("reviews", []),
        "issues_and_tasks": dev.get("issues", []),
        "ci_and_quality": {
            "github_actions": dev.get("github_actions", []),
            "private_ci": dev.get("private_ci", []),
        },
        "code_statistics": dev.get("summary", {}) if include_code_statistics else {},
        "by_repository": dev.get("by_repository", []),
        "risks_and_blockers": [],
        "next_week_candidates": [],
        "source_counts": {
            "commits": len(dev.get("commits", [])),
            "pull_requests": len(dev.get("pull_requests", [])),
            "reviews": len(dev.get("reviews", [])),
            "issues": len(dev.get("issues", [])),
            "github_actions": len(dev.get("github_actions", [])),
            "private_ci": len(dev.get("private_ci", [])),
            "open_work": len(dev.get("open_work", [])),
        },
        "source_timings": dev.get("timings", {}),
        "warnings": dev.get("warnings", []),
        "open_work_total_count": dev.get("open_work_total_count", 0),
        "open_work_returned_count": len(dev.get("open_work", [])),
        "open_work_truncated": dev.get("open_work_truncated", False),
        "cache_hit": False,
        "cache_age_seconds": 0,
        "rate_limit_remaining": dev.get("rate_limit_remaining"),
        "rate_limit_reset_at": dev.get("rate_limit_reset_at"),
    }

    for pr in dev.get("pull_requests", []):
        if "merged" in pr.get("matched_activities", []):
            result["completed_work"].append({"type": "merged_pr", **pr})

    for item in dev.get("open_work", []):
        result["work_in_progress"].append({"type": "open_pr", **item})

    for j in dev.get("private_ci", []):
        if j.get("status") in ("failed", "timed_out"):
            result["risks_and_blockers"].append({
                "type": "ci_failure",
                "repository": j.get("repository"),
                "commit_sha": j.get("commit_sha"),
                "profile": j.get("profile"),
                "exit_code": j.get("exit_code"),
            })

    for item in dev.get("open_work", []):
        result["next_week_candidates"].append({
            "type": "open_pr", "repository": item.get("repository"),
            "title": item.get("title"), "pull_number": item.get("pull_number"),
        })

    _cache_set(cache_key, result, ttl=300)
    return result
