import logging
from fastapi import APIRouter, Request, Query, Depends, HTTPException
from starlette.concurrency import run_in_threadpool
from typing import Optional

from app.auth import verify_api_key
from app.github_client import GitHubClient
from app.services.github_service import GitHubService
from app.models import (
    FileGetResponse,
    DirectoryResponse,
    BranchCreateRequest,
    BranchResponse,
    CommitRequest,
    CommitResponse,
    PullRequestCreateRequest,
    PullRequestResponse,
)
from app.exceptions import AppError

logger = logging.getLogger(__name__)

router = APIRouter()

_client = GitHubClient()
_service = GitHubService(_client)


@router.get("/api/v1/github/file")
async def get_file(
    request: Request,
    repository: str = Query(..., description="Repository in owner/repo format"),
    path: str = Query(..., description="File path in the repository"),
    ref: str = Query("", description="Git reference (branch, tag, commit SHA)"),
    start_line: Optional[int] = Query(None, ge=1),
    end_line: Optional[int] = Query(None, ge=1),
):
    verify_api_key(request)
    try:
        result = await run_in_threadpool(
            _service.get_file,
            repository=repository,
            path=path,
            ref=ref,
            start_line=start_line,
            end_line=end_line,
        )
        return result
    except AppError as e:
        raise HTTPException(status_code=e.status_code, detail={"error": e.error, "message": e.message, "details": e.details})


@router.get("/api/v1/github/directory")
async def list_directory(
    request: Request,
    repository: str = Query(..., description="Repository in owner/repo format"),
    path: str = Query(..., description="Directory path in the repository"),
    ref: str = Query("", description="Git reference"),
):
    verify_api_key(request)
    try:
        result = await run_in_threadpool(
            _service.list_directory, repository=repository, path=path, ref=ref
        )
        return result
    except AppError as e:
        raise HTTPException(status_code=e.status_code, detail={"error": e.error, "message": e.message, "details": e.details})


@router.post("/api/v1/github/branches")
async def create_branch(request: Request, body: BranchCreateRequest):
    verify_api_key(request)
    try:
        result = await run_in_threadpool(
            _service.create_branch,
            repository=body.repository,
            branch=body.branch,
            base_branch=body.base_branch,
        )
        return result
    except AppError as e:
        raise HTTPException(status_code=e.status_code, detail={"error": e.error, "message": e.message, "details": e.details})


@router.post("/api/v1/github/commits")
async def commit_files(request: Request, body: CommitRequest):
    verify_api_key(request)
    try:
        result = await run_in_threadpool(_service.commit_files, body)
        return result
    except AppError as e:
        raise HTTPException(status_code=e.status_code, detail={"error": e.error, "message": e.message, "details": e.details})


@router.post("/api/v1/github/pull-requests")
async def create_pull_request(request: Request, body: PullRequestCreateRequest):
    verify_api_key(request)
    try:
        result = await run_in_threadpool(
            _service.create_pull_request,
            repository=body.repository,
            head_branch=body.head_branch,
            base_branch=body.base_branch,
            title=body.title,
            body=body.body,
            draft=body.draft,
        )
        return result
    except AppError as e:
        raise HTTPException(status_code=e.status_code, detail={"error": e.error, "message": e.message, "details": e.details})
