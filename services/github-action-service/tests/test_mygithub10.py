import base64
import hashlib
import json
from types import SimpleNamespace

import pytest

from app import mygithub10


class FakeRepo:
    def __init__(self, data: bytes, blob_sha: str = "blob-1", commit_sha: str = "head-1"):
        self.data = data
        self.blob_sha = blob_sha
        self.commit_sha = commit_sha
        self.default_branch = "main"

    def get_commit(self, ref):
        return SimpleNamespace(sha=self.commit_sha, commit=SimpleNamespace(tree=SimpleNamespace(sha="tree-1")))

    def get_contents(self, path, ref=None):
        return SimpleNamespace(sha=self.blob_sha, size=len(self.data))

    def get_git_blob(self, sha):
        return SimpleNamespace(encoding="base64", content=base64.b64encode(self.data).decode())


class FakeRaw:
    def __init__(self, repo):
        self._pygithub = SimpleNamespace(get_repo=lambda name: repo)


class FakeService:
    def __init__(self, repo):
        self.client = FakeRaw(repo)

    def _check_repository_allowed(self, repository):
        return None

    def _check_default_branch_write(self, repository, branch):
        return None


def test_manifest_and_reassembly_for_520kb_chinese_utf8():
    data = ("中文行🙂\n" * 60000).encode("utf-8")
    service = FakeService(FakeRepo(data))
    manifest = mygithub10.file_manifest(service, "owner/repo", "large.txt", "main")
    assert manifest["size_bytes"] == len(data)
    assert manifest["content_sha256"] == hashlib.sha256(data).hexdigest()
    chunks = []
    offset = 0
    while True:
        item = mygithub10.file_chunk(service, "owner/repo", "large.txt", "main", offset, 65536, "blob-1")
        chunks.append(item["content"].encode())
        if item["eof"]:
            break
        offset = item["next_offset"]
    rebuilt = b"".join(chunks)
    assert rebuilt == data
    assert hashlib.sha256(rebuilt).hexdigest() == manifest["content_sha256"]


def test_json_crlf_emoji_and_no_final_newline_manifest():
    data = (json.dumps({"text": "😀"}, ensure_ascii=False) * 50000).replace("}", "}\r\n").encode()
    service = FakeService(FakeRepo(data))
    manifest = mygithub10.file_manifest(service, "owner/repo", "data.json", "main")
    assert manifest["eol"] == "CRLF"
    assert manifest["ends_with_newline"]
    assert mygithub10.file_chunk(service, "owner/repo", "data.json", "main", 0, 65536)["eof"] is False


def test_strict_patch_dry_run_and_range_dry_run():
    original = b"one\ntwo\nthree\n"
    service = FakeService(FakeRepo(original))
    patch = "--- a/file.txt\n+++ b/file.txt\n@@ -1,3 +1,3 @@\n one\n-two\n+TWO\n three\n"
    result = mygithub10.apply_patch(service, "owner/repo", "main", "head-1", "{}", patch, "change", True)
    assert result["dry_run"] is True
    assert result["changed_files"][0]["path"] == "file.txt"
    old_hash = hashlib.sha256(b"two\n").hexdigest()
    ranges = [{"path": "file.txt", "operation": "replace", "start_line": 2, "end_line": 2, "expected_old_text_sha256": old_hash, "replacement": "TWO\n"}]
    range_result = mygithub10.edit_ranges(service, "owner/repo", "main", "head-1", json.dumps(ranges), "change", True)
    assert range_result["dry_run"] is True


def test_patch_dry_run_rejects_wrong_expected_blob_sha():
    service = FakeService(FakeRepo(b"one\ntwo\n"))
    patch = "--- a/file.txt\n+++ b/file.txt\n@@ -1,2 +1,2 @@\n one\n-two\n+TWO\n"
    with pytest.raises(mygithub10.MyGithub10Error) as exc:
        mygithub10.apply_patch(service, "owner/repo", "main", "head-1", json.dumps({"file.txt": "wrong-blob"}), patch, "change", True)
    assert exc.value.code == "PATCH_FILE_CHANGED"


def test_patch_commit_uses_pygithub_git_commit_tree(monkeypatch):
    class Ref:
        object = SimpleNamespace(sha="head-1")

        def edit(self, sha, force):
            assert sha == "commit-1"
            assert force is False

    class Repo(FakeRepo):
        def get_git_ref(self, name):
            assert name == "heads/main"
            return Ref()

        def get_git_commit(self, sha):
            return SimpleNamespace(tree=SimpleNamespace(sha="tree-1"))

    class Client(FakeRaw):
        def create_blob(self, repository, content):
            return SimpleNamespace(sha="blob-2")

        def create_git_tree(self, repository, elements, base_tree):
            assert base_tree == "tree-1"
            return SimpleNamespace(sha="tree-2")

        def create_commit(self, repository, message, tree_sha, parents):
            assert tree_sha == "tree-2"
            assert parents == ["head-1"]
            return SimpleNamespace(sha="commit-1")

    repo = Repo(b"one\ntwo\n")
    service = SimpleNamespace(client=Client(repo), _check_repository_allowed=lambda _: None, _check_default_branch_write=lambda *_: None)
    monkeypatch.setattr(mygithub10, "_repo", lambda *_: repo)
    result = mygithub10._commit_files(service, "owner/repo", "main", "head-1", {"file.txt": b"updated\n"}, {}, "change")
    assert result["commit_sha"] == "commit-1"


def test_invalid_utf8_boundary_is_rejected():
    service = FakeService(FakeRepo("😀".encode()))
    with pytest.raises(mygithub10.MyGithub10Error) as exc:
        mygithub10.file_chunk(service, "owner/repo", "emoji.txt", "main", 1, 10)
    assert exc.value.code == "FILE_UTF8_BOUNDARY_INVALID"
