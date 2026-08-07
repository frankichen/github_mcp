from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app import github_utils


def _pr(number: int, head_owner: str, head_ref: str, *, base_ref: str = "main", state: str = "open"):
    now = datetime(2026, 8, 7, tzinfo=timezone.utc)
    head_user = SimpleNamespace(login=head_owner)
    head_repo = SimpleNamespace(owner=SimpleNamespace(login=head_owner))
    head = SimpleNamespace(
        ref=head_ref,
        sha=f"{number:040x}"[-40:],
        user=head_user,
        repo=head_repo,
        label=f"{head_owner}:{head_ref}",
    )
    base = SimpleNamespace(ref=base_ref)
    return SimpleNamespace(
        number=number,
        title=f"PR {number}",
        state=state,
        draft=False,
        head=head,
        base=base,
        user=SimpleNamespace(login=head_owner),
        created_at=now,
        updated_at=now,
        html_url=f"https://example.invalid/pull/{number}",
    )


def _call_with_pulls(pulls, **kwargs):
    repo = MagicMock()
    repo.get_pulls.return_value = pulls
    gh = MagicMock()
    gh.get_repo.return_value = repo
    with patch.object(github_utils, "_get_gh", return_value=gh):
        result = github_utils.list_github_pull_requests("owner/repo", **kwargs)
    return result, repo


def test_head_branch_is_normalized_and_filters_exact_branch():
    pulls = [_pr(1, "owner", "ai/a"), _pr(2, "owner", "ai/b"), _pr(3, "owner", "ai/c")]
    result, repo = _call_with_pulls(pulls, head_branch="ai/b")

    assert repo.get_pulls.call_args.kwargs["head"] == "owner:ai/b"
    assert [pr["head_branch"] for pr in result["pull_requests"]] == ["ai/b"]
    assert result["warnings"] == ["UPSTREAM_HEAD_FILTER_MISMATCH"]


def test_nonexistent_head_branch_returns_empty_instead_of_all_prs():
    pulls = [_pr(1, "owner", "ai/a"), _pr(2, "owner", "ai/b")]
    result, repo = _call_with_pulls(pulls, head_branch="ai/not-exists")

    assert repo.get_pulls.call_args.kwargs["head"] == "owner:ai/not-exists"
    assert result["pull_requests"] == []
    assert "UPSTREAM_HEAD_FILTER_MISMATCH" in result["warnings"]


def test_explicit_owner_head_filter_does_not_match_same_named_fork_branch():
    pulls = [_pr(1, "owner1", "ai/test"), _pr(2, "owner2", "ai/test")]
    result, repo = _call_with_pulls(pulls, head_branch="owner1:ai/test")

    assert repo.get_pulls.call_args.kwargs["head"] == "owner1:ai/test"
    assert [pr["pull_number"] for pr in result["pull_requests"]] == [1]


def test_state_and_head_branch_filters_are_both_enforced():
    pulls = [_pr(1, "owner", "ai/test", state="open"), _pr(2, "owner", "ai/test", state="closed")]
    result, repo = _call_with_pulls(pulls, state="open", head_branch="ai/test")

    call = repo.get_pulls.call_args.kwargs
    assert call["state"] == "open"
    assert call["head"] == "owner:ai/test"
    assert [pr["pull_number"] for pr in result["pull_requests"]] == [1]
    assert "UPSTREAM_STATE_FILTER_MISMATCH" in result["warnings"]


def test_base_and_head_branch_filters_are_both_enforced():
    pulls = [_pr(1, "owner", "ai/test", base_ref="main"), _pr(2, "owner", "ai/test", base_ref="release")]
    result, repo = _call_with_pulls(pulls, head_branch="ai/test", base_branch="main")

    call = repo.get_pulls.call_args.kwargs
    assert call["head"] == "owner:ai/test"
    assert call["base"] == "main"
    assert [pr["pull_number"] for pr in result["pull_requests"]] == [1]
    assert "UPSTREAM_BASE_FILTER_MISMATCH" in result["warnings"]


def test_pagination_never_returns_mismatched_head_branch():
    pulls = [_pr(1, "owner", "ai/test")] + [_pr(i, "owner", f"ai/other-{i}") for i in range(2, 121)]
    result, repo = _call_with_pulls(pulls, head_branch="ai/test", page=1, limit=10)

    assert repo.get_pulls.call_args.kwargs["head"] == "owner:ai/test"
    assert result["page"] == 1
    assert result["limit"] == 10
    assert all(pr["head_branch"] == "ai/test" for pr in result["pull_requests"])
    assert [pr["pull_number"] for pr in result["pull_requests"]] == [1]
    assert "UPSTREAM_HEAD_FILTER_MISMATCH" in result["warnings"]


def test_normal_upstream_filtered_page_has_no_warning():
    pulls = [_pr(7, "owner", "ai/test")]
    result, repo = _call_with_pulls(pulls, head_branch="ai/test")

    assert repo.get_pulls.call_args.kwargs["head"] == "owner:ai/test"
    assert [pr["pull_number"] for pr in result["pull_requests"]] == [7]
    assert "warnings" not in result
