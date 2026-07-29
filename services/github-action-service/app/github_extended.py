"""Bounded REST/GraphQL support for GitHub collaboration and delivery APIs."""

from __future__ import annotations

from typing import Any

import requests

from app.config import settings
from app.github_auth import credential_provider
from app.github_policy import ensure_repository_allowed, repository_is_allowed


API_VERSION = "2022-11-28"


class GitHubExtendedService:
    def _headers(self, accept: str = "application/vnd.github+json") -> dict:
        return {
            "Authorization": f"Bearer {credential_provider.token()}",
            "Accept": accept,
            "X-GitHub-Api-Version": API_VERSION,
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        payload: dict | None = None,
        accept: str = "application/vnd.github+json",
    ) -> Any:
        response = requests.request(
            method,
            f"{settings.GITHUB_API_URL.rstrip('/')}{path}",
            headers=self._headers(accept),
            params=params,
            json=payload,
            timeout=30,
            allow_redirects=False,
        )
        request_id = response.headers.get("X-GitHub-Request-Id")
        if response.status_code >= 400:
            try:
                body = response.json()
                message = body.get("message", "GitHub request failed")
            except ValueError:
                message = "GitHub request failed"
            return {
                "ok": False,
                "error": {
                    "code": self._error_code(response.status_code),
                    "message": message,
                    "github_status": response.status_code,
                    "github_request_id": request_id,
                    "retryable": response.status_code in {429, 502, 503, 504},
                },
            }
        if response.status_code == 204:
            return {"ok": True, "github_request_id": request_id}
        if 300 <= response.status_code < 400:
            return {
                "ok": True,
                "download_url": response.headers.get("Location"),
                "github_request_id": request_id,
            }
        try:
            data = response.json()
        except ValueError:
            data = response.text[:60000]
        return {"ok": True, "data": data, "github_request_id": request_id}

    @staticmethod
    def _error_code(status: int) -> str:
        return {
            401: "GITHUB_AUTHENTICATION_FAILED",
            403: "GITHUB_PERMISSION_DENIED",
            404: "GITHUB_NOT_FOUND",
            409: "GITHUB_CONFLICT",
            422: "GITHUB_VALIDATION_FAILED",
            429: "GITHUB_RATE_LIMITED",
        }.get(status, "GITHUB_API_FAILED")

    @staticmethod
    def _page(page: int, per_page: int) -> dict:
        return {"page": max(1, page), "per_page": min(100, max(1, per_page))}

    @staticmethod
    def _repo(repository: str) -> str:
        return ensure_repository_allowed(repository)

    @staticmethod
    def _confirmed(confirm: bool) -> dict | None:
        if confirm:
            return None
        return {
            "ok": False,
            "error": {
                "code": "CONFIRMATION_REQUIRED",
                "message": "Set confirm=true to perform this consequential operation",
                "retryable": False,
            },
        }

    def list_issues(self, repository, state="open", labels="", page=1, per_page=30):
        repo = self._repo(repository)
        params = {**self._page(page, per_page), "state": state}
        if labels:
            params["labels"] = labels
        return self._request("GET", f"/repos/{repo}/issues", params=params)

    def get_issue(self, repository, issue_number):
        repo = self._repo(repository)
        return self._request("GET", f"/repos/{repo}/issues/{issue_number}")

    def create_issue(self, repository, title, body="", labels=None, assignees=None, confirm=False):
        if error := self._confirmed(confirm):
            return error
        repo = self._repo(repository)
        return self._request("POST", f"/repos/{repo}/issues", payload={
            "title": title, "body": body, "labels": labels or [], "assignees": assignees or [],
        })

    def update_issue(self, repository, issue_number, changes, confirm=False):
        if error := self._confirmed(confirm):
            return error
        repo = self._repo(repository)
        allowed = {"title", "body", "state", "state_reason", "labels", "assignees", "milestone"}
        payload = {key: value for key, value in changes.items() if key in allowed}
        return self._request("PATCH", f"/repos/{repo}/issues/{issue_number}", payload=payload)

    def comment_issue(self, repository, issue_number, body, confirm=False):
        if error := self._confirmed(confirm):
            return error
        repo = self._repo(repository)
        return self._request("POST", f"/repos/{repo}/issues/{issue_number}/comments", payload={"body": body})

    def create_review(self, repository, pull_number, body="", event="COMMENT", commit_id="", comments=None, confirm=False):
        if error := self._confirmed(confirm):
            return error
        repo = self._repo(repository)
        event = event.upper()
        if event not in {"APPROVE", "REQUEST_CHANGES", "COMMENT", "PENDING"}:
            raise ValueError("event must be APPROVE, REQUEST_CHANGES, COMMENT, or PENDING")
        payload = {"body": body, "event": event, "comments": comments or []}
        if commit_id:
            payload["commit_id"] = commit_id
        return self._request("POST", f"/repos/{repo}/pulls/{pull_number}/reviews", payload=payload)

    def reply_review_comment(self, repository, pull_number, comment_id, body, confirm=False):
        if error := self._confirmed(confirm):
            return error
        repo = self._repo(repository)
        return self._request(
            "POST", f"/repos/{repo}/pulls/{pull_number}/comments/{comment_id}/replies",
            payload={"body": body},
        )

    def graphql(self, query: str, variables: dict):
        response = requests.post(
            f"{settings.GITHUB_API_URL.rstrip('/')}/graphql",
            headers=self._headers(),
            json={"query": query, "variables": variables},
            timeout=30,
        )
        request_id = response.headers.get("X-GitHub-Request-Id")
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.status_code >= 400 or payload.get("errors"):
            return {
                "ok": False,
                "error": {
                    "code": self._error_code(response.status_code),
                    "message": "GitHub GraphQL request failed",
                    "details": payload.get("errors", [])[:10],
                    "github_request_id": request_id,
                },
            }
        return {"ok": True, "data": payload.get("data"), "github_request_id": request_id}

    def list_review_threads(self, repository, pull_number, first=50):
        owner, name = self._repo(repository).split("/", 1)
        query = """query($owner:String!,$name:String!,$number:Int!,$first:Int!){repository(owner:$owner,name:$name){pullRequest(number:$number){reviewThreads(first:$first){nodes{id isResolved isOutdated path line originalLine comments(first:50){nodes{id databaseId body author{login} createdAt url}}} pageInfo{hasNextPage endCursor}}}}}"""
        return self.graphql(query, {"owner": owner, "name": name, "number": pull_number, "first": min(100, max(1, first))})

    def set_review_thread_resolved(self, repository, thread_id, resolved, confirm=False):
        if error := self._confirmed(confirm):
            return error
        self._repo(repository)
        field = "resolveReviewThread" if resolved else "unresolveReviewThread"
        query = f"""mutation($id:ID!){{{field}(input:{{threadId:$id}}){{thread{{id isResolved}}}}}}"""
        return self.graphql(query, {"id": thread_id})

    def list_artifacts(self, repository, page=1, per_page=30, name=""):
        repo = self._repo(repository)
        params = self._page(page, per_page)
        if name:
            params["name"] = name
        return self._request("GET", f"/repos/{repo}/actions/artifacts", params=params)

    def get_artifact(self, repository, artifact_id):
        repo = self._repo(repository)
        return self._request("GET", f"/repos/{repo}/actions/artifacts/{artifact_id}")

    def download_artifact(self, repository, artifact_id):
        repo = self._repo(repository)
        return self._request("GET", f"/repos/{repo}/actions/artifacts/{artifact_id}/zip")

    def list_run_jobs(self, repository, run_id, page=1, per_page=30, filter_name="latest"):
        repo = self._repo(repository)
        return self._request("GET", f"/repos/{repo}/actions/runs/{run_id}/jobs", params={
            **self._page(page, per_page), "filter": filter_name,
        })

    def rerun(self, repository, target, identifier, confirm=False):
        if error := self._confirmed(confirm):
            return error
        repo = self._repo(repository)
        paths = {
            "run": f"/repos/{repo}/actions/runs/{identifier}/rerun",
            "failed": f"/repos/{repo}/actions/runs/{identifier}/rerun-failed-jobs",
            "job": f"/repos/{repo}/actions/jobs/{identifier}/rerun",
        }
        if target not in paths:
            raise ValueError("target must be run, failed, or job")
        return self._request("POST", paths[target])

    def get_job_logs(self, repository, job_id):
        repo = self._repo(repository)
        return self._request("GET", f"/repos/{repo}/actions/jobs/{job_id}/logs")

    def list_releases(self, repository, page=1, per_page=30):
        repo = self._repo(repository)
        return self._request("GET", f"/repos/{repo}/releases", params=self._page(page, per_page))

    def create_release(self, repository, tag_name, name="", body="", target_commitish="", draft=False, prerelease=False, confirm=False):
        if error := self._confirmed(confirm):
            return error
        repo = self._repo(repository)
        payload = {"tag_name": tag_name, "name": name, "body": body, "draft": draft, "prerelease": prerelease}
        if target_commitish:
            payload["target_commitish"] = target_commitish
        return self._request("POST", f"/repos/{repo}/releases", payload=payload)

    def update_release(self, repository, release_id, changes, confirm=False):
        if error := self._confirmed(confirm):
            return error
        repo = self._repo(repository)
        allowed = {"tag_name", "target_commitish", "name", "body", "draft", "prerelease", "make_latest"}
        return self._request("PATCH", f"/repos/{repo}/releases/{release_id}", payload={
            key: value for key, value in changes.items() if key in allowed
        })

    def delete_release(self, repository, release_id, confirm=False):
        if error := self._confirmed(confirm):
            return error
        repo = self._repo(repository)
        return self._request("DELETE", f"/repos/{repo}/releases/{release_id}")

    def list_tags(self, repository, page=1, per_page=30):
        repo = self._repo(repository)
        return self._request("GET", f"/repos/{repo}/tags", params=self._page(page, per_page))

    def create_tag(self, repository, tag, target_sha, message="", tagger=None, confirm=False):
        if error := self._confirmed(confirm):
            return error
        repo = self._repo(repository)
        if message:
            annotated = self._request("POST", f"/repos/{repo}/git/tags", payload={
                "tag": tag, "message": message, "object": target_sha, "type": "commit",
                **({"tagger": tagger} if tagger else {}),
            })
            if not annotated.get("ok"):
                return annotated
            target_sha = annotated["data"]["sha"]
        return self._request("POST", f"/repos/{repo}/git/refs", payload={
            "ref": f"refs/tags/{tag}", "sha": target_sha,
        })

    def create_deployment(self, repository, ref, environment, description="", transient=False, production=False, required_contexts=None, confirm=False):
        if error := self._confirmed(confirm):
            return error
        repo = self._repo(repository)
        return self._request("POST", f"/repos/{repo}/deployments", payload={
            "ref": ref, "environment": environment, "description": description,
            "transient_environment": transient, "production_environment": production,
            "required_contexts": required_contexts or [],
            "auto_merge": False,
        })

    def create_deployment_status(self, repository, deployment_id, state, environment_url="", log_url="", description="", confirm=False):
        if error := self._confirmed(confirm):
            return error
        repo = self._repo(repository)
        payload = {"state": state, "description": description}
        if environment_url:
            payload["environment_url"] = environment_url
        if log_url:
            payload["log_url"] = log_url
        return self._request("POST", f"/repos/{repo}/deployments/{deployment_id}/statuses", payload=payload)

    def list_environments(self, repository, page=1, per_page=30):
        repo = self._repo(repository)
        return self._request("GET", f"/repos/{repo}/environments", params=self._page(page, per_page))

    def repository_governance(self, repository, branch=""):
        repo = self._repo(repository)
        result = {
            "repository": self._request("GET", f"/repos/{repo}"),
            "rulesets": self._request("GET", f"/repos/{repo}/rulesets"),
        }
        if branch:
            result["branch_protection"] = self._request("GET", f"/repos/{repo}/branches/{branch}/protection")
        return {"ok": all(item.get("ok") for item in result.values()), "data": result}

    def list_webhooks(self, repository, page=1, per_page=30):
        repo = self._repo(repository)
        return self._request("GET", f"/repos/{repo}/hooks", params=self._page(page, per_page))

    def create_webhook(self, repository, url, events=None, content_type="json", secret="", active=True, confirm=False):
        if error := self._confirmed(confirm):
            return error
        repo = self._repo(repository)
        config = {"url": url, "content_type": content_type, "insecure_ssl": "0"}
        if secret:
            config["secret"] = secret
        return self._request("POST", f"/repos/{repo}/hooks", payload={
            "name": "web", "active": active, "events": events or ["push"], "config": config,
        })

    def delete_webhook(self, repository, hook_id, confirm=False):
        if error := self._confirmed(confirm):
            return error
        repo = self._repo(repository)
        return self._request("DELETE", f"/repos/{repo}/hooks/{hook_id}")

    def list_events(self, repository, page=1, per_page=30):
        repo = self._repo(repository)
        return self._request("GET", f"/repos/{repo}/events", params=self._page(page, per_page))

    def list_notifications(self, repository="", all_items=False, participating=False, page=1, per_page=30):
        path = "/notifications"
        if repository:
            path = f"/repos/{self._repo(repository)}/notifications"
        result = self._request("GET", path, params={
            **self._page(page, per_page), "all": str(all_items).lower(),
            "participating": str(participating).lower(),
        })
        if not repository and result.get("ok") and isinstance(result.get("data"), list):
            result["data"] = [
                item for item in result["data"]
                if repository_is_allowed((item.get("repository") or {}).get("full_name", ""))
            ]
            result["filtered_by_repository_policy"] = True
        return result

    def mark_notification_read(self, thread_id, confirm=False):
        if error := self._confirmed(confirm):
            return error
        current = self._request("GET", f"/notifications/threads/{thread_id}")
        if not current.get("ok"):
            return current
        repository = (current.get("data") or {}).get("repository") or {}
        ensure_repository_allowed(repository.get("full_name", ""))
        return self._request("PATCH", f"/notifications/threads/{thread_id}")


extended_github = GitHubExtendedService()
