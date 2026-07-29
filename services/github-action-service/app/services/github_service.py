import re
import logging
import hashlib
from typing import Optional
from github.GithubException import GithubException, UnknownObjectException

from app.config import settings
from app.github_client import GitHubClient
from app.github_policy import ensure_repository_allowed
from app.exceptions import (
    RepositoryNotAllowedError,
    DefaultBranchWriteDeniedError,
    BranchConflictError,
    BranchExistsError,
    ShaConflictError,
    HeadShaConflictError,
    NotFoundError,
    ContentTooLargeError,
    ValidationError,
    GitHubApiError,
    RateLimitError,
    NotConfiguredError,
)

logger = logging.getLogger(__name__)

_PATH_RE = re.compile(r"^[^\x00-\x1f\x7f-\x9f/](?:[^\x00-\x1f\x7f-\x9f]*[^\x00-\x1f\x7f-\x9f/])?$")
_RESERVED_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


class GitHubService:
    def __init__(self, client: GitHubClient):
        self.client = client

    def _check_repository_allowed(self, repository: str):
        ensure_repository_allowed(repository)

    def _check_default_branch_write(self, repository: str, branch: str):
        if settings.ALLOW_DEFAULT_BRANCH_WRITE:
            return
        default_branch = self.client.get_default_branch(repository)
        if branch == default_branch:
            raise DefaultBranchWriteDeniedError(repository, branch)

    def _validate_path(self, path: str):
        if not path:
            raise ValidationError("File path cannot be empty")
        if path.startswith("/"):
            raise ValidationError("File path cannot start with '/'")
        if ".." in path.split("/"):
            raise ValidationError("File path cannot contain '..'")
        if _RESERVED_CHARS.search(path):
            raise ValidationError("File path contains invalid characters")
        if not _PATH_RE.match(path):
            raise ValidationError("Invalid file path")

    def get_file(
        self,
        repository: str,
        path: str,
        ref: str = "",
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
    ) -> dict:
        self._check_repository_allowed(repository)
        self._validate_path(path)

        content, sha, size = self.client.get_file(repository, path, ref if ref else "")
        if content is None:
            raise NotFoundError(f"File '{path}' not found in '{repository}'")

        lines = content.split("\n")
        total_lines = len(lines)
        if content and content.endswith("\n"):
            total_lines = max(total_lines - 1, 0)

        sl = start_line if start_line is not None else 1
        el = end_line if end_line is not None else total_lines

        if sl < 1:
            sl = 1
        if el > total_lines:
            el = total_lines
        if sl > total_lines:
            sl = total_lines
            el = total_lines

        truncated = False
        if len(content) > settings.MAX_FILE_CHARACTERS:
            truncated = True
            selected_lines = lines[sl - 1:el] if sl <= el else []
            content = "\n".join(selected_lines)
        elif sl > 1 or el < total_lines:
            selected_lines = lines[sl - 1:el]
            content = "\n".join(selected_lines)

        return {
            "repository": repository,
            "path": path,
            "ref": ref if ref else self.client.get_default_branch(repository),
            "sha": sha,
            "size": size,
            "content": content,
            "start_line": sl,
            "end_line": el,
            "total_lines": total_lines,
            "truncated": truncated,
        }

    def list_directory(self, repository: str, path: str, ref: str = "") -> dict:
        self._check_repository_allowed(repository)
        self._validate_path(path)

        items = self.client.get_directory(repository, path, ref if ref else "")
        if items is None:
            raise NotFoundError(f"Directory '{path}' not found in '{repository}'")

        return {
            "repository": repository,
            "path": path,
            "ref": ref if ref else self.client.get_default_branch(repository),
            "items": items,
        }

    def create_branch(self, repository: str, branch: str, base_branch: str = "main") -> dict:
        self._check_repository_allowed(repository)

        existing = self.client.get_branch(repository, branch)
        if existing:
            raise BranchExistsError(repository, branch)

        ref = self.client.create_branch(repository, branch, base_branch)
        return {
            "success": True,
            "repository": repository,
            "branch": branch,
            "base_branch": base_branch,
            "commit_sha": ref.object.sha,
        }

    def commit_files(self, request) -> dict:
        repository = request.repository
        branch = request.branch
        base_branch = request.base_branch

        self._check_repository_allowed(repository)
        self._check_default_branch_write(repository, branch)

        if not request.files:
            raise ValidationError("At least one file is required")

        if len(request.files) > settings.MAX_FILES_PER_COMMIT:
            raise ContentTooLargeError(
                f"Too many files. Maximum {settings.MAX_FILES_PER_COMMIT} files per commit, got {len(request.files)}"
            )

        total_chars = 0
        for f in request.files:
            self._validate_path(f.path)
            if f.operation not in ("upsert", "delete"):
                raise ValidationError(f"Invalid operation '{f.operation}'. Supported: upsert, delete")
            if f.operation == "upsert":
                if f.content is None:
                    raise ValidationError(f"Content is required for upsert operation on '{f.path}'")
                if len(f.content) > settings.MAX_FILE_CHARACTERS:
                    raise ContentTooLargeError(
                        f"File '{f.path}' exceeds max size of {settings.MAX_FILE_CHARACTERS} characters"
                    )
                total_chars += len(f.content)

        if total_chars > settings.MAX_TOTAL_CHARACTERS:
            raise ContentTooLargeError(
                f"Total content size {total_chars} exceeds max of {settings.MAX_TOTAL_CHARACTERS} characters"
            )

        branch_obj = self.client.get_branch(repository, branch)
        if not branch_obj:
            if request.create_branch_if_missing:
                ref = self.client.create_branch(repository, branch, base_branch)
                parent_sha = ref.object.sha
            else:
                raise NotFoundError(f"Branch '{branch}' not found in '{repository}'")
        else:
            parent_sha = branch_obj.commit.sha

        if request.expected_head_sha:
            if parent_sha != request.expected_head_sha:
                raise HeadShaConflictError(expected=request.expected_head_sha, actual=parent_sha)

        tree_elements = []
        old_shas = {}

        for f in request.files:
            old_shas[f.path] = self.client.get_file_sha(repository, f.path, branch)
            if f.operation == "delete":
                actual_sha = old_shas[f.path]
                if actual_sha is None:
                    raise NotFoundError(f"File '{f.path}' not found for deletion")
                if f.expected_sha:
                    if actual_sha != f.expected_sha:
                        raise ShaConflictError(path=f.path, expected=f.expected_sha, actual=actual_sha)
                tree_elements.append({
                    "path": f.path,
                    "mode": "100644",
                    "type": "blob",
                    "sha": None,
                })
            else:
                if f.expected_sha:
                    actual_sha = old_shas[f.path]
                    if actual_sha != f.expected_sha:
                        raise ShaConflictError(path=f.path, expected=f.expected_sha, actual=actual_sha)
                blob = self.client.create_blob(repository, f.content)
                tree_elements.append({
                    "path": f.path,
                    "mode": "100644",
                    "type": "blob",
                    "sha": blob.sha,
                })

        base_tree = self.client.get_git_tree(repository, parent_sha)
        base_tree_sha = base_tree.sha if base_tree else ""

        tree = self.client.create_git_tree(repository, tree_elements, base_tree_sha)

        commit = self.client.create_commit(
            repository,
            message=request.commit_message,
            tree_sha=tree.sha,
            parent_shas=[parent_sha],
        )

        ref_name = f"refs/heads/{branch}"
        self.client.update_ref(repository, ref_name, commit.sha, force=False)

        verified_files = []
        for f in request.files:
            if f.operation == "delete":
                if self.client.get_file_sha(repository, f.path, commit.sha) is not None:
                    raise ValidationError(f"Write verification failed: deleted file '{f.path}' is still present")
                verified_files.append({"path": f.path, "operation": "delete", "old_blob_sha": old_shas[f.path], "new_blob_sha": None, "content_sha256": None, "size_bytes": 0})
                continue
            actual_content, actual_sha, actual_size = self.client.get_file(repository, f.path, commit.sha)
            if actual_content is None or actual_content.encode("utf-8") != f.content.encode("utf-8"):
                raise ValidationError(f"Write verification failed: read-back bytes differ for '{f.path}'")
            verified_files.append({"path": f.path, "operation": "modify" if old_shas[f.path] else "add", "old_blob_sha": old_shas[f.path], "new_blob_sha": actual_sha, "content_sha256": hashlib.sha256(f.content.encode("utf-8")).hexdigest(), "size_bytes": actual_size})

        commit_url = f"https://github.com/{repository}/commit/{commit.sha}"

        changed_files = [
            {"path": f.path, "operation": f.operation}
            for f in request.files
        ]

        response_data = {
            "success": True,
            "repository": repository,
            "branch": branch,
            "commit_sha": commit.sha,
            "commit_url": commit_url,
            "changed_files": verified_files,
            "old_head_sha": parent_sha,
            "new_head_sha": commit.sha,
            "tree_sha": tree.sha,
            "operation_count": len(request.files),
            "pull_request": None,
        }

        pr_config = request.pull_request
        if pr_config and pr_config.create:
            pr_title = pr_config.title or request.commit_message
            pr_body = pr_config.body or ""

            try:
                pr = self.client.create_pull_request(
                    repo_name=repository,
                    head_branch=branch,
                    base_branch=pr_config.base_branch,
                    title=pr_title,
                    body=pr_body,
                    draft=False,
                )
                response_data["pull_request"] = {
                    "number": pr.number,
                    "url": pr.html_url,
                }
            except GithubException as e:
                logger.warning(f"Failed to create PR: {e}")

        return response_data

    def create_pull_request(
        self,
        repository: str,
        head_branch: str,
        base_branch: str = "main",
        title: str = "",
        body: str = "",
        draft: bool = True,
    ) -> dict:
        self._check_repository_allowed(repository)

        pr = self.client.create_pull_request(
            repo_name=repository,
            head_branch=head_branch,
            base_branch=base_branch,
            title=title,
            body=body,
            draft=draft,
        )

        return {
            "success": True,
            "repository": repository,
            "head_branch": head_branch,
            "base_branch": base_branch,
            "pull_request": {
                "number": pr.number,
                "url": pr.html_url,
            },
        }
