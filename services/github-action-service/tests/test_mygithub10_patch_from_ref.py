"""Focused regression coverage for patches stored in GitHub ref blobs."""

import base64
import hashlib
import json
from types import SimpleNamespace

import pytest

from app import mygithub10
from app.config import settings
from app.github_policy import ensure_repository_allowed


def blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


class Repo:
    default_branch = "main"

    def __init__(self, files, commit_sha="commit-1"):
        self.files = files
        self.commit_sha = commit_sha
        self.commits = 0

    def get_commit(self, ref):
        return SimpleNamespace(sha=self.commit_sha)

    def get_contents(self, path, ref=None):
        if path not in self.files:
            error = RuntimeError("not found")
            error.status = 404
            raise error
        data = self.files[path]
        return SimpleNamespace(sha=blob_sha(data), size=len(data))

    def get_git_blob(self, sha):
        for data in self.files.values():
            if blob_sha(data) == sha:
                return SimpleNamespace(encoding="base64", content=base64.b64encode(data).decode())
        error = RuntimeError("not found")
        error.status = 404
        raise error


class Service:
    def __init__(self, repo):
        self.client = SimpleNamespace(_pygithub=SimpleNamespace(get_repo=lambda _: repo))


class RefsRepo(Repo):
    """Small ref-aware fake for source refs and target branches."""

    def __init__(self, snapshots, refs):
        self.snapshots = snapshots
        self.refs = refs
        self.files = snapshots[refs.get("main", next(iter(refs)))]
        self.commit_sha = refs.get("main", next(iter(refs)))

    def get_commit(self, ref):
        sha = self.refs.get(ref, ref)
        if sha not in self.snapshots:
            error = RuntimeError("not found")
            error.status = 404
            raise error
        return SimpleNamespace(sha=sha, tree=SimpleNamespace(sha=f"tree-{sha}"))

    def get_contents(self, path, ref=None):
        sha = self.refs.get(ref, ref) if ref else self.commit_sha
        files = self.snapshots[sha]
        if path not in files:
            error = RuntimeError("not found")
            error.status = 404
            raise error
        data = files[path]
        return SimpleNamespace(sha=blob_sha(data), size=len(data))

    def get_git_blob(self, sha):
        for files in self.snapshots.values():
            for data in files.values():
                if blob_sha(data) == sha:
                    return SimpleNamespace(encoding="base64", content=base64.b64encode(data).decode())
        error = RuntimeError("not found")
        error.status = 404
        raise error


class RefsService:
    def __init__(self, repos):
        self.repos = repos
        self.client = SimpleNamespace(_pygithub=SimpleNamespace(get_repo=lambda name: repos[name]))

    def _check_repository_allowed(self, repository):
        ensure_repository_allowed(repository)


PATCH = b"diff --git a/file.txt b/file.txt\n--- a/file.txt\n+++ b/file.txt\n@@ -1 +1 @@\n-old\n+new\n"


def reference_service(source_data=PATCH, source_commit="source-1", target_head="target-1"):
    source = RefsRepo({source_commit: {"patch.diff": source_data}}, {"main": source_commit})
    target = RefsRepo({target_head: {"file.txt": b"old\n"}}, {"main": target_head})
    return RefsService({"owner/allowed-repo": source, "owner/target": target}), source, target


def identity(data: bytes):
    return blob_sha(data), hashlib.sha256(data).hexdigest(), len(data)


def resolve(service, data, path="patch.diff", ref="main"):
    sha, digest, size = identity(data)
    return mygithub10.resolve_patch_from_ref(service, "owner/allowed-repo", ref, path, sha, digest, size)


@pytest.fixture(autouse=True)
def allowed_repository(monkeypatch):
    monkeypatch.setattr(settings, "ALLOWED_REPOSITORIES", "owner/allowed-repo,owner/target")


def test_single_file_ref_patch_and_exact_context_bytes():
    data = b"diff --git a/file.txt b/file.txt\n--- a/file.txt\n+++ b/file.txt\n@@ -1,2 +1,2 @@\n context\tvalue\n-old\n+new\n"
    patch, item = resolve(Service(Repo({"patch.diff": data})), data)
    assert patch.encode() == data
    assert item["patch_blob_sha"] == identity(data)[0]
    parsed, _ = mygithub10._parse_patch_details(patch)
    assert parsed[0][2][0][2] == ["context\tvalue\n", "old\n"]


def test_two_file_and_add_modify_patch():
    data = """diff --git a/one.txt b/one.txt
--- a/one.txt
+++ b/one.txt
@@ -1 +1 @@
-old
+new
diff --git a/two.txt b/two.txt
new file mode 100644
--- /dev/null
+++ b/two.txt
@@ -0,0 +1 @@
+Chinese 中文
""".encode()
    parsed, _ = mygithub10._parse_patch_details(resolve(Service(Repo({"patch.diff": data})), data)[0])
    assert [(item[0], item[1]) for item in parsed] == [("one.txt", "modify"), ("two.txt", "add")]


def test_thirty_one_file_patch_parses():
    blocks = []
    for index in range(31):
        blocks.append(f"diff --git a/f{index}.txt b/f{index}.txt\n--- /dev/null\n+++ b/f{index}.txt\n@@ -0,0 +1 @@\n+v{index}\n")
    data = "".join(blocks).encode()
    parsed, _ = mygithub10._parse_patch_details(resolve(Service(Repo({"patch.diff": data})), data)[0])
    assert len(parsed) == 31


@pytest.mark.parametrize("data,code", [
    (b"x", "PATCH_SOURCE_BLOB_CHANGED"),
    (b"x", "PATCH_SOURCE_HASH_MISMATCH"),
    (b"x", "PATCH_SOURCE_SIZE_MISMATCH"),
])
def test_expected_artifact_mismatch(data, code):
    service = Service(Repo({"patch.diff": data}))
    sha, digest, size = identity(data)
    kwargs = {"expected_patch_blob_sha": sha, "expected_patch_sha256": digest, "expected_patch_size_bytes": size}
    if code == "PATCH_SOURCE_BLOB_CHANGED": kwargs["expected_patch_blob_sha"] = "0" * 40
    if code == "PATCH_SOURCE_HASH_MISMATCH": kwargs["expected_patch_sha256"] = "0" * 64
    if code == "PATCH_SOURCE_SIZE_MISMATCH": kwargs["expected_patch_size_bytes"] = size + 1
    with pytest.raises(mygithub10.MyGithub10Error) as exc:
        mygithub10.resolve_patch_from_ref(service, "owner/allowed-repo", "main", "patch.diff", **kwargs)
    assert exc.value.code == code


@pytest.mark.parametrize("path", ["../patch.diff", "/patch.diff", "dir\\patch.diff"])
def test_source_path_safety(path):
    data = b"patch"
    sha, digest, size = identity(data)
    with pytest.raises(mygithub10.MyGithub10Error) as exc:
        mygithub10.resolve_patch_from_ref(Service(Repo({path: data})), "owner/allowed-repo", "main", path, sha, digest, size)
    assert exc.value.code == "PATCH_SOURCE_UNSAFE_PATH"


def test_source_repo_auth_and_invalid_utf8():
    data = b"\xff"
    sha, digest, size = identity(data)
    with pytest.raises(mygithub10.MyGithub10Error) as exc:
        mygithub10.resolve_patch_from_ref(Service(Repo({"patch.diff": data})), "owner/other", "main", "patch.diff", sha, digest, size)
    assert exc.value.code == "PATCH_SOURCE_REPOSITORY_NOT_ALLOWED"
    with pytest.raises(mygithub10.MyGithub10Error) as exc:
        mygithub10.resolve_patch_from_ref(Service(Repo({"patch.diff": data})), "owner/allowed-repo", "main", "patch.diff", sha, digest, size)
    assert exc.value.code == "PATCH_SOURCE_UTF8_INVALID"


def test_zero_size_is_compared_not_treated_as_missing():
    data = b""
    sha, digest, _ = identity(data)
    with pytest.raises(mygithub10.MyGithub10Error) as exc:
        mygithub10.resolve_patch_from_ref(Service(Repo({"patch.diff": data})), "owner/allowed-repo", "main", "patch.diff", sha, digest, 1)
    assert exc.value.code == "PATCH_SOURCE_SIZE_MISMATCH"


def test_no_final_newline_is_preserved():
    data = b"diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1 +1 @@\n-old\n+new"
    patch, _ = resolve(Service(Repo({"patch.diff": data})), data)
    assert patch.encode() == data
    assert not patch.endswith("\n")
    parsed, _ = mygithub10._parse_patch_details(patch)
    assert parsed[0][2][0][2] == ["old\n"]
    assert parsed[0][2][0][3] == ["new"]


def test_leading_space_context_and_tab_reach_strict_parser():
    data = b"diff --git a/file.txt b/file.txt\n--- a/file.txt\n+++ b/file.txt\n@@ -1,2 +1,2 @@\n context\tvalue\n-old\n+new\n"
    patch, _ = resolve(Service(Repo({"patch.diff": data})), data)
    assert patch.encode() == data
    parsed, _ = mygithub10._parse_patch_details(patch)
    assert parsed[0][2][0][2] == ["context\tvalue\n", "old\n"]


def test_apply_patch_from_ref_dry_run_uses_source_and_never_commits(monkeypatch):
    service, _, _ = reference_service()
    called = False

    def fail_commit(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("dry-run must not commit")

    monkeypatch.setattr(mygithub10, "_commit_files", fail_commit)
    result = mygithub10.apply_patch_from_ref(
        service, "owner/target", "main", "target-1", '{"file.txt":"%s"}' % blob_sha(b"old\n"),
        "owner/allowed-repo", "main", "patch.diff", *identity(PATCH), "change", True,
    )
    assert result["dry_run"] is True
    assert result["patch_blob_sha"] == blob_sha(PATCH)
    assert result["operation_fingerprint"]
    assert result["changed_files"][0]["path"] == "file.txt"
    assert called is False


def test_apply_patch_from_ref_commit_reuses_verified_commit_path(tmp_path, monkeypatch):
    service, _, _ = reference_service()
    monkeypatch.setattr(settings, "IDEMPOTENCY_DB_PATH", str(tmp_path / "reference.db"))
    verified = {"write_verified": True, "commit_sha": "commit-2", "tree_sha": "tree-2", "new_head_sha": "commit-2"}
    seen = {}

    def fake_commit(*args):
        seen["args"] = args
        return verified

    monkeypatch.setattr(mygithub10, "_commit_files", fake_commit)
    result = mygithub10.apply_patch_from_ref(
        service, "owner/target", "main", "target-1", '{"file.txt":"%s"}' % blob_sha(b"old\n"),
        "owner/allowed-repo", "main", "patch.diff", *identity(PATCH), "change", False, "ref-key",
    )
    assert seen["args"][1:4] == ("owner/target", "main", "target-1")
    assert result["write_verified"] is True
    assert result["commit_sha"] == "commit-2"


@pytest.mark.parametrize(("expected_head", "expected_blob", "code"), [
    ("wrong-head", blob_sha(b"old\n"), "PATCH_HEAD_CHANGED"),
    ("target-1", "0" * 40, "BLOB_CHANGED"),
])
def test_reference_apply_preserves_target_cas_errors(expected_head, expected_blob, code):
    service, _, _ = reference_service()
    with pytest.raises(mygithub10.MyGithub10Error) as exc:
        mygithub10.apply_patch_from_ref(
            service, "owner/target", "main", expected_head, '{"file.txt":"%s"}' % expected_blob,
            "owner/allowed-repo", "main", "patch.diff", *identity(PATCH), "change", True,
        )
    assert exc.value.code == code


def test_reference_idempotency_binds_artifact_target_and_workspace_identity(tmp_path, monkeypatch):
    source_data = PATCH
    changed_patch = PATCH.replace(b"+new", b"+newer")
    service, source, _ = reference_service(source_data)
    monkeypatch.setattr(settings, "IDEMPOTENCY_DB_PATH", str(tmp_path / "identity.db"))
    args = (service, "owner/target", "main", "target-1", '{"file.txt":"%s"}' % blob_sha(b"old\n"),
            "owner/allowed-repo", "main", "patch.diff", *identity(source_data), "change", False, "same-key",
            {"workspace_id": "ws-1", "workspace_revision": 7})
    monkeypatch.setattr(mygithub10, "_commit_files", lambda *a: {"write_verified": True, "commit_sha": "c", "tree_sha": "t"})
    first_result = mygithub10.apply_patch_from_ref(*args)
    source.snapshots["source-2"] = {"patch.diff": changed_patch}
    source.refs["main"] = "source-2"
    with pytest.raises(mygithub10.MyGithub10Error) as exc:
        mygithub10.apply_patch_from_ref(
            service, "owner/target", "main", "target-1", '{"file.txt":"%s"}' % blob_sha(b"old\n"),
            "owner/allowed-repo", "main", "patch.diff", *identity(changed_patch), "change", False, "same-key",
            {"workspace_id": "ws-1", "workspace_revision": 7},
        )
    assert exc.value.code == "IDEMPOTENCY_CONFLICT"
    row = mygithub10._idempotent_existing("same-key")
    request = json.loads(row["request_json"])
    assert request["repository"] == "owner/target"
    assert request["branch"] == "main"
    assert request["expected_head_sha"] == "target-1"
    assert request["commit_message"] == "change"
    assert request["patch_artifact"]["patch_repository"] == "owner/allowed-repo"
    assert request["patch_artifact"]["patch_ref"] == "main"
    assert request["patch_artifact"]["resolved_patch_commit_sha"] == "source-1"
    assert request["patch_artifact"]["patch_path"] == "patch.diff"
    assert request["patch_artifact"]["patch_blob_sha"] == blob_sha(source_data)
    assert request["patch_artifact"]["patch_sha256"] == hashlib.sha256(source_data).hexdigest()
    assert request["patch_artifact"]["patch_size_bytes"] == len(source_data)
    assert request["dry_run"] is False
    assert request["workspace_id"] == "ws-1"
    assert request["expected_workspace_revision"] == 7
    expected_fingerprint = mygithub10._sha256(json.dumps(request, sort_keys=True, separators=(",", ":")).encode())
    assert first_result["operation_fingerprint"] == expected_fingerprint
    assert row["request_sha256"] == expected_fingerprint


def test_mutable_source_ref_requires_expected_old_identity_and_reports_matching_commit():
    service, source, _ = reference_service()
    new_patch = PATCH.replace(b"+new", b"+newer")
    source.snapshots["source-2"] = {"patch.diff": new_patch}
    source.refs["main"] = "source-2"
    with pytest.raises(mygithub10.MyGithub10Error) as exc:
        mygithub10.resolve_patch_from_ref(service, "owner/allowed-repo", "main", "patch.diff", *identity(PATCH))
    assert exc.value.code == "PATCH_SOURCE_BLOB_CHANGED"
    patch, item = mygithub10.resolve_patch_from_ref(service, "owner/allowed-repo", "main", "patch.diff", *identity(new_patch))
    assert patch.encode() == new_patch
    assert item["resolved_patch_commit_sha"] == "source-2"


def test_analyze_patch_from_ref_reuses_resolver_identity_and_parser_count(monkeypatch):
    service, _, target = reference_service()
    from app import mygithub12
    monkeypatch.setattr(mygithub12, "affected_tests", lambda *args, **kwargs: {"tests": []})
    result = mygithub12.analyze_patch_from_ref(service, "owner/target", "target-1", "owner/allowed-repo", "main", "patch.diff", *identity(PATCH))
    assert result["resolved_patch_commit_sha"] == "source-1"
    assert result["patch_blob_sha"] == blob_sha(PATCH)
    assert result["parsed_files"] == 1
