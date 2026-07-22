from types import SimpleNamespace

from app import github_utils


def test_check_classification_non_required_failure_and_infrastructure_signals():
    result = github_utils._classify_check_run({"name": "Console", "status": "completed", "conclusion": "failure", "duration_seconds": 3, "steps": [], "runner_id": 0}, False)
    assert result["classification"] == "GITHUB_ACTIONS_QUOTA_OR_INFRA_FAILURE"
    assert result["is_required"] is False
    assert result["blocking"] is False

    required_infra = github_utils._classify_check_run(
        {"name": "Console", "status": "completed", "conclusion": "failure", "duration_seconds": 3, "steps": []},
        True,
        "branch_protection",
    )
    assert required_infra["classification"] == "GITHUB_ACTIONS_QUOTA_OR_INFRA_FAILURE"
    assert required_infra["is_required"] is True
    assert required_infra["blocking"] is False

    result = github_utils._classify_check_run({"name": "unit", "status": "completed", "conclusion": "failure", "steps": [{"name": "npm test"}], "duration_seconds": 42}, False)
    assert result["classification"] == "GITHUB_ACTIONS_CODE_FAILURE"
    assert result["blocking"] is False


def test_required_check_failure_blocks_and_missing_blocks():
    failed = github_utils._classify_check_run({"name": "ci", "status": "completed", "conclusion": "failure"}, True, "branch_protection")
    pending = github_utils._classify_check_run({"name": "ci", "status": "in_progress", "conclusion": None}, True, "ruleset")
    assert failed["classification"] == "REQUIRED_CHECK_FAILED" and failed["blocking"] is True
    assert pending["classification"] == "REQUIRED_CHECK_PENDING" and pending["blocking"] is True


def test_required_sources_are_only_protection_ruleset_and_explicit_policy():
    class Requester:
        def requestJsonAndCheck(self, method, path):
            if path.endswith("/protection"):
                return ({"required_status_checks": {"contexts": ["branch-ci"], "checks": [{"context": "branch-check"}]}}, {})
            if path.endswith("/rulesets?includes_parents=true"):
                return ([{"id": 7, "enforcement": "active", "conditions": {"ref_name": {"include": ["refs/heads/main"]}}}], {})
            return ({"rules": [{"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "ruleset-ci"}]}}, {"type": "required_workflows", "parameters": {"workflows": [{"path": ".github/workflows/release.yml"}]}}]}, {})

    repo = SimpleNamespace(full_name="frankichen/sxt", _requester=Requester())
    result = github_utils._required_check_sources(repo, "main", {"required_workflows": ["policy-ci"]})
    assert set(result["contexts"]) == {"branch-ci", "ruleset-ci"}
    assert set(result["checks"]) == {"branch-check", ".github/workflows/release.yml", "policy-ci"}
    assert set(result["sources"]) == {"branch_protection", "ruleset", "repository_policy"}


def test_short_circuit_merged_and_closed(monkeypatch):
    def pr(state="open", merged=False):
        return {"ok": True, "state": state, "merged": merged, "draft": False, "base_branch": "main", "base_sha": "b" * 40,
                "head_branch": "feature", "head_sha": "a" * 40, "mergeable": None, "mergeable_state": "unknown",
                "review_decision": "REVIEW_REQUIRED", "reviews": [], "requested_reviewers": [], "requested_teams": []}

    monkeypatch.setattr(github_utils, "get_github_pull_request", lambda *args: pr(merged=True))
    merged = github_utils.get_github_pull_request_merge_readiness("frankichen/sxt", 565, "a" * 40, "job")
    assert merged["reasons"] == ["ALREADY_MERGED"]
    assert merged["blocking"] == ["ALREADY_MERGED"]

    monkeypatch.setattr(github_utils, "get_github_pull_request", lambda *args: pr(state="closed"))
    closed = github_utils.get_github_pull_request_merge_readiness("frankichen/sxt", 1, "a" * 40, "job")
    assert closed["reasons"] == ["PR_NOT_OPEN"]


def test_all_merge_tools_short_circuit_terminal_pr_before_input_gates(monkeypatch):
    def merged_pr(*_args):
        return {"ok": True, "state": "closed", "merged": True, "draft": False, "base_branch": "main", "base_sha": "b" * 40,
                "head_branch": "feature", "head_sha": "a" * 40, "mergeable": False, "mergeable_state": "clean",
                "review_decision": "REVIEW_REQUIRED", "reviews": [], "requested_reviewers": [], "requested_teams": []}

    monkeypatch.setattr(github_utils, "get_github_pull_request", merged_pr)
    readiness = github_utils.get_github_pull_request_merge_readiness("frankichen/sxt", 565)
    plan = github_utils.plan_github_pull_request_merge("frankichen/sxt", 565, "invalid")
    merge = github_utils.merge_github_pull_request("frankichen/sxt", 565, "invalid", confirm=False)
    assert readiness["reasons"] == ["ALREADY_MERGED"]
    assert plan["blocking_reasons"] == ["ALREADY_MERGED"]
    assert merge["reasons"] == ["ALREADY_MERGED"]
    assert "PR_NOT_OPEN" not in merge["reasons"]
    assert "CONFIRM_REQUIRED" not in merge.get("error", {})


def test_private_ci_exact_sha_profile_and_superseded(monkeypatch):
    base = {"ok": True, "state": "open", "merged": False, "draft": False, "base_branch": "main", "base_sha": "b" * 40,
            "head_branch": "feature", "head_sha": "a" * 40, "mergeable": True, "mergeable_state": "clean",
            "review_decision": "APPROVED", "reviews": [], "requested_reviewers": [], "requested_teams": []}
    monkeypatch.setattr(github_utils, "get_github_pull_request", lambda *args: base)
    monkeypatch.setattr(github_utils, "_get_gh", lambda: SimpleNamespace(get_repo=lambda *_args: SimpleNamespace(allow_squash_merge=True)))
    monkeypatch.setattr(github_utils, "_review_policy", lambda *args: {"required_approvals": 0, "current_approvals": 0, "source": "none", "changes_requested": False})
    monkeypatch.setattr(github_utils, "get_github_pull_request_checks", lambda *args: {"ok": True, "checks": [], "statuses": [], "overall_conclusion": "neutral", "required_check_sources": {"errors": []}})
    monkeypatch.setattr(github_utils, "get_github_repository", lambda *args: {"allow_squash_merge": True})
    for job, reason in [
        ({"repository": "frankichen/sxt", "branch": "feature", "commit_sha": "x" * 40, "profile": "repo-auto-check", "status": "passed", "exit_code": 0}, "PRIVATE_CI_SHA_MISMATCH"),
        ({"repository": "frankichen/sxt", "branch": "feature", "commit_sha": "a" * 40, "profile": "wrong", "status": "passed", "exit_code": 0}, "PRIVATE_CI_PROFILE_MISMATCH"),
        ({"repository": "frankichen/sxt", "branch": "feature", "commit_sha": "a" * 40, "profile": "repo-auto-check", "status": "passed", "exit_code": 0, "superseded_by_job_id": "new"}, "PRIVATE_CI_SUPERSEDED"),
    ]:
        monkeypatch.setattr(github_utils, "_private_ci_job", lambda *_args, job=job: job)
        result = github_utils._readiness("frankichen/sxt", 1, "a" * 40, "job")
        assert reason in result["blocking"]

    passed_job = {"repository": "frankichen/sxt", "branch": "feature", "commit_sha": "a" * 40,
                  "profile": "repo-auto-check", "status": "passed", "exit_code": 0}
    monkeypatch.setattr(github_utils, "_private_ci_job", lambda *_args: passed_job)
    result = github_utils._readiness("frankichen/sxt", 1, "a" * 40, "job")
    assert result["private_ci"]["valid"] is True
    assert result["ready"] is True


def test_infrastructure_signals_are_classified_without_project_failure():
    assert github_utils._is_actions_infrastructure_failure({"status": "completed", "conclusion": "failure", "runner_id": 0})
    assert github_utils._is_actions_infrastructure_failure({"status": "completed", "conclusion": "failure", "logs_http_status": 404})
    assert github_utils._is_actions_infrastructure_failure({"status": "completed", "conclusion": "failure", "duration_seconds": 3, "steps": []})
    assert not github_utils._is_actions_infrastructure_failure({"status": "completed", "conclusion": "failure", "duration_seconds": 42, "steps": [{"name": "go test"}]})


def test_build_merge_kwargs_uses_current_pygithub_names_and_omits_empty_values():
    assert github_utils.build_merge_kwargs("a" * 40, "squash") == {"sha": "a" * 40, "merge_method": "squash"}
    assert github_utils.build_merge_kwargs("a" * 40, "merge", "title", "") == {
        "sha": "a" * 40, "merge_method": "merge", "commit_title": "title"
    }
    assert github_utils.build_merge_kwargs("a" * 40, "rebase", "", "message") == {
        "sha": "a" * 40, "merge_method": "rebase", "commit_message": "message"
    }
    assert github_utils.build_merge_kwargs("a" * 40, "squash", "title", "message", True) == {
        "sha": "a" * 40, "merge_method": "squash", "commit_title": "title",
        "commit_message": "message", "delete_branch": True,
    }


def test_merge_calls_pygithub_with_supported_keywords_and_confirms_result(monkeypatch):
    head = "a" * 40
    base = "b" * 40
    pr_read = {"ok": True, "state": "open", "merged": False, "draft": False, "base_branch": "main",
               "base_sha": base, "head_branch": "feature", "head_sha": head, "mergeable": True,
               "mergeable_state": "clean", "review_decision": "APPROVED", "reviews": [],
               "requested_reviewers": [], "requested_teams": []}
    monkeypatch.setattr(github_utils, "get_github_pull_request", lambda *_: pr_read)
    monkeypatch.setattr(github_utils, "_private_ci_job", lambda *_: {"repository": "owner/repo", "branch": "feature", "commit_sha": head, "profile": "repo-auto-check", "status": "passed", "exit_code": 0})
    monkeypatch.setattr(github_utils, "_repository_merge_policy", lambda *_: {"required_private_ci_profile": "repo-auto-check"})
    monkeypatch.setattr(github_utils, "get_github_pull_request_checks", lambda *_: {"ok": True, "checks": [], "statuses": [], "required_check_sources": {"errors": []}})
    monkeypatch.setattr(github_utils, "_review_policy", lambda *_: {"required_approvals": 0, "current_approvals": 0, "source": "none"})
    monkeypatch.setattr(github_utils, "get_github_repository", lambda *_: {"allow_squash_merge": True})

    class FakePR:
        merged = True
        merge_commit_sha = "c" * 40
        html_url = "https://github.com/owner/repo/pull/1"

        def __init__(self):
            self.calls = []

        def merge(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(merged=True, sha="c" * 40, message="merged")

    fake_pr = FakePR()
    fake_repo = SimpleNamespace(get_pull=lambda *_: fake_pr, get_branch=lambda *_: SimpleNamespace(commit=SimpleNamespace(sha="d" * 40)))
    monkeypatch.setattr(github_utils, "_get_gh", lambda: SimpleNamespace(get_repo=lambda *_: fake_repo))
    result = github_utils.merge_github_pull_request("owner/repo", 1, "squash", head, "job", "main", "", "", False, True)
    assert result["ok"] is True
    assert fake_pr.calls == [{"sha": head, "merge_method": "squash"}]
