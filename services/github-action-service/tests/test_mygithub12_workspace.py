import pytest

from app import mygithub12 as core
from app import mygithub12_workspace as workspace
from app.exceptions import BranchExistsError, GitHubApiError, NotConfiguredError, RateLimitError


RESOLVED_SHA = "a" * 40


def _resolve_identity(*args, **kwargs):
    return {"repository": "owner/repo", "commit_sha": RESOLVED_SHA, "tree_sha": "b" * 40}


class FailingBranchService:
    def __init__(self, error):
        self.error = error
        self.base_ref = None

    def create_branch(self, repository, branch, base_ref):
        self.base_ref = base_ref
        raise self.error


def test_create_workspace_uses_resolved_commit_sha_and_preserves_ref_error(monkeypatch):
    monkeypatch.setattr(workspace, "resolve_identity", _resolve_identity)
    original = core.MyGithub12Error("REF_NOT_FOUND", "base ref disappeared")
    service = FailingBranchService(original)

    with pytest.raises(workspace.MyGithub12Error) as exc:
        workspace.create_workspace(
            service,
            "owner/repo",
            "workspace test",
            base_ref="main",
            branch="ai/workspace-test",
        )

    assert service.base_ref == RESOLVED_SHA
    assert exc.value is original
    assert exc.value.code == "REF_NOT_FOUND"


def test_branch_exists_is_not_reported_as_workspace_lease_conflict(monkeypatch):
    monkeypatch.setattr(workspace, "resolve_identity", _resolve_identity)
    service = FailingBranchService(BranchExistsError("owner/repo", "ai/workspace-test"))

    with pytest.raises(workspace.MyGithub12Error) as exc:
        workspace.create_workspace(
            service,
            "owner/repo",
            "workspace test",
            base_ref=RESOLVED_SHA,
            branch="ai/workspace-test",
        )

    assert exc.value.code == "BRANCH_EXISTS"
    assert exc.value.code != "WORKSPACE_LEASE_CONFLICT"
    assert exc.value.details == {"branch": "ai/workspace-test", "base_ref": RESOLVED_SHA}


@pytest.mark.parametrize(
    ("error", "expected_code", "github_status"),
    [
        (RateLimitError(), "RATE_LIMIT", None),
        (NotConfiguredError(), "GITHUB_NOT_CONFIGURED", None),
        (GitHubApiError(401, "auth failed"), "GITHUB_AUTH_FAILED", 401),
        (GitHubApiError(403, "forbidden"), "GITHUB_FORBIDDEN", 403),
        (GitHubApiError(404, "not found"), "GITHUB_NOT_FOUND", 404),
        (GitHubApiError(409, "conflict"), "GITHUB_CONFLICT", 409),
        (GitHubApiError(422, "validation"), "GITHUB_VALIDATION_ERROR", 422),
        (GitHubApiError(503, "temporary failure"), "GITHUB_TEMPORARY_FAILURE", 503),
    ],
)
def test_branch_create_app_errors_keep_their_semantics(monkeypatch, error, expected_code, github_status):
    monkeypatch.setattr(workspace, "resolve_identity", _resolve_identity)
    service = FailingBranchService(error)

    with pytest.raises(workspace.MyGithub12Error) as exc:
        workspace.create_workspace(
            service,
            "owner/repo",
            "workspace test",
            base_ref=RESOLVED_SHA,
            branch="ai/workspace-test",
        )

    assert exc.value.code == expected_code
    assert exc.value.code != "WORKSPACE_LEASE_CONFLICT"
    if github_status is not None:
        assert exc.value.details["github_status"] == github_status


def test_unknown_branch_create_error_has_dedicated_failure_code(monkeypatch):
    monkeypatch.setattr(workspace, "resolve_identity", _resolve_identity)
    service = FailingBranchService(RuntimeError("do not leak this raw message"))

    with pytest.raises(workspace.MyGithub12Error) as exc:
        workspace.create_workspace(
            service,
            "owner/repo",
            "workspace test",
            base_ref=RESOLVED_SHA,
            branch="ai/workspace-test",
        )

    assert exc.value.code == "WORKSPACE_BRANCH_CREATE_FAILED"
    assert exc.value.details["cause_type"] == "RuntimeError"
    assert "do not leak" not in exc.value.message
