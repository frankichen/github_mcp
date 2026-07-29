import logging
from typing import Any, Optional
import httpx

from app.config import settings
from app.github_auth import credential_provider
from app.github_policy import ensure_repository_allowed

logger = logging.getLogger(__name__)

GITHUB_API_URL = "https://api.github.com"


def _get_headers() -> dict:
    token = credential_provider.token()
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def _github_request(method: str, path: str, json_data: Optional[dict] = None) -> dict:
    url = f"{GITHUB_API_URL}{path}"
    headers = _get_headers()
    async with httpx.AsyncClient(timeout=30.0) as client:
        if method == "GET":
            resp = await client.get(url, headers=headers)
        elif method == "POST":
            resp = await client.post(url, headers=headers, json=json_data)
        else:
            raise ValueError(f"Unsupported method: {method}")
        resp.raise_for_status()
        if resp.status_code == 204:
            return {}
        return resp.json()


def _parse_repo(repository: str) -> tuple:
    ensure_repository_allowed(repository)
    parts = repository.split("/")
    if len(parts) != 2:
        raise ValueError(f"Invalid repository format: {repository}. Expected owner/repo.")
    return parts[0], parts[1]


class CiService:
    async def list_ci_workers(self, repository: str) -> dict:
        owner, repo = _parse_repo(repository)
        data = await _github_request("GET", f"/repos/{owner}/{repo}/actions/runners")
        runners = data.get("runners", [])
        return {
            "repository": repository,
            "total_count": data.get("total_count", len(runners)),
            "runners": [
                {
                    "id": r["id"],
                    "name": r["name"],
                    "os": r.get("os", ""),
                    "status": r.get("status", ""),
                    "busy": r.get("busy", False),
                    "labels": [lbl.get("name", "") for lbl in r.get("labels", [])],
                }
                for r in runners
            ],
        }

    async def list_ci_profiles(self, repository: str) -> dict:
        owner, repo = _parse_repo(repository)
        data = await _github_request("GET", f"/repos/{owner}/{repo}/actions/workflows")
        workflows = data.get("workflows", [])
        return {
            "repository": repository,
            "total_count": data.get("total_count", len(workflows)),
            "workflows": [
                {
                    "id": w["id"],
                    "name": w["name"],
                    "path": w.get("path", ""),
                    "state": w.get("state", ""),
                    "url": w.get("html_url", ""),
                }
                for w in workflows
            ],
        }

    async def list_ci_jobs(
        self,
        repository: str,
        workflow_id: str = "",
        branch: str = "",
        status: str = "",
        limit: int = 20,
    ) -> dict:
        owner, repo = _parse_repo(repository)
        params = [f"per_page={min(limit, 100)}"]
        if branch:
            params.append(f"branch={branch}")
        if status:
            params.append(f"status={status}")
        query = "&".join(params)

        if workflow_id:
            path = f"/repos/{owner}/{repo}/actions/workflows/{workflow_id}/runs"
        else:
            path = f"/repos/{owner}/{repo}/actions/runs"

        data = await _github_request("GET", f"{path}?{query}")
        runs = data.get("workflow_runs", [])
        return {
            "repository": repository,
            "total_count": data.get("total_count", len(runs)),
            "workflow_runs": [
                {
                    "id": r["id"],
                    "name": r.get("name", ""),
                    "status": r.get("status", ""),
                    "conclusion": r.get("conclusion", ""),
                    "head_branch": r.get("head_branch", ""),
                    "head_sha": r.get("head_sha", ""),
                    "run_number": r.get("run_number", 0),
                    "event": r.get("event", ""),
                    "created_at": r.get("created_at", ""),
                    "updated_at": r.get("updated_at", ""),
                    "url": r.get("html_url", ""),
                    "actor": r.get("actor", {}).get("login", "") if r.get("actor") else "",
                }
                for r in runs[:limit]
            ],
        }

    async def start_ci_job(
        self,
        repository: str,
        workflow_id: str,
        ref: str = "main",
        inputs: Optional[dict] = None,
    ) -> dict:
        owner, repo = _parse_repo(repository)
        payload: dict[str, Any] = {"ref": ref}
        if inputs:
            payload["inputs"] = inputs

        await _github_request(
            "POST",
            f"/repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches",
            json_data=payload,
        )
        return {
            "success": True,
            "repository": repository,
            "workflow_id": workflow_id,
            "ref": ref,
            "message": f"Workflow dispatch triggered for workflow {workflow_id} on ref {ref}",
        }

    async def get_ci_job(self, repository: str, run_id: str) -> dict:
        owner, repo = _parse_repo(repository)
        data = await _github_request("GET", f"/repos/{owner}/{repo}/actions/runs/{run_id}")
        jobs_data = await _github_request("GET", f"/repos/{owner}/{repo}/actions/runs/{run_id}/jobs")
        jobs = jobs_data.get("jobs", [])

        return {
            "repository": repository,
            "run_id": data.get("id", run_id),
            "name": data.get("name", ""),
            "status": data.get("status", ""),
            "conclusion": data.get("conclusion", ""),
            "head_branch": data.get("head_branch", ""),
            "head_sha": data.get("head_sha", ""),
            "run_number": data.get("run_number", 0),
            "event": data.get("event", ""),
            "url": data.get("html_url", ""),
            "created_at": data.get("created_at", ""),
            "updated_at": data.get("updated_at", ""),
            "jobs": [
                {
                    "id": j["id"],
                    "name": j.get("name", ""),
                    "status": j.get("status", ""),
                    "conclusion": j.get("conclusion", ""),
                    "started_at": j.get("started_at", ""),
                    "completed_at": j.get("completed_at", ""),
                    "steps": [
                        {
                            "name": s.get("name", ""),
                            "status": s.get("status", ""),
                            "conclusion": s.get("conclusion", ""),
                        }
                        for s in j.get("steps", [])
                    ],
                }
                for j in jobs
            ],
        }

    async def get_ci_logs(self, repository: str, run_id: str, job_id: str = "") -> dict:
        owner, repo = _parse_repo(repository)
        if job_id:
            jobs = [{"id": int(job_id)}]
        else:
            jobs_data = await _github_request("GET", f"/repos/{owner}/{repo}/actions/runs/{run_id}/jobs")
            jobs = jobs_data.get("jobs", [])

        log_results = []
        for j in jobs:
            jid = j["id"]
            try:
                log_url = f"/repos/{owner}/{repo}/actions/jobs/{jid}/logs"
                headers = _get_headers()
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.get(f"{GITHUB_API_URL}{log_url}", headers=headers, follow_redirects=True)
                    if resp.status_code >= 400:
                        log_content = f"[Error fetching logs: HTTP {resp.status_code}]"
                    else:
                        log_content = resp.text[:50000]
                log_results.append({"job_id": jid, "log": log_content})
            except Exception as e:
                log_results.append({"job_id": jid, "log": f"[Error: {str(e)}]"})

        return {
            "repository": repository,
            "run_id": run_id,
            "logs": log_results,
        }

    async def cancel_ci_job(self, repository: str, run_id: str) -> dict:
        owner, repo = _parse_repo(repository)
        await _github_request("POST", f"/repos/{owner}/{repo}/actions/runs/{run_id}/cancel")
        return {
            "success": True,
            "repository": repository,
            "run_id": run_id,
            "message": f"Workflow run {run_id} cancelled successfully",
        }


_ci_service = CiService()


def get_ci_service() -> CiService:
    return _ci_service
