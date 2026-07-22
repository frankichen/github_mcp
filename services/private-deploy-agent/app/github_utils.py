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
from functools import lru_cache

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

def _get_gh() -> Github:
    token = settings.GITHUB_TOKEN.get_secret_value()
    if not token or token == "REPLACE_WITH_FINE_GRAINED_GITHUB_TOKEN":
        raise GitHubApiError(401, "GitHub token not configured")
    return Github(token)


def _parse_repo(repository: str) -> tuple:
    parts = repository.split("/")
    if len(parts) != 2:
        raise ValueError(f"Invalid repository format: {repository}")
    return parts[0], parts[1]


def _rate_limit_info(gh: Github) -> dict:
    try:
        rl = gh.get_rate_limit()
        core = rl.core
        return {
            "rate_limit_remaining": core.remaining,
            "rate_limit_limit": core.limit,
            "rate_limit_reset_at": core.reset.isoformat() if core.reset else None,
        }
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


def _readiness(repository: str, pull_number: int, expected_head_sha: str = "",
               required_private_ci_job_id: str = "", expected_base_branch: str = "main") -> dict:
    pr_result = get_github_pull_request(repository, pull_number)
    if not pr_result.get("ok"):
        return {"ok": True, "ready": False, "reasons": ["PR_NOT_FOUND"], "repository": repository, "pull_number": pull_number}
    reasons = []
    if pr_result["state"] != "open": reasons.append("PR_NOT_OPEN")
    if pr_result["merged"]: reasons.append("ALREADY_MERGED")
    if pr_result["draft"]: reasons.append("PR_IS_DRAFT")
    if pr_result["base_branch"] != expected_base_branch: reasons.append("BASE_BRANCH_MISMATCH")
    if expected_head_sha and pr_result["head_sha"] != expected_head_sha: reasons.append("HEAD_CHANGED")
    if pr_result["mergeable"] is not True: reasons.append("MERGEABLE_NOT_TRUE")
    if pr_result["mergeable_state"] in ("dirty", "blocked", "behind", "unknown"): reasons.append("MERGEABLE_STATE_BLOCKED")
    if pr_result["review_decision"] == "CHANGES_REQUESTED": reasons.append("CHANGES_REQUESTED")

    try:
        checks = get_github_pull_request_checks(repository, pull_number)
    except GitHubApiError as exc:
        github_status = getattr(exc, "github_status", None)
        if github_status == 403:
            checks = _error_response("GITHUB_CHECKS_PERMISSION_DENIED", "GitHub token cannot read Checks for this repository")
        else:
            checks = _error_response("GITHUB_CHECKS_UNAVAILABLE", "GitHub Checks could not be read", retryable=(github_status or 0) >= 500)
    if not checks.get("ok"):
        reasons.append(checks.get("error", {}).get("code", "GITHUB_CHECKS_UNAVAILABLE"))
    else:
        if checks.get("overall_conclusion") == "failure": reasons.append("GITHUB_CHECK_FAILED")
        if checks.get("overall_status") != "completed": reasons.append("GITHUB_CHECKS_PENDING")

    private_ci = None
    if required_private_ci_job_id:
        private_ci = _private_ci_job(required_private_ci_job_id)
        if not private_ci: reasons.append("PRIVATE_CI_JOB_NOT_FOUND")
        else:
            if private_ci.get("repository") != repository: reasons.append("PRIVATE_CI_REPOSITORY_MISMATCH")
            if private_ci.get("branch") != pr_result["head_branch"]: reasons.append("PRIVATE_CI_BRANCH_MISMATCH")
            if private_ci.get("commit_sha") != pr_result["head_sha"]: reasons.append("PRIVATE_CI_SHA_MISMATCH")
            if private_ci.get("profile") != "repo-auto-check": reasons.append("PRIVATE_CI_PROFILE_MISMATCH")
            if private_ci.get("status") != "passed" or private_ci.get("exit_code") != 0: reasons.append("PRIVATE_CI_NOT_PASSED")
            if private_ci.get("superseded_by_job_id"): reasons.append("PRIVATE_CI_SUPERSEDED")
    else:
        reasons.append("PRIVATE_CI_REQUIRED")

    repo_meta = get_github_repository(repository)
    allowed = []
    if repo_meta.get("allow_merge_commit"): allowed.append("merge")
    if repo_meta.get("allow_squash_merge"): allowed.append("squash")
    if repo_meta.get("allow_rebase_merge"): allowed.append("rebase")
    return {
        "ok": True, "ready": not reasons, "reasons": reasons,
        "repository": repository, "pull_number": pull_number,
        "state": pr_result["state"], "draft": pr_result["draft"], "merged": pr_result["merged"],
        "base_branch": pr_result["base_branch"], "base_sha": pr_result["base_sha"],
        "head_branch": pr_result["head_branch"], "head_sha": pr_result["head_sha"],
        "expected_head_match": bool(expected_head_sha) and pr_result["head_sha"] == expected_head_sha,
        "mergeable": pr_result["mergeable"], "mergeable_state": pr_result["mergeable_state"],
        "review_decision": pr_result["review_decision"],
        "requested_reviewers": pr_result["requested_reviewers"], "change_requests": [r for r in pr_result["reviews"] if r["state"] == "CHANGES_REQUESTED"],
        "github_checks": {"overall": checks.get("overall_conclusion") if checks.get("ok") else "unavailable", "checks": checks.get("checks", []) if checks.get("ok") else []},
        "private_ci": private_ci, "allowed_merge_methods": allowed,
    }


def get_github_pull_request_merge_readiness(repository: str, pull_number: int, expected_head_sha: str = "",
                                            required_private_ci_job_id: str = "", expected_base_branch: str = "main") -> dict:
    return _readiness(repository, pull_number, expected_head_sha, required_private_ci_job_id, expected_base_branch)


def merge_github_pull_request(repository: str, pull_number: int, merge_method: str = "squash",
                              expected_head_sha: str = "", required_private_ci_job_id: str = "",
                              expected_base_branch: str = "main", commit_title: str = "",
                              commit_message: str = "", delete_head_branch: bool = False,
                              confirm: bool = False) -> dict:
    if not confirm: return _error_response("CONFIRM_REQUIRED", "confirm must be true")
    if not SHA_RE.fullmatch(expected_head_sha): return _error_response("EXPECTED_HEAD_SHA_REQUIRED", "expected_head_sha must be a full 40-character SHA")
    if not required_private_ci_job_id: return _error_response("PRIVATE_CI_REQUIRED", "required_private_ci_job_id is required")
    if merge_method not in ("merge", "squash", "rebase"): return _error_response("INVALID_MERGE_METHOD", "merge_method must be merge, squash, or rebase")
    readiness = _readiness(repository, pull_number, expected_head_sha, required_private_ci_job_id, expected_base_branch)
    if not readiness.get("ready"):
        code = readiness.get("reasons", ["NOT_READY"])[0]
        return _error_response(code, "Pull request is not ready to merge", details={"readiness": readiness})
    gh = _get_gh()
    try:
        repo = gh.get_repo(repository)
        pr = repo.get_pull(pull_number)
        result = pr.merge(sha=expected_head_sha, merge_method=merge_method,
                          title=commit_title or None, message=commit_message or None)
        if not result.merged:
            return _error_response("GITHUB_MERGE_REJECTED", result.message or "GitHub did not merge the pull request")
        merged_pr = repo.get_pull(pull_number)
        base_after = repo.get_branch(expected_base_branch).commit.sha
        merge_sha = result.sha
        if not merge_sha or not SHA_RE.fullmatch(merge_sha):
            return _error_response("MERGE_COMMIT_SHA_INVALID", "GitHub did not return a full merge commit SHA")
        return {"ok": True, "merged": True, "repository": repository, "pull_number": pull_number,
                "merge_method": merge_method, "previous_head_sha": expected_head_sha,
                "merge_commit_sha": merge_sha, "base_branch": expected_base_branch,
                "base_head_before": readiness["base_sha"], "base_head_after": base_after,
                "html_url": merged_pr.html_url, "head_branch_deleted": False,
                "message": result.message}
    except GithubException as e:
        if e.status == 409: return _error_response("MERGE_CONFLICT", "GitHub reported a merge conflict", retryable=True)
        if e.status == 405: return _error_response("MERGE_METHOD_NOT_ALLOWED", "GitHub rejected the merge method")
        if e.status == 403: return _error_response("MERGE_PERMISSION_DENIED", "GitHub App needs Contents write and Pull requests write")
        raise GitHubApiError(e.status, str(e))


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


def mark_github_pull_request_ready(repository: str, pull_number: int, expected_head_sha: str) -> dict:
    pr, error = _pr_with_sha(repository, pull_number, expected_head_sha)
    if error: return error
    if not pr.draft: return {"ok": True, "draft": False, "message": "PR is already ready for review"}
    pr.mark_ready_for_review()
    return {"ok": True, "draft": False, "repository": repository, "pull_number": pull_number}


def convert_github_pull_request_to_draft(repository: str, pull_number: int, expected_head_sha: str) -> dict:
    pr, error = _pr_with_sha(repository, pull_number, expected_head_sha)
    if error: return error
    if pr.draft: return {"ok": True, "draft": True, "message": "PR is already draft"}
    pr.convert_to_draft()
    return {"ok": True, "draft": True, "repository": repository, "pull_number": pull_number}


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
        commit = repo.get_commit(head_sha)

        # Check runs
        check_runs = list(commit.get_check_runs())
        statuses = list(commit.get_statuses())

        pending = sum(1 for c in check_runs if c.status == "queued" or c.status == "in_progress")
        passed = sum(1 for c in check_runs if c.conclusion == "success")
        failed = sum(1 for c in check_runs if c.conclusion in ("failure", "timed_out", "cancelled", "action_required"))

        overall_status = "pending" if pending > 0 else "completed"
        overall_conclusion = None
        if overall_status == "completed":
            if failed > 0:
                overall_conclusion = "failure"
            elif passed > 0:
                overall_conclusion = "success"
            else:
                overall_conclusion = "neutral"

        return {
            "ok": True,
            "repository": repository,
            "pull_number": pull_number,
            "head_sha": head_sha,
            "overall_status": overall_status,
            "overall_conclusion": overall_conclusion,
            "pending_count": pending,
            "passed_count": passed,
            "failed_count": failed,
            "total_checks": len(check_runs) + len(statuses),
            "checks": [
                {
                    "name": c.name,
                    "status": c.status,
                    "conclusion": c.conclusion,
                    "url": c.html_url,
                    "started_at": c.started_at.isoformat() if c.started_at else None,
                    "completed_at": c.completed_at.isoformat() if c.completed_at else None,
                }
                for c in check_runs[:50]
            ],
            "statuses": [
                {
                    "context": s.context,
                    "state": s.state,
                    "description": s.description,
                    "url": s.target_url if hasattr(s, "target_url") else None,
                }
                for s in statuses[:50]
            ],
        }
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
