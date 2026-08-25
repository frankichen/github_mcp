from private_ci_agent.affected_selection import select_affected


WORKSPACES = [
    {"path": "services/api", "stack": "python"},
    {"path": "web", "stack": "node"},
]


def test_fast_selection_maps_changed_paths_and_test_evidence():
    result = select_affected(
        ["services/api/app.py", "services/api/tests/test_app.py"], WORKSPACES
    )

    assert result["complete"] is True
    assert result["selected_workspaces"] == [WORKSPACES[0]]
    assert result["selected_tests"] == ["services/api/tests/test_app.py"]
    assert result["reasons"] == ["path_prefix_match"]


def test_global_manifest_change_widens_to_every_workspace():
    result = select_affected(["package-lock.json"], WORKSPACES)

    assert result["complete"] is True
    assert result["selected_workspaces"] == WORKSPACES
    assert result["reasons"] == ["global_dependency_or_ci_change"]


def test_truncated_unknown_and_empty_inputs_never_narrow_to_empty():
    truncated = select_affected(["web/app.ts"], WORKSPACES, truncated=True)
    unknown = select_affected(["unmapped/data.bin"], WORKSPACES)
    empty = select_affected([], WORKSPACES)

    assert truncated["complete"] is False
    assert unknown["complete"] is False
    assert empty["complete"] is False
    assert truncated["selected_workspaces"] == WORKSPACES
    assert unknown["selected_workspaces"] == WORKSPACES
    assert empty["selected_workspaces"] == WORKSPACES


def test_full_ci_always_runs_every_workspace_and_reports_incomplete_evidence():
    result = select_affected(
        ["web/app.ts"], WORKSPACES, truncated=True, affected_only=False
    )

    assert result["selected_workspaces"] == WORKSPACES
    assert result["complete"] is False
    assert result["reasons"] == ["full_ci_all_workspaces", "changed_files_truncated"]
