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


class ReadService:
    def __init__(self, repo):
        self.client = SimpleNamespace(_pygithub=SimpleNamespace(get_repo=lambda _: repo))

    def _check_repository_allowed(self, _):
        return None

    def _check_default_branch_write(self, _, __):
        return None


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
