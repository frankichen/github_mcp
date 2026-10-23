"""Release evidence is a terminal success contract, not a progress log regex."""

import pytest
import scripts.deploy_executor as executor

SHA = "2208db46e0b99f6b4a4de2cc3f4978d0ca3f13e5"
ID = "20261023-123755-2208db46e0b9"
PATH = f"/home/dly/releases/{ID}"


def _combined(*, release_id=ID, git_sha=SHA, current=PATH, health="passed"):
    return (
        f"release_id={release_id} git_sha={git_sha} "
        f"previous_release=/home/dly/releases/old current={current} "
        f"health={health} strategy=bluegreen"
    )


def test_bluegreen_combined_report_is_accepted():
    actual = "\n".join([
        "BLUEGREEN_CUTOVER_PASSED slot=green previous_slot=blue release_id=" + ID,
        _combined(),
        "[deploy-gongshi-test +643s] deployment completed",
        "removing isolated PostgreSQL container",
    ])
    assert executor._extract_release_evidence(actual, SHA) == (ID, PATH)


def test_legacy_multiline_sxt_success_report_is_accepted():
    actual = "\n".join([
        f"release_id={ID}", f"git_sha={SHA}", "incoming=/home/dly/releases/incoming/unused",
        f"release_dir={PATH}", f"current={PATH}", "previous_release=/home/dly/releases/old",
        "health=passed",
        "[deploy-gongshi-test +643s] deployment completed",
    ])
    assert executor._extract_release_evidence(actual, SHA) == (ID, PATH)


@pytest.mark.parametrize("report", [
    _combined(git_sha="f" * 40),
    _combined(release_id="20261023-123755-deadbeefdead"),
    _combined(current="/home/dly/releases/another"),
    _combined(current="../bad/" + ID),
    _combined(current="/tmp/" + ID),
    _combined(health="failed"),
    _combined().replace(" current=", " previous_current="),
    "BLUEGREEN_CUTOVER_PASSED slot=green release_id=" + ID,
    f"release_id={ID} git_sha={SHA} health=passed",
    f"release_id={ID} started_at=2026-10-23T12:37:55Z",
])
def test_invalid_or_incomplete_sxt_evidence_fails_closed(report):
    assert executor._extract_release_evidence(report, SHA) is None


def test_later_invalid_report_cannot_reuse_earlier_success():
    assert executor._extract_release_evidence(
        _combined() + "\n" + _combined(current="/tmp/" + ID), SHA
    ) is None


def test_last_valid_report_wins_after_intermediate_progress():
    actual = f"release_id={ID} started_at=2026-10-23T12:37:55Z\n" + _combined()
    assert executor._extract_release_evidence(actual, SHA) == (ID, PATH)


@pytest.mark.parametrize("repo", ["frankichen/devhub", "frankichen/auto_gupiao"])
def test_other_repositories_keep_legacy_standalone_contract(repo):
    other_path = "/srv/legacy/releases/" + SHA
    report = f"release_id={SHA}\ncurrent={other_path}\n"
    assert executor._extract_release_evidence(report, SHA, repo) == (SHA, other_path)
