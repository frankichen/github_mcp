"""P2P repository release queue remains isolated from SXT."""
from app import deploy_worker


def test_p2p_contract_is_fixed(monkeypatch):
    c = deploy_worker._contract("frankichen/p2p-server")
    assert c["environment"] == "gongshi-p2p-lab"
    assert c["scope"] == "p2p-lab"
    assert c["script"] == "scripts/deploy_gongshi_p2p_lab.sh"
    assert c["status_default"] != deploy_worker._contract("frankichen/sxt")["status_default"]
    assert deploy_worker._contract("frankichen/unknown") is None
    monkeypatch.setenv("DEPLOY_EXECUTION_MODE", "claim_only")
    assert deploy_worker._should_delegate_to_wsl("frankichen/p2p-server")
    monkeypatch.setenv("DEPLOY_EXECUTION_MODE", "execute")
    assert not deploy_worker._should_delegate_to_wsl("frankichen/p2p-server")
