"""P2P release uses a fixed isolated target and exact evidence."""
import pytest
from scripts import p2p_deploy_executor as p

SHA = "a" * 40
ID = "20261113-060318-" + SHA[:12]
PATH = "/opt/p2p-server-lab/releases/" + ID
GOOD = f"release_id={ID} git_sha={SHA} current={PATH} health=passed checksum=passed manifest=passed"
ROW = {"repository": "frankichen/p2p-server", "environment": "gongshi-p2p-lab",
       "requested_scope": "p2p-lab", "commit_sha": SHA,
       "deployment_id": "dep_" + "c" * 32}


def test_p2p_exact_policy():
    assert p.approved(ROW)
    for k, v in (("repository", "frankichen/sxt"),
                 ("environment", "gongshi-test"),
                 ("requested_scope", "fullstack"),
                 ("commit_sha", "short"),
                 ("deployment_id", "dep_invalid")):
        assert not p.approved(dict(ROW, **{k: v}))


def test_p2p_release_evidence():
    assert p.release_evidence(GOOD, SHA) == (ID, PATH)


@pytest.mark.parametrize("bad", [
    GOOD.replace("health=passed", "health=failed"),
    GOOD.replace("checksum=passed", "checksum=failed"),
    GOOD.replace("manifest=passed", "manifest=failed"),
    GOOD.replace("git_sha=" + SHA, "git_sha=" + "b" * 40),
    GOOD.replace("current=/opt/", "current=/home/dly/"),
    GOOD.replace("release_id=" + ID, "release_id=invalid"),
    GOOD.replace(" checksum=", " checksum=passed checksum="),
])
def test_invalid_evidence_stops(bad):
    assert p.release_evidence(bad, SHA) is None


def test_unrelated_queue_does_not_execute(monkeypatch):
    monkeypatch.setattr(p.base, "_callback",
                        lambda *a, **k: pytest.fail("foreign repo got callback"))
    p.execute(dict(ROW, repository="frankichen/sxt"))
