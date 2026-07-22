import hmac
import json
import logging
from typing import Optional

import httpx

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import AuthSettings
from mcp.server.auth.provider import AccessToken, TokenVerifier

from app.config import settings as app_settings
from app.github_client import GitHubClient
from app.services.github_service import GitHubService
from app.services.ci_service import get_ci_service

logger = logging.getLogger(__name__)

_client = GitHubClient()
_service = GitHubService(_client)
_ci_service = get_ci_service()


class ApiKeyVerifier:
    async def verify_token(self, token: str) -> Optional[AccessToken]:
        if not token or not app_settings.ACTION_API_KEY.get_secret_value():
            return None
        if not hmac.compare_digest(token, app_settings.ACTION_API_KEY.get_secret_value()):
            return None
        return AccessToken(
            token=token,
            client_id="github-action-service",
            scopes=[],
            resource=app_settings.SERVICE_URL,
        )


mcp = FastMCP(
    "GitHub Action Service",
    instructions="""\
Use this MCP server to read and write code on GitHub repositories, and manage CI/CD workflows.

GitHub Tools:
- get_github_file: Read file content from a GitHub repository
- list_github_directory: List directory contents in a GitHub repository
- create_github_branch: Create a new branch in a GitHub repository
- commit_github_files: Commit one or more files to a GitHub repository
- create_github_pull_request: Create a pull request on GitHub

CI Tools:
- list_ci_workers: List CI runners/workers for a GitHub repository
- list_ci_profiles: List CI workflow definitions
- list_ci_jobs: List CI job runs
- start_ci_job: Trigger a CI workflow dispatch
- get_ci_job: Get details of a CI job run
- get_ci_logs: Get logs from a CI job
- cancel_ci_job: Cancel a running CI job
""",
    token_verifier=ApiKeyVerifier(),
    auth=AuthSettings(
        issuer_url=app_settings.SERVICE_URL,
        resource_server_url=app_settings.SERVICE_URL,
    ),
    stateless_http=True,
)


def _log_registered_tools():
    import asyncio
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            tools = loop.run_until_complete(mcp.list_tools())
        else:
            tools = asyncio.run(mcp.list_tools())
        tool_names = [t.name for t in tools]
        for name in tool_names:
            logger.info("Registered tool: %s", name)
        logger.info("Total registered tools: %d", len(tools))
    except Exception as e:
        logger.error("Failed to list registered tools: %s", e)


@mcp.tool(
    name="get_github_file",
    description="Get file content from a GitHub repository. Returns the file content with metadata including SHA, size, and line range.",
)
async def get_github_file(
    repository: str,
    path: str,
    ref: str = "",
    start_line: int = 0,
    end_line: int = 0,
) -> str:
    try:
        sl = start_line if start_line > 0 else None
        el = end_line if end_line > 0 else None
        result = _service.get_file(
            repository=repository,
            path=path,
            ref=ref,
            start_line=sl,
            end_line=el,
        )
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": type(e).__name__, "message": str(e)})


@mcp.tool(
    name="list_github_directory",
    description="List directory contents in a GitHub repository. Returns files and subdirectories with their metadata.",
)
async def list_github_directory(
    repository: str,
    path: str,
    ref: str = "",
) -> str:
    try:
        result = _service.list_directory(
            repository=repository,
            path=path,
            ref=ref,
        )
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": type(e).__name__, "message": str(e)})


@mcp.tool(
    name="create_github_branch",
    description="Create a new branch in a GitHub repository. Fails if the branch already exists.",
)
async def create_github_branch(
    repository: str,
    branch: str,
    base_branch: str = "main",
) -> str:
    try:
        result = _service.create_branch(
            repository=repository,
            branch=branch,
            base_branch=base_branch,
        )
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": type(e).__name__, "message": str(e)})


@mcp.tool(
    name="diagnose_text_payload",
    description="Diagnostic: reports metadata of a received text payload (character_count, utf8_byte_count, sha256, marker checks). Use this to verify ChatGPT is not truncating tool call arguments when committing large files.",
)
async def diagnose_text_payload(
    payload: str,
    label: str = "",
    start_marker: str = "",
    end_marker: str = "",
) -> str:
    import hashlib
    utf8_bytes = payload.encode("utf-8")
    return json.dumps({
        "label": label,
        "character_count": len(payload),
        "utf8_byte_count": len(utf8_bytes),
        "sha256": hashlib.sha256(utf8_bytes).hexdigest(),
        "start_marker_found": start_marker in payload if start_marker else None,
        "end_marker_found": end_marker in payload if end_marker else None,
        "preview_first_100": payload[:100],
        "preview_last_100": payload[-100:] if len(payload) >= 100 else "",
        "ends_with_newline": payload.endswith("\n") if payload else None,
        "null_bytes": "\x00" in payload,
    }, ensure_ascii=False)


@mcp.tool(
    name="commit_github_files",
    description="""\
Commit one or more files to a GitHub repository. All files go into a single commit.

Each file in the files array must have:
- path: File path in the repository
- operation: "upsert" or "delete"
- content: Full file content (required for upsert, omitted for delete)
- expected_sha: Optional SHA of the current file for optimistic concurrency control

Optional pull_request object:
- create: Set to true to create a PR
- base_branch: Target branch for PR (default: main)
- title: PR title (defaults to commit message)
- body: PR description (optional)
""",
)
async def commit_github_files(
    repository: str,
    branch: str,
    commit_message: str,
    files_json: str,
    base_branch: str = "main",
    create_branch_if_missing: bool = False,
    expected_head_sha: str = "",
    pull_request_json: str = "{}",
) -> str:
    from app.models import CommitRequest, FileOperation, PullRequestConfig

    try:
        logger.info(
            "MCP commit_github_files: repo=%s branch=%s files_json_len=%d pr_json_len=%d",
            repository, branch, len(files_json), len(pull_request_json),
        )
        files_data = json.loads(files_json)
        pr_data = json.loads(pull_request_json) if pull_request_json else {}

        file_ops = [FileOperation(**f) for f in files_data]
        pr_config = PullRequestConfig(**pr_data) if pr_data.get("create") else None

        request = CommitRequest(
            repository=repository,
            branch=branch,
            base_branch=base_branch,
            create_branch_if_missing=create_branch_if_missing,
            commit_message=commit_message,
            expected_head_sha=expected_head_sha if expected_head_sha else None,
            files=file_ops,
            pull_request=pr_config,
        )

        result = _service.commit_files(request)
        return json.dumps(result, ensure_ascii=False)
    except json.JSONDecodeError as e:
        return json.dumps({
            "error": "json_parse_error",
            "message": f"files_json is not valid JSON (pos {e.pos}: {e.msg}). Chatbot may have truncated the payload.",
            "received_len": len(files_json),
            "preview": files_json[:200],
        })
    except Exception as e:
        return json.dumps({"error": type(e).__name__, "message": str(e)})


@mcp.tool(
    name="create_github_pull_request",
    description="Create a pull request on GitHub between two branches.",
)
async def create_github_pull_request(
    repository: str,
    head_branch: str,
    base_branch: str = "main",
    title: str = "",
    body: str = "",
    draft: bool = True,
) -> str:
    try:
        result = _service.create_pull_request(
            repository=repository,
            head_branch=head_branch,
            base_branch=base_branch,
            title=title,
            body=body,
            draft=draft,
        )
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": type(e).__name__, "message": str(e)})


# ========================================================================
# CI Tools
# ========================================================================


@mcp.tool(
    name="list_ci_workers",
    description="List self-hosted CI runners/workers for a GitHub repository. Returns runner name, status, OS, and labels.",
)
async def list_ci_workers(repository: str) -> str:
    try:
        result = await _ci_service.list_ci_workers(repository)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": type(e).__name__, "message": str(e)})


@mcp.tool(
    name="list_ci_profiles",
    description="List CI workflow definitions (profiles) for a GitHub repository. Returns workflow name, state, and URL.",
)
async def list_ci_profiles(repository: str) -> str:
    try:
        result = await _ci_service.list_ci_profiles(repository)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": type(e).__name__, "message": str(e)})


@mcp.tool(
    name="list_ci_jobs",
    description="List CI job runs for a GitHub repository. Can filter by workflow_id, branch, or status.",
)
async def list_ci_jobs(
    repository: str,
    workflow_id: str = "",
    branch: str = "",
    status: str = "",
    limit: int = 20,
) -> str:
    try:
        result = await _ci_service.list_ci_jobs(
            repository=repository,
            workflow_id=workflow_id,
            branch=branch,
            status=status,
            limit=limit,
        )
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": type(e).__name__, "message": str(e)})


@mcp.tool(
    name="start_ci_job",
    description="Start/trigger a CI workflow dispatch on GitHub Actions. Requires workflow_id and a ref (branch/tag). Optionally pass workflow inputs as JSON string.",
)
async def start_ci_job(
    repository: str,
    workflow_id: str,
    ref: str = "main",
    inputs_json: str = "{}",
) -> str:
    try:
        inputs = json.loads(inputs_json) if inputs_json else {}
    except json.JSONDecodeError as e:
        return json.dumps({"error": "json_parse_error", "message": f"inputs_json is not valid JSON: {e.msg}"})

    if not isinstance(inputs, dict):
        return json.dumps({"error": "validation_error", "message": "inputs_json must decode to a JSON object"})

    try:
        result = await _ci_service.start_ci_job(
            repository=repository,
            workflow_id=workflow_id,
            ref=ref,
            inputs=inputs,
        )
        return json.dumps(result, ensure_ascii=False)
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        body = e.response.text[:1000]
        return json.dumps({"error": "http_error", "status_code": status, "message": body})
    except httpx.TimeoutException:
        return json.dumps({"error": "timeout", "message": "GitHub API request timed out"})
    except Exception as e:
        return json.dumps({"error": type(e).__name__, "message": str(e)})


@mcp.tool(
    name="get_ci_job",
    description="Get details of a specific CI job/workflow run by run_id. Includes job status, conclusion, and step details.",
)
async def get_ci_job(repository: str, run_id: str) -> str:
    try:
        result = await _ci_service.get_ci_job(repository, run_id)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": type(e).__name__, "message": str(e)})


@mcp.tool(
    name="get_ci_logs",
    description="Get CI job logs for a workflow run. Specify run_id and optionally job_id to get specific job logs.",
)
async def get_ci_logs(repository: str, run_id: str, job_id: str = "") -> str:
    try:
        result = await _ci_service.get_ci_logs(repository, run_id, job_id)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": type(e).__name__, "message": str(e)})


@mcp.tool(
    name="cancel_ci_job",
    description="Cancel a running CI job/workflow run by run_id.",
)
async def cancel_ci_job(repository: str, run_id: str) -> str:
    try:
        result = await _ci_service.cancel_ci_job(repository, run_id)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": type(e).__name__, "message": str(e)})
