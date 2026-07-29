"""Shared repository authorization policy for every GitHub API surface."""

import re

from app.config import settings
from app.exceptions import RepositoryNotAllowedError


_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def ensure_repository_allowed(repository: str) -> str:
    if not _REPOSITORY.fullmatch(repository or ""):
        raise ValueError("repository must use owner/name format")
    allowed = settings.ALLOWED_REPOSITORIES.strip()
    repositories = {item.strip() for item in allowed.split(",") if item.strip()}
    if allowed != "*" and repository not in repositories:
        raise RepositoryNotAllowedError(repository)
    return repository


def repository_is_allowed(repository: str) -> bool:
    try:
        ensure_repository_allowed(repository)
        return True
    except (RepositoryNotAllowedError, ValueError):
        return False
