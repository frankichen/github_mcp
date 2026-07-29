"""MCP registrations for extended GitHub collaboration and delivery tools."""

import json

from app.github_auth import credential_provider
from app.github_extended import extended_github


def register_github_extended_tools(mcp, run_sync):
    def encode(value):
        return json.dumps(value, ensure_ascii=False)

    def parse_object(value: str) -> dict:
        parsed = json.loads(value or "{}")
        if not isinstance(parsed, dict):
            raise ValueError("JSON value must be an object")
        return parsed

    def parse_list(value: str) -> list:
        parsed = json.loads(value or "[]")
        if not isinstance(parsed, list):
            raise ValueError("JSON value must be an array")
        return parsed

    async def call(method, *args, **kwargs):
        try:
            return encode(await run_sync(method, *args, **kwargs))
        except Exception as exc:
            return encode({"ok": False, "error": {
                "code": type(exc).__name__,
                "message": str(exc),
                "retryable": False,
            }})

    @mcp.tool(name="list_github_issues", description="List bounded GitHub issues and pull requests with state and label filters.")
    async def list_github_issues(repository: str, state: str = "open", labels: str = "", page: int = 1, per_page: int = 30) -> str:
        return await call(extended_github.list_issues, repository, state, labels, page, per_page)

    @mcp.tool(name="get_github_issue", description="Get one GitHub issue or pull request issue record.")
    async def get_github_issue(repository: str, issue_number: int) -> str:
        return await call(extended_github.get_issue, repository, issue_number)

    @mcp.tool(name="create_github_issue", description="Create a GitHub issue. Requires confirm=true.")
    async def create_github_issue(repository: str, title: str, body: str = "", labels_json: str = "[]", assignees_json: str = "[]", confirm: bool = False) -> str:
        return await call(extended_github.create_issue, repository, title, body, parse_list(labels_json), parse_list(assignees_json), confirm)

    @mcp.tool(name="update_github_issue", description="Update title, body, state, labels, assignees, or milestone on an issue. Requires confirm=true.")
    async def update_github_issue(repository: str, issue_number: int, changes_json: str, confirm: bool = False) -> str:
        return await call(extended_github.update_issue, repository, issue_number, parse_object(changes_json), confirm)

    @mcp.tool(name="create_github_issue_comment", description="Add a comment to an issue or pull request. Requires confirm=true.")
    async def create_github_issue_comment(repository: str, issue_number: int, body: str, confirm: bool = False) -> str:
        return await call(extended_github.comment_issue, repository, issue_number, body, confirm)

    @mcp.tool(name="create_github_pull_request_review", description="Create a PR review, including optional inline comments. Requires confirm=true.")
    async def create_github_pull_request_review(repository: str, pull_number: int, body: str = "", event: str = "COMMENT", commit_id: str = "", comments_json: str = "[]", confirm: bool = False) -> str:
        return await call(extended_github.create_review, repository, pull_number, body, event, commit_id, parse_list(comments_json), confirm)

    @mcp.tool(name="reply_github_review_comment", description="Reply to an inline pull-request review comment. Requires confirm=true.")
    async def reply_github_review_comment(repository: str, pull_number: int, comment_id: int, body: str, confirm: bool = False) -> str:
        return await call(extended_github.reply_review_comment, repository, pull_number, comment_id, body, confirm)

    @mcp.tool(name="list_github_review_threads", description="List inline PR review threads with resolution and outdated state.")
    async def list_github_review_threads(repository: str, pull_number: int, first: int = 50) -> str:
        return await call(extended_github.list_review_threads, repository, pull_number, first)

    @mcp.tool(name="set_github_review_thread_resolved", description="Resolve or unresolve an inline PR review thread by GraphQL node ID. Requires confirm=true.")
    async def set_github_review_thread_resolved(repository: str, thread_id: str, resolved: bool = True, confirm: bool = False) -> str:
        return await call(extended_github.set_review_thread_resolved, repository, thread_id, resolved, confirm)

    @mcp.tool(name="list_github_actions_artifacts", description="List workflow artifacts with bounded pagination.")
    async def list_github_actions_artifacts(repository: str, name: str = "", page: int = 1, per_page: int = 30) -> str:
        return await call(extended_github.list_artifacts, repository, page, per_page, name)

    @mcp.tool(name="get_github_actions_artifact", description="Get metadata for one workflow artifact.")
    async def get_github_actions_artifact(repository: str, artifact_id: int) -> str:
        return await call(extended_github.get_artifact, repository, artifact_id)

    @mcp.tool(name="get_github_actions_artifact_download", description="Get the short-lived redirect URL for an artifact zip without embedding binary data.")
    async def get_github_actions_artifact_download(repository: str, artifact_id: int) -> str:
        return await call(extended_github.download_artifact, repository, artifact_id)

    @mcp.tool(name="list_github_actions_run_jobs", description="List jobs and steps for a workflow run.")
    async def list_github_actions_run_jobs(repository: str, run_id: int, filter_name: str = "latest", page: int = 1, per_page: int = 30) -> str:
        return await call(extended_github.list_run_jobs, repository, run_id, page, per_page, filter_name)

    @mcp.tool(name="rerun_github_actions", description="Rerun a workflow run, its failed jobs, or one job. target is run, failed, or job. Requires confirm=true.")
    async def rerun_github_actions(repository: str, target: str, identifier: int, confirm: bool = False) -> str:
        return await call(extended_github.rerun, repository, target, identifier, confirm)

    @mcp.tool(name="get_github_actions_job_logs", description="Get the short-lived redirect URL for one Actions job log.")
    async def get_github_actions_job_logs(repository: str, job_id: int) -> str:
        return await call(extended_github.get_job_logs, repository, job_id)

    @mcp.tool(name="list_github_releases", description="List repository releases with bounded pagination.")
    async def list_github_releases(repository: str, page: int = 1, per_page: int = 30) -> str:
        return await call(extended_github.list_releases, repository, page, per_page)

    @mcp.tool(name="create_github_release", description="Create a draft, prerelease, or published GitHub release. Requires confirm=true.")
    async def create_github_release(repository: str, tag_name: str, name: str = "", body: str = "", target_commitish: str = "", draft: bool = False, prerelease: bool = False, confirm: bool = False) -> str:
        return await call(extended_github.create_release, repository, tag_name, name, body, target_commitish, draft, prerelease, confirm)

    @mcp.tool(name="update_github_release", description="Update a GitHub release using a bounded changes object. Requires confirm=true.")
    async def update_github_release(repository: str, release_id: int, changes_json: str, confirm: bool = False) -> str:
        return await call(extended_github.update_release, repository, release_id, parse_object(changes_json), confirm)

    @mcp.tool(name="delete_github_release", description="Delete a GitHub release record. Requires confirm=true; this does not delete its Git tag.")
    async def delete_github_release(repository: str, release_id: int, confirm: bool = False) -> str:
        return await call(extended_github.delete_release, repository, release_id, confirm)

    @mcp.tool(name="list_github_tags", description="List repository Git tags with bounded pagination.")
    async def list_github_tags(repository: str, page: int = 1, per_page: int = 30) -> str:
        return await call(extended_github.list_tags, repository, page, per_page)

    @mcp.tool(name="create_github_tag", description="Create a lightweight or annotated Git tag. Requires confirm=true.")
    async def create_github_tag(repository: str, tag: str, target_sha: str, message: str = "", tagger_json: str = "{}", confirm: bool = False) -> str:
        tagger = parse_object(tagger_json)
        return await call(extended_github.create_tag, repository, tag, target_sha, message, tagger or None, confirm)

    @mcp.tool(name="create_github_deployment", description="Create a GitHub deployment for a ref and environment. Requires confirm=true.")
    async def create_github_deployment(repository: str, ref: str, environment: str, description: str = "", transient: bool = False, production: bool = False, required_contexts_json: str = "[]", confirm: bool = False) -> str:
        return await call(extended_github.create_deployment, repository, ref, environment, description, transient, production, parse_list(required_contexts_json), confirm)

    @mcp.tool(name="create_github_deployment_status", description="Create a status for a GitHub deployment. Requires confirm=true.")
    async def create_github_deployment_status(repository: str, deployment_id: int, state: str, environment_url: str = "", log_url: str = "", description: str = "", confirm: bool = False) -> str:
        return await call(extended_github.create_deployment_status, repository, deployment_id, state, environment_url, log_url, description, confirm)

    @mcp.tool(name="list_github_environments", description="List repository environments and protection metadata.")
    async def list_github_environments(repository: str, page: int = 1, per_page: int = 30) -> str:
        return await call(extended_github.list_environments, repository, page, per_page)

    @mcp.tool(name="get_github_repository_governance", description="Read repository settings, rulesets, and optional branch protection in one bounded response.")
    async def get_github_repository_governance(repository: str, branch: str = "") -> str:
        return await call(extended_github.repository_governance, repository, branch)

    @mcp.tool(name="list_github_webhooks", description="List repository webhooks without revealing configured secrets.")
    async def list_github_webhooks(repository: str, page: int = 1, per_page: int = 30) -> str:
        return await call(extended_github.list_webhooks, repository, page, per_page)

    @mcp.tool(name="create_github_webhook", description="Create a repository webhook; its secret is sent to GitHub and never returned by this service. Requires confirm=true.")
    async def create_github_webhook(repository: str, url: str, events_json: str = "[\"push\"]", content_type: str = "json", secret: str = "", active: bool = True, confirm: bool = False) -> str:
        return await call(extended_github.create_webhook, repository, url, parse_list(events_json), content_type, secret, active, confirm)

    @mcp.tool(name="delete_github_webhook", description="Delete a repository webhook. Requires confirm=true.")
    async def delete_github_webhook(repository: str, hook_id: int, confirm: bool = False) -> str:
        return await call(extended_github.delete_webhook, repository, hook_id, confirm)

    @mcp.tool(name="list_github_repository_events", description="List recent public repository events with bounded pagination.")
    async def list_github_repository_events(repository: str, page: int = 1, per_page: int = 30) -> str:
        return await call(extended_github.list_events, repository, page, per_page)

    @mcp.tool(name="list_github_notifications", description="List authenticated-user notifications globally or for one allowed repository.")
    async def list_github_notifications(repository: str = "", all_items: bool = False, participating: bool = False, page: int = 1, per_page: int = 30) -> str:
        return await call(extended_github.list_notifications, repository, all_items, participating, page, per_page)

    @mcp.tool(name="mark_github_notification_read", description="Mark one notification thread read. Requires confirm=true.")
    async def mark_github_notification_read(thread_id: str, confirm: bool = False) -> str:
        return await call(extended_github.mark_notification_read, thread_id, confirm)

    @mcp.tool(name="get_github_auth_status", description="Return redacted GitHub PAT/App authentication mode, cache, and expiry status.")
    async def get_github_auth_status() -> str:
        return encode(credential_provider.status())

    @mcp.tool(name="refresh_github_app_installation_token", description="Force refresh the configured GitHub App installation token without returning credential material. Requires confirm=true.")
    async def refresh_github_app_installation_token(confirm: bool = False) -> str:
        if not confirm:
            return encode({"ok": False, "error": {"code": "CONFIRMATION_REQUIRED", "message": "Set confirm=true to refresh the credential"}})
        return await call(credential_provider.refresh)
