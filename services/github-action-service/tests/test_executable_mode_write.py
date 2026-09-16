from types import SimpleNamespace

import pytest

from app import development_orchestrator as dx
from app import mygithub10, mygithub12


HEAD = "a" * 40
OTHER_HEAD = "b" * 40
BLOB = "c" * 40
OTHER_BLOB = "d" * 40
TREE = "e" * 40
COMMIT = "f" * 40


class FakeRef:
    def __init__(self, client):
        self.client = client
        self.object = SimpleNamespace(sha=client.head)

    def edit(self, sha, force=False):
        assert force is False
        self.client.head = sha
        self.object.sha = sha


class FakeRepo:
    def __init__(self, client):
        self.client = client
        self.ref = FakeRef(client)

    def get_git_ref(self, _name):
        self.ref.object.sha = self.client.head
        return self.ref

    def get_contents(self, _path, ref=None):
        return SimpleNamespace(sha=self.client.blob, size=len(self.client.content))

    def get_git_commit(self, _sha):
        return SimpleNamespace(tree=SimpleNamespace(sha="0" * 40))


class FakeClient:
    def __init__(self, *, head=HEAD, blob=BLOB, mode="100644", observed_mode=None):
        self.head = head
        self.blob = blob
        self.mode = mode
        self.observed_mode = observed_mode
        self.content = b"#!/bin/sh\n"
        self.repo = FakeRepo(self)
        self._pygithub = SimpleNamespace(get_repo=lambda _name: self.repo)
        self.created_blobs = []
        self.created_trees = []
        self.created_commits = []

    def create_blob(self, _repository, content):
        self.created_blobs.append(content)
        self.content = content.encode()
        self.blob = OTHER_BLOB
        return SimpleNamespace(sha=OTHER_BLOB)

    def get_branch(self, _repository, _branch):
        return SimpleNamespace(commit=SimpleNamespace(sha=self.head))

    def create_git_tree(self, _repository, elements, base_tree_sha=""):
        self.created_trees.append((elements, base_tree_sha))
        element = elements[0]
        self.mode = element["mode"]
        if element["sha"]:
            self.blob = element["sha"]
        return SimpleNamespace(sha=TREE)

    def create_commit(self, _repository, message, tree_sha, parents):
        self.created_commits.append((message, tree_sha, parents))
        return SimpleNamespace(sha=COMMIT)

    def get_file_mode_fresh(self, _repository, _path, _ref):
        return self.observed_mode if self.observed_mode is not None else self.mode

    def get_branch_head_fresh(self, _repository, _branch):
        return self.head

    def get_commit_state_fresh(self, _repository, sha):
        return {"commit_sha": sha, "tree_sha": TREE}

    def get_tree_sha_fresh(self, _repository, sha):
        return sha

    def get_file_sha_fresh(self, _repository, _path, _ref):
        return self.blob

    def get_file(self, _repository, _path, _ref=""):
        return self.content.decode(), self.blob, len(self.content)


class FakeService:
    def __init__(self, client):
        self.client = client

    def _check_repository_allowed(self, _repository):
        return None

    def _check_default_branch_write(self, _repository, _branch):
        return None


def _change(executable):
    return [{"path": "scripts/run.sh", "expected_blob_sha": BLOB, "executable": executable}]


def test_mode_only_commit_promotes_100644_to_100755_without_new_blob():
    client = FakeClient(mode="100644")
    result = mygithub10.set_file_modes(
        FakeService(client), "owner/repo", "ai/mode", HEAD, _change(True), "chmod +x", False
    )
    assert result["write_verified"] is True
    assert result["verified_paths"] == [{"path": "scripts/run.sh", "blob_sha": BLOB, "mode": "100755"}]
    assert result["changed_files"] == [{
        "path": "scripts/run.sh", "operation": "mode_change", "old_blob_sha": BLOB,
        "new_blob_sha": BLOB, "old_mode": "100644", "new_mode": "100755",
        "blob_identity_preserved": True,
    }]
    assert client.created_blobs == []
    assert client.created_trees[0][0][0] == {
        "path": "scripts/run.sh", "mode": "100755", "type": "blob", "sha": BLOB,
    }


def test_mode_only_commit_demotes_100755_to_100644():
    client = FakeClient(mode="100755")
    result = mygithub10.set_file_modes(
        FakeService(client), "owner/repo", "ai/mode", HEAD, _change(False), "chmod -x", False
    )
    assert result["changed_files"][0]["old_mode"] == "100755"
    assert result["changed_files"][0]["new_mode"] == "100644"
    assert result["changed_files"][0]["old_blob_sha"] == result["changed_files"][0]["new_blob_sha"] == BLOB


def test_content_write_preserves_existing_executable_mode():
    client = FakeClient(mode="100755")
    result = mygithub10._commit_files(
        FakeService(client), "owner/repo", "ai/mode", HEAD,
        {"scripts/run.sh": b"#!/bin/sh\necho ok\n"}, {"scripts/run.sh": BLOB}, "edit script",
    )
    assert client.created_trees[0][0][0]["mode"] == "100755"
    assert result["changed_files"][0]["old_mode"] == "100755"
    assert result["changed_files"][0]["new_mode"] == "100755"
    assert result["verified_paths"][0]["mode"] == "100755"


def test_generated_file_write_preserves_existing_executable_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(mygithub10, "_UPLOAD_ROOT", tmp_path / "uploads")
    client = FakeClient(mode="100755")
    prepared = mygithub10.prepare_generated_files([
        {"path": "scripts/run.sh", "content": "#!/bin/sh\necho generated\n"},
    ])
    result = mygithub10.execute_put_files(
        FakeService(client),
        "owner/repo",
        "ai/mode",
        HEAD,
        prepared,
        "update generated script",
        False,
        infer_expected_blob_shas=True,
        canonical_payload_hash="1" * 64,
    )
    assert result["write_verified"] is True
    assert client.created_trees[0][0][0]["mode"] == "100755"
    assert result["changed_files"][0]["old_mode"] == "100755"
    assert result["changed_files"][0]["new_mode"] == "100755"
    assert result["verified_paths"] == [{
        "path": "scripts/run.sh", "blob_sha": OTHER_BLOB, "mode": "100755",
    }]


@pytest.mark.parametrize(
    ("client", "expected_blob", "code"),
    [
        (FakeClient(head=OTHER_HEAD), BLOB, "PATCH_HEAD_CHANGED"),
        (FakeClient(blob=OTHER_BLOB), BLOB, "BLOB_CHANGED"),
    ],
)
def test_mode_write_concurrent_head_or_blob_change_fails_closed(client, expected_blob, code):
    change = [{"path": "scripts/run.sh", "expected_blob_sha": expected_blob, "executable": True}]
    with pytest.raises(mygithub10.MyGithub10Error) as exc:
        mygithub10.set_file_modes(
            FakeService(client), "owner/repo", "ai/mode", HEAD, change, "chmod +x", False
        )
    assert exc.value.code == code
    assert client.created_commits == []


def test_mode_readback_mismatch_fails_closed():
    client = FakeClient(mode="100644", observed_mode="100644")
    with pytest.raises(mygithub10.MyGithub10Error) as exc:
        mygithub10.set_file_modes(
            FakeService(client), "owner/repo", "ai/mode", HEAD, _change(True), "chmod +x", False
        )
    assert exc.value.code == "WRITE_VERIFY_FAILED"
    assert exc.value.details["failed_stage"] == "path_mode_readback"


def test_mode_change_set_schema_requires_exact_blob_and_boolean():
    parsed = dx.parse_change_set(
        '{"schema_version":1,"mode":"file_mode","file_modes":[{"path":"scripts/run.sh","expected_blob_sha":"' + BLOB + '","executable":true}]}'
    )
    assert parsed["mode"] == "file_mode"
    with pytest.raises(dx.MyGithub12Error):
        dx.parse_change_set('{"schema_version":1,"mode":"file_mode","file_modes":[{"path":"scripts/run.sh","executable":true}]}')


def test_file_mode_change_set_routes_through_strict_writer(monkeypatch):
    parsed = dx.parse_change_set(
        '{"schema_version":1,"mode":"file_mode","file_modes":[{"path":"scripts/run.sh","expected_blob_sha":"' + BLOB + '","executable":true}]}'
    )
    captured = {}

    def fake_set_file_modes(*args):
        captured["args"] = args
        return {"ok": True, "dry_run": True, "changed_files": [{"path": "scripts/run.sh", "old_blob_sha": BLOB}]}

    monkeypatch.setattr(mygithub10, "set_file_modes", fake_set_file_modes)
    session = {"repository": "owner/repo", "branch": "ai/mode"}
    result = dx.execute_change_set(
        object(), session, {"workspace_id": "ws-mode"}, parsed, HEAD, 4,
        "chmod +x", True, "mode-key", {"workspace_id": "ws-mode", "workspace_revision": 4},
    )
    assert captured["args"][3] == HEAD
    assert captured["args"][4] == parsed["change"]["file_modes"]
    assert captured["args"][6] is True
    assert result["change_set_canonical_hash"] == parsed["canonical_hash"]


def test_mode_write_idempotency_replays_verified_result(tmp_path, monkeypatch):
    monkeypatch.setattr(mygithub10.settings, "IDEMPOTENCY_DB_PATH", str(tmp_path / "idempotency.db"))
    client = FakeClient(mode="100644")
    service = FakeService(client)
    first = mygithub10.set_file_modes(
        service, "owner/repo", "ai/mode", HEAD, _change(True), "chmod +x", False, "mode-key"
    )
    operation_id = first.pop("_operation_id")
    mygithub10._idempotent_finish(operation_id, "success_verified", COMMIT, result=first)
    replay = mygithub10.set_file_modes(
        service, "owner/repo", "ai/mode", HEAD, _change(True), "chmod +x", False, "mode-key"
    )
    assert replay["replayed"] is True
    assert len(client.created_commits) == 1


def _seed_workspace(tmp_path, monkeypatch, *, revision=4, lease_valid=True):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "workspace.db"))
    mygithub12.init_db()
    now = mygithub12._now()
    with mygithub12._db() as db:
        db.execute(
            "INSERT INTO workspaces VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "ws-mode", "owner/repo", "ai/mode", "main", HEAD, HEAD, TREE,
                "active", revision, "writer", now + 600 if lease_valid else now - 1,
                HEAD, "{}", None, None, now, now,
            ),
        )


def test_mode_writer_workspace_revision_cas_fails_closed(tmp_path, monkeypatch):
    _seed_workspace(tmp_path, monkeypatch, revision=4)
    with pytest.raises(mygithub12.MyGithub12Error) as exc:
        mygithub12.workspace_write_preflight(
            FakeService(FakeClient()), "owner/repo", "ai/mode", HEAD, "ws-mode", 3
        )
    assert exc.value.code == "WORKSPACE_REVISION_MISMATCH"


def test_mode_writer_requires_current_writer_lease(tmp_path, monkeypatch):
    _seed_workspace(tmp_path, monkeypatch, lease_valid=False)
    with pytest.raises(mygithub12.MyGithub12Error) as exc:
        mygithub12.workspace_write_preflight(
            FakeService(FakeClient()), "owner/repo", "ai/mode", HEAD, "ws-mode", 4
        )
    assert exc.value.code == "WORKSPACE_LEASE_REQUIRED"
