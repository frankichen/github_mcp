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
    monkeypatch.setitem(executor.CONTRACTS["frankichen/sxt"], "mirror", str(mirror))
    monkeypatch.setattr(executor, "DEPLOY_WORKSPACES", str(tmp_path / "workspaces"))
    monkeypatch.setattr(executor, "_git", fake_git)
    monkeypatch.setattr(executor.os.path, "exists", lambda path: False)
    workspace, lines = executor.prepare_workspace({
        "deployment_id": "dep_1",
        "repository": "frankichen/sxt",
        "commit_sha": expected,
    })
    assert workspace.endswith("/workspaces/dep_1")
    assert "origin_main_sha=" + expected in lines
    assert any(call[0][:1] == ("clone",) for call in calls)
    assert not any("sxt-deploy-origin-627" in " ".join(call[0]) for call in calls)


def test_prepare_workspace_reports_stale_mirror(monkeypatch, tmp_path):
    mirror = tmp_path / "frankichen-sxt.git"
    mirror.mkdir()
    monkeypatch.setattr(executor, "DEPLOY_MIRROR", str(mirror))
    monkeypatch.setitem(executor.CONTRACTS["frankichen/sxt"], "mirror", str(mirror))
    monkeypatch.setattr(executor, "_git", lambda *args, **kwargs: executor.AUTHORITATIVE_REPOSITORY_URL if args[:3] == ("remote", "get-url", "origin") else "b" * 40)
    try:
        executor.prepare_workspace({
            "deployment_id": "dep_2",
            "repository": "frankichen/sxt",
            "commit_sha": "a" * 40,
        })
    except executor.DeploymentSourceError as exc:
        assert exc.code == "DEPLOY_MAIN_SHA_MISMATCH"
    else:
        raise AssertionError("stale mirror must stop deployment")


def test_prepare_workspace_fetch_failure_has_stable_code(monkeypatch, tmp_path):
    mirror = tmp_path / "frankichen-sxt.git"
    mirror.mkdir()
    monkeypatch.setattr(executor, "DEPLOY_MIRROR", str(mirror))
    monkeypatch.setitem(executor.CONTRACTS["frankichen/sxt"], "mirror", str(mirror))

    def fail_update(*args, **kwargs):
        if args[:3] == ("remote", "update", "--prune"):
            raise executor.DeploymentSourceError("DEPLOY_SOURCE_FETCH_FAILED", "network failure")
        return executor.AUTHORITATIVE_REPOSITORY_URL

    monkeypatch.setattr(executor, "_git", fail_update)
    try:
        executor.prepare_workspace({
            "deployment_id": "dep_3",
            "repository": "frankichen/sxt",
            "commit_sha": "a" * 40,
        })
    except executor.DeploymentSourceError as exc:
        assert exc.code == "DEPLOY_SOURCE_FETCH_FAILED"
    else:
        raise AssertionError("fetch failure must stop deployment")


def test_devhub_executor_contract_is_fixed_and_provider_specific():
    contract = executor.CONTRACTS["frankichen/devhub"]
    assert contract["environment"] == "devhub-production"
    assert contract["repository_url"] == "https://github.com/frankichen/devhub.git"
    assert contract["script"] == "scripts/deploy_production.sh"
    assert contract["frontend"] is False
    assert contract["mirror"].endswith("/frankichen-devhub.git")


def _unstarted_cancelled_row():
    return {
        "deployment_id": "dep_" + "a" * 32, "repository": "frankichen/sxt",
        "environment": "gongshi-test", "requested_scope": "fullstack",
        "commit_sha": "b" * 40, "current_release_before": "rel_old",
        "current_step": "claimed", "status": "cancel_requested",
        "cancel_requested": True, "log_revision": 1, "claim_generation": 4,
    }


def test_cancel_ack_only_when_deployment_checkout_is_absent(monkeypatch, tmp_path):
    root = tmp_path / "workspaces"
    root.mkdir()
    monkeypatch.setattr(executor, "DEPLOY_WORKSPACES", str(root))
    calls = []
    monkeypatch.setattr(executor, "_callback", lambda path, body: calls.append((path, body)))
    row = _unstarted_cancelled_row()
    assert executor.acknowledge_unstarted_cancellation(row) is True
    assert calls[0][0].endswith("/cancel-ack") and calls[0][1]["expected_claim_generation"] == 4
    (root / row["deployment_id"]).mkdir()
    assert executor.acknowledge_unstarted_cancellation(row) is False
    assert executor.acknowledge_unstarted_cancellation({**row, "current_step": "building"}) is False
    assert len(calls) == 1


def test_cancel_ack_fails_closed_on_unknown_cache_or_id(monkeypatch, tmp_path):
    monkeypatch.setattr(executor, "DEPLOY_WORKSPACES", str(tmp_path / "missing"))
    monkeypatch.setattr(executor, "_callback", lambda path, body: (_ for _ in ()).throw(AssertionError("must not call")))
    assert executor.acknowledge_unstarted_cancellation(_unstarted_cancelled_row()) is False
    (tmp_path / "missing").mkdir()
    assert executor.acknowledge_unstarted_cancellation(
        {**_unstarted_cancelled_row(), "deployment_id": "../bad"}
    ) is False


def test_assigned_cancellation_never_runs_deployment_command(monkeypatch):
    import pytest
    from unittest.mock import Mock
    monkeypatch.setattr(executor, "_key", lambda: "fake")
    monkeypatch.setattr(executor.requests, "get", lambda *args, **kwargs: Mock(
        raise_for_status=lambda: None, json=lambda: {"items": [_unstarted_cancelled_row()]}
    ))
    seen = []
    monkeypatch.setattr(executor, "acknowledge_unstarted_cancellation",
                        lambda row: seen.append(row["deployment_id"]) or True)
    monkeypatch.setattr(executor, "execute", lambda row: pytest.fail("cancelled deployment must not execute"))
    monkeypatch.setattr(executor.time, "sleep", lambda _: (_ for _ in ()).throw(StopIteration()))
    with pytest.raises(StopIteration):
        executor.main()
    assert seen == ["dep_" + "a" * 32]


def test_controller_read_timeout_does_not_crash_executor(monkeypatch):
    import pytest
    monkeypatch.setattr(executor, "_key", lambda: "fake")
    monkeypatch.setattr(executor.requests, "get",
                        lambda *args, **kwargs: (_ for _ in ()).throw(executor.requests.ReadTimeout("offline")))
    monkeypatch.setattr(executor.time, "sleep", lambda _: (_ for _ in ()).throw(StopIteration()))
    with pytest.raises(StopIteration):
        executor.main()
