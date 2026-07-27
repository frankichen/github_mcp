import base64
import hashlib
import json
from types import SimpleNamespace

import pytest

from app import mygithub10


class FakeRef:
    def __init__(self, sha="head-1"):
        self.object = SimpleNamespace(sha=sha)
        self.edits = []

    def edit(self, sha, force=False):
        self.edits.append((sha, force))
        self.object.sha = sha


class FakeRepo:
    def __init__(self, data=b"one\ntwo\nthree\n", blob_sha="blob-1", head_sha="head-1"):
        self.data = data
        self.blob_sha = blob_sha
        self.head_sha = head_sha
        self.default_branch = "main"
        self.ref = FakeRef(head_sha)

    def get_commit(self, _ref):
        return SimpleNamespace(sha=self.head_sha)

    def get_contents(self, _path, ref=None):
        return SimpleNamespace(sha=self.blob_sha, size=len(self.data))

    def get_git_blob(self, _sha):
        return SimpleNamespace(encoding="base64", content=base64.b64encode(self.data).decode())

    def get_git_ref(self, _ref):
        return self.ref

    def get_git_commit(self, _sha):
        # Deliberately no `.commit` attribute.  PyGithub exposes `.tree` directly.
        return SimpleNamespace(tree=SimpleNamespace(sha="base-tree"))


class FakeGitHubClient:
    def __init__(self, repo):
        self.repo = repo
        self._pygithub = SimpleNamespace(get_repo=lambda _name: repo)
        self.created_blobs = []
        self.created_trees = []
        self.created_commits = []

    def create_blob(self, _repository, content):
        self.created_blobs.append(content)
        return SimpleNamespace(sha="new-blob")

    def create_git_tree(self, _repository, elements, base_tree_sha=""):
        self.created_trees.append((elements, base_tree_sha))
        return SimpleNamespace(sha="new-tree")

    def create_commit(self, _repository, message, tree_sha, parents):
        self.created_commits.append((message, tree_sha, parents))
        return SimpleNamespace(sha="new-commit")


class FakeService:
    def __init__(self, repo):
        self.client = FakeGitHubClient(repo)

    def _check_repository_allowed(self, _repository):
        return None

    def _check_default_branch_write(self, _repository, _branch):
        return None


def _replace_line_two():
    return [{
        "path": "file.txt",
        "operation": "replace",
        "start_line": 2,
        "end_line": 2,
        "expected_old_text_sha256": hashlib.sha256(b"two\n").hexdigest(),
        "replacement": "TWO\n",
    }]


def test_runtime_fix_is_installed_on_app_import():
    assert mygithub10._runtime_strict_write_fix_installed is True
    assert mygithub10.edit_ranges.__module__ == "app.mygithub10_runtime_fix"


def test_formal_range_write_uses_direct_gitcommit_tree_and_non_forced_ref_update():
    repo = FakeRepo()
    service = FakeService(repo)

    result = mygithub10.edit_ranges(
        service,
        "owner/repo",
        "feature",
        "head-1",
        json.dumps(_replace_line_two()),
        "replace line two",
        False,
    )

    assert result["commit_sha"] == "new-commit"
    assert service.client.created_blobs == ["one\nTWO\nthree\n"]
    assert service.client.created_trees[0][1] == "base-tree"
    assert service.client.created_commits[0] == ("replace line two", "new-tree", ["head-1"])
    assert repo.ref.edits == [("new-commit", False)]


def test_insert_before_and_after_preserve_anchor_lines():
    service = FakeService(FakeRepo())
    operations = [
        {"path": "file.txt", "operation": "insert_before", "start_line": 2, "replacement": "before\n"},
        {"path": "file.txt", "operation": "insert_after", "start_line": 3, "end_line": 3, "replacement": "after\n"},
    ]

    result = mygithub10.edit_ranges(
        service, "owner/repo", "feature", "head-1",
        json.dumps(operations), "insert", True,
    )

    expected = b"one\nbefore\ntwo\nthree\nafter\n"
    assert result["new_content_sha256"]["file.txt"] == hashlib.sha256(expected).hexdigest()
    assert result["resolved_head_sha"] == "head-1"


def test_insert_before_line_count_plus_one_appends():
    service = FakeService(FakeRepo(data=b"one\ntwo\n"))
    operations = [{
        "path": "file.txt",
        "operation": "insert_before",
        "start_line": 3,
        "replacement": "three\n",
    }]
    result = mygithub10.edit_ranges(
        service, "owner/repo", "feature", "head-1",
        json.dumps(operations), "append", True,
    )
    assert result["new_content_sha256"]["file.txt"] == hashlib.sha256(b"one\ntwo\nthree\n").hexdigest()


def test_dry_run_rejects_stale_head_before_calculating_changes():
    service = FakeService(FakeRepo(head_sha="actual-head"))
    with pytest.raises(mygithub10.MyGithub10Error) as exc:
        mygithub10.edit_ranges(
            service, "owner/repo", "feature", "stale-head",
            json.dumps(_replace_line_two()), "stale", True,
        )
    assert exc.value.code == "PATCH_HEAD_CHANGED"
    assert exc.value.details == {"expected": "stale-head", "actual": "actual-head"}


def test_inclusive_ranges_sharing_a_line_are_rejected():
    service = FakeService(FakeRepo())
    operations = [
        {
            "path": "file.txt",
            "operation": "replace",
            "start_line": 1,
            "end_line": 2,
            "expected_old_text_sha256": hashlib.sha256(b"one\ntwo\n").hexdigest(),
            "replacement": "ONE\nTWO\n",
        },
        {
            "path": "file.txt",
            "operation": "delete",
            "start_line": 2,
            "end_line": 3,
            "expected_old_text_sha256": hashlib.sha256(b"two\nthree\n").hexdigest(),
        },
    ]
    with pytest.raises(mygithub10.MyGithub10Error) as exc:
        mygithub10.edit_ranges(
            service, "owner/repo", "feature", "head-1",
            json.dumps(operations), "overlap", True,
        )
    assert exc.value.code == "PATCH_SCOPE_EXCEEDED"


def test_duplicate_insert_boundary_is_rejected_as_ambiguous():
    service = FakeService(FakeRepo())
    operations = [
        {"path": "file.txt", "operation": "insert_before", "start_line": 2, "replacement": "a\n"},
        {"path": "file.txt", "operation": "insert_after", "start_line": 1, "replacement": "b\n"},
    ]
    with pytest.raises(mygithub10.MyGithub10Error) as exc:
        mygithub10.edit_ranges(
            service, "owner/repo", "feature", "head-1",
            json.dumps(operations), "ambiguous", True,
        )
    assert exc.value.code == "PATCH_SCOPE_EXCEEDED"


@pytest.mark.parametrize("payload,code", [
    ("not-json", "PATCH_INVALID_FORMAT"),
    ("{}", "PATCH_EMPTY"),
    ("[]", "PATCH_EMPTY"),
])
def test_invalid_operations_payloads_have_stable_errors(payload, code):
    service = FakeService(FakeRepo())
    with pytest.raises(mygithub10.MyGithub10Error) as exc:
        mygithub10.edit_ranges(
            service, "owner/repo", "feature", "head-1", payload, "invalid", True,
        )
    assert exc.value.code == code


def test_replace_requires_lowercase_sha256_old_text_hash():
    service = FakeService(FakeRepo())
    operation = [{
        "path": "file.txt",
        "operation": "replace",
        "start_line": 1,
        "replacement": "ONE\n",
    }]
    with pytest.raises(mygithub10.MyGithub10Error) as exc:
        mygithub10.edit_ranges(
            service, "owner/repo", "feature", "head-1",
            json.dumps(operation), "invalid hash", True,
        )
    assert exc.value.code == "PATCH_INVALID_FORMAT"
