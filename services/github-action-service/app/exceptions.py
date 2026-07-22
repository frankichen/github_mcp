from typing import Optional


class AppError(Exception):
    def __init__(self, error: str, message: str, status_code: int = 500, details: Optional[dict] = None):
        self.error = error
        self.message = message
        self.status_code = status_code
        self.details = details


class RepositoryNotAllowedError(AppError):
    def __init__(self, repository: str):
        super().__init__(
            error="repository_not_allowed",
            message=f"Repository '{repository}' is not in the allowed list",
            status_code=403,
        )


class DefaultBranchWriteDeniedError(AppError):
    def __init__(self, repository: str, branch: str):
        super().__init__(
            error="default_branch_write_denied",
            message=f"Writing to default branch '{branch}' of '{repository}' is not allowed",
            status_code=403,
        )


class BranchConflictError(AppError):
    def __init__(self, message: str, details: Optional[dict] = None):
        super().__init__(
            error="branch_conflict",
            message=message,
            status_code=409,
            details=details,
        )


class BranchExistsError(AppError):
    def __init__(self, repository: str, branch: str):
        super().__init__(
            error="branch_exists",
            message=f"Branch '{branch}' already exists in '{repository}'",
            status_code=409,
        )


class ShaConflictError(AppError):
    def __init__(self, path: str, expected: Optional[str], actual: str):
        super().__init__(
            error="sha_conflict",
            message=f"SHA mismatch for '{path}'",
            status_code=409,
            details={"path": path, "expected": expected, "actual": actual},
        )


class HeadShaConflictError(AppError):
    def __init__(self, expected: str, actual: str):
        super().__init__(
            error="head_sha_conflict",
            message="Branch HEAD has changed",
            status_code=409,
            details={"expected": expected, "actual": actual},
        )


class NotFoundError(AppError):
    def __init__(self, message: str):
        super().__init__(
            error="not_found",
            message=message,
            status_code=404,
        )


class ContentTooLargeError(AppError):
    def __init__(self, message: str):
        super().__init__(
            error="content_too_large",
            message=message,
            status_code=413,
        )


class ValidationError(AppError):
    def __init__(self, message: str):
        super().__init__(
            error="validation_error",
            message=message,
            status_code=422,
        )


class GitHubApiError(AppError):
    def __init__(self, status_code: int, message: str):
        super().__init__(
            error="github_api_error",
            message=message,
            status_code=503 if status_code >= 500 else 502,
        )


class RateLimitError(AppError):
    def __init__(self):
        super().__init__(
            error="rate_limit",
            message="GitHub API rate limit exceeded. Please try again later.",
            status_code=429,
        )


class NotConfiguredError(AppError):
    def __init__(self):
        super().__init__(
            error="not_configured",
            message="GitHub Token is not configured. Please set GITHUB_TOKEN in the environment.",
            status_code=503,
        )
