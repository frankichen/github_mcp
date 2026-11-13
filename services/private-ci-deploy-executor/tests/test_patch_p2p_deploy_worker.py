"""The P2P Worker overlay must preserve lease fencing and reject stale code."""
import pytest
from scripts import patch_p2p_deploy_worker as patcher

BASE = '''CONTRACTS = {
    "frankichen/sxt": {},
    "frankichen/devhub": {
        "workspace": "TEST",
    },
}
def delegate(repository):
    return repository in {"frankichen/sxt", "frankichen/devhub"}
q = "WHERE status='queued' AND repository IN (?, ?, ?) "
p = ("frankichen/sxt", "frankichen/devhub", "frankichen/auto_gupiao"),
claim_owner = "owner"
lease_expires_at = "expires"
claim_generation = "claim_generation=claim_generation+1"
state_revision = "state_revision=state_revision+1"
'''


def test_overlay_is_limited_and_idempotent():
    actual = patcher.patch(BASE)
    assert patcher.REPOSITORY in actual
    assert "claim_owner" in actual
    assert "lease_expires_at" in actual
    assert patcher.patch(actual) == actual


def test_incomplete_lease_fails_closed():
    with pytest.raises(ValueError):
        patcher.patch(BASE.replace("claim_generation=claim_generation+1", "missing"))


def test_partial_contract_fails_closed():
    with pytest.raises(ValueError):
        patcher.patch(BASE + '\nrepo = "frankichen/p2p-server"\n')
