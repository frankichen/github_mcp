"""CI data models for the private CI system."""

import hashlib
import json
from enum import Enum
from datetime import datetime, timezone
from typing import Optional
from pydantic import BaseModel, Field


class CIJobStatus(str, Enum):
    QUEUED = "queued"
    LEASED = "leased"
    DOWNLOADING = "downloading"
    PREPARING = "preparing"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    WORKER_LOST = "worker_lost"
    INTERNAL_ERROR = "internal_error"
    SUPERSEDED = "superseded"


ALLOWED_PRIORITIES = {"normal": 50, "high": 40}


def effective_priority(branch: str, profile: str, requested: int) -> int:
    """Lower numbers are leased first; preserve main/PR/fast ordering."""
    if profile == "repo-auto-check" and branch == "main":
        return 130
    if profile == "repo-auto-check":
        return 100
    if profile == "repo-fast-check":
        return 60
    return requested


def make_idempotency_key(repository: str, commit_sha: str, profile: str) -> str:
    raw = f"{repository}|{commit_sha}|{profile}|v1"
    return hashlib.sha256(raw.encode()).hexdigest()


class StartCIJobRequest(BaseModel):
    repository: str
    branch: str = ""
    commit_sha: str = Field(..., min_length=40, max_length=40)
    profile: str = "repo-auto-check"
    timeout_seconds: int = Field(default=900, ge=60, le=3600)
    priority: str = "normal"
    force_rerun: bool = False
    supersede_previous: bool = False


class StartCIJobResponse(BaseModel):
    job_id: str
    idempotency_key: str
    repository: str
    branch: str
    commit_sha: str
    profile: str
    status: str
    priority: int
    queue_position: int
    worker_online: bool
    deduplicated: bool
    previous_job_id: Optional[str] = None
    queued_count: int
    created_at: str


class CIJobResponse(BaseModel):
    job_id: str
    repository: str
    branch: str
    commit_sha: str
    profile: str
    status: str
    priority: int
    worker_id: Optional[str] = None
    queue_position: Optional[int] = None
    current_step: Optional[str] = None
    exit_code: Optional[int] = None
    created_at: Optional[str] = None
    queued_at: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    duration_seconds: Optional[float] = None
    summary: Optional[dict] = None
    log_truncated: bool = False
    cancel_requested: bool = False
    superseded_by_job_id: Optional[str] = None
    attempts: int = 0


class CIJobStepResponse(BaseModel):
    step_name: str
    status: str
    exit_code: Optional[int] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    duration_seconds: Optional[float] = None


class CIJobLogResponse(BaseModel):
    job_id: str
    chunks: list[dict]
    next_offset: Optional[int] = None
    total_bytes: int
    truncated: bool


class CIWorkerInfo(BaseModel):
    worker_id: str
    online: bool
    last_heartbeat: Optional[str] = None
    supported_profiles: list[str]
    max_concurrent: int
    current_job: Optional[str] = None
    status: str = "idle"


class CIProfileInfo(BaseModel):
    name: str
    description: str
    language: Optional[str] = None
    languages: Optional[list[str]] = None
