"""No-session legacy workspace recovery must preserve Writer identity and fail closed."""

import json
import sys
from types import SimpleNamespace
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).parent))

from app import development_sessionless_recovery as legacy
from app import development_session_store as sessions
from app import mygithub12
from test_drift_recovery import (
    FakeService,
    REPO,
    BRANCH,
    BASE,
    BASE_SHA,
    WORKSPACE_ID,
    OLD_HEAD,
    NEW_HEAD,
    OLD_TREE,
    NEW_TREE,
    _workspace_row,
)


def seed(tmp_path, monkeypatch, *, drifted=True):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "legacy-recovery.db"))
    service = FakeService()
    # Older general-purpose fixture does not model compare file statuses; this
    # suite requires complete GitHub evidence even for original pin -> HEAD.
    compare_original = service.repo.compare

    def with_complete_file_evidence(old_sha, new_sha):
        result = compare_original(old_sha, new_sha)
        if (old_sha, new_sha) == (OLD_HEAD, NEW_HEAD):
            for item in result.files:
                item.status = "modified"
        return result

    service.repo.compare = with_complete_file_evidence
    sessions.init_session_db()
    original = _workspace_row(
        status="drifted" if drifted else "active",
        revision=7,
        head=NEW_HEAD,
        tree=NEW_TREE,
        drift_reason="branch_moved_externally" if drifted else None,
    )
    values = list(original)
    values[11] = OLD_HEAD if drifted else NEW_HEAD
    with sessions._LOCK, sessions._db() as db:
        db.execute(
            "INSERT INTO workspaces VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values
        )
    monkeypatch.setattr(
        legacy.mygithub12,
        "workspace_overlap",
        lambda service, workspace_id: {
            "ok": True,
            "workspace_id": workspace_id,
            "items": [],
            "comparison_verified": True,
        },
    )
    monkeypatch.setattr(
        legacy.mygithub12,
        "get_index_status",
        lambda service, repository, commit_sha="", ref="": {
            "status": "ready",
            "commit_sha": commit_sha,
            "tree_sha": NEW_TREE,
        },
    )
    monkeypatch.setattr(
        legacy.mygithub12,
        "request_index_build",
        lambda *a, **k: pytest.fail("existing exact-head index expected"),
    )
    return service


def args(*, drifted=True, **overrides):
    options = dict(
        repository=REPO,
        branch=BRANCH,
        workspace_id=WORKSPACE_ID,
        expected_owner="chatgpt",
        expected_workspace_revision=7,
        expected_previous_head_sha=OLD_HEAD if drifted else NEW_HEAD,
        expected_current_head_sha=NEW_HEAD,
        expected_current_tree_sha=NEW_TREE,
        expected_base_branch=BASE,
        expected_base_sha=BASE_SHA,
        idempotency_key="legacy-recovery-only-once",
        confirm=False,
    )
    options.update(overrides)
    return options


def state():
    with sessions._db() as db:
        w = db.execute(
            "SELECT * FROM workspaces WHERE workspace_id=?", (WORKSPACE_ID,)
        ).fetchone()
        s = db.execute(
            "SELECT * FROM development_sessions WHERE workspace_id=?", (WORKSPACE_ID,)
        ).fetchall()
        return dict(w), [dict(x) for x in s]


def expect_error(service, code, **kwargs):
    with pytest.raises(mygithub12.MyGithub12Error) as exc:
        legacy.recover_sessionless_workspace(service, **args(**kwargs))
    assert exc.value.code == code


def test_preview_is_read_only_and_identifies_original_writer(tmp_path, monkeypatch):
    service = seed(tmp_path, monkeypatch)
    before, s0 = state()
    result = legacy.recover_sessionless_workspace(service, **args())
    assert result["ok"] and not result["confirmed"]
    assert result["no_existing_session"]
    assert result["owner"] == "chatgpt"
    assert result["ancestry"]["verified"]
    assert result["required_scope_expansion_paths"] == []
    after, s1 = state()
    assert before == after and s0 == s1 == []


def test_confirm_creates_first_session_on_same_original_workspace(
    tmp_path, monkeypatch
):
    service = seed(tmp_path, monkeypatch)
    result = legacy.recover_sessionless_workspace(service, **args(confirm=True))
    w, session_rows = state()
    assert result["confirmed"] and result["created_first_session"]
    assert result["no_replacement_workspace"] and not result["git_ref_changed"]
    assert w["status"] == "active" and w["drift_reason"] is None
    assert w["revision"] == 8 and w["head_sha"] == NEW_HEAD
    assert len(session_rows) == 1
    assert session_rows[0]["owner"] == "chatgpt"
    assert session_rows[0]["workspace_revision"] == 8
    assert session_rows[0]["head_commit_sha"] == NEW_HEAD
    assert result["writer_ready"] is True
    with sessions._db() as db:
        assert db.execute("SELECT count(*) FROM workspaces").fetchone()[0] == 1
        events = db.execute(
            "SELECT event_type FROM development_session_events"
        ).fetchall()
        assert [x[0] for x in events] == ["legacy_workspace_session_reconciled"]


def test_replay_is_idempotent_no_second_session(tmp_path, monkeypatch):
    service = seed(tmp_path, monkeypatch)
    kwargs = args(confirm=True)
    first = legacy.recover_sessionless_workspace(service, **kwargs)
    second = legacy.recover_sessionless_workspace(service, **kwargs)
    assert second["replayed"] and not second["git_ref_changed"]
    assert (
        first["development_session"]["session_id"]
        == second["development_session"]["session_id"]
    )
    assert len(state()[1]) == 1


def test_active_same_head_first_session(tmp_path, monkeypatch):
    service = seed(tmp_path, monkeypatch, drifted=False)
    out = legacy.recover_sessionless_workspace(
        service, **args(drifted=False, confirm=True)
    )
    assert out["confirmed"] and out["writer_ready"]
    w, s = state()
    assert w["head_sha"] == NEW_HEAD and w["revision"] == 8
    assert len(s) == 1 and s[0]["owner"] == "chatgpt"


def test_wrong_owner_fails(tmp_path, monkeypatch):
    service = seed(tmp_path, monkeypatch)
    expect_error(service, "RECOVERY_OWNER_MISMATCH", expected_owner="other")
    assert not state()[1]


def test_stale_cas_fails(tmp_path, monkeypatch):
    service = seed(tmp_path, monkeypatch)
    expect_error(service, "WORKSPACE_REVISION_MISMATCH", expected_workspace_revision=6)
    assert state()[0]["revision"] == 7


def test_previous_head_must_match_durable_pin(tmp_path, monkeypatch):
    service = seed(tmp_path, monkeypatch)
    expect_error(
        service, "RECOVERY_ANCESTRY_MISMATCH", expected_previous_head_sha="f" * 40
    )


def test_forward_only_required(tmp_path, monkeypatch):
    service = seed(tmp_path, monkeypatch)
    service.repo.merge_base = BASE_SHA
    expect_error(service, "RECOVERY_ANCESTRY_MISMATCH")
    assert not state()[1]


def test_scope_exact_review_before_commit(tmp_path, monkeypatch):
    service = seed(tmp_path, monkeypatch)
    service.repo.changed_paths = ["outside/control.py"]
    preview = legacy.recover_sessionless_workspace(service, **args())
    assert preview["required_scope_expansion_paths"] == ["outside/control.py"]
    expect_error(service, "RECOVERY_SCOPE_VIOLATION", confirm=True)
    actual = legacy.recover_sessionless_workspace(
        service,
        **args(
            confirm=True, reviewed_scope_expansion_paths_json='["outside/control.py"]'
        ),
    )
    assert actual["confirmed"]
    assert "outside/control.py" in json.loads(state()[0]["scope_json"])["paths"]


def test_session_already_exists_fails_closed(tmp_path, monkeypatch):
    service = seed(tmp_path, monkeypatch)
    ws = mygithub12.get_workspace(service, WORKSPACE_ID)
    sessions.create_session(ws, owner="chatgpt", idempotency_key="existing")
    expect_error(service, "DEVELOPMENT_SESSION_WORKSPACE_MISMATCH")
    assert len(state()[1]) == 1
    assert state()[0]["status"] == "drifted"


def test_transaction_rolls_back_if_audit_fails(tmp_path, monkeypatch):
    service = seed(tmp_path, monkeypatch)

    def refuse_audit(*args, **kwargs):
        raise RuntimeError("synthetic audit failure")

    monkeypatch.setattr(sessions, "_append_event", refuse_audit)
    with pytest.raises(RuntimeError, match="synthetic"):
        legacy.recover_sessionless_workspace(service, **args(confirm=True))
    w, ss = state()
    assert w["status"] == "drifted" and w["revision"] == 7
    assert ss == []


def test_active_workspace_must_have_live_lease(tmp_path, monkeypatch):
    service = seed(tmp_path, monkeypatch, drifted=False)
    with sessions._LOCK, sessions._db() as db:
        db.execute(
            "UPDATE workspaces SET lease_expires_at=0 WHERE workspace_id=?",
            (WORKSPACE_ID,),
        )
    expect_error(service, "RECOVERY_DRIFT_REASON_UNSUPPORTED", drifted=False)


def test_replayed_key_different_request_fails(tmp_path, monkeypatch):
    service = seed(tmp_path, monkeypatch)
    legacy.recover_sessionless_workspace(service, **args(confirm=True))
    expect_error(
        service,
        "IDEMPOTENCY_CONFLICT",
        reviewed_scope_expansion_paths_json='["other/file.py"]',
    )


def test_high_overlap_blocks_first_session(tmp_path, monkeypatch):
    service = seed(tmp_path, monkeypatch)
    monkeypatch.setattr(
        legacy.mygithub12,
        "workspace_overlap",
        lambda *a: {
            "ok": True,
            "comparison_verified": True,
            "items": [{"workspace_id": "other", "level": "high", "evidence": []}],
        },
    )
    expect_error(service, "RECOVERY_WORKSPACE_OVERLAP")
    assert state()[0]["status"] == "drifted" and not state()[1]


def test_changed_git_identity_within_transaction_rolls_back(tmp_path, monkeypatch):
    service = seed(tmp_path, monkeypatch)
    original = legacy.drift._fresh_github_identity
    calls = {"count": 0}

    def identity_changes(*a, **kw):
        calls["count"] += 1
        if calls["count"] > 1:
            raise mygithub12.MyGithub12Error(
                "RECOVERY_HEAD_MISMATCH", "changed after preflight"
            )
        return original(*a, **kw)

    monkeypatch.setattr(legacy.drift, "_fresh_github_identity", identity_changes)
    with pytest.raises(mygithub12.MyGithub12Error, match="changed after preflight"):
        legacy.recover_sessionless_workspace(service, **args(confirm=True))
    assert state()[0]["revision"] == 7 and not state()[1]


def test_base_move_fails_closed(tmp_path, monkeypatch):
    service = seed(tmp_path, monkeypatch)
    service.client.heads[BASE] = "f" * 40
    expect_error(service, "RECOVERY_SCOPE_INCOMPLETE")
    assert state()[0]["status"] == "drifted" and not state()[1]


def test_replay_fails_if_owner_changes(tmp_path, monkeypatch):
    service = seed(tmp_path, monkeypatch)
    legacy.recover_sessionless_workspace(service, **args(confirm=True))
    with sessions._LOCK, sessions._db() as db:
        db.execute(
            "UPDATE workspaces SET owner='other' WHERE workspace_id=?", (WORKSPACE_ID,)
        )
    expect_error(service, "IDEMPOTENCY_CONFLICT", confirm=True)


LIVE_BASE_SHA = "e" * 40


def advanced_main(service, *, main_paths=None, task_paths=None, divergent=False):
    """Replace only the fake GitHub compare surface, keeping the old-head proof."""
    main_paths = main_paths if main_paths is not None else ["docs/main_only.md"]
    task_paths = task_paths if task_paths is not None else ["allowed/feature.py"]
    service.client.heads[BASE] = LIVE_BASE_SHA
    service.repo.trees[LIVE_BASE_SHA] = "5" * 40
    previous_compare = service.repo.compare

    def compare(left, right):
        if (left, right) == (BASE_SHA, LIVE_BASE_SHA):
            return SimpleNamespace(
                merge_base_commit=SimpleNamespace(
                    sha=("f" * 40) if divergent else BASE_SHA
                ),
                ahead_by=1, behind_by=0,
                files=[SimpleNamespace(filename=p, status="modified", previous_filename=None)
                       for p in main_paths],
            )
        if (left, right) == (BASE_SHA, NEW_HEAD):
            return SimpleNamespace(
                merge_base_commit=SimpleNamespace(sha=BASE_SHA),
                ahead_by=2, behind_by=0,
                files=[SimpleNamespace(filename=p, status="modified", previous_filename=None)
                       for p in task_paths],
            )
        return previous_compare(left, right)

    service.repo.compare = compare
    return service


def test_advanced_main_preview_is_disjoint_read_only_and_explicit(tmp_path, monkeypatch):
    service = advanced_main(seed(tmp_path, monkeypatch))
    before, sessions_before = state()
    preview = legacy.recover_sessionless_workspace(service, **args())
    assert preview["ok"] and not preview["confirmed"]
    assert preview["base_advance"]["advanced"]
    assert preview["required_expected_live_base_sha"] == LIVE_BASE_SHA
    assert preview["base_advance"]["overlapping_paths"] == []
    assert state() == (before, sessions_before)


def test_advanced_main_confirm_requires_reviewed_live_base(tmp_path, monkeypatch):
    service = advanced_main(seed(tmp_path, monkeypatch))
    expect_error(service, "RECOVERY_BASE_ADVANCE_REVIEW_REQUIRED", confirm=True)
    assert state()[0]["status"] == "drifted" and not state()[1]


def test_advanced_main_preserves_pinned_base_and_original_workspace(tmp_path, monkeypatch):
    service = advanced_main(seed(tmp_path, monkeypatch))
    request = args(confirm=True, expected_live_base_sha=LIVE_BASE_SHA)
    result = legacy.recover_sessionless_workspace(service, **request)
    assert result["confirmed"] and result["writer_ready"]
    assert result["base_advance"]["advanced"]
    workspace, records = state()
    assert workspace["base_commit_sha"] == BASE_SHA
    assert workspace["revision"] == 8 and workspace["status"] == "active"
    assert len(records) == 1 and records[0]["base_commit_sha"] == BASE_SHA
    assert records[0]["head_commit_sha"] == NEW_HEAD
    assert legacy.recover_sessionless_workspace(service, **request)["replayed"]
    assert len(state()[1]) == 1


def test_advanced_main_overlapping_files_fails_closed(tmp_path, monkeypatch):
    service = advanced_main(
        seed(tmp_path, monkeypatch),
        main_paths=["allowed/feature.py"],
        task_paths=["allowed/feature.py"],
    )
    expect_error(service, "RECOVERY_BASE_OVERLAP",
                 confirm=True, expected_live_base_sha=LIVE_BASE_SHA)
    assert state()[0]["status"] == "drifted" and not state()[1]


def test_advanced_main_rename_previous_file_overlap_fails_closed(tmp_path, monkeypatch):
    service = advanced_main(seed(tmp_path, monkeypatch))
    previous_compare = service.repo.compare

    def rename_compare(left, right):
        result = previous_compare(left, right)
        if (left, right) == (BASE_SHA, LIVE_BASE_SHA):
            result.files[0].status = "renamed"
            result.files[0].previous_filename = "allowed/feature.py"
        return result

    service.repo.compare = rename_compare
    expect_error(service, "RECOVERY_BASE_OVERLAP",
                 confirm=True, expected_live_base_sha=LIVE_BASE_SHA)
    assert not state()[1]


def test_advanced_main_rewritten_history_fails_closed(tmp_path, monkeypatch):
    service = advanced_main(seed(tmp_path, monkeypatch), divergent=True)
    expect_error(service, "RECOVERY_ANCESTRY_MISMATCH",
                 confirm=True, expected_live_base_sha=LIVE_BASE_SHA)
    assert state()[0]["revision"] == 7 and not state()[1]


def test_advanced_main_wrong_reviewed_live_sha_fails_closed(tmp_path, monkeypatch):
    service = advanced_main(seed(tmp_path, monkeypatch))
    expect_error(service, "RECOVERY_BASE_CHANGED",
                 confirm=True, expected_live_base_sha="9" * 40)
    assert not state()[1]


def test_advanced_main_invalid_reviewed_live_sha_rejected(tmp_path, monkeypatch):
    service = advanced_main(seed(tmp_path, monkeypatch))
    expect_error(service, "SEARCH_QUERY_INVALID",
                 confirm=True, expected_live_base_sha="not-a-commit")
    assert not state()[1]


def test_advanced_main_moves_between_preflight_and_transaction_rollback(
    tmp_path, monkeypatch
):
    service = advanced_main(seed(tmp_path, monkeypatch))
    original = legacy._fresh_pinned_base_proof
    calls = {"total": 0}

    def changed_base(*a, **kw):
        calls["total"] += 1
        if calls["total"] == 2:
            service.client.heads[BASE] = "f" * 40
        return original(*a, **kw)

    monkeypatch.setattr(legacy, "_fresh_pinned_base_proof", changed_base)
    with pytest.raises(mygithub12.MyGithub12Error) as exc:
        legacy.recover_sessionless_workspace(
            service, **args(confirm=True, expected_live_base_sha=LIVE_BASE_SHA)
        )
    assert exc.value.code == "RECOVERY_BASE_CHANGED"
    assert state()[0]["revision"] == 7 and not state()[1]


def test_original_pin_missing_files_compare_evidence_fails_closed(tmp_path, monkeypatch):
    service = seed(tmp_path, monkeypatch)
    original = service.repo.compare

    def bad_original_compare(base_sha, head_sha):
        if (base_sha, head_sha) == (OLD_HEAD, NEW_HEAD):
            return SimpleNamespace(
                merge_base_commit=SimpleNamespace(sha=OLD_HEAD),
                ahead_by=1, behind_by=0,
            )
        return original(base_sha, head_sha)

    service.repo.compare = bad_original_compare
    expect_error(service, "RECOVERY_SCOPE_INCOMPLETE", confirm=True)
    assert state()[0]["status"] == "drifted" and not state()[1]


def test_original_pin_rename_previous_path_requires_scope_review(tmp_path, monkeypatch):
    service = seed(tmp_path, monkeypatch)
    original = service.repo.compare

    def renamed_original_compare(base_sha, head_sha):
        result = original(base_sha, head_sha)
        if (base_sha, head_sha) == (OLD_HEAD, NEW_HEAD):
            result.files[0].status = "renamed"
            result.files[0].previous_filename = "external/old_file.py"
        return result

    service.repo.compare = renamed_original_compare
    expect_error(service, "RECOVERY_SCOPE_VIOLATION", confirm=True)
    assert state()[0]["status"] == "drifted" and not state()[1]


def test_advanced_main_missing_files_compare_field_fails_closed(tmp_path, monkeypatch):
    service = advanced_main(seed(tmp_path, monkeypatch))
    previous = service.repo.compare

    def missing_files(base, head):
        if (base, head) == (BASE_SHA, LIVE_BASE_SHA):
            return SimpleNamespace(
                merge_base_commit=SimpleNamespace(sha=BASE_SHA),
                ahead_by=1, behind_by=0,
            )
        return previous(base, head)

    service.repo.compare = missing_files
    expect_error(service, "RECOVERY_SCOPE_INCOMPLETE", confirm=True,
                 expected_live_base_sha=LIVE_BASE_SHA)
    assert state()[0]["status"] == "drifted" and not state()[1]


def test_advanced_main_missing_behind_count_fails_closed(tmp_path, monkeypatch):
    service = advanced_main(seed(tmp_path, monkeypatch))
    previous = service.repo.compare

    def missing_count(base, head):
        result = previous(base, head)
        if (base, head) == (BASE_SHA, LIVE_BASE_SHA):
            del result.behind_by
        return result

    service.repo.compare = missing_count
    expect_error(service, "RECOVERY_ANCESTRY_MISMATCH", confirm=True,
                 expected_live_base_sha=LIVE_BASE_SHA)
    assert not state()[1]


def test_advanced_main_rename_without_previous_path_fails_closed(
    tmp_path, monkeypatch
):
    service = advanced_main(seed(tmp_path, monkeypatch))
    previous = service.repo.compare

    def missing_rename_source(base, head):
        result = previous(base, head)
        if (base, head) == (BASE_SHA, LIVE_BASE_SHA):
            result.files[0].status = "renamed"
            result.files[0].previous_filename = None
        return result

    service.repo.compare = missing_rename_source
    expect_error(service, "RECOVERY_SCOPE_INCOMPLETE", confirm=True,
                 expected_live_base_sha=LIVE_BASE_SHA)
    assert not state()[1]


def test_advanced_main_copy_only_changes_destination(tmp_path, monkeypatch):
    service = advanced_main(seed(tmp_path, monkeypatch))
    previous = service.repo.compare

    def copied_target(base, head):
        result = previous(base, head)
        if (base, head) == (BASE_SHA, LIVE_BASE_SHA):
            result.files[0].status = "copied"
            result.files[0].previous_filename = "allowed/feature.py"
        return result

    service.repo.compare = copied_target
    result = legacy.recover_sessionless_workspace(
        service, **args(confirm=True, expected_live_base_sha=LIVE_BASE_SHA)
    )
    assert result["confirmed"]
    assert result["base_advance"]["base_changed_paths"] == ["docs/main_only.md"]
    assert len(state()[1]) == 1


def test_advanced_main_copy_destination_overlap_fails_closed(
    tmp_path, monkeypatch
):
    service = advanced_main(
        seed(tmp_path, monkeypatch),
        main_paths=["allowed/feature.py"],
        task_paths=["allowed/feature.py"],
    )
    previous = service.repo.compare

    def copied_into_task(base, head):
        result = previous(base, head)
        if (base, head) == (BASE_SHA, LIVE_BASE_SHA):
            result.files[0].status = "copied"
            result.files[0].previous_filename = "docs/source.py"
        return result

    service.repo.compare = copied_into_task
    expect_error(service, "RECOVERY_BASE_OVERLAP", confirm=True,
                 expected_live_base_sha=LIVE_BASE_SHA)
    assert not state()[1]


def test_advanced_main_unknown_file_status_fails_closed(tmp_path, monkeypatch):
    service = advanced_main(seed(tmp_path, monkeypatch))
    previous = service.repo.compare

    def unknown_status(base, head):
        result = previous(base, head)
        if (base, head) == (BASE_SHA, LIVE_BASE_SHA):
            result.files[0].status = "unknown"
        return result

    service.repo.compare = unknown_status
    expect_error(service, "RECOVERY_SCOPE_INCOMPLETE", confirm=True,
                 expected_live_base_sha=LIVE_BASE_SHA)
    assert not state()[1]
