#!/usr/bin/env python3
"""Guarded, idempotent P2P-only overlay for the already-installed lease-safe Worker.

The host deploy-agent carries a newer claim/lease implementation than the
checked-in legacy template. Never overwrite that live implementation with an
older source file. Apply only these fixed P2P additions, preserving its lease.
"""
import os
import pathlib
import sys
import tempfile

REPOSITORY = "frankichen/p2p-server"
CONTRACT = '''    "frankichen/p2p-server": {
        "workspace": "P2P_DEPLOY_WORKSPACE",
        "workspace_default": "/srv/private-ci/deploy-workspace/p2p-server",
        "script": "scripts/deploy_gongshi_p2p_lab.sh",
        "environment": "gongshi-p2p-lab",
        "scope": "p2p-lab",
        "status_file": "P2P_DEPLOY_STATUS_FILE",
        "status_default": "/var/lib/private-ci/gongshi-p2p-lab-status.json",
    },
'''
INSERTION = '    "frankichen/devhub": {\n'
DELEGATE_OLD = 'repository in {"frankichen/sxt", "frankichen/devhub"}'
DELEGATE_NEW = 'repository in {"frankichen/sxt", "frankichen/devhub", "frankichen/p2p-server"}'
QUEUE_OLD = "WHERE status='queued' AND repository IN (?, ?, ?) "
QUEUE_NEW = "WHERE status='queued' AND repository IN (?, ?, ?, ?) "
TUPLE_OLD = '("frankichen/sxt", "frankichen/devhub", "frankichen/auto_gupiao"),'
TUPLE_NEW = '("frankichen/sxt", "frankichen/devhub", "frankichen/auto_gupiao", "frankichen/p2p-server"),'
LEASE_SIGNS = ("claim_owner", "lease_expires_at", "claim_generation=claim_generation+1", "state_revision=state_revision+1")


def _replace_once(text, before, after):
    if text.count(before) != 1:
        raise ValueError("installed Worker guard does not match the audited lease-safe runtime")
    return text.replace(before, after, 1)


def patch(text):
    if not all(sign in text for sign in LEASE_SIGNS):
        raise ValueError("unsafe Worker baseline: durable claim/lease state is missing")
    if REPOSITORY in text:
        if all(item in text for item in (CONTRACT, DELEGATE_NEW, QUEUE_NEW, TUPLE_NEW)):
            return text
        raise ValueError("partially installed P2P Worker contract")
    updated = _replace_once(text, INSERTION, CONTRACT + INSERTION)
    updated = _replace_once(updated, DELEGATE_OLD, DELEGATE_NEW)
    updated = _replace_once(updated, QUEUE_OLD, QUEUE_NEW)
    updated = _replace_once(updated, TUPLE_OLD, TUPLE_NEW)
    compile(updated, "deploy_worker.py", "exec")
    return updated


def main():
    if len(sys.argv) != 2 or sys.argv[1] != "/opt/private-deploy-agent/app/deploy_worker.py":
        raise SystemExit("fixed deployed Worker path required")
    destination = pathlib.Path(sys.argv[1])
    stat = destination.stat()
    original = destination.read_text(encoding="utf-8")
    updated = patch(original)
    if original == updated:
        print("P2P_DEPLOY_WORKER_CONTRACT=already_installed")
        return
    fd, name = tempfile.mkstemp(prefix=".p2p-deploy-worker-", dir=str(destination.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(updated)
            f.flush()
            os.fsync(f.fileno())
        os.chown(name, stat.st_uid, stat.st_gid)
        os.chmod(name, stat.st_mode & 0o777)
        os.replace(name, destination)
        print("P2P_DEPLOY_WORKER_CONTRACT=installed")
    finally:
        if os.path.exists(name):
            os.unlink(name)


if __name__ == "__main__":
    main()
