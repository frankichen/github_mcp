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


def _seed_workspace(
    db,
    *,
    workspace_id: str,
    branch: str,
    lease_expires_at: float,
    status: str = "active",
    revision: int = 1,
):
    db.execute(
        "INSERT INTO workspaces VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            workspace_id,
            "owner/repo",
            branch,
            "main",
            "a" * 40,
            "b" * 40,
            "c" * 40,
            status,
            revision,
            "test-owner",
            lease_expires_at,
            "b" * 40,
            '{"paths":["app/**"]}',
            None,
            42,
            10.0,
            20.0,
        ),
    )


def test_workspace_public_separates_write_lease_from_index_pin_grace(monkeypatch):
    monkeypatch.setenv("MYGITHUB12_EXPIRED_WORKSPACE_PIN_GRACE_SECONDS", "3600")
    monkeypatch.setattr(workspace, "_now", lambda: 10_000.0)

    inside_grace = workspace._workspace_public(
        {
            "workspace_id": "inside",
            "status": "active",
            "lease_expires_at": 9_000.0,
            "scope_json": "{}",
        }
    )
    beyond_grace = workspace._workspace_public(
        {
            "workspace_id": "beyond",
            "status": "active",
            "lease_expires_at": 6_000.0,
            "scope_json": "{}",
        }
    )
    drifted = workspace._workspace_public(
        {
            "workspace_id": "drifted",
            "status": "drifted",
            "lease_expires_at": 9_500.0,
            "scope_json": "{}",
        }
    )

    assert inside_grace["lease_valid"] is False
    assert inside_grace["index_pin_active"] is True
    assert inside_grace["index_pin_grace_expires_at"] == 12_600.0
    assert beyond_grace["lease_valid"] is False
    assert beyond_grace["index_pin_active"] is False
    assert drifted["lease_valid"] is False
    assert drifted["index_pin_active"] is True


def test_expired_workspace_write_is_rejected_then_renewal_restores_lease_and_pin(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "renew-expired.db"))
    monkeypatch.setenv("MYGITHUB12_EXPIRED_WORKSPACE_PIN_GRACE_SECONDS", "0")
    monkeypatch.setattr(workspace, "_now", lambda: 1000.0)
    monkeypatch.setattr(workspace, "_service_repo", lambda *args, **kwargs: object())
    workspace.init_db()
    with workspace._db() as db:
        _seed_workspace(
            db,
            workspace_id="ws-expired",
            branch="ai/expired",
            lease_expires_at=900.0,
        )

    with pytest.raises(workspace.MyGithub12Error) as exc:
        workspace.workspace_write_preflight(
            object(),
            "owner/repo",
            "ai/expired",
            "b" * 40,
            workspace_id="ws-expired",
            expected_workspace_revision=1,
        )
    assert exc.value.code == "WORKSPACE_LEASE_REQUIRED"

    renewed = workspace.renew_workspace_lease(
        object(), "ws-expired", 1, lease_seconds=300
    )

    assert renewed["revision"] == 2
    assert renewed["lease_expires_at"] == 1300.0
    assert renewed["lease_valid"] is True
    assert renewed["index_pin_active"] is True
    assert renewed["repository"] == "owner/repo"
    assert renewed["branch"] == "ai/expired"
    assert renewed["base_commit_sha"] == "a" * 40
    assert renewed["head_sha"] == "b" * 40
    assert renewed["tree_sha"] == "c" * 40
    assert renewed["scope"] == {"paths": ["app/**"]}
    assert renewed["pr_number"] == 42


def test_default_workspace_lease_is_two_hours():
    assert workspace.DEFAULT_LEASE_SECONDS == 7200
    assert workspace.MAX_LEASE_SECONDS == 14400


def test_workspace_branch_ownership_allows_parallel_branches_but_not_duplicates(
    tmp_path, monkeypatch
):
    import sqlite3

    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "branch-ownership.db"))
    workspace.init_db()
    with workspace._db() as db:
        _seed_workspace(
            db,
            workspace_id="ws-a",
            branch="ai/feature-a",
            lease_expires_at=1.0,
        )
        _seed_workspace(
            db,
            workspace_id="ws-b",
            branch="ai/feature-b",
            lease_expires_at=1.0,
        )
        with pytest.raises(sqlite3.IntegrityError):
            _seed_workspace(
                db,
                workspace_id="ws-a-duplicate",
                branch="ai/feature-a",
                lease_expires_at=0.0,
            )

        rows = db.execute(
            "SELECT branch FROM workspaces WHERE repository='owner/repo' ORDER BY branch"
        ).fetchall()

    assert [row[0] for row in rows] == ["ai/feature-a", "ai/feature-b"]
