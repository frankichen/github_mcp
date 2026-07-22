from pydantic import BaseModel, Field
from typing import Optional


class FileGetResponse(BaseModel):
    repository: str
    path: str
    ref: str
    sha: str
    size: int
    content: str
    start_line: int
    end_line: int
    total_lines: int
    truncated: bool


class DirectoryItem(BaseModel):
    name: str
    path: str
    type: str
    sha: str
    size: int


class DirectoryResponse(BaseModel):
    repository: str
    path: str
    ref: str
    items: list[DirectoryItem]


class BranchCreateRequest(BaseModel):
    repository: str
    branch: str
    base_branch: str = "main"


class BranchResponse(BaseModel):
    success: bool
    repository: str
    branch: str
    base_branch: str
    commit_sha: str


class PullRequestConfig(BaseModel):
    create: bool = False
    base_branch: str = "main"
    title: str = ""
    body: str = ""


class FileOperation(BaseModel):
    path: str
    operation: str = "upsert"
    content: Optional[str] = None
    expected_sha: Optional[str] = None


class CommitRequest(BaseModel):
    repository: str
    branch: str
    base_branch: str = "main"
    create_branch_if_missing: bool = False
    commit_message: str
    expected_head_sha: Optional[str] = None
    files: list[FileOperation]
    pull_request: Optional[PullRequestConfig] = None


class ChangedFile(BaseModel):
    path: str
    operation: str


class PullRequestInfo(BaseModel):
    number: int
    url: str


class CommitResponse(BaseModel):
    success: bool
    repository: str
    branch: str
    commit_sha: str
    commit_url: str
    changed_files: list[ChangedFile]
    pull_request: Optional[PullRequestInfo] = None


class PullRequestCreateRequest(BaseModel):
    repository: str
    head_branch: str
    base_branch: str = "main"
    title: str
    body: str = ""
    draft: bool = True


class PullRequestResponse(BaseModel):
    success: bool
    repository: str
    head_branch: str
    base_branch: str
    pull_request: PullRequestInfo


class ErrorResponse(BaseModel):
    error: str
    message: str
    details: Optional[dict] = None
