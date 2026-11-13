#!/usr/bin/env python3
"""Install the fixed P2P lab deploy runner without touching SXT source or leases."""
import os
import pathlib
import pwd
import re
import stat
import subprocess
import tempfile

from scripts import patch_p2p_deploy_worker as overlay

ROOT = pathlib.Path(__file__).resolve().parents[3]
WORKER = pathlib.Path("/opt/private-deploy-agent/app/deploy_worker.py")
RUNNER_SRC = ROOT / "services/private-ci-deploy-executor/scripts/p2p_deploy_executor.py"
RUNNER_DST = pathlib.Path("/home/xiaowu/work/private-ci-controller-node-workspace/scripts/p2p_deploy_executor.py")
UNIT_SRC = ROOT / "services/private-ci-deploy-executor/systemd/p2p-deploy-executor.service.example"
UNIT_DST = pathlib.Path("/etc/systemd/system/mygithub12-p2p-deploy-executor.service")
ENV_DST = pathlib.Path("/etc/private-ci/p2p-deploy-executor.env")
EXPORT_SRC = ROOT / "services/private-ci-deploy-executor/scripts/export_p2p_ci_source.py"
EXPORT_DST = pathlib.Path("/usr/local/libexec/mygithub12/p2p-source-export.py")
EXPORT_SERVICE_SRC = ROOT / "services/private-ci-deploy-executor/systemd/p2p-source-export.service.example"
EXPORT_TIMER_SRC = ROOT / "services/private-ci-deploy-executor/systemd/p2p-source-export.timer.example"
EXPORT_SERVICE_DST = pathlib.Path("/etc/systemd/system/mygithub12-p2p-source-export.service")
EXPORT_TIMER_DST = pathlib.Path("/etc/systemd/system/mygithub12-p2p-source-export.timer")
EXPORT_DIR = pathlib.Path("/var/lib/mygithub12-p2p-source")
CONTROLLER = "http://127.0.0.1:8765"


def callback_path(value):
    matches = [entry.partition("=")[2] for entry in value.split()
               if entry.startswith("DEPLOY_CALLBACK_API_KEY_FILE=")]
    if len(matches) != 1:
        raise ValueError("existing deployment credential-file path unavailable")
    path = pathlib.Path(matches[0])
    if not path.is_absolute() or not re.fullmatch(r"/[A-Za-z0-9._/-]+", str(path)):
        raise ValueError("existing credential-file path must be an absolute local path")
    if not path.is_file() or path.is_symlink():
        raise ValueError("existing credential file must be a regular non-symlink file")
    return path


def atomic_write(target, data, *, uid, gid, mode):
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".p2p-runtime-", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chown(temporary, uid, gid)
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    if os.geteuid() != 0:
        raise SystemExit("root required by fixed infrastructure deployment")
    for source in (WORKER, RUNNER_SRC, UNIT_SRC, EXPORT_SRC, EXPORT_SERVICE_SRC, EXPORT_TIMER_SRC):
        if not source.is_file() or source.is_symlink():
            raise SystemExit("fixed P2P deployment source unavailable")
    inspected = subprocess.run(["systemctl", "show", "private-ci-deploy-executor.service",
                                "-p", "Environment", "--value"],
                               capture_output=True, text=True, check=True, timeout=10)
    key_path = callback_path(inspected.stdout)
    user = pwd.getpwnam("xiaowu")
    old_worker = WORKER.read_text(encoding="utf-8")
    updated = overlay.patch(old_worker)
    if old_worker != updated:
        mode = stat.S_IMODE(WORKER.stat().st_mode)
        atomic_write(WORKER, updated.encode("utf-8"),
                     uid=WORKER.stat().st_uid, gid=WORKER.stat().st_gid, mode=mode)
    runner = RUNNER_SRC.read_bytes()
    compile(runner.decode("utf-8"), str(RUNNER_SRC), "exec")
    atomic_write(RUNNER_DST, runner, uid=user.pw_uid, gid=user.pw_gid, mode=0o644)
    atomic_write(UNIT_DST, UNIT_SRC.read_bytes(), uid=0, gid=0, mode=0o644)
    if EXPORT_DST.parent.is_symlink() or EXPORT_DIR.is_symlink():
        raise SystemExit("P2P source export installation path is unsafe")
    EXPORT_DIR.mkdir(parents=True, exist_ok=True, mode=0o750)
    os.chown(EXPORT_DIR, 0, user.pw_gid)
    os.chmod(EXPORT_DIR, 0o750)
    exporter = EXPORT_SRC.read_bytes()
    compile(exporter.decode("utf-8"), str(EXPORT_SRC), "exec")
    atomic_write(EXPORT_DST, exporter, uid=0, gid=0, mode=0o644)
    atomic_write(EXPORT_SERVICE_DST, EXPORT_SERVICE_SRC.read_bytes(), uid=0, gid=0, mode=0o644)
    atomic_write(EXPORT_TIMER_DST, EXPORT_TIMER_SRC.read_bytes(), uid=0, gid=0, mode=0o644)
    ENV_DST.parent.mkdir(parents=True, exist_ok=True)
    config = ("DEPLOY_CONTROLLER_URL=" + CONTROLLER + "\n"
              + "DEPLOY_CALLBACK_API_KEY_FILE=" + str(key_path) + "\n")
    atomic_write(ENV_DST, config.encode("utf-8"), uid=0, gid=0, mode=0o600)
    print("P2P_DEPLOY_RUNTIME=installed")


if __name__ == "__main__":
    main()
