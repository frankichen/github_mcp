from types import SimpleNamespace

import scripts.deploy_executor as executor


def test_prepare_workspace_refreshes_authoritative_mirror(monkeypatch, tmp_path):
    mirror = tmp_path / "frankichen-sxt.git"
    mirror.mkdir()
    calls = []
    expected = "a" * 40

    def fake_git(*args, cwd=None):
        calls.append((args, cwd))
        if args[:3] == ("remote", "get-url", "origin"):
            return executor.AUTHORITATIVE_REPOSITORY_URL
        if args[:3] == ("remote", "update", "--prune"):
            return ""
        if args[:2] == ("rev-parse", "refs/heads/main"):
            return expected
        if args[:2] == ("cat-file", "-e"):
            return ""
        if args[:1] == ("clone",):
            return ""
        if args[:2] == ("rev-parse", "refs/remotes/origin/main"):
            return expected
        if args[:2] == ("rev-parse", "HEAD"):
            return expected
        if args[:2] == ("branch", "--show-current"):
            return "main"
        if args[:2] == ("status", "--porcelain"):
            return ""
        return ""

    monkeypatch.setattr(executor, "DEPLOY_MIRROR", str(mirror))
    monkeypatch.setattr(executor, "DEPLOY_WORKSPACES", str(tmp_path / "workspaces"))
    monkeypatch.setattr(executor, "_git", fake_git)
    monkeypatch.setattr(executor.os.path, "exists", lambda path: False)
    workspace, lines = executor.prepare_workspace({"deployment_id": "dep_1", "commit_sha": expected})
    assert workspace.endswith("/workspaces/dep_1")
    assert "origin_main_sha=" + expected in lines
    assert any(call[0][:1] == ("clone",) for call in calls)
    assert not any("sxt-deploy-origin-627" in " ".join(call[0]) for call in calls)


def test_prepare_workspace_reports_stale_mirror(monkeypatch, tmp_path):
    mirror = tmp_path / "frankichen-sxt.git"
    mirror.mkdir()
    monkeypatch.setattr(executor, "DEPLOY_MIRROR", str(mirror))
    monkeypatch.setattr(executor, "_git", lambda *args, **kwargs: executor.AUTHORITATIVE_REPOSITORY_URL if args[:3] == ("remote", "get-url", "origin") else "b" * 40)
    try:
        executor.prepare_workspace({"deployment_id": "dep_2", "commit_sha": "a" * 40})
    except executor.DeploymentSourceError as exc:
        assert exc.code == "DEPLOY_MAIN_SHA_MISMATCH"
    else:
        raise AssertionError("stale mirror must stop deployment")


def test_prepare_workspace_fetch_failure_has_stable_code(monkeypatch, tmp_path):
    mirror = tmp_path / "frankichen-sxt.git"
    mirror.mkdir()
    monkeypatch.setattr(executor, "DEPLOY_MIRROR", str(mirror))

    def fail_update(*args, **kwargs):
        if args[:3] == ("remote", "update", "--prune"):
            raise executor.DeploymentSourceError("DEPLOY_SOURCE_FETCH_FAILED", "network failure")
        return executor.AUTHORITATIVE_REPOSITORY_URL

    monkeypatch.setattr(executor, "_git", fail_update)
    try:
        executor.prepare_workspace({"deployment_id": "dep_3", "commit_sha": "a" * 40})
    except executor.DeploymentSourceError as exc:
        assert exc.code == "DEPLOY_SOURCE_FETCH_FAILED"
    else:
        raise AssertionError("fetch failure must stop deployment")
