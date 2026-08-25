"""Blue/green runtime identity and a shared SQLite leader lease."""
from __future__ import annotations

import os
import socket
import sqlite3
import time
import uuid
from typing import Any

from app import mygithub12 as core
from app.version import runtime_build_sha

SCHEMA_VERSION = 1
SCHEMA_COMPAT_MIN = 1
SCHEMA_COMPAT_MAX = 1
_ALLOWED_ROLES = {"active", "standby", "draining"}
_db = core._db
_LOCK = core._LOCK


def generation_id() -> str:
    configured = os.getenv("MYGITHUB12_GENERATION_ID", "").strip()
    return configured or f"gen-{runtime_build_sha()[:12]}-{socket.gethostname()}"


def runtime_role() -> str:
    role = os.getenv("MYGITHUB12_RUNTIME_ROLE", "active").strip().lower()
    return role if role in _ALLOWED_ROLES else "standby"


def init_runtime_db() -> None:
    core.init_db()
    with _LOCK, _db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS runtime_generations(
              generation_id TEXT PRIMARY KEY,
              build_sha TEXT NOT NULL,
              role TEXT NOT NULL,
              schema_version INTEGER NOT NULL,
              schema_compat_min INTEGER NOT NULL,
              schema_compat_max INTEGER NOT NULL,
              started_at REAL NOT NULL,
              heartbeat_at REAL NOT NULL,
              metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS runtime_leader_leases(
              lease_name TEXT PRIMARY KEY,
              generation_id TEXT NOT NULL,
              lease_token TEXT NOT NULL,
              expires_at REAL NOT NULL,
              updated_at REAL NOT NULL
            );
            """
        )


def register_generation() -> dict[str, Any]:
    init_runtime_db()
    gid = generation_id(); now=time.time(); build=runtime_build_sha(); role=runtime_role()
    with _LOCK, _db() as db:
        db.execute(
            """INSERT INTO runtime_generations(generation_id,build_sha,role,schema_version,schema_compat_min,schema_compat_max,started_at,heartbeat_at,metadata_json)
               VALUES(?,?,?,?,?,?,?,?, '{}')
               ON CONFLICT(generation_id) DO UPDATE SET build_sha=excluded.build_sha,role=excluded.role,schema_version=excluded.schema_version,schema_compat_min=excluded.schema_compat_min,schema_compat_max=excluded.schema_compat_max,heartbeat_at=excluded.heartbeat_at""",
            (gid,build,role,SCHEMA_VERSION,SCHEMA_COMPAT_MIN,SCHEMA_COMPAT_MAX,now,now),
        )
    return runtime_status()


def heartbeat() -> None:
    init_runtime_db()
    with _LOCK, _db() as db:
        db.execute("UPDATE runtime_generations SET heartbeat_at=?,role=? WHERE generation_id=?", (time.time(), runtime_role(), generation_id()))


def acquire_leader(lease_name: str = "controller-maintenance", ttl_seconds: int = 30) -> dict[str, Any]:
    init_runtime_db(); gid=generation_id(); role=runtime_role(); now=time.time(); ttl=max(5,min(int(ttl_seconds),300))
    if role != "active":
        return {"acquired": False, "reason": "runtime_not_active", "generation_id": gid, "role": role}
    token=uuid.uuid4().hex
    with _LOCK, _db() as db:
        db.execute("BEGIN IMMEDIATE")
        current=db.execute("SELECT * FROM runtime_leader_leases WHERE lease_name=?",(lease_name,)).fetchone()
        if current and float(current["expires_at"])>now and current["generation_id"]!=gid:
            db.rollback(); return {"acquired":False,"reason":"held_by_other_generation","holder":current["generation_id"],"expires_at":current["expires_at"],"generation_id":gid,"role":role}
        if current and current["generation_id"]==gid:
            token=current["lease_token"]
        db.execute(
            "INSERT OR REPLACE INTO runtime_leader_leases(lease_name,generation_id,lease_token,expires_at,updated_at) VALUES(?,?,?,?,?)",
            (lease_name,gid,token,now+ttl,now),
        ); db.commit()
    return {"acquired":True,"generation_id":gid,"role":role,"lease_name":lease_name,"lease_token":token,"expires_at":now+ttl}


def leader_status(lease_name: str = "controller-maintenance") -> dict[str, Any]:
    init_runtime_db(); now=time.time()
    with _db() as db: row=db.execute("SELECT * FROM runtime_leader_leases WHERE lease_name=?",(lease_name,)).fetchone()
    if not row or float(row["expires_at"])<=now:
        return {"held":False,"lease_name":lease_name,"generation_id":generation_id(),"is_leader":False}
    return {"held":True,"lease_name":lease_name,"holder_generation_id":row["generation_id"],"expires_at":row["expires_at"],"generation_id":generation_id(),"is_leader":row["generation_id"]==generation_id()}


def is_cleanup_leader() -> bool:
    # Legacy-compatible default: without explicit blue/green role configuration,
    # an active instance may maintain its own shared files.
    if runtime_role() != "active":
        return False
    status=leader_status()
    if status.get("is_leader"):
        return True
    return bool(acquire_leader().get("acquired"))


def runtime_status() -> dict[str, Any]:
    init_runtime_db(); gid=generation_id(); now=time.time()
    try: build=runtime_build_sha()
    except RuntimeError: build=""
    role=runtime_role()
    leader=leader_status() if _runtime_tables_ready() else {"held":False,"is_leader":False}
    return {
        "generation_id":gid,"build_sha":build,"role":role,"schema_version":SCHEMA_VERSION,
        "schema_compat_min":SCHEMA_COMPAT_MIN,"schema_compat_max":SCHEMA_COMPAT_MAX,
        "schema_compatible":SCHEMA_COMPAT_MIN<=SCHEMA_VERSION<=SCHEMA_COMPAT_MAX,
        "leader":leader,
        "ready_for_reads":True,
        "ready_for_side_effects":role=="active" and bool(leader.get("is_leader")),
        "checked_at":now,
    }


def _runtime_tables_ready() -> bool:
    try:
        with _db() as db:
            db.execute("SELECT 1 FROM runtime_leader_leases LIMIT 1").fetchone()
        return True
    except sqlite3.Error:
        return False
