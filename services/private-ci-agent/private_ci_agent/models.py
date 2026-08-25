"""Data models for CI Agent."""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Job:
    job_id: str
    repository: str
    branch: str
    commit_sha: str
    profile: str
    timeout_seconds: int
    lease_token: str
    lease_expires_at: str
    base_sha: str = ""
    changed_files: list[str] = field(default_factory=list)
    changed_files_total: int = 0
    changed_files_truncated: bool = False
    contract_integrity_attested: bool = False
    workspace: str = ""
    source_dir: str = ""
    status: str = "leased"


@dataclass
class StepResult:
    step_name: str
    status: str = "pending"
    exit_code: Optional[int] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    duration_seconds: Optional[float] = None
    log_start_offset: int = 0
    log_end_offset: int = 0
