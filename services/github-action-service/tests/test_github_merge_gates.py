from types import SimpleNamespace

import pytest

from app import github_utils


HEAD = "a" * 40
TREE = "c" * 40
FORMAL_PROFILE = "repo-auto-check"


def _attestation(job_id="job", repository="owner/repo", commit_sha=HEAD, tree_sha=TREE, **overrides):
    item = {
        "attestation_id": "att-1",
        "repository": repository,
        "tested_commit_sha": commit_sha,
        "tested_tree_sha": tree_sha,
        "private_ci_job_id": job_id,
        "profile": FORMAL_PROFILE,
        "status": "active",
        "expires_at": 4102444800.0,
    }
    item.update(overrides)
    return item


def _valid_attestation_result(**overrides):
    item = _attestation(**overrides)
    return {"ok": True, "found": True, "reusable": True, "attestation": item}


def _formal_job(repository="owner/repo", branch="feature", commit_sha=HEAD, tree_sha=TREE, profile=FORMAL_PROFILE, **overrides):
    job = {
        "repository": repository,
        "branch": branch,
        "commit_sha": commit_sha,
        "profile": profile,
        "status": "passed",
        "exit_code": 0,
        "summary": {"git_tree_sha": tree_sha},
    }
    job.update(overrides)
    return job


def _install_gate_baseline(monkeypatch, *, repository="owner/repo", job=None, attestation=None, fake_pr=None):
    job = job or _formal_job(repository=repository)
    attestation = attestation or _valid_attestation_result(repository=repository)
    pr_read = {
        "ok": True,
        "state": "open",
        "merged": False,
        "draft": False,
        "base_branch": "main",
        "base_sha": "b" * 40,
        "head_branch": "feature",
        "head_sha": HEAD,
        "mergeable": True,
        "mergeable_state": "clean",
        "review_decision": "APPROVED",
        "reviews": [],
        "requested_reviewers": [],
        "requested_teams": [],
    }
    monkeypatch.setattr(github_utils, "get_github_pull_request", lambda *_: pr_read)
    monkeypatch.setattr(github_utils, "_private_ci_policy", lambda *_: (True, True))
    monkeypatch.setattr(github_utils, "_private_ci_job", lambda *_: job)
    monkeypatch.setattr(github_utils, "_repository_merge_policy", lambda *_: {"required_private_ci_profile": FORMAL_PROFILE})
    monkeypatch.setattr(github_utils, "_github_commit_tree_sha", lambda *_: TREE)
    monkeypatch.setattr(github_utils, "_validated_attestation_for_job", lambda *_: attestation)
    monkeypatch.setattr(github_utils, "_review_policy", lambda *_: {"required_approvals": 0, "current_approvals": 0, "source": "none", "changes_requested": False})
    monkeypatch.setattr(github_utils, "get_github_pull_request_checks", lambda *_: {"ok": True, "checks": [], "statuses": [], "overall_conclusion": "success", "required_check_sources": {"errors": []}})
    monkeypatch.setattr(github_utils, "get_github_repository", lambda *_: {"allow_squash_merge": True})
    repo_obj = SimpleNamespace(allow_squash_merge=True)
    if fake_pr is not None:
        repo_obj.get_pull = lambda *_: fake_pr
        repo_obj.get_branch = lambda *_: SimpleNamespace(commit=SimpleNamespace(sha="d" * 40))
    monkeypatch.setattr(github_utils, "_get_gh", lambda: SimpleNamespace(get_repo=lambda *_: repo_obj))


def test_check_classification_non_required_failure_and_infrastructure_signals():
    result = github_utils._classify_check_run({"name": "Console", "status": "completed", "conclusion": "failure", "duration_seconds": 3, "steps": [], "runner_id": 0}, False)
    assert result["classification"] == "GITHUB_ACTIONS_QUOTA_OR_INFRA_FAILURE"
    assert result["is_required"] is False
    assert result["blocking"] is False

    required_infra = github_utils._classify_check_run(
        {"name": "Console", "status": "completed", "conclusion": "failure", "duration_seconds": 3, "steps": []}, True, "branch_protection"
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
                "head_branch": "feature", "head_sha": HEAD, "mergeable": None, "mergeable_state": "unknown",
                "review_decision": "REVIEW_REQUIRED", "reviews": [], "requested_reviewers": [], "requested_teams": []}

    monkeypatch.setattr(github_utils, "get_github_pull_request", lambda *args: pr(merged=True))
    merged = github_utils.get_github_pull_request_merge_readiness("frankichen/sxt", 565, HEAD, "job")
    assert merged["reasons"] == ["ALREADY_MERGED"]
    assert merged["blocking"] == ["ALREADY_MERGED"]

    monkeypatch.setattr(github_utils, "get_github_pull_request", lambda *args: pr(state="closed"))
    closed = github_utils.get_github_pull_request_merge_readiness("frankichen/sxt", 1, HEAD, "job")
    assert closed["reasons"] == ["PR_NOT_OPEN"]


def test_all_merge_tools_short_circuit_terminal_pr_before_input_gates(monkeypatch):
    def merged_pr(*_args):
        return {"ok": True, "state": "closed", "merged": True, "draft": False, "base_branch": "main", "base_sha": "b" * 40,
                "head_branch": "feature", "head_sha": HEAD, "mergeable": False, "mergeable_state": "clean",
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
    _install_gate_baseline(monkeypatch, repository="frankichen/sxt", attestation=_valid_attestation_result(repository="frankichen/sxt"))
    for job, reason in [
        (_formal_job(repository="frankichen/sxt", commit_sha="x" * 40), "PRIVATE_CI_SHA_MISMATCH"),
        (_formal_job(repository="frankichen/sxt", profile="wrong"), "PRIVATE_CI_PROFILE_MISMATCH"),
        (_formal_job(repository="frankichen/sxt", superseded_by_job_id="new"), "PRIVATE_CI_SUPERSEDED"),
    ]:
        monkeypatch.setattr(github_utils, "_private_ci_job", lambda *_args, job=job: job)
        result = github_utils._readiness("frankichen/sxt", 1, HEAD, "job")
        assert reason in result["blocking"]

    monkeypatch.setattr(github_utils, "_private_ci_job", lambda *_: _formal_job(repository="frankichen/sxt"))
    result = github_utils._readiness("frankichen/sxt", 1, HEAD, "job")
    assert result["private_ci"]["valid"] is True
    assert result["ready"] is True


@pytest.mark.parametrize(
    ("job", "attestation", "reason"),
    [
        (_formal_job(), {"ok": False, "found": False, "reusable": False, "error_code": "ATTESTATION_NOT_FOUND"}, "PRIVATE_CI_ATTESTATION_REQUIRED"),
        (_formal_job(tree_sha="x" * 40), _valid_attestation_result(), "PRIVATE_CI_TREE_MISMATCH"),
        (_formal_job(), _valid_attestation_result(job_id="other-job"), "PRIVATE_CI_ATTESTATION_JOB_MISMATCH"),
        (_formal_job(), _valid_attestation_result(commit_sha="x" * 40), "PRIVATE_CI_ATTESTATION_SHA_MISMATCH"),
        (_formal_job(), _valid_attestation_result(tree_sha="x" * 40), "PRIVATE_CI_ATTESTATION_TREE_MISMATCH"),
        (_formal_job(), {"ok": False, "found": True, "reusable": False, "error_code": "ATTESTATION_REVOKED"}, "PRIVATE_CI_ATTESTATION_REVOKED"),
        (_formal_job(), {"ok": False, "found": True, "reusable": False, "error_code": "ATTESTATION_EXPIRED"}, "PRIVATE_CI_ATTESTATION_EXPIRED"),
        (_formal_job(), {"ok": True, "found": True, "reusable": False, "attestation": _attestation()}, "PRIVATE_CI_ATTESTATION_NOT_REUSABLE"),
        (_formal_job(profile="repo-fast-check"), _valid_attestation_result(), "PRIVATE_CI_PROFILE_MISMATCH"),
    ],
    ids=["no-attestation", "wrong-job-tree", "wrong-attestation-job", "wrong-attestation-commit", "wrong-attestation-tree", "revoked", "expired", "non-reusable", "fast-ci"],
)
def test_formal_merge_gate_negative_matrix_is_shared_by_readiness_plan_and_merge(monkeypatch, job, attestation, reason):
    class NeverMerge:
        def merge(self, **_kwargs):
            raise AssertionError("negative readiness must stop before GitHub merge")

    _install_gate_baseline(monkeypatch, job=job, attestation=attestation, fake_pr=NeverMerge())
    readiness = github_utils.get_github_pull_request_merge_readiness("owner/repo", 1, HEAD, "job", "main")
    plan = github_utils.plan_github_pull_request_merge("owner/repo", 1, "squash", HEAD, "job", "main")
    merge = github_utils.merge_github_pull_request("owner/repo", 1, "squash", HEAD, "job", "main", confirm=True)

    assert reason in readiness["blocking"]
    assert readiness["ready"] is False
    assert reason in plan["blocking_reasons"]
    assert plan["ready"] is False
    assert merge["ok"] is False
    assert reason in merge["error"]["details"]["readiness"]["blocking"]


def test_exact_full_ci_and_exact_reusable_attestation_are_ready_and_mergeable(monkeypatch):
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
    _install_gate_baseline(monkeypatch, fake_pr=fake_pr)
    readiness = github_utils.get_github_pull_request_merge_readiness("owner/repo", 1, HEAD, "job", "main")
    plan = github_utils.plan_github_pull_request_merge("owner/repo", 1, "squash", HEAD, "job", "main")
    merge = github_utils.merge_github_pull_request("owner/repo", 1, "squash", HEAD, "job", "main", confirm=True)

    assert readiness["ready"] is True
    assert readiness["expected_tree_sha"] == TREE
    assert readiness["private_ci"]["job_tree_sha"] == TREE
    assert readiness["private_ci"]["attestation_validation"]["reusable"] is True
    assert plan["ready"] is True
    assert merge["ok"] is True
    assert fake_pr.calls == [{"sha": HEAD, "merge_method": "squash"}]


def test_private_ci_disabled_skips_gate_for_readiness_and_merge(monkeypatch):
    base = {"ok": True, "state": "open", "merged": False, "draft": False, "base_branch": "main", "base_sha": "b" * 40,
            "head_branch": "feature", "head_sha": HEAD, "mergeable": True, "mergeable_state": "clean",
            "review_decision": "APPROVED", "reviews": [], "requested_reviewers": [], "requested_teams": []}
    monkeypatch.setattr(github_utils, "get_github_pull_request", lambda *args: base)
    monkeypatch.setattr(github_utils, "_private_ci_policy", lambda *_: (True, False))
    monkeypatch.setattr(github_utils, "_get_gh", lambda: SimpleNamespace(get_repo=lambda *_: SimpleNamespace(allow_squash_merge=True)))
    monkeypatch.setattr(github_utils, "_review_policy", lambda *args: {"required_approvals": 0, "current_approvals": 0, "source": "none", "changes_requested": False})
    monkeypatch.setattr(github_utils, "get_github_pull_request_checks", lambda *args: {"ok": True, "checks": [], "statuses": [], "overall_conclusion": "neutral", "required_check_sources": {"errors": []}})
    monkeypatch.setattr(github_utils, "get_github_repository", lambda *args: {"allow_squash_merge": True})

    readiness = github_utils.get_github_pull_request_merge_readiness("frankichen/auto_gupiao", 32, HEAD)
    assert "PRIVATE_CI_REQUIRED" not in readiness["blocking"]
    assert readiness["private_ci_required"] is False

    merge = github_utils.merge_github_pull_request("frankichen/auto_gupiao", 32, "squash", HEAD, confirm=True)
    assert merge["error"]["code"] != "PRIVATE_CI_REQUIRED"


def test_infrastructure_signals_are_classified_without_project_failure():
    assert github_utils._is_actions_infrastructure_failure({"status": "completed", "conclusion": "failure", "runner_id": 0})
    assert github_utils._is_actions_infrastructure_failure({"status": "completed", "conclusion": "failure", "logs_http_status": 404})
    assert github_utils._is_actions_infrastructure_failure({"status": "completed", "conclusion": "failure", "duration_seconds": 3, "steps": []})
    assert not github_utils._is_actions_infrastructure_failure({"status": "completed", "conclusion": "failure", "duration_seconds": 42, "steps": [{"name": "go test"}]})


def test_build_merge_kwargs_uses_current_pygithub_names_and_omits_empty_values():
    assert github_utils.build_merge_kwargs(HEAD, "squash") == {"sha": HEAD, "merge_method": "squash"}
    assert github_utils.build_merge_kwargs(HEAD, "merge", "title", "") == {"sha": HEAD, "merge_method": "merge", "commit_title": "title"}
    assert github_utils.build_merge_kwargs(HEAD, "rebase", "", "message") == {"sha": HEAD, "merge_method": "rebase", "commit_message": "message"}
    assert github_utils.build_merge_kwargs(HEAD, "squash", "title", "message", True) == {
        "sha": HEAD, "merge_method": "squash", "commit_title": "title", "commit_message": "message", "delete_branch": True,
    }


def test_merge_calls_pygithub_with_supported_keywords_and_confirms_result(monkeypatch):
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
    _install_gate_baseline(monkeypatch, fake_pr=fake_pr)
    result = github_utils.merge_github_pull_request("owner/repo", 1, "squash", HEAD, "job", "main", "", "", False, True)
    assert result["ok"] is True
    assert fake_pr.calls == [{"sha": HEAD, "merge_method": "squash"}]
