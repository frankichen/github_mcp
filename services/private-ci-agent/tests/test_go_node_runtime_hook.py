from private_ci_agent.profiles import apply_workspace_hooks, go_commands_for_workspace
from private_ci_agent.podman import LOCAL_ONLY_IMAGE_PREFIXES


def test_go_node_runtime_hook_preserves_real_go_tests(tmp_path):
    (tmp_path / "go.mod").write_text("module sample\ngo 1.26.4\n", encoding="utf-8")
    commands = apply_workspace_hooks(
        go_commands_for_workspace(str(tmp_path)),
        {"path": ".", "stack": "go", "hooks": ["go-migrate", "go-node-runtime"]},
    )
    assert commands["image"] == "localhost/private-ci-go-node:1.26.4-node22"
    assert commands["selected_image"] == commands["image"]
    assert commands["source_image"] == "docker.io/library/golang:1.26.4"
    assert any(item["name"] == "gotest" and "go test -p 6" in item["command"] for item in commands["check"])
    assert any(item["name"] == "node_runtime" for item in commands["setup"])
    assert any(commands["image"].startswith(prefix) for prefix in LOCAL_ONLY_IMAGE_PREFIXES)


def test_go_node_runtime_rejects_unsupported_version_and_stack(tmp_path):
    (tmp_path / "go.mod").write_text("module sample\ngo 1.25.3\n", encoding="utf-8")
    for stack in ("go", "python"):
        result = apply_workspace_hooks(
            go_commands_for_workspace(str(tmp_path)),
            {"path": ".", "stack": stack, "hooks": ["go-node-runtime"]},
        )
        assert result["error"] == "configuration_error"
