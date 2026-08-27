from types import SimpleNamespace

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


def _resume_service(branch_sha: str = "b" * 40, base_sha: str = "a" * 40):
    repo = SimpleNamespace(get_commit=lambda sha: SimpleNamespace(tree=SimpleNamespace(sha="c" * 40)))

    def get_branch(repository, branch):
        sha = base_sha if branch == "main" else branch_sha
        return SimpleNamespace(commit=SimpleNamespace(sha=sha))

    return SimpleNamespace(client=SimpleNamespace(get_branch=get_branch)), repo


def test_legacy_active_expired_is_effectively_expired_and_absent_from_active_overlap(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "effective-expired.db"))
    monkeypatch.setattr(workspace, "_now", lambda: 1000.0)
    monkeypatch.setattr(workspace, "_service_repo", lambda *args, **kwargs: object())
    workspace.init_db()
    with workspace._db() as db:
        _seed_workspace(db, workspace_id="ws-expired", branch="ai/expired", lease_expires_at=900.0)
        _seed_workspace(db, workspace_id="ws-live", branch="ai/live", lease_expires_at=1300.0)

    expired = workspace.get_workspace(object(), "ws-expired")
    assert expired["status"] == "expired"
    assert expired["persisted_status"] == "active"
    assert expired["scope"] == {"paths": ["app/**"]}
    assert expired["pr_number"] == 42
    assert [item["workspace_id"] for item in workspace.list_workspaces(object(), "owner/repo", "active")["items"]] == ["ws-live"]
    assert [item["workspace_id"] for item in workspace.list_workspaces(object(), "owner/repo", "expired")["items"]] == ["ws-expired"]
    assert workspace.workspace_overlap(object(), "ws-live", "[]")["items"] == []


def test_expired_workspace_requires_explicit_resume_and_preserves_identity(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "resume-expired.db"))
    monkeypatch.setattr(workspace, "_now", lambda: 1000.0)
    service, repo = _resume_service()
    monkeypatch.setattr(workspace, "_service_repo", lambda *args, **kwargs: repo)
    workspace.init_db()
    with workspace._db() as db:
        _seed_workspace(db, workspace_id="ws-expired", branch="ai/expired", lease_expires_at=900.0)

    with pytest.raises(workspace.MyGithub12Error) as exc:
        workspace.renew_workspace_lease(service, "ws-expired", 1, lease_seconds=300)
    assert exc.value.code == "WORKSPACE_LEASE_REQUIRED"
    assert exc.value.details["requires_resume"] is True

    resumed = workspace.resume_workspace(service, "ws-expired", 1, lease_seconds=300)
    assert resumed["revision"] == 2
    assert resumed["status"] == "active"
    assert resumed["persisted_status"] == "active"
    assert resumed["lease_expires_at"] == 1300.0
    assert resumed["lease_valid"] is True
    assert resumed["base_commit_sha"] == "a" * 40
    assert resumed["head_sha"] == "b" * 40
    assert resumed["tree_sha"] == "c" * 40
    assert resumed["scope"] == {"paths": ["app/**"]}
    assert resumed["pr_number"] == 42
    assert resumed["resume_evidence"]["base_advanced"] is False


def test_expired_workspace_resume_rejects_branch_drift_without_mutation(tmp_path, monkeypatch):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "resume-drift.db"))
    monkeypatch.setattr(workspace, "_now", lambda: 1000.0)
    service, repo = _resume_service(branch_sha="d" * 40)
    monkeypatch.setattr(workspace, "_service_repo", lambda *args, **kwargs: repo)
    workspace.init_db()
    with workspace._db() as db:
        _seed_workspace(db, workspace_id="ws-expired", branch="ai/expired", lease_expires_at=900.0)

    with pytest.raises(workspace.MyGithub12Error) as exc:
        workspace.resume_workspace(service, "ws-expired", 1, lease_seconds=300)
    assert exc.value.code == "WORKSPACE_BRANCH_DRIFTED"
    with workspace._db() as db:
        row = db.execute("SELECT status,revision,lease_expires_at FROM workspaces WHERE workspace_id='ws-expired'").fetchone()
    assert tuple(row) == ("active", 1, 900.0)


def test_new_workspace_converges_legacy_expired_branch_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "branch-owner.db"))
    monkeypatch.setattr(workspace, "_now", lambda: 1000.0)
    monkeypatch.setattr(workspace, "resolve_identity", _resolve_identity)
    service, repo = _resume_service()
    monkeypatch.setattr(workspace, "_service_repo", lambda *args, **kwargs: repo)
    monkeypatch.setattr(workspace, "get_index_status", lambda *args, **kwargs: {"status": "ready"})
    workspace.init_db()
    with workspace._db() as db:
        _seed_workspace(db, workspace_id="ws-old", branch="ai/shared", lease_expires_at=900.0)

    created = workspace.create_workspace(service, "owner/repo", "replacement", branch="ai/shared", create_branch=False, lease_seconds=300)
    assert created["status"] == "active"
    with workspace._db() as db:
        old = db.execute("SELECT status,revision FROM workspaces WHERE workspace_id='ws-old'").fetchone()
    assert tuple(old) == ("expired", 2)


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
