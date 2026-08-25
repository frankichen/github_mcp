import pytest

from app import mygithub10, mygithub12


def _modify_block(path: str, old: str = "old", new: str = "new", *, index_line: str = "index 1111111..2222222 100644") -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        f"{index_line}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1 +1 @@\n"
        f"-{old}\n"
        f"+{new}\n"
    )


def _add_block(path: str, text: str = "new") -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "index 0000000..2222222\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        "@@ -0,0 +1 @@\n"
        f"+{text}\n"
    )


def _delete_block(path: str, text: str = "old") -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        "deleted file mode 100644\n"
        "index 1111111..0000000\n"
        f"--- a/{path}\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n"
        f"-{text}\n"
    )


def test_single_file_git_diff_still_parses_strictly():
    parsed = mygithub10._parse_patch(_modify_block("one.txt"))
    assert [(path, operation) for path, operation, _ in parsed] == [("one.txt", "modify")]
    assert len(parsed[0][2]) == 1


def test_modify_then_modify_diff_git_header_is_next_file_boundary():
    parsed = mygithub10._parse_patch(_modify_block("a.txt", "a", "A") + _modify_block("b.txt", "b", "B"))
    assert [(path, operation) for path, operation, _ in parsed] == [
        ("a.txt", "modify"),
        ("b.txt", "modify"),
    ]


def test_add_then_modify_multi_file_git_diff():
    parsed = mygithub10._parse_patch(_add_block("created.txt") + _modify_block("existing.txt"))
    assert [(path, operation) for path, operation, _ in parsed] == [
        ("created.txt", "add"),
        ("existing.txt", "modify"),
    ]


def test_modify_then_add_multi_file_git_diff():
    parsed = mygithub10._parse_patch(_modify_block("existing.txt") + _add_block("created.txt"))
    assert [(path, operation) for path, operation, _ in parsed] == [
        ("existing.txt", "modify"),
        ("created.txt", "add"),
    ]


def test_multiple_hunks_then_next_file_without_blank_line():
    patch = (
        "diff --git a/a.txt b/a.txt\n"
        "index 1111111..2222222 100644\n"
        "--- a/a.txt\n"
        "+++ b/a.txt\n"
        "@@ -1 +1 @@\n"
        "-a\n"
        "+A\n"
        "@@ -3 +3 @@\n"
        "-c\n"
        "+C\n"
        "diff --git a/b.txt b/b.txt\n"
        "index 3333333..4444444 100644\n"
        "--- a/b.txt\n"
        "+++ b/b.txt\n"
        "@@ -1 +1 @@\n"
        "-b\n"
        "+B\n"
    )
    parsed = mygithub10._parse_patch(patch)
    assert [(path, operation, len(hunks)) for path, operation, hunks in parsed] == [
        ("a.txt", "modify", 2),
        ("b.txt", "modify", 1),
    ]


def test_git_metadata_is_outside_hunks_for_modify_add_and_delete():
    parsed = mygithub10._parse_patch(
        _modify_block("modified.txt") + _add_block("created.txt") + _delete_block("deleted.txt")
    )
    assert [(path, operation) for path, operation, _ in parsed] == [
        ("modified.txt", "modify"),
        ("created.txt", "add"),
        ("deleted.txt", "delete"),
    ]


def test_genuinely_invalid_hunk_line_is_still_rejected():
    patch = (
        "diff --git a/a.txt b/a.txt\n"
        "index 1111111..2222222 100644\n"
        "--- a/a.txt\n"
        "+++ b/a.txt\n"
        "@@ -1 +1 @@\n"
        "this-is-not-a-diff-body-line\n"
    )
    with pytest.raises(mygithub10.MyGithub10Error) as exc:
        mygithub10._parse_patch(patch)
    assert exc.value.code == "PATCH_INVALID_FORMAT"
    assert exc.value.message == "invalid unified diff line"


def test_large_31_file_git_diff_parses_all_files_in_order():
    patch = "".join(
        _modify_block(f"pkg/file_{index:02d}.txt", f"old-{index}", f"new-{index}")
        for index in range(31)
    )
    parsed = mygithub10._parse_patch(patch)
    assert len(parsed) == 31
    assert [path for path, _, _ in parsed] == [f"pkg/file_{index:02d}.txt" for index in range(31)]
    assert all(operation == "modify" for _, operation, _ in parsed)


def test_metadata_only_deleted_file_mode_still_parses_and_next_file_continues():
    patch = (
        "diff --git a/empty.txt b/empty.txt\n"
        "deleted file mode 100644\n"
        "index e69de29..0000000\n"
        + _modify_block("next.txt")
    )
    parsed = mygithub10._parse_patch(patch)
    assert [(path, operation) for path, operation, _ in parsed] == [
        ("next.txt", "modify"),
        ("empty.txt", "delete"),
    ]


def test_read_only_analyze_patch_reuses_strict_parser_and_reports_31_files(monkeypatch):
    patch = "".join(
        _modify_block(f"pkg/file_{index:02d}.txt", f"old-{index}", f"new-{index}")
        for index in range(31)
    )
    monkeypatch.setattr(
        mygithub12,
        "resolve_identity",
        lambda *args, **kwargs: {"repository": "owner/repo", "commit_sha": "a" * 40, "tree_sha": "b" * 40},
    )
    monkeypatch.setattr(
        mygithub12,
        "affected_tests",
        lambda *args, **kwargs: {"tests": []},
    )
    result = mygithub12.analyze_patch(object(), "owner/repo", "a" * 40, patch)
    assert result["parsed_files"] == 31
    assert len(result["file_patches"]) == 31
    assert [item["path"] for item in result["file_patches"]] == [
        f"pkg/file_{index:02d}.txt" for index in range(31)
    ]
