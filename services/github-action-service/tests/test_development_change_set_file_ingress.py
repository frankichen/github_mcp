import concurrent.futures
import hashlib
import json
import os
import stat
from types import SimpleNamespace

import pytest

from app import artifact_store
from app import development_change_set_store as prepared_store
from app import development_orchestrator as dx
from app import mygithub10, mygithub12
from app import mygithub12_dx_mcp as dx_mcp
from app import runtime_file_ingress
from app.mcp_response import StructuredFastMCP


HEAD = "a" * 40
NEW_HEAD = "b" * 40
TREE = "c" * 40
OLD_BLOB = "d" * 40


def _git_blob_sha(data: bytes) -> str:
    return hashlib.sha1(f"blob {len(data)}\0".encode("ascii") + data).hexdigest()


def _raw_change_set(payload: str = "line\r\n中文😀\"\\escaped\n", *, trailing_newline: bool = False) -> bytes:
    raw = json.dumps(
        {
            "schema_version": 1,
            "mode": "patch",
            "expected_blob_shas": {"src/example.txt": OLD_BLOB},
            "patch": payload,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return raw + (b"\n" if trailing_newline else b"")


def _structured_result(call_result):
    if isinstance(call_result, tuple):
        return call_result[1]
    return getattr(call_result, "structured_content", None) or getattr(
        call_result, "structuredContent", None
    )


@pytest.fixture
def ingress_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "mygithub12.db"))
    monkeypatch.setenv("MYGITHUB12_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    session = {
        "session_id": "dev_test",
        "repository": "owner/repo",
        "branch": "ai/task",
        "base_branch": "main",
        "head_commit_sha": HEAD,
        "tree_sha": TREE,
        "session_revision": 1,
        "workspace_revision": 3,
        "workspace_id": "ws_test",
        "status": "active",
    }
    workspace = {
        "workspace_id": "ws_test",
        "repository": "owner/repo",
        "branch": "ai/task",
        "head_sha": HEAD,
        "tree_sha": TREE,
        "revision": 3,
        "status": "active",
        "lease_valid": True,
    }
    state = {
        "raw": _raw_change_set(),
        "executions": [],
        "finalize_calls": 0,
        "ingress_calls": 0,
        "ingress_enabled": True,
    }

    async def ingester(reference, **kwargs):
        assert set(reference) >= {"download_url", "file_id"}
        state["ingress_calls"] += 1
        if not state["ingress_enabled"]:
            raise RuntimeError("host capability expired")
        return artifact_store.store_bytes(
            state["raw"],
            kind=kwargs["kind"],
            max_bytes=kwargs["max_bytes"],
            source_transport="test_openai_adapter",
            session_scope=kwargs.get("session_scope", ""),
            ttl_seconds=kwargs.get("ttl_seconds", 1800),
        )

    def maintenance(*_args, **_kwargs):
        return {
            "renewed": False,
            "session": session,
            "workspace": workspace,
            "remaining_seconds": 4000,
            "audit": None,
            "recovery": None,
        }

    def require(*_args, **_kwargs):
        return session, workspace

    def execute(
        _service,
        _session,
        _workspace,
        parsed,
        expected_head_sha,
        expected_workspace_revision,
        _commit_message,
        dry_run,
        _idempotency_key,
        _audit,
    ):
        state["executions"].append(
            {
                "dry_run": dry_run,
                "canonical_hash": parsed["canonical_hash"],
                "expected_head_sha": expected_head_sha,
                "workspace_revision": expected_workspace_revision,
            }
        )
        result = {
            "ok": True,
            "dry_run": dry_run,
            "repository": session["repository"],
            "branch": session["branch"],
            "expected_head_sha": expected_head_sha,
            "changed_files": [
                {
                    "path": "src/example.txt",
                    "operation": "modify",
                    "old_blob_sha": OLD_BLOB,
                    "new_blob_sha": "e" * 40,
                }
            ],
            "change_set_canonical_hash": parsed["canonical_hash"],
        }
        if not dry_run:
            result.update(
                {
                    "write_verified": True,
                    "commit_sha": NEW_HEAD,
                    "tree_sha": TREE,
                    "operation_id": "op-write",
                }
            )
        return result

    monkeypatch.setattr(dx, "maybe_auto_renew_session_workspace", maintenance)
    monkeypatch.setattr(dx, "require_session_workspace", require)
    monkeypatch.setattr(dx, "execute_change_set", execute)
    monkeypatch.setattr(
        dx,
        "after_verified_change",
        lambda *_args, **_kwargs: {
            "session": {**session, "head_commit_sha": NEW_HEAD, "session_revision": 2},
            "index": None,
            "index_error": None,
        },
    )

    async def github_call(function, *args, **kwargs):
        return function(*args, **kwargs)

    async def finalize_write(result, workspace_id, expected_revision):
        state["finalize_calls"] += 1
        return {
            **result,
            "workspace": {
                **workspace,
                "workspace_id": workspace_id,
                "revision": expected_revision + 1,
                "head_sha": NEW_HEAD,
            },
        }

    mcp = StructuredFastMCP("change-set-file-ingress-test")
    dx_mcp.register_dx_tools(
        mcp, github_call, SimpleNamespace(), finalize_write, ingester
    )

    async def call(**overrides):
        arguments = {
            "development_session_id": "dev_test",
            "expected_session_revision": 1,
            "expected_workspace_revision": 3,
            "expected_head_sha": HEAD,
            "commit_message": "test prepared ChangeSet",
            "change_set_file": {
                "download_url": "https://files.oaiusercontent.com/candidate.json",
                "file_id": "file_candidate",
            },
            "expected_change_set_size_bytes": len(state["raw"]),
            "expected_change_set_sha256": hashlib.sha256(state["raw"]).hexdigest(),
            "expected_change_set_git_blob_sha": _git_blob_sha(state["raw"]),
            "dry_run": True,
            "idempotency_key": "prepare-key",
        }
        arguments.update(overrides)
        return _structured_result(
            await mcp.call_tool("apply_development_change_set", arguments)
        )

    return {
        "mcp": mcp,
        "call": call,
        "state": state,
        "session": session,
        "workspace": workspace,
        "tmp_path": tmp_path,
    }


@pytest.mark.parametrize(
    "raw",
    [
        _raw_change_set("ASCII"),
        _raw_change_set("中文"),
        _raw_change_set("emoji 😀"),
        _raw_change_set('quote " and slash \\'),
        _raw_change_set("line one\r\nline two\r\n"),
        _raw_change_set("line one\nline two\n"),
        _raw_change_set("escaped \\r\\n \\u4e2d"),
        _raw_change_set("with final JSON newline", trailing_newline=True),
    ],
)
def test_raw_identity_and_parser_preserve_exact_bytes(raw):
    identity = dx.change_set_raw_identity(raw)
    assert identity == {
        "received_size_bytes": len(raw),
        "received_sha256": hashlib.sha256(raw).hexdigest(),
        "received_git_blob_sha": _git_blob_sha(raw),
    }
    parsed = dx.parse_change_set_bytes(raw)
    assert len(parsed["canonical_hash"]) == 64


def test_file_parser_rejects_invalid_utf8_and_json_without_normalization():
    with pytest.raises(mygithub12.MyGithub12Error) as utf8_exc:
        dx.parse_change_set_bytes(b"{\xff}")
    assert utf8_exc.value.code == "CHANGE_SET_INVALID_UTF8"
    with pytest.raises(mygithub12.MyGithub12Error) as json_exc:
        dx.parse_change_set_bytes(b"\xef\xbb\xbf{}")
    assert json_exc.value.code == "CHANGE_SET_INVALID_JSON"


def test_capabilities_separate_changeset_transport_from_patch_limit():
    capabilities = mygithub10.capabilities(HEAD)
    assert capabilities["supports_development_change_set_file_ingress"] is True
    assert capabilities["supports_prepared_change_set"] is True
    assert (
        capabilities["development_change_set_inline_limit_bytes"]
        == mygithub10.MAX_DEVELOPMENT_CHANGE_SET_INLINE_BYTES
    )
    assert (
        capabilities["development_change_set_file_limit_bytes"]
        == mygithub10.MAX_DEVELOPMENT_CHANGE_SET_FILE_BYTES
    )
    assert (
        capabilities["prepared_change_set_ttl_seconds"]
        == mygithub10.PREPARED_CHANGE_SET_TTL_SECONDS
    )
    assert capabilities["max_patch_bytes"] == mygithub10.MAX_PATCH_BYTES


@pytest.mark.asyncio
async def test_schema_has_runtime_file_param_and_mutually_exclusive_sources(ingress_env):
    tools = {tool.name: tool for tool in await ingress_env["mcp"].list_tools()}
    tool = tools["apply_development_change_set"]
    prepare_tool = tools["prepare_development_change_set_file"]
    prepare_meta = getattr(prepare_tool, "meta", None) or getattr(prepare_tool, "_meta", None) or {}
    assert prepare_meta["openai/fileParams"] == ["bundle_file"]
    prepare_properties = prepare_tool.inputSchema["properties"]
    assert "bundle_file" in prepare_properties
    bundle_schema = prepare_properties["bundle_file"]
    if "anyOf" in bundle_schema:
        bundle_schema = next(
            schema for schema in bundle_schema["anyOf"]
            if schema.get("type") == "object" or "$ref" in schema
        )
    if "$ref" in bundle_schema:
        bundle_schema = prepare_tool.inputSchema["$defs"][
            bundle_schema["$ref"].rsplit("/", 1)[-1]
        ]
    assert {"download_url", "file_id"} <= set(bundle_schema["properties"])
    assert {"download_url", "file_id"} <= set(bundle_schema["required"])
    properties = tool.inputSchema["properties"]
    assert {
        "change_set_json",
        "change_set_file",
        "bundle_file",
        "prepared_change_set_id",
        "expected_change_set_size_bytes",
        "expected_change_set_sha256",
        "expected_change_set_git_blob_sha",
    } <= set(properties)
    change_set_file_schema = properties["change_set_file"]
    if "anyOf" in change_set_file_schema:
        change_set_file_schema = next(
            schema for schema in change_set_file_schema["anyOf"]
            if schema.get("type") == "object" or "$ref" in schema
        )
    if "$ref" in change_set_file_schema:
        change_set_file_schema = tool.inputSchema["$defs"][
            change_set_file_schema["$ref"].rsplit("/", 1)[-1]
        ]
    assert {"download_url", "file_id"} <= set(change_set_file_schema["properties"])
    assert {"download_url", "file_id"} <= set(change_set_file_schema["required"])
    bundle_file_schema = properties["bundle_file"]
    if "anyOf" in bundle_file_schema:
        bundle_file_schema = next(
            schema for schema in bundle_file_schema["anyOf"]
            if schema.get("type") == "object" or "$ref" in schema
        )
    if "$ref" in bundle_file_schema:
        bundle_file_schema = tool.inputSchema["$defs"][
            bundle_file_schema["$ref"].rsplit("/", 1)[-1]
        ]
    assert {"download_url", "file_id"} <= set(bundle_file_schema["properties"])
    assert {"download_url", "file_id"} <= set(bundle_file_schema["required"])
    tool_meta = getattr(tool, "meta", None) or getattr(tool, "_meta", None) or {}
    assert tool_meta["openai/fileParams"] == ["change_set_file", "bundle_file"]
    both = await ingress_env["call"](change_set_json="{}")
    assert both["error"]["code"] == "CHANGE_SET_SOURCE_CONFLICT"
    both_file_names = await ingress_env["call"](
        bundle_file={
            "download_url": "https://files.oaiusercontent.com/alias.json",
            "file_id": "file_alias",
        }
    )
    assert both_file_names["error"]["code"] == "CHANGE_SET_SOURCE_CONFLICT"
    neither = await ingress_env["call"](
        change_set_file=None,
        expected_change_set_size_bytes=0,
        expected_change_set_sha256="",
        expected_change_set_git_blob_sha="",
    )
    assert neither["error"]["code"] == "CHANGE_SET_FILE_REQUIRED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("override", "code"),
    [
        ({"expected_change_set_size_bytes": 1}, "CHANGE_SET_SIZE_MISMATCH"),
        ({"expected_change_set_sha256": "0" * 64}, "CHANGE_SET_SHA256_MISMATCH"),
        (
            {"expected_change_set_git_blob_sha": "0" * 40},
            "CHANGE_SET_GIT_BLOB_SHA_MISMATCH",
        ),
    ],
)
async def test_file_identity_mismatch_fails_before_parse_or_execute(
    ingress_env, override, code
):
    result = await ingress_env["call"](**override)
    assert result["error"]["code"] == code
    assert ingress_env["state"]["executions"] == []


@pytest.mark.asyncio
async def test_truncated_invalid_and_oversized_files_fail_stop(ingress_env, monkeypatch):
    raw = ingress_env["state"]["raw"]
    ingress_env["state"]["raw"] = raw[:-1]
    truncated = await ingress_env["call"](
        expected_change_set_size_bytes=len(raw),
        expected_change_set_sha256=hashlib.sha256(raw).hexdigest(),
        expected_change_set_git_blob_sha=_git_blob_sha(raw),
    )
    assert truncated["error"]["code"] == "CHANGE_SET_SIZE_MISMATCH"
    ingress_env["state"]["raw"] = b"{\xff}"
    invalid_utf8 = await ingress_env["call"](
        expected_change_set_size_bytes=3,
        expected_change_set_sha256=hashlib.sha256(b"{\xff}").hexdigest(),
        expected_change_set_git_blob_sha=_git_blob_sha(b"{\xff}"),
    )
    assert invalid_utf8["error"]["code"] == "CHANGE_SET_INVALID_UTF8"

    async def too_large(_reference, **_kwargs):
        raise runtime_file_ingress.RuntimeFileIngressError(
            "TOO_LARGE", "change_set_file exceeds the ingress limit"
        )

    mcp = StructuredFastMCP("oversized-change-set")

    async def github_call(function, *args, **kwargs):
        return function(*args, **kwargs)

    async def finalize(*_args):
        raise AssertionError("write must not start")

    dx_mcp.register_dx_tools(mcp, github_call, SimpleNamespace(), finalize, too_large)
    result = _structured_result(
        await mcp.call_tool(
            "apply_development_change_set",
            {
                "development_session_id": "dev_test",
                "expected_session_revision": 1,
                "expected_workspace_revision": 3,
                "expected_head_sha": HEAD,
                "commit_message": "oversized",
                "change_set_file": {
                    "download_url": "https://files.oaiusercontent.com/large",
                    "file_id": "large",
                },
                "expected_change_set_size_bytes": mygithub10.MAX_DEVELOPMENT_CHANGE_SET_FILE_BYTES,
                "expected_change_set_sha256": "0" * 64,
            },
        )
    )
    assert result["error"]["code"] == "CHANGE_SET_FILE_TOO_LARGE"


@pytest.mark.asyncio
async def test_bundle_file_alias_uses_same_changeset_ingress(ingress_env):
    result = await ingress_env["call"](
        change_set_file=None,
        bundle_file={
            "download_url": "https://files.oaiusercontent.com/candidate-alias.json",
            "file_id": "file_candidate_alias",
        },
        idempotency_key="prepare-key-alias",
    )
    assert result["ok"] is True
    assert result["payload_source"] == "change_set_file"
    assert result["file_parameter"] == "bundle_file"
    assert result["prepared_change_set_id"].startswith("pcs_")
    assert ingress_env["state"]["ingress_calls"] == 1


@pytest.mark.asyncio
async def test_large_exact_file_dry_run_freezes_then_writes_same_candidate(ingress_env):
    large_payload = "x" * 120_000 + "\r\n中文😀\n"
    raw = _raw_change_set(large_payload, trailing_newline=True)
    assert len(raw) > 109_181
    ingress_env["state"]["raw"] = raw
    prepared = await ingress_env["call"](
        expected_change_set_size_bytes=len(raw),
        expected_change_set_sha256=hashlib.sha256(raw).hexdigest(),
        expected_change_set_git_blob_sha=_git_blob_sha(raw),
    )
    assert prepared["ok"] is True
    assert prepared["payload_source"] == "change_set_file"
    assert prepared["file_parameter"] == "change_set_file"
    assert prepared["received_size_bytes"] == len(raw)
    assert prepared["received_sha256"] == hashlib.sha256(raw).hexdigest()
    assert prepared["received_git_blob_sha"] == _git_blob_sha(raw)
    assert prepared["prepared_change_set_id"].startswith("pcs_")
    metadata, stored = prepared_store.load_prepared_bytes(
        prepared["prepared_change_set_id"]
    )
    assert stored == raw
    assert metadata["canonical_change_set_hash"] == prepared["change_set_canonical_hash"]
    assert metadata["artifact_id"] != prepared["prepared_change_set_id"]
    artifact = artifact_store._storage_path(metadata["artifact_id"])
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
    ingress_env["state"]["ingress_enabled"] = False

    written = await ingress_env["call"](
        change_set_file=None,
        expected_change_set_size_bytes=0,
        expected_change_set_sha256="",
        expected_change_set_git_blob_sha="",
        prepared_change_set_id=prepared["prepared_change_set_id"],
        dry_run=False,
        idempotency_key="prepared-write-key",
    )
    assert written["ok"] is True
    assert written["payload_source"] == "prepared_change_set"
    assert written["received_sha256"] == prepared["received_sha256"]
    assert written["change_set_canonical_hash"] == prepared["change_set_canonical_hash"]
    assert [item["dry_run"] for item in ingress_env["state"]["executions"]] == [
        True,
        True,
        False,
    ]
    assert not artifact.exists()
    assert ingress_env["state"]["finalize_calls"] == 1
    assert ingress_env["state"]["ingress_calls"] == 1

    replay = await ingress_env["call"](
        change_set_file=None,
        expected_change_set_size_bytes=0,
        expected_change_set_sha256="",
        expected_change_set_git_blob_sha="",
        prepared_change_set_id=prepared["prepared_change_set_id"],
        dry_run=False,
        idempotency_key="prepared-write-key",
    )
    assert replay["replayed"] is True
    assert replay["commit_sha"] == NEW_HEAD
    assert ingress_env["state"]["finalize_calls"] == 1

    conflict = await ingress_env["call"](
        change_set_file=None,
        expected_change_set_size_bytes=0,
        expected_change_set_sha256="",
        expected_change_set_git_blob_sha="",
        prepared_change_set_id=prepared["prepared_change_set_id"],
        dry_run=False,
        idempotency_key="prepared-write-key",
        commit_message="different payload",
    )
    assert conflict["error"]["code"] == "IDEMPOTENCY_CONFLICT"


@pytest.mark.asyncio
async def test_inline_prepare_backward_compatibility_and_large_inline_rejection(ingress_env):
    raw = _raw_change_set("small")
    inline = await ingress_env["call"](
        change_set_file=None,
        expected_change_set_size_bytes=0,
        expected_change_set_sha256="",
        expected_change_set_git_blob_sha="",
        change_set_json=raw.decode("utf-8"),
        dry_run=True,
        idempotency_key="inline-prepare",
    )
    assert inline["ok"] is True
    assert inline["payload_source"] == "change_set_json"
    assert inline["prepared_change_set_id"].startswith("pcs_")
    raw_write = await ingress_env["call"](
        change_set_file=None,
        expected_change_set_size_bytes=0,
        expected_change_set_sha256="",
        expected_change_set_git_blob_sha="",
        change_set_json=raw.decode("utf-8"),
        dry_run=False,
    )
    assert raw_write["error"]["code"] == "CHANGE_SET_SOURCE_CONFLICT"
    large = json.dumps(
        {"schema_version": 1, "mode": "patch", "patch": "x" * 60_000},
        separators=(",", ":"),
    )
    rejected = await ingress_env["call"](
        change_set_file=None,
        expected_change_set_size_bytes=0,
        expected_change_set_sha256="",
        expected_change_set_git_blob_sha="",
        change_set_json=large,
        dry_run=True,
    )
    assert rejected["error"]["code"] == "CHANGE_SET_FILE_REQUIRED"


@pytest.mark.asyncio
async def test_prepared_scope_and_current_cas_failures_preserve_error_semantics(
    ingress_env, monkeypatch
):
    prepared = await ingress_env["call"]()
    prepared_id = prepared["prepared_change_set_id"]
    common = {
        "change_set_file": None,
        "expected_change_set_size_bytes": 0,
        "expected_change_set_sha256": "",
        "expected_change_set_git_blob_sha": "",
        "prepared_change_set_id": prepared_id,
        "dry_run": False,
        "idempotency_key": "scope-write",
    }
    wrong_session = await ingress_env["call"](
        **common, development_session_id="dev_other"
    )
    assert wrong_session["error"]["code"] == "PREPARED_CHANGE_SET_SCOPE_MISMATCH"
    wrong_session_revision = await ingress_env["call"](
        **{**common, "expected_session_revision": 2}
    )
    assert (
        wrong_session_revision["error"]["code"]
        == "DEVELOPMENT_SESSION_REVISION_MISMATCH"
    )
    wrong_workspace_revision = await ingress_env["call"](
        **{**common, "expected_workspace_revision": 4}
    )
    assert wrong_workspace_revision["error"]["code"] == "WORKSPACE_REVISION_MISMATCH"
    wrong_head = await ingress_env["call"](
        **{**common, "expected_head_sha": "f" * 40}
    )
    assert wrong_head["error"]["code"] == "HEAD_CHANGED"
    conflict = await ingress_env["call"](**common, change_set_json="{}")
    assert conflict["error"]["code"] == "CHANGE_SET_SOURCE_CONFLICT"

    def stale_revision(*_args, **_kwargs):
        raise mygithub12.MyGithub12Error(
            "DEVELOPMENT_SESSION_REVISION_MISMATCH", "session changed"
        )

    monkeypatch.setattr(dx, "require_session_workspace", stale_revision)
    stale = await ingress_env["call"](**common)
    assert stale["error"]["code"] == "DEVELOPMENT_SESSION_REVISION_MISMATCH"


@pytest.mark.asyncio
async def test_prepared_blob_recheck_fails_before_claim(ingress_env, monkeypatch):
    prepared = await ingress_env["call"]()

    def blob_changed(*_args, **_kwargs):
        raise mygithub12.MyGithub12Error(
            "BLOB_CHANGED", "blob changed", {"path": "src/example.txt"}
        )

    monkeypatch.setattr(dx, "execute_change_set", blob_changed)
    result = await ingress_env["call"](
        change_set_file=None,
        expected_change_set_size_bytes=0,
        expected_change_set_sha256="",
        expected_change_set_git_blob_sha="",
        prepared_change_set_id=prepared["prepared_change_set_id"],
        dry_run=False,
        idempotency_key="blob-recheck",
    )
    assert result["error"]["code"] == "BLOB_CHANGED"
    assert (
        prepared_store.get_prepared_change_set(prepared["prepared_change_set_id"])[
            "status"
        ]
        == "PREPARED"
    )


def _prepared_for_store(tmp_path, monkeypatch):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "store.db"))
    monkeypatch.setenv("MYGITHUB12_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    raw = _raw_change_set("concurrency")
    parsed = dx.parse_change_set_bytes(raw)
    artifact = artifact_store.store_bytes(
        raw,
        kind="development_change_set",
        max_bytes=1024 * 1024,
        source_transport="test",
    )
    return prepared_store.create_prepared_change_set(
        artifact,
        parsed,
        {"changed_files": [{"path": "src/example.txt", "old_blob_sha": OLD_BLOB}]},
        {
            "repository": "owner/repo",
            "branch": "ai/task",
            "session_id": "dev_test",
            "session_revision": 1,
        },
        {"workspace_id": "ws_test", "revision": 3},
        expected_head_sha=HEAD,
    )


def test_prepared_expiry_wrong_id_and_atomic_concurrent_consume(tmp_path, monkeypatch):
    with pytest.raises(mygithub12.MyGithub12Error) as missing:
        prepared_store.get_prepared_change_set("pcs_" + "x" * 32)
    assert missing.value.code == "PREPARED_CHANGE_SET_NOT_FOUND"

    prepared = _prepared_for_store(tmp_path, monkeypatch)
    prepared_id = prepared["prepared_change_set_id"]

    def claim(key):
        try:
            prepared_store.claim_for_write(
                prepared_id, idempotency_key=key, request_fingerprint=key
            )
            return "claimed"
        except mygithub12.MyGithub12Error as exc:
            return exc.code

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(claim, ["one", "two"]))
    assert outcomes.count("claimed") == 1
    assert outcomes.count("PREPARED_CHANGE_SET_ALREADY_CONSUMED") == 1

    other = _prepared_for_store(tmp_path, monkeypatch)
    monkeypatch.setattr(prepared_store.core, "_now", lambda: other["expires_at"] + 1)
    with pytest.raises(mygithub12.MyGithub12Error) as expired:
        prepared_store.load_prepared_bytes(other["prepared_change_set_id"])
    assert expired.value.code == "PREPARED_CHANGE_SET_EXPIRED"
    assert not artifact_store._storage_path(other["artifact_id"]).exists()


def _client_for_chunks(chunks, headers):
    class FakeResponse:
        is_redirect = False

        def __init__(self):
            self.headers = headers

        def raise_for_status(self):
            return None

        async def aiter_raw(self):
            for chunk in chunks:
                yield chunk

    class FakeStream:
        async def __aenter__(self):
            return FakeResponse()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def stream(self, method, url, headers=None):
            assert method == "GET"
            assert headers == {"Accept-Encoding": "identity"}
            return FakeStream()

    return FakeClient()


def _public_dns(host, port, type=0):
    return [(2, 1, 6, "", ("93.184.216.34", port))]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("expected", "chunks"),
    [
        (b"one-byte", [bytes([value]) for value in b"one-byte"]),
        (b"random-boundaries", [b"ra", b"n", b"dom-b", b"ound", b"aries"]),
        (b"x" * 65536 + b"tail", [b"x" * 65536, b"tail"]),
        (b"line\r\nnext", [b"line\r", b"\nnext"]),
        ("中文😀".encode(), [b"\xe4", b"\xb8\xad\xe6\x96", b"\x87\xf0\x9f", b"\x98\x80"]),
    ],
)
async def test_runtime_ingress_stream_hashing_is_chunk_boundary_independent(
    tmp_path, monkeypatch, expected, chunks
):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "artifact.db"))
    monkeypatch.setenv("MYGITHUB12_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    artifact = await runtime_file_ingress.ingest_runtime_artifact(
        {
            "download_url": "https://files.oaiusercontent.com/exact",
            "file_id": "file_exact",
        },
        kind="diagnostic",
        max_bytes=len(expected),
        label="change_set_file",
        resolver=_public_dns,
        client_factory=lambda **_kwargs: _client_for_chunks(
            chunks, {"content-length": str(len(expected))}
        ),
    )
    assert isinstance(artifact, artifact_store.ArtifactRef)
    assert artifact.size_bytes == len(expected)
    assert artifact.sha256 == hashlib.sha256(expected).hexdigest()
    assert artifact_store.read_artifact_bytes(artifact.artifact_id) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("declared_delta", [-1, 1])
async def test_runtime_ingress_rejects_overlong_and_truncated_declared_stream(
    tmp_path, monkeypatch, declared_delta
):
    expected = b"exact bytes"
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "artifact.db"))
    monkeypatch.setenv("MYGITHUB12_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    with pytest.raises(runtime_file_ingress.RuntimeFileIngressError) as mismatch:
        await runtime_file_ingress.ingest_runtime_artifact(
            {
                "download_url": "https://files.oaiusercontent.com/exact",
                "file_id": "file_exact",
            },
            kind="diagnostic",
            max_bytes=1024,
            label="change_set_file",
            resolver=_public_dns,
            client_factory=lambda **_kwargs: _client_for_chunks(
                [expected],
                {"content-length": str(len(expected) + declared_delta)},
            ),
        )
    assert mismatch.value.code == "TRANSPORT_SIZE_MISMATCH"
    assert not list((tmp_path / "artifacts").glob("*.bin"))


@pytest.mark.asyncio
async def test_runtime_ingress_rejects_stream_limit_and_content_encoding(
    tmp_path, monkeypatch
):
    expected = b"too-large"
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "artifact.db"))
    monkeypatch.setenv("MYGITHUB12_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    with pytest.raises(runtime_file_ingress.RuntimeFileIngressError) as oversized:
        await runtime_file_ingress.ingest_runtime_artifact(
            {"download_url": "https://files.oaiusercontent.com/x", "file_id": "x"},
            kind="diagnostic",
            max_bytes=len(expected) - 1,
            label="change_set_file",
            resolver=_public_dns,
            client_factory=lambda **_kwargs: _client_for_chunks([expected], {}),
        )
    assert oversized.value.code == "TOO_LARGE"
    with pytest.raises(runtime_file_ingress.RuntimeFileIngressError) as encoded:
        await runtime_file_ingress.ingest_runtime_artifact(
            {"download_url": "https://files.oaiusercontent.com/x", "file_id": "x"},
            kind="diagnostic",
            max_bytes=1024,
            label="change_set_file",
            resolver=_public_dns,
            client_factory=lambda **_kwargs: _client_for_chunks(
                [], {"content-encoding": "gzip"}
            ),
        )
    assert encoded.value.code == "INVALID_REFERENCE"


def test_recover_executing_write_releases_only_unstarted_stale_claim(tmp_path, monkeypatch):
    prepared = _prepared_for_store(tmp_path, monkeypatch)
    prepared_id = prepared["prepared_change_set_id"]
    prepared_store.claim_for_write(
        prepared_id,
        idempotency_key="recover-key",
        request_fingerprint="fingerprint",
    )
    monkeypatch.setattr(
        prepared_store.mygithub10, "_idempotent_existing", lambda _key: None
    )
    monkeypatch.setattr(
        prepared_store.core,
        "_now",
        lambda: prepared["created_at"] + prepared_store.EXECUTION_STALE_SECONDS + 1,
    )

    recovered = prepared_store.recover_executing_write(
        prepared_id,
        idempotency_key="recover-key",
        request_fingerprint="fingerprint",
    )

    assert recovered == {"action": "retry_unstarted"}
    assert prepared_store.get_prepared_change_set(prepared_id)["status"] == "PREPARED"
    metadata, raw = prepared_store.load_prepared_bytes(prepared_id)
    assert metadata["prepared_change_set_id"] == prepared_id
    assert raw == _raw_change_set("concurrency")


def test_recover_executing_write_resumes_verified_durable_operation(tmp_path, monkeypatch):
    prepared = _prepared_for_store(tmp_path, monkeypatch)
    prepared_id = prepared["prepared_change_set_id"]
    prepared_store.claim_for_write(
        prepared_id,
        idempotency_key="recover-key",
        request_fingerprint="fingerprint",
    )
    durable_result = {
        "write_verified": True,
        "commit_sha": NEW_HEAD,
        "tree_sha": TREE,
        "repository": "owner/repo",
        "branch": "ai/task",
    }
    monkeypatch.setattr(
        prepared_store.mygithub10,
        "_idempotent_existing",
        lambda _key: {
            "status": "git_verified",
            "operation_id": "op-recover",
            "result_json": json.dumps(durable_result),
        },
    )

    recovered = prepared_store.recover_executing_write(
        prepared_id,
        idempotency_key="recover-key",
        request_fingerprint="fingerprint",
    )

    assert recovered == {
        "action": "resume_git_verified",
        "operation_id": "op-recover",
        "result": durable_result,
    }
    assert prepared_store.get_prepared_change_set(prepared_id)["status"] == "EXECUTING"
