"""Durable GitHub post-write verification.

A GitHub write is not successful merely because object creation and ref-edit
calls returned.  The branch, commit, tree, and changed paths must be read back
from GitHub again before any caller may persist success in Workspace or
idempotency state.
"""
from __future__ import annotations

import time
from typing import Any


class WriteVerificationError(Exception):
    def __init__(self, message: str, details: dict[str, Any]):
        super().__init__(message)
        self.message = message
        self.details = details


def _details(
    repository: str,
    branch: str,
    expected_previous_head: str,
    new_commit_sha: str,
    expected_tree_sha: str,
    failed_stage: str,
    *,
    observed_branch_head: str = "",
    observed_commit_sha: str = "",
    observed_tree_sha: str = "",
    path: str = "",
    expected_blob_sha: str | None = None,
    observed_blob_sha: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "repository": repository,
        "branch": branch,
        "expected_previous_head": expected_previous_head,
        "new_commit_sha": new_commit_sha,
        "observed_branch_head": observed_branch_head,
        "expected_tree_sha": expected_tree_sha,
        "observed_tree_sha": observed_tree_sha,
        "failed_stage": failed_stage,
    }
    if observed_commit_sha:
        result["observed_commit_sha"] = observed_commit_sha
    if path:
        result["path"] = path
        result["expected_blob_sha"] = expected_blob_sha
        result["observed_blob_sha"] = observed_blob_sha
    return result


def post_write_verify(
    client: Any,
    repository: str,
    branch: str,
    expected_previous_head: str,
    new_commit_sha: str,
    expected_tree_sha: str,
    expected_paths: dict[str, str | None],
    *,
    attempts: int = 3,
    retry_delay_seconds: float = 0.2,
) -> dict[str, Any]:
    """Read GitHub again and return durable write evidence.

    ``client`` is the GitHubClient adapter.  Every adapter method called here
    performs a fresh GitHub API request; no object returned by the write path is
    accepted as verification evidence.
    """
    attempts = max(1, min(int(attempts), 3))
    last_details = _details(
        repository,
        branch,
        expected_previous_head,
        new_commit_sha,
        expected_tree_sha,
        "branch_ref_readback",
    )

    for attempt in range(1, attempts + 1):
        observed_branch_head = ""
        observed_commit_sha = ""
        observed_tree_sha = ""
        try:
            observed_branch_head = client.get_branch_head_fresh(repository, branch) or ""
            if observed_branch_head != new_commit_sha:
                last_details = _details(
                    repository,
                    branch,
                    expected_previous_head,
                    new_commit_sha,
                    expected_tree_sha,
                    "branch_ref_readback",
                    observed_branch_head=observed_branch_head,
                )
                raise WriteVerificationError("GitHub branch read-back did not confirm the new commit", last_details)

            commit_state = client.get_commit_state_fresh(repository, new_commit_sha)
            if not commit_state:
                last_details = _details(
                    repository,
                    branch,
                    expected_previous_head,
                    new_commit_sha,
                    expected_tree_sha,
                    "commit_readback",
                    observed_branch_head=observed_branch_head,
                )
                raise WriteVerificationError("GitHub commit read-back could not find the new commit", last_details)
            observed_commit_sha = str(commit_state.get("commit_sha") or "")
            observed_tree_sha = str(commit_state.get("tree_sha") or "")
            if observed_commit_sha != new_commit_sha:
                last_details = _details(
                    repository,
                    branch,
                    expected_previous_head,
                    new_commit_sha,
                    expected_tree_sha,
                    "commit_readback",
                    observed_branch_head=observed_branch_head,
                    observed_commit_sha=observed_commit_sha,
                    observed_tree_sha=observed_tree_sha,
                )
                raise WriteVerificationError("GitHub commit read-back returned the wrong commit", last_details)
            if observed_tree_sha != expected_tree_sha:
                last_details = _details(
                    repository,
                    branch,
                    expected_previous_head,
                    new_commit_sha,
                    expected_tree_sha,
                    "tree_readback",
                    observed_branch_head=observed_branch_head,
                    observed_commit_sha=observed_commit_sha,
                    observed_tree_sha=observed_tree_sha,
                )
                raise WriteVerificationError("GitHub commit tree does not match the expected tree", last_details)

            observed_tree_object = client.get_tree_sha_fresh(repository, expected_tree_sha) or ""
            if observed_tree_object != expected_tree_sha:
                last_details = _details(
                    repository,
                    branch,
                    expected_previous_head,
                    new_commit_sha,
                    expected_tree_sha,
                    "tree_readback",
                    observed_branch_head=observed_branch_head,
                    observed_commit_sha=observed_commit_sha,
                    observed_tree_sha=observed_tree_object,
                )
                raise WriteVerificationError("GitHub tree read-back did not confirm the expected tree", last_details)

            verified_paths = []
            for path in sorted(expected_paths):
                expected_blob_sha = expected_paths[path]
                observed_blob_sha = client.get_file_sha_fresh(repository, path, new_commit_sha)
                if expected_blob_sha is None:
                    if observed_blob_sha is not None:
                        last_details = _details(
                            repository,
                            branch,
                            expected_previous_head,
                            new_commit_sha,
                            expected_tree_sha,
                            "path_readback",
                            observed_branch_head=observed_branch_head,
                            observed_commit_sha=observed_commit_sha,
                            observed_tree_sha=observed_tree_sha,
                            path=path,
                            expected_blob_sha=None,
                            observed_blob_sha=observed_blob_sha,
                        )
                        raise WriteVerificationError("GitHub read-back still contains a deleted path", last_details)
                elif observed_blob_sha != expected_blob_sha:
                    last_details = _details(
                        repository,
                        branch,
                        expected_previous_head,
                        new_commit_sha,
                        expected_tree_sha,
                        "path_readback",
                        observed_branch_head=observed_branch_head,
                        observed_commit_sha=observed_commit_sha,
                        observed_tree_sha=observed_tree_sha,
                        path=path,
                        expected_blob_sha=expected_blob_sha,
                        observed_blob_sha=observed_blob_sha,
                    )
                    raise WriteVerificationError("GitHub changed-path read-back did not confirm the expected blob", last_details)
                verified_paths.append({"path": path, "blob_sha": observed_blob_sha})

            return {
                "write_verified": True,
                "repository": repository,
                "branch": branch,
                "previous_head_sha": expected_previous_head,
                "commit_sha": new_commit_sha,
                "tree_sha": expected_tree_sha,
                "verified_branch_head_sha": observed_branch_head,
                "verified_commit_sha": observed_commit_sha,
                "verified_tree_sha": observed_tree_sha,
                "verified_paths": verified_paths,
                "verify_attempts": attempt,
            }
        except WriteVerificationError:
            if attempt >= attempts:
                break
        except Exception as exc:
            stage = "branch_ref_readback" if not observed_branch_head else "commit_readback"
            last_details = _details(
                repository,
                branch,
                expected_previous_head,
                new_commit_sha,
                expected_tree_sha,
                stage,
                observed_branch_head=observed_branch_head,
                observed_commit_sha=observed_commit_sha,
                observed_tree_sha=observed_tree_sha,
            )
            last_details["cause_type"] = type(exc).__name__
            if attempt >= attempts:
                break
        if retry_delay_seconds > 0:
            time.sleep(retry_delay_seconds)

    raise WriteVerificationError("GitHub post-write verification failed", last_details)
