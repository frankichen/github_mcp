from app.repository_policy import get_policy, is_operation_allowed


def test_repository_policy_is_server_owned():
    for operation in ("read", "patch", "merge"):
        assert is_operation_allowed("frankichen/sxt", operation)
    assert is_operation_allowed("frankichen/sxt", "private_ci")
    assert is_operation_allowed("frankichen/sxt", "test_deploy")
    assert not is_operation_allowed("frankichen/sxt", "self_deploy")
    assert is_operation_allowed("frankichen/github_mcp", "patch")
    assert is_operation_allowed("frankichen/github_mcp", "ci")
    assert is_operation_allowed("frankichen/github_mcp", "read")
    assert is_operation_allowed("frankichen/github_mcp", "update_pr")
    assert not is_operation_allowed("frankichen/github_mcp", "delete_branch")
    assert not is_operation_allowed("frankichen/github_mcp", "test_deploy")
    assert not is_operation_allowed("unknown/repository", "read")
    assert not is_operation_allowed("unknown/repository", "patch")
    assert not get_policy("unknown/repository")
