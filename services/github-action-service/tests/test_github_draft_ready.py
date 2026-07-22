from types import SimpleNamespace
from unittest.mock import MagicMock
import inspect

import pytest
import requests
from github.PullRequest import PullRequest

from app import github_utils


HEAD = "a" * 40


def _pr(*, draft, state="open", merged=False, head=HEAD, node_id="PR_node"):
    return SimpleNamespace(
        state=state,
        merged=merged,
        draft=draft,
        node_id=node_id,
        raw_data={"node_id": node_id} if node_id else {},
        head=SimpleNamespace(sha=head),
    )


def _repo(before, after=None):
    repo = MagicMock()
    repo.get_pull.side_effect = [before] if after is None else [before, after]
    return repo


def _graphql_response(payload, status=200, request_id="req-draft"):
    response = MagicMock()
    response.status_code = status
    response.headers = {"X-GitHub-Request-Id": request_id, "Content-Type": "application/json"}
    response.json.return_value = payload
    return response


def _patch_gh(monkeypatch, repo):
    monkeypatch.setattr(github_utils, "_get_gh", lambda: SimpleNamespace(get_repo=lambda _: repo))


def test_installed_pygithub_does_not_provide_fake_draft_methods():
    # The production image is PyGithub 2.5.0, where both methods are absent.
    # Local environments may have a newer SDK, so the stronger contract is
    # that our adapter never depends on either SDK convenience method.
    if not hasattr(PullRequest, "mark_ready_for_review"):
        assert not hasattr(PullRequest, "mark_ready_for_review")
    if not hasattr(PullRequest, "convert_to_draft"):
        assert not hasattr(PullRequest, "convert_to_draft")
    assert "mark_ready_for_review" not in inspect.getsource(github_utils.mark_github_pull_request_ready)
    assert "convert_to_draft" not in inspect.getsource(github_utils.convert_github_pull_request_to_draft)


def test_mark_ready_uses_graphql_and_confirms_rest(monkeypatch):
    before = _pr(draft=True)
    after = _pr(draft=False)
    repo = _repo(before, after)
    _patch_gh(monkeypatch, repo)
    post = MagicMock(return_value=_graphql_response({"data": {"markPullRequestReadyForReview": {
        "pullRequest": {"isDraft": False, "headRefOid": HEAD, "id": "PR_node", "number": 1, "state": "OPEN"}
    }}}))
    monkeypatch.setattr(github_utils.requests, "post", post)

    result = github_utils.mark_github_pull_request_ready("owner/repo", 1, HEAD)

    assert result["ok"] is True
    assert result["draft"] is False
    assert result["previous_draft"] is True
    assert result["head_sha"] == HEAD
    assert result["changed"] is True
    query = post.call_args.kwargs["json"]
    assert "markPullRequestReadyForReview" in query["query"]
    assert query["variables"] == {"pullRequestId": "PR_node"}


def test_convert_to_draft_uses_graphql_and_confirms_rest(monkeypatch):
    before = _pr(draft=False)
    after = _pr(draft=True)
    repo = _repo(before, after)
    _patch_gh(monkeypatch, repo)
    monkeypatch.setattr(github_utils.requests, "post", MagicMock(return_value=_graphql_response({"data": {
        "convertPullRequestToDraft": {"pullRequest": {"isDraft": True, "headRefOid": HEAD}}
    }})))

    result = github_utils.convert_github_pull_request_to_draft("owner/repo", 1, HEAD)

    assert result["ok"] is True
    assert result["draft"] is True
    assert result["previous_draft"] is False
    assert result["changed"] is True


@pytest.mark.parametrize(("ready", "draft", "code"), [
    (True, False, "ALREADY_READY"),
    (False, True, "ALREADY_DRAFT"),
])
def test_draft_operations_are_idempotent_without_graphql(monkeypatch, ready, draft, code):
    repo = _repo(_pr(draft=draft))
    _patch_gh(monkeypatch, repo)
    post = MagicMock()
    monkeypatch.setattr(github_utils.requests, "post", post)

    result = (github_utils.mark_github_pull_request_ready if ready else github_utils.convert_github_pull_request_to_draft)(
        "owner/repo", 1, HEAD
    )

    assert result["ok"] is True
    assert result["changed"] is False
    assert result["message"] == ("Pull request is already ready for review" if ready else "Pull request is already draft")
    post.assert_not_called()


@pytest.mark.parametrize("state,merged,code", [("closed", False, "PR_NOT_OPEN"), ("closed", True, "ALREADY_MERGED")])
def test_terminal_prs_are_rejected_before_graphql(monkeypatch, state, merged, code):
    repo = _repo(_pr(draft=True, state=state, merged=merged))
    _patch_gh(monkeypatch, repo)
    post = MagicMock()
    monkeypatch.setattr(github_utils.requests, "post", post)

    result = github_utils.mark_github_pull_request_ready("owner/repo", 1, HEAD)

    assert result["error"]["code"] == code
    post.assert_not_called()


def test_head_required_and_changed_and_node_id_missing(monkeypatch):
    result = github_utils.mark_github_pull_request_ready("owner/repo", 1, "")
    assert result["error"]["code"] == "EXPECTED_HEAD_SHA_REQUIRED"

    repo = _repo(_pr(draft=True, head="b" * 40))
    _patch_gh(monkeypatch, repo)
    result = github_utils.mark_github_pull_request_ready("owner/repo", 1, HEAD)
    assert result["error"]["code"] == "HEAD_CHANGED"

    repo = _repo(_pr(draft=True, node_id=None))
    _patch_gh(monkeypatch, repo)
    result = github_utils.mark_github_pull_request_ready("owner/repo", 1, HEAD)
    assert result["error"]["code"] == "PR_NODE_ID_MISSING"


def test_graphql_errors_empty_data_and_state_mismatch(monkeypatch):
    repo = _repo(_pr(draft=True))
    _patch_gh(monkeypatch, repo)
    monkeypatch.setattr(github_utils.requests, "post", MagicMock(return_value=_graphql_response({"errors": [{"type": "FORBIDDEN", "message": "denied"}]})))
    assert github_utils.mark_github_pull_request_ready("owner/repo", 1, HEAD)["error"]["code"] == "GITHUB_GRAPHQL_FAILED"

    repo = _repo(_pr(draft=True))
    _patch_gh(monkeypatch, repo)
    monkeypatch.setattr(github_utils.requests, "post", MagicMock(return_value=_graphql_response({"data": {}})))
    assert github_utils.mark_github_pull_request_ready("owner/repo", 1, HEAD)["error"]["code"] == "GITHUB_GRAPHQL_INVALID_RESPONSE"

    repo = _repo(_pr(draft=True))
    _patch_gh(monkeypatch, repo)
    monkeypatch.setattr(github_utils.requests, "post", MagicMock(return_value=_graphql_response({"data": {
        "markPullRequestReadyForReview": {"pullRequest": {"isDraft": True, "headRefOid": HEAD}}
    }})))
    assert github_utils.mark_github_pull_request_ready("owner/repo", 1, HEAD)["error"]["code"] == "READY_STATE_NOT_CONFIRMED"


@pytest.mark.parametrize(("status", "code"), [
    (401, "GITHUB_AUTH_FAILED"), (403, "GITHUB_PERMISSION_DENIED"), (422, "GITHUB_GRAPHQL_FAILED"),
    (429, "GITHUB_RATE_LIMITED"), (500, "GITHUB_GRAPHQL_FAILED"),
])
def test_graphql_http_errors_are_stable(monkeypatch, status, code):
    repo = _repo(_pr(draft=True))
    _patch_gh(monkeypatch, repo)
    monkeypatch.setattr(github_utils.requests, "post", MagicMock(return_value=_graphql_response({}, status=status)))
    result = github_utils.mark_github_pull_request_ready("owner/repo", 1, HEAD)
    assert result["error"]["code"] == code


def test_graphql_timeout_is_stable(monkeypatch):
    repo = _repo(_pr(draft=True))
    _patch_gh(monkeypatch, repo)
    monkeypatch.setattr(github_utils.requests, "post", MagicMock(side_effect=requests.Timeout()))
    result = github_utils.mark_github_pull_request_ready("owner/repo", 1, HEAD)
    assert result["error"]["code"] == "GITHUB_GRAPHQL_FAILED"
