import subprocess

from private_ci_agent.contract_integrity import verify_product_contract_integrity


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _commit(repo, message):
    _git(repo, "add", "-A")
    subprocess.run(
        [
            "git", "-C", str(repo), "-c", "user.name=CI",
            "-c", "user.email=ci@example.invalid", "commit", "-m", message,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return _git(repo, "rev-parse", "HEAD")


def _repo(tmp_path, with_policy=False):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    if with_policy:
        policy = repo / "docs" / "product-contracts" / "README.md"
        policy.parent.mkdir(parents=True)
        policy.write_text("# Product contract integrity\n", encoding="utf-8")
    return repo


def test_gate_blocks_frozen_history_mutation(tmp_path):
    repo = _repo(tmp_path)
    requirement = repo / "P2P服务资源授权与就近分配需求.md"
    requirement.write_text(
        "# P2P\n状态：已确认，作为后续开发和验收的产品依据\n",
        encoding="utf-8",
    )
    base = _commit(repo, "base")
    policy = repo / "docs" / "product-contracts" / "README.md"
    policy.parent.mkdir(parents=True)
    policy.write_text("# policy\n", encoding="utf-8")
    requirement.write_text("# P2P\n状态：已确认，作为产品依据\nchanged\n", encoding="utf-8")
    head = _commit(repo, "mutate")

    result = verify_product_contract_integrity("owner/repo", str(repo), base, head)

    assert result["ok"] is False
    assert any(item["code"] == "HISTORICAL_CONTRACT_MUTATION" for item in result["errors"])


def test_gate_allows_versioned_contract_only_revision(tmp_path):
    repo = _repo(tmp_path, with_policy=True)
    base = _commit(repo, "policy")
    contract = repo / "docs" / "product-contracts" / "p2p" / "2026-08-25-r2-routing.md"
    contract.parent.mkdir(parents=True)
    contract.write_text(
        "contract_id: p2p-routing\ncontract_revision: r2\ncontract_status: frozen\n"
        "approved_source: wiki-133\nsupersedes: r1\n",
        encoding="utf-8",
    )
    head = _commit(repo, "contract r2")

    result = verify_product_contract_integrity("owner/repo", str(repo), base, head)

    assert result["ok"] is True
    assert result["new_contract_revisions"] == [
        "docs/product-contracts/p2p/2026-08-25-r2-routing.md"
    ]


def test_gate_blocks_contract_and_implementation_mix(tmp_path):
    repo = _repo(tmp_path, with_policy=True)
    base = _commit(repo, "policy")
    contract = repo / "docs" / "product-contracts" / "p2p" / "2026-08-25-r2-routing.md"
    contract.parent.mkdir(parents=True)
    contract.write_text(
        "contract_id: p2p-routing\ncontract_revision: r2\ncontract_status: frozen\n"
        "approved_source: wiki-133\nsupersedes: r1\n",
        encoding="utf-8",
    )
    runtime = repo / "internal" / "devicebind" / "service.go"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("package devicebind\n", encoding="utf-8")
    head = _commit(repo, "mixed")

    result = verify_product_contract_integrity("owner/repo", str(repo), base, head)

    assert result["ok"] is False
    assert any(item["code"] == "CONTRACT_AND_IMPLEMENTATION_MIXED" for item in result["errors"])
