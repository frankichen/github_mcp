import base64
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from app import mygithub10


class ReadRepo:
    default_branch = "main"

    def __init__(self, data, head="head-1", blob="blob-1"):
        self.data, self.head, self.blob = data, head, blob

    def get_commit(self, ref):
        return SimpleNamespace(sha=self.head)

    def get_contents(self, path, ref=None):
        return SimpleNamespace(sha=self.blob, size=len(self.data))

    def get_git_blob(self, sha):
        return SimpleNamespace(encoding="base64", content=base64.b64encode(self.data).decode())


class MissingRepo(ReadRepo):
    def get_contents(self, path, ref=None):
        error = RuntimeError("missing")
        error.status = 404
        raise error


class ReadService:
    def __init__(self, repo):
        self.client = SimpleNamespace(_pygithub=SimpleNamespace(get_repo=lambda _: repo))

    def _check_repository_allowed(self, _):
        return None

    def _check_default_branch_write(self, _, __):
        return None


class MultiReadRepo(ReadRepo):
    def __init__(self, files, head="head-1"):
        super().__init__(b"", head=head)
        self.files = files

    def get_contents(self, path, ref=None):
        data, blob = self.files[path]
        self.data = data
        return SimpleNamespace(sha=blob, size=len(data))

    def get_git_blob(self, sha):
        data = next(data for data, blob in self.files.values() if blob == sha)
        return SimpleNamespace(encoding="base64", content=base64.b64encode(data).decode())


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def test_single_range_never_drops_prefix_or_suffix():
    original = 'func baseP2PSyncInput() {\n    LocalP2P: LocalP2PInfo{\n        P2PID: "x",\n    },\n    FirmwareVersion: "v1",\n}\n'
    service = ReadService(ReadRepo(original.encode()))
    ops = [{"path": "sample.go", "operation": "replace", "start_line": 3, "end_line": 3,
            "expected_old_text_sha256": digest('        P2PID: "x",\n'),
            "replacement": '        P2PID: "x",\n        Provider: "p",\n        ProviderConfigVersion: 1,\n'}]
    result = mygithub10.edit_ranges(service, "owner/repo", "feature", "head-1", json.dumps(ops), "edit", True)
    assert result["new_content_sha256"]["sample.go"] == hashlib.sha256(
        original.replace('        P2PID: "x",\n', '        P2PID: "x",\n        Provider: "p",\n        ProviderConfigVersion: 1,\n').encode()
    ).hexdigest()


def test_two_ranges_use_original_offsets_and_preserve_unicode_crlf():
    original = "头部🙂\r\nkeep\r\n中间\r\n尾部\r\n"
    service = ReadService(ReadRepo(original.encode()))
    ops = [
        {"path": "unicode.txt", "operation": "replace", "start_line": 1, "end_line": 1,
         "expected_old_text_sha256": digest("头部🙂\r\n"), "replacement": "新头🙂\r\n"},
        {"path": "unicode.txt", "operation": "replace", "start_line": 4, "end_line": 4,
         "expected_old_text_sha256": digest("尾部\r\n"), "replacement": "新尾部\r\n"},
    ]
    result = mygithub10.edit_ranges(service, "owner/repo", "feature", "head-1", json.dumps(ops), "edit", True)
    expected = "新头🙂\r\nkeep\r\n中间\r\n新尾部\r\n".encode()
    assert result["new_content_sha256"]["unicode.txt"] == hashlib.sha256(expected).hexdigest()


def test_duplicate_insert_boundary_is_rejected():
    service = ReadService(ReadRepo(b"a\nb\n"))
    ops = [
        {"path": "x", "operation": "insert_before", "start_line": 2, "replacement": "x\n"},
        {"path": "x", "operation": "insert_after", "start_line": 1, "replacement": "y\n"},
    ]
    with pytest.raises(mygithub10.MyGithub10Error) as exc:
        mygithub10.edit_ranges(service, "owner/repo", "feature", "head-1", json.dumps(ops), "edit", True)
    assert exc.value.code == "PATCH_SCOPE_EXCEEDED"


def test_patch_multiple_hunks_is_exact_and_supports_no_final_newline():
    old = "一\n二\n三\n四".encode()
    patch = "--- a/x.txt\n+++ b/x.txt\n@@ -1,2 +1,2 @@\n-一\n+壹\n 二\n@@ -4 +4 @@\n-四\n\\ No newline at end of file\n+肆\n\\ No newline at end of file\n"
    assert mygithub10._apply_file_patch(old, mygithub10._parse_patch(patch)[0][2]) == "壹\n二\n三\n肆".encode()


def test_patch_context_mismatch_never_returns_bytes():
    patch = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-wrong\n+new\n"
    with pytest.raises(mygithub10.MyGithub10Error) as exc:
        mygithub10._apply_file_patch(b"right\n", mygithub10._parse_patch(patch)[0][2])
    assert exc.value.code == "PATCH_DOES_NOT_APPLY"


def test_hunk_counts_are_normalized_without_changing_business_lines():
    patch = "--- a/x\n+++ b/x\n@@ -1,99 +1,88 @@\n-old\n+new\n"
    parsed, details = mygithub10._parse_patch_details(patch)
    assert details["patch_normalized"] is True
    assert details["normalized_hunks"][0]["normalized"] == "@@ -1,1 +1,1 @@"
    assert mygithub10._apply_file_patch(b"old\n", parsed[0][2]) == b"new\n"


def test_normalized_patch_fingerprint_ignores_only_header_count_errors():
    service = ReadService(ReadRepo(b"old\n", blob="blob-old"))
    malformed = "--- a/x\n+++ b/x\n@@ -1,99 +1,88 @@\n-old\n+new\n"
    canonical = "--- a/x\n+++ b/x\n@@ -1,1 +1,1 @@\n-old\n+new\n"
    arguments = ("owner/repo", "feature", "head-1", '{"x":"blob-old"}')
    first = mygithub10.apply_patch(service, *arguments, malformed, "change", True)
    second = mygithub10.apply_patch(service, *arguments, canonical, "change", True)
    assert first["patch_normalized"] is True
    assert second["patch_normalized"] is False
    assert first["operation_fingerprint"] == second["operation_fingerprint"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("expected_head_sha", "head-2"),
        ("expected_blob_sha", "blob-other"),
        ("path", "other"),
        ("new_text", "different\n"),
        ("commit_message", "other message"),
    ],
)
def test_patch_fingerprint_changes_for_semantic_changes(field, value):
    parsed = mygithub10._parse_patch("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n")
    repository = "owner/repo"
    branch = "feature"
    head = "head-1"
    blobs = {"x": "blob-old"}
    message = "change"
    if field == "expected_head_sha":
        head = value
    elif field == "expected_blob_sha":
        blobs = {"x": value}
    elif field == "path":
        parsed = [(value, parsed[0][1], parsed[0][2])]
        blobs = {value: "blob-old"}
    elif field == "new_text":
        old_start, new_start, old_lines, _ = parsed[0][2][0]
        parsed = [("x", "modify", [(old_start, new_start, old_lines, [value])])]
    elif field == "commit_message":
        message = value
    baseline = mygithub10._canonical_patch_request(
        repository,
        branch,
        "head-1",
        {"x": "blob-old"},
        mygithub10._parse_patch("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n"),
        "change",
    )
    changed = mygithub10._canonical_patch_request(repository, branch, head, blobs, parsed, message)
    assert mygithub10._sha256(mygithub10._json(changed).encode()) != mygithub10._sha256(
        mygithub10._json(baseline).encode()
    )


def test_normalized_patch_request_is_idempotency_compatible(tmp_path, monkeypatch):
    monkeypatch.setattr(mygithub10.settings, "IDEMPOTENCY_DB_PATH", str(tmp_path / "normalized.db"))
    bad = mygithub10._parse_patch("--- a/x\n+++ b/x\n@@ -1,99 +1,88 @@\n-old\n+new\n")
    good = mygithub10._parse_patch("--- a/x\n+++ b/x\n@@ -1,1 +1,1 @@\n-old\n+new\n")
    first_request = mygithub10._canonical_patch_request(
        "owner/repo", "feature", "head-1", {"x": "blob-old"}, bad, "change"
    )
    second_request = mygithub10._canonical_patch_request(
        "owner/repo", "feature", "head-1", {"x": "blob-old"}, good, "change"
    )
    operation_id, replay = mygithub10._idempotent_start(
        "apply_github_patch", "normalized-key", first_request
    )
    assert operation_id and replay is None
    mygithub10._idempotent_finish(operation_id, "succeeded", result={"ok": True})
    replay_id, replay = mygithub10._idempotent_start(
        "apply_github_patch", "normalized-key", second_request
    )
    assert replay_id == "replay"
    assert replay["replayed"] is True


def test_context_diagnostic_reports_unique_candidate_but_does_not_apply_it():
    patch = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n"
    with pytest.raises(mygithub10.MyGithub10Error) as exc:
        mygithub10._apply_file_patch(
            b"prefix\nold\nsuffix\n", mygithub10._parse_patch(patch)[0][2], path="x"
        )
    assert exc.value.code == "PATCH_DOES_NOT_APPLY"
    assert exc.value.details["path"] == "x"
    assert exc.value.details["hunk_index"] == 1
    assert exc.value.details["expected_old_start"] == 1
    assert exc.value.details["nearest_candidate_start"] == 2
    assert exc.value.details["exact_match_count"] == 1


def test_context_diagnostic_rejects_multiple_candidates():
    patch = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n"
    with pytest.raises(mygithub10.MyGithub10Error) as exc:
        mygithub10._apply_file_patch(
            b"prefix\nold\nold\n", mygithub10._parse_patch(patch)[0][2], path="x"
        )
    assert exc.value.code == "PATCH_CONTEXT_AMBIGUOUS"
    assert exc.value.details["hunk_index"] == 1
    assert exc.value.details["exact_match_count"] == 2


def test_second_hunk_failure_reports_second_index():
    patch = (
        "--- a/x\n+++ b/x\n"
        "@@ -1 +1 @@\n-a\n+A\n"
        "@@ -3 +3 @@\n-wrong\n+C\n"
    )
    with pytest.raises(mygithub10.MyGithub10Error) as exc:
        mygithub10._apply_file_patch(
            b"a\nb\nc\n", mygithub10._parse_patch(patch)[0][2], path="x"
        )
    assert exc.value.code == "PATCH_DOES_NOT_APPLY"
    assert exc.value.details["hunk_index"] == 2
    assert exc.value.details["expected_old_start"] == 3


def test_multi_file_second_file_failure_reports_path_and_first_file_hunk_index():
    service = ReadService(
        MultiReadRepo({
            "a.txt": (b"a\n", "blob-a"),
            "b.txt": (b"actual\n", "blob-b"),
        })
    )
    patch = (
        "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-a\n+A\n"
        "--- a/b.txt\n+++ b/b.txt\n@@ -1 +1 @@\n-expected\n+B\n"
    )
    with pytest.raises(mygithub10.MyGithub10Error) as exc:
        mygithub10.apply_patch(
            service,
            "owner/repo",
            "feature",
            "head-1",
            '{"a.txt":"blob-a","b.txt":"blob-b"}',
            patch,
            "change",
            True,
        )
    assert exc.value.code == "PATCH_DOES_NOT_APPLY"
    assert exc.value.details["path"] == "b.txt"
    assert exc.value.details["hunk_index"] == 1


def test_hunk_beyond_file_end_reports_expected_start():
    patch = "--- a/x\n+++ b/x\n@@ -5 +5 @@\n-old\n+new\n"
    with pytest.raises(mygithub10.MyGithub10Error) as exc:
        mygithub10._apply_file_patch(b"one\n", mygithub10._parse_patch(patch)[0][2], path="x")
    assert exc.value.code == "PATCH_DOES_NOT_APPLY"
    assert exc.value.details["hunk_index"] == 1
    assert exc.value.details["expected_old_start"] == 5
    assert exc.value.details["mismatch"]["actual"] == ""


def test_build_patch_is_pure_and_preserves_no_final_newline_and_unicode_path():
    old, new = "旧🙂", "新🙂"
    result = mygithub10.build_patch("目录/文件.txt", mygithub10._git_blob_sha(old.encode()), old, new)
    assert result["dry_run"] is True
    assert result["old_blob_sha"] == mygithub10._git_blob_sha(old.encode())
    parsed = mygithub10._parse_patch(result["patch"])
    assert mygithub10._apply_file_patch(old.encode(), parsed[0][2]) == new.encode()


@pytest.mark.parametrize(
    ("original", "operations", "expected", "added", "deleted"),
    [
        (
            "first\nkeep1\nkeep2\nkeep3\nkeep4\n",
            [{"operation": "replace", "start_line": 1, "end_line": 1, "expected_old_text": "first\n", "replacement_text": "FIRST\n"}],
            "FIRST\nkeep1\nkeep2\nkeep3\nkeep4\n",
            1,
            1,
        ),
        (
            "one\ntwo\nthree\n",
            [{"operation": "replace", "start_line": 2, "end_line": 2, "expected_old_text": "two\n", "replacement_text": "TWO\n"}],
            "one\nTWO\nthree\n",
            1,
            1,
        ),
        (
            "one\ntwo\nthree\n",
            [{"operation": "delete", "start_line": 2, "end_line": 2, "expected_old_text": "two\n", "replacement_text": ""}],
            "one\nthree\n",
            0,
            1,
        ),
        (
            "one\nthree\n",
            [{"operation": "insert_after", "start_line": 1, "end_line": 1, "replacement_text": "two\n"}],
            "one\ntwo\nthree\n",
            1,
            0,
        ),
        (
            "一🙂\r\n二\r\n三\r\n四\r\n",
            [
                {"operation": "replace", "start_line": 1, "end_line": 1, "expected_old_text": "一🙂\r\n", "replacement_text": "壹🙂\r\n"},
                {"operation": "replace", "start_line": 4, "end_line": 4, "expected_old_text": "四\r\n", "replacement_text": "肆\r\n"},
            ],
            "壹🙂\r\n二\r\n三\r\n肆\r\n",
            2,
            2,
        ),
        (
            "one\ntwo",
            [{"operation": "replace", "start_line": 2, "end_line": 2, "expected_old_text": "two", "replacement_text": "TWO"}],
            "one\nTWO",
            1,
            1,
        ),
    ],
)
def test_range_edit_returns_minimal_reapplicable_diff(original, operations, expected, added, deleted):
    for item in operations:
        item.update({"path": "x.txt", "expected_blob_sha": "blob-1"})
    service = ReadService(ReadRepo(original.encode(), blob="blob-1"))
    result = mygithub10.edit_ranges(
        service,
        "owner/repo",
        "feature",
        "head-1",
        json.dumps(operations, ensure_ascii=False),
        "edit",
        True,
    )
    assert result["changed_files"][0]["added_lines"] == added
    assert result["changed_files"][0]["deleted_lines"] == deleted
    parsed = mygithub10._parse_patch(result["diff_preview"])
    assert mygithub10._apply_file_patch(
        original.encode(), parsed[0][2], path="x.txt"
    ) == expected.encode()


def test_range_edit_first_line_diff_has_only_bounded_context():
    original = "".join(f"line-{index}\n" for index in range(1, 101))
    operations = [{
        "path": "README.md",
        "operation": "replace",
        "start_line": 1,
        "end_line": 1,
        "expected_blob_sha": "blob-1",
        "expected_old_text": "line-1\n",
        "replacement_text": "new-title\n",
    }]
    result = mygithub10.edit_ranges(
        ReadService(ReadRepo(original.encode())),
        "owner/repo",
        "feature",
        "head-1",
        json.dumps(operations),
        "edit",
        True,
    )
    assert "-line-1\n+new-title\n" in result["diff_preview"]
    assert " line-4\n" in result["diff_preview"]
    assert "line-5" not in result["diff_preview"]
    assert len(result["diff_preview"].splitlines()) < 12


def test_range_edit_preview_truncates_on_utf8_boundary(monkeypatch):
    monkeypatch.setattr(mygithub10, "MAX_INLINE_RESPONSE_BYTES", 90)
    original = "".join(f"旧🙂-{index}\n" for index in range(30))
    operations = [{
        "path": "中文.txt",
        "operation": "replace",
        "start_line": 1,
        "end_line": 30,
        "expected_blob_sha": "blob-1",
        "expected_old_text": original,
        "replacement_text": "".join(f"新🙂-{index}\n" for index in range(30)),
    }]
    result = mygithub10.edit_ranges(
        ReadService(ReadRepo(original.encode())),
        "owner/repo",
        "feature",
        "head-1",
        json.dumps(operations, ensure_ascii=False),
        "edit",
        True,
    )
    assert result["diff_truncated"] is True
    assert len(result["diff_preview"].encode("utf-8")) <= 90
    result["diff_preview"].encode("utf-8").decode("utf-8")


def test_expected_old_text_that_looks_like_sha_is_compared_as_text():
    old_line = "a" * 64 + "\n"
    operations = [{
        "path": "x",
        "operation": "replace",
        "start_line": 1,
        "end_line": 1,
        "expected_blob_sha": "blob-1",
        "expected_old_text": old_line,
        "replacement_text": "new\n",
    }]
    result = mygithub10.edit_ranges(
        ReadService(ReadRepo(old_line.encode())),
        "owner/repo",
        "feature",
        "head-1",
        json.dumps(operations),
        "edit",
        True,
    )
    assert result["dry_run"] is True


def test_range_edit_requires_exact_old_text_and_blob_sha():
    service = ReadService(ReadRepo(b"one\r\ntwo\r\n"))
    ops = [{"path": "x", "operation": "replace", "start_line": 2, "end_line": 2,
            "expected_blob_sha": "wrong", "expected_old_text": "two\r\n", "replacement_text": "二\r\n"}]
    with pytest.raises(mygithub10.MyGithub10Error) as exc:
        mygithub10.edit_ranges(service, "owner/repo", "feature", "head-1", json.dumps(ops), "edit", True)
    assert exc.value.code == "BLOB_CHANGED"


def test_metadata_only_git_delete_supports_tracked_empty_file_and_blob_cas(monkeypatch):
    patch = (
        "diff --git a/empty.txt b/empty.txt\n"
        "deleted file mode 100644\n"
        "index e69de29..0000000\n"
    )
    assert mygithub10._parse_patch(patch) == [("empty.txt", "delete", [])]

    empty_blob = mygithub10._git_blob_sha(b"")
    service = ReadService(ReadRepo(b"", blob=empty_blob))
    dry_run = mygithub10.apply_patch(
        service,
        "owner/repo",
        "feature",
        "head-1",
        json.dumps({"empty.txt": empty_blob}),
        patch,
        "delete empty",
        True,
    )
    assert dry_run["changed_files"] == [{
        "path": "empty.txt",
        "operation": "delete",
        "old_blob_sha": empty_blob,
        "new_blob_sha": None,
        "new_content_sha256": None,
        "added_lines": 0,
        "deleted_lines": 0,
    }]

    with pytest.raises(mygithub10.MyGithub10Error) as exc:
        mygithub10.apply_patch(
            service,
            "owner/repo",
            "feature",
            "head-1",
            json.dumps({"empty.txt": "wrong-blob"}),
            patch,
            "delete empty",
            True,
        )
    assert exc.value.code == "BLOB_CHANGED"

    captured = {}

    def commit_files(_client, _repository, _branch, _head, changed, expected_blobs, _message):
        captured["changed"] = changed
        captured["expected_blobs"] = expected_blobs
        return {
            "commit_sha": "commit-1",
            "old_head_sha": "head-1",
            "new_head_sha": "commit-1",
            "tree_sha": "tree-1",
            "changed_files": [],
        }

    monkeypatch.setattr(mygithub10, "_commit_files", commit_files)
    committed = mygithub10.apply_patch(
        service,
        "owner/repo",
        "feature",
        "head-1",
        json.dumps({"empty.txt": empty_blob}),
        patch,
        "delete empty",
        False,
    )
    assert committed["commit_sha"] == "commit-1"
    assert captured["changed"] == {"empty.txt": None}
    assert captured["expected_blobs"] == {"empty.txt": empty_blob}


def test_metadata_only_git_delete_rejects_non_empty_tracked_file():
    patch = (
        "diff --git a/not-empty.txt b/not-empty.txt\n"
        "deleted file mode 100644\n"
        "index 1234567..0000000\n"
    )
    service = ReadService(ReadRepo(b"content\n", blob="blob-1"))
    with pytest.raises(mygithub10.MyGithub10Error) as exc:
        mygithub10.apply_patch(
            service,
            "owner/repo",
            "feature",
            "head-1",
            json.dumps({"not-empty.txt": "blob-1"}),
            patch,
            "delete non-empty",
            True,
        )
    assert exc.value.code == "PATCH_INVALID_FORMAT"


def test_new_file_patch_accepts_zero_old_range_and_preserves_bytes():
    patch = "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1,2 @@\n+中文🙂\n+\tline2"
    path, operation, hunks = mygithub10._parse_patch(patch)[0]
    assert (path, operation) == ("new.txt", "add")
    assert mygithub10._apply_file_patch(b"", hunks, allow_empty_old=True) == "中文🙂\n\tline2".encode()


def test_zero_old_range_is_rejected_for_modify_patch():
    patch = "--- a/x.txt\n+++ b/x.txt\n@@ -0,0 +1,1 @@\n+new\n"
    with pytest.raises(mygithub10.MyGithub10Error) as exc:
        mygithub10._apply_file_patch(b"old\n", mygithub10._parse_patch(patch)[0][2])
    assert exc.value.code == "PATCH_INVALID_FORMAT"


def test_new_file_patch_rejects_existing_target_and_commits_exact_bytes(monkeypatch):
    repo = MissingRepo(b"")
    service = ReadService(repo)
    captured = {}

    def commit_files(*args):
        captured["changed"] = args[4]
        return {"commit_sha": "c1", "old_head_sha": "h1", "new_head_sha": "c1", "tree_sha": "t1", "changed_files": []}

    monkeypatch.setattr(mygithub10, "_commit_files", commit_files)
    patch = "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1,2 @@\n+line1\n+line2\n"
    result = mygithub10.apply_patch(service, "owner/repo", "feature", "head-1", "{}", patch, "add", False)
    assert result["commit_sha"] == "c1"
    assert captured["changed"]["new.txt"] == b"line1\nline2\n"


def test_idempotency_key_is_scoped_to_full_request(tmp_path, monkeypatch):
    db_path = tmp_path / "ops.db"
    monkeypatch.setattr(mygithub10.settings, "IDEMPOTENCY_DB_PATH", str(db_path))
    operation_id, replay = mygithub10._idempotent_start(
        "edit_github_file_ranges", "same-key",
        {"tool_name": "edit_github_file_ranges", "repository": "a/r", "branch": "one", "expected_head_sha": "h1", "operations_sha256": "p1", "commit_message": "m"},
    )
    assert operation_id and replay is None
    mygithub10._idempotent_finish(operation_id, "succeeded", "commit-1")
    with pytest.raises(mygithub10.MyGithub10Error) as exc:
        mygithub10._idempotent_start(
            "edit_github_file_ranges", "same-key",
            {"tool_name": "edit_github_file_ranges", "repository": "a/r", "branch": "two", "expected_head_sha": "h1", "operations_sha256": "p1", "commit_message": "m"},
        )
    assert exc.value.code == "IDEMPOTENCY_CONFLICT"


def test_idempotency_replays_complete_result_without_remote_read(tmp_path, monkeypatch):
    monkeypatch.setattr(mygithub10.settings, "IDEMPOTENCY_DB_PATH", str(tmp_path / "replay.db"))
    request = {"tool_name": "edit_github_file_ranges", "repository": "a/r", "branch": "one", "expected_head_sha": "h1", "operations": [{"path": "x", "operation": "replace"}], "commit_message": "m"}
    operation_id, replay = mygithub10._idempotent_start("edit_github_file_ranges", "replay-key", request)
    assert operation_id and replay is None
    original = {"ok": True, "commit_sha": "commit-1", "old_head_sha": "h1", "new_head_sha": "commit-1", "changed_files": [{"path": "x", "content_sha256": "abc", "size_bytes": 3}]}
    mygithub10._idempotent_finish(operation_id, "succeeded", "commit-1", result=original)
    operation_id, replay = mygithub10._idempotent_start("edit_github_file_ranges", "replay-key", request)
    assert operation_id == "replay"
    assert replay["commit_sha"] == "commit-1"
    assert replay["replayed"] is True


def test_upload_replay_survives_body_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr(mygithub10, "_UPLOAD_ROOT", tmp_path / "uploads")
    monkeypatch.setattr(mygithub10.settings, "IDEMPOTENCY_DB_PATH", str(tmp_path / "upload-ops.db"))
    upload = mygithub10.begin_upload()
    data = b"upload content\n"
    mygithub10.append_upload(upload["upload_id"], 0, data, digest(data.decode()))
    mygithub10.finalize_upload(upload["upload_id"], len(data), digest(data.decode()))
    result = {"ok": True, "commit_sha": "upload-commit", "old_head_sha": "h1", "new_head_sha": "upload-commit", "tree_sha": "t1", "changed_files": [{"path": "upload.txt", "content_sha256": digest(data.decode()), "size_bytes": len(data)}], "upload_id": upload["upload_id"]}
    monkeypatch.setattr(mygithub10, "_commit_files", lambda *args: {key: value for key, value in result.items() if key != "ok" and key != "upload_id"})
    service = ReadService(ReadRepo(b""))
    first = mygithub10.commit_upload(service, "owner/repo", "feature", "head-1", "upload.txt", "", upload["upload_id"], "upload", "upload-key")
    assert first["commit_sha"] == "upload-commit"
    assert not mygithub10._upload_paths(upload["upload_id"])[0].exists()
    replay = mygithub10.commit_upload(service, "owner/repo", "feature", "head-1", "upload.txt", "", upload["upload_id"], "upload", "upload-key")
    assert replay["commit_sha"] == "upload-commit"
    assert replay["replayed"] is True


def test_concurrent_same_idempotency_key_has_one_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(mygithub10.settings, "IDEMPOTENCY_DB_PATH", str(tmp_path / "concurrent.db"))
    request = {"tool_name": "apply_github_patch", "repository": "a/r", "branch": "one", "expected_head_sha": "h1", "patch_sha256": "p1", "commit_message": "m"}

    def claim():
        try:
            return mygithub10._idempotent_start("apply_github_patch", "concurrent-key", request)[0]
        except mygithub10.MyGithub10Error as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: claim(), range(2)))
    assert sum(isinstance(value, str) and value not in {"IDEMPOTENCY_IN_PROGRESS", "IDEMPOTENCY_CONFLICT"} for value in results) == 1
    assert "IDEMPOTENCY_IN_PROGRESS" in results
