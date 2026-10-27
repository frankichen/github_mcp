from app import deployment_service as deployment


REPOSITORY = "frankichen/sxt"
MAIN_SHA = "1" * 40
PR_SHA = "2" * 40
TREE_SHA = "a" * 40


def _job(*, branch="ai/test-deploy", commit_sha=PR_SHA, tree_sha=TREE_SHA, **overrides):
    value = {
        "job_id": "ci-tree-job",
        "repository": REPOSITORY,
        "branch": branch,
        "commit_sha": commit_sha,
        "profile": "repo-auto-check",
        "status": "passed",
        "exit_code": 0,
        "superseded_by_job_id": None,
        "summary": {"git_tree_sha": tree_sha} if tree_sha else {},
    }
    value.update(overrides)
    return value


def _private_ci(monkeypatch, job):
    monkeypatch.setattr(
        deployment, "_deployment_spec",
        lambda _repository: {"private_ci": True, "profile": "repo-auto-check"},
    )
    monkeypatch.setattr(deployment, "get_job", lambda _job_id: job)


def test_exact_main_sha_ci_remains_valid_without_tree_lookup(monkeypatch):
    job = _job(branch="main", commit_sha=MAIN_SHA, tree_sha="")
    _private_ci(monkeypatch, job)
    monkeypatch.setattr(
        deployment, "_github_commit_tree_sha",
        lambda *_args: (_ for _ in ()).throw(AssertionError("unexpected GitHub tree lookup")),
    )

    reason, verified = deployment._ci_gate(REPOSITORY, "ci-tree-job", MAIN_SHA)

    assert reason is None
    assert verified["deployment_ci_evidence"] == {
        "mode": "exact_main_sha",
        "tested_commit_sha": MAIN_SHA,
        "target_commit_sha": MAIN_SHA,
    }


def test_sxt_ai_pr_ci_is_valid_only_for_exact_github_and_worker_tree(monkeypatch):
    job = _job()
    _private_ci(monkeypatch, job)
    monkeypatch.setattr(deployment, "_github_commit_tree_sha", lambda _repo, _sha: TREE_SHA)

    reason, verified = deployment._ci_gate(
        REPOSITORY, "ci-tree-job", MAIN_SHA, expected_tree_sha=TREE_SHA,
    )

    assert reason is None
    assert verified["deployment_ci_evidence"] == {
        "mode": "tree_equivalent_pr",
        "tested_commit_sha": PR_SHA,
        "tested_tree_sha": TREE_SHA,
        "target_commit_sha": MAIN_SHA,
        "target_tree_sha": TREE_SHA,
    }


def test_tree_equivalent_ci_rejects_worker_tree_that_does_not_match_github(monkeypatch):
    job = _job(tree_sha="b" * 40)
    _private_ci(monkeypatch, job)
    monkeypatch.setattr(deployment, "_github_commit_tree_sha", lambda _repo, _sha: TREE_SHA)

    reason, _ = deployment._ci_gate(
        REPOSITORY, "ci-tree-job", MAIN_SHA, expected_tree_sha=TREE_SHA,
    )

    assert reason == "PRIVATE_CI_TREE_MISMATCH"


def test_tree_equivalent_ci_rejects_pr_tree_that_differs_from_current_main(monkeypatch):
    job = _job()
    _private_ci(monkeypatch, job)
    github_trees = {PR_SHA: TREE_SHA, MAIN_SHA: "b" * 40}
    monkeypatch.setattr(
        deployment, "_github_commit_tree_sha",
        lambda _repo, sha: github_trees[sha],
    )

    reason, _ = deployment._ci_gate(REPOSITORY, "ci-tree-job", MAIN_SHA)

    assert reason == "PRIVATE_CI_TREE_MISMATCH"


def test_tree_equivalent_ci_fails_closed_when_worker_tree_is_missing(monkeypatch):
    job = _job(tree_sha="")
    _private_ci(monkeypatch, job)
    monkeypatch.setattr(deployment, "_github_commit_tree_sha", lambda _repo, _sha: TREE_SHA)

    reason, _ = deployment._ci_gate(
        REPOSITORY, "ci-tree-job", MAIN_SHA, expected_tree_sha=TREE_SHA,
    )

    assert reason == "PRIVATE_CI_TREE_EVIDENCE_MISSING"


def test_non_ai_branch_cannot_use_tree_equivalence(monkeypatch):
    job = _job(branch="feature/untrusted")
    _private_ci(monkeypatch, job)
    monkeypatch.setattr(
        deployment, "_github_commit_tree_sha",
        lambda *_args: (_ for _ in ()).throw(AssertionError("branch gate must fail first")),
    )

    reason, _ = deployment._ci_gate(
        REPOSITORY, "ci-tree-job", MAIN_SHA, expected_tree_sha=TREE_SHA,
    )

    assert reason == "PRIVATE_CI_BRANCH_MISMATCH"


def test_other_repositories_cannot_use_sxt_tree_equivalence(monkeypatch):
    repository = "frankichen/auto_gupiao"
    job = {**_job(), "repository": repository}
    _private_ci(monkeypatch, job)

    reason, _ = deployment._ci_gate(repository, "ci-tree-job", MAIN_SHA)

    assert reason == "PRIVATE_CI_BRANCH_MISMATCH"


def test_tree_equivalent_ci_still_requires_passed_not_superseded_full_profile(monkeypatch):
    job = _job(status="failed", exit_code=1)
    _private_ci(monkeypatch, job)
    monkeypatch.setattr(
        deployment, "_github_commit_tree_sha",
        lambda *_args: (_ for _ in ()).throw(AssertionError("failed CI must be rejected first")),
    )

    reason, _ = deployment._ci_gate(
        REPOSITORY, "ci-tree-job", MAIN_SHA, expected_tree_sha=TREE_SHA,
    )

    assert reason == "PRIVATE_CI_NOT_PASSED"


def test_deployment_plan_reports_tree_evidence_but_still_requires_current_main_sha(monkeypatch):
    job = _job()
    _private_ci(monkeypatch, job)
    monkeypatch.setattr(deployment, "_validate_common", lambda *_args: None)
    monkeypatch.setattr(
        deployment, "_repo_state",
        lambda _repository, commit_sha: (MAIN_SHA, ["internal/device.go"]),
    )
    monkeypatch.setattr(deployment, "_github_commit_tree_sha", lambda _repo, _sha: TREE_SHA)
    monkeypatch.setattr(deployment, "_status_snapshot", lambda _repository: {})

    current_main_plan = deployment.plan_test_deployment(
        REPOSITORY, "gongshi-test", MAIN_SHA, "ci-tree-job",
    )
    stale_sha_plan = deployment.plan_test_deployment(
        REPOSITORY, "gongshi-test", PR_SHA, "ci-tree-job",
    )

    assert current_main_plan["ready"] is True
    assert current_main_plan["private_ci"]["deployment_ci_evidence"]["mode"] == "tree_equivalent_pr"
    assert stale_sha_plan["ready"] is False
    assert "COMMIT_NOT_CURRENT_MAIN" in stale_sha_plan["reasons"]
