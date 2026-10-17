"""Authorized local CLI uses the guarded recovery contract; no OAuth bypass."""

import io
import json
import sys
from types import SimpleNamespace

from app import maintenance_legacy_workspace as cli


def _request():
    return {
        "repository": "owner/repo",
        "branch": "ai/original",
        "workspace_id": "ws_original",
        "expected_owner": "WORKER-A",
        "expected_workspace_revision": 4,
        "expected_previous_head_sha": "a" * 40,
        "expected_current_head_sha": "b" * 40,
        "expected_current_tree_sha": "c" * 40,
        "expected_base_branch": "main",
        "expected_base_sha": "d" * 40,
        "idempotency_key": "idempotent-run",
    }


def test_default_cli_previews_without_confirm(monkeypatch, capsys):
    monkeypatch.setitem(
        sys.modules, "app.mcp_server", SimpleNamespace(_service=object())
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(_request())))
    monkeypatch.setattr(sys, "argv", ["maintenance"])
    observed = {}

    def capture(service, **kwargs):
        observed.update(kwargs)
        return {
            "ok": True,
            "confirmed": False,
            "workspace_id": "ws_original",
            "owner": "WORKER-A",
            "workspace_revision": 4,
            "current_head_sha": "b" * 40,
            "required_scope_expansion_paths": ["app/src/main/java/A.java"],
            "ancestry": {"verified": True},
            "overlap": {"comparison_verified": True},
        }

    monkeypatch.setattr(cli.recovery, "recover_sessionless_workspace", capture)
    assert cli.main() == 0
    assert observed["confirm"] is False and observed["expected_owner"] == "WORKER-A"
    data = json.loads(capsys.readouterr().out)
    assert data["confirmed"] is False
    assert data["required_scope_expansion_paths"] == ["app/src/main/java/A.java"]


def test_explicit_confirm_only_after_cli_flag(monkeypatch, capsys):
    monkeypatch.setitem(
        sys.modules, "app.mcp_server", SimpleNamespace(_service=object())
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(_request())))
    monkeypatch.setattr(sys, "argv", ["maintenance", "--confirm"])
    observed = {}

    def capture(service, **kwargs):
        observed.update(kwargs)
        return {
            "ok": True,
            "confirmed": True,
            "replayed": False,
            "workspace": {
                "workspace_id": "ws_original",
                "revision": 5,
                "status": "active",
                "lease_valid": True,
                "owner": "WORKER-A",
                "head_sha": "b" * 40,
            },
            "development_session": {
                "session_id": "dev_firstreal",
                "session_revision": 1,
                "owner": "WORKER-A",
            },
            "index": {"ready": True},
        }

    monkeypatch.setattr(cli.recovery, "recover_sessionless_workspace", capture)
    assert cli.main() == 0
    assert observed["confirm"] is True
    data = json.loads(capsys.readouterr().out)
    assert data["confirmed"] and data["session_owner"] == "WORKER-A"


def test_cli_rejects_unexpected_fields(monkeypatch, capsys):
    request = _request()
    request["token"] = "should_not_echo"
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request)))
    monkeypatch.setattr(sys, "argv", ["maintenance", "--confirm"])
    assert cli.main() == 2
    data = json.loads(capsys.readouterr().err)
    assert data["ok"] is False and "should_not_echo" not in str(data)
