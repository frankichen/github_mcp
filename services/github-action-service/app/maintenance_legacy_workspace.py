"""Operator-only, guarded first-Session reconciliation from stdin JSON.

Usage (inside the authorized local Controller container):
    python -m app.maintenance_legacy_workspace < request.json
    python -m app.maintenance_legacy_workspace --confirm < reviewed-request.json

No bearer token is read, displayed, or forwarded. Docker/host operator access
is the administration boundary; HTTP MCP authentication remains unchanged.
"""

from __future__ import annotations

import argparse
import json
import sys

from app import development_sessionless_recovery as recovery
from app.mygithub12 import MyGithub12Error

_FIELDS = {
    "repository",
    "branch",
    "workspace_id",
    "expected_owner",
    "expected_workspace_revision",
    "expected_previous_head_sha",
    "expected_current_head_sha",
    "expected_current_tree_sha",
    "expected_base_branch",
    "expected_base_sha",
    "idempotency_key",
    "lease_seconds",
    "reviewed_scope_expansion_paths_json",
    "expected_live_base_sha",
    "next_owner",
    "reviewed_base_overlap_paths_json",
    "expected_writer_lease_id",
    "expected_active_binding_id",
}
_REQUIRED = _FIELDS - {
    "lease_seconds", "reviewed_scope_expansion_paths_json",
    "expected_live_base_sha", "next_owner", "reviewed_base_overlap_paths_json",
    "expected_writer_lease_id", "expected_active_binding_id",
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Official local MyGithut12 Workspace repair"
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Commit the exact reviewed Workspace CAS. Default only previews.",
    )
    args = parser.parse_args()
    try:
        request = json.load(sys.stdin)
        if (
            not isinstance(request, dict)
            or not _REQUIRED <= request.keys()
            or set(request) - _FIELDS
        ):
            raise ValueError("request must contain exactly supported recovery fields")
        from app.mcp_server import (
            _service,
        )  # existing configured service; no credential extraction

        result = recovery.recover_sessionless_workspace(
            _service, **request, confirm=args.confirm
        )
        # The output is intentionally bounded and contains no tokens, file contents, or credentials.
        if result.get("confirmed") or result.get("replayed"):
            workspace = result["workspace"]
            session = result["development_session"]
            summary = {
                "ok": True,
                "confirmed": True,
                "replayed": bool(result.get("replayed")),
                "workspace_id": workspace["workspace_id"],
                "workspace_revision": workspace["revision"],
                "status": workspace["status"],
                "lease_valid": workspace["lease_valid"],
                "owner": workspace["owner"],
                "head_sha": workspace["head_sha"],
                "development_session_id": session["session_id"],
                "session_revision": session["session_revision"],
                "session_owner": session["owner"],
                "index_ready": (result.get("index") or {}).get("ready"),
                "git_ref_changed": False,
            }
        else:
            summary = {
                "ok": True,
                "confirmed": False,
                "workspace_id": result["workspace_id"],
                "owner": result["owner"],
                "next_owner": result.get("next_owner", result["owner"]),
                "reviewed_base_overlap_paths": result.get("reviewed_base_overlap_paths", []),
                "base_advance": result.get("base_advance", {}),
                "workspace_revision": result["workspace_revision"],
                "current_head_sha": result["current_head_sha"],
                "required_scope_expansion_paths": result[
                    "required_scope_expansion_paths"
                ],
                "ancestry": result["ancestry"],
                "overlap": result["overlap"],
            }
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 0
    except (ValueError, json.JSONDecodeError, MyGithub12Error) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "code": getattr(exc, "code", type(exc).__name__),
                    "message": getattr(exc, "message", str(exc)),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
