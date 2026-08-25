"""CI Agent main entry point.

Polls the German CI Controller for jobs, executes them with Podman isolation.
Implements automatic recovery on startup via reconcile.
"""

import logging
import os
import signal
import sys
import threading
import time

from private_ci_agent.config import load_config, load_profiles, refresh_proxy_before_external_access
from private_ci_agent.controller_client import ControllerClient
from private_ci_agent.executor import JobExecutor
from private_ci_agent.models import Job
from private_ci_agent.reconcile import Reconciler
from private_ci_agent.source import (
    download_source_archive, extract_source,
    SourceDownloadTimeout, ProxyUnavailableError, DownloadError, _error_code,
    prepare_source_from_mirror, remove_source_worktree,
)
from private_ci_agent.workspace import WorkspaceManager
from private_ci_agent.cleanup import cleanup_containers
from private_ci_agent.podman import PodmanRunner
from private_ci_agent.services import cleanup_job_services

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("ci-agent")

_running = True
_current_job_id = None
_current_lease_token = None
_cancel_event = threading.Event()
_podman_binary = "/usr/bin/podman"
REGISTER_BACKOFF_SECONDS = (2, 5, 10, 30, 60, 60)


def signal_handler(sig, frame):
    global _running
    logger.info("Received signal %s, shutting down...", sig)
    _running = False


def _request_cancel(job_id: str | None = None) -> bool:
    """Signal cancel for the current job and force-reclaim its containers.

    Safe to call from the heartbeat thread while the job thread is blocked in
    podman run: it only flips the event and stops containers by job prefix.
    When job_id is given (the job the cancel response was for), the request is
    ignored if the worker has already moved on to a newer lease.  Returns True
    when a job was actually cancelled.
    """
    current = _current_job_id
    if not current:
        return False
    if job_id is not None and job_id != current:
        # A stale cancel response for a previous job must not kill a fresh
        # lease that started after the heartbeat was sent.
        logger.info("Ignoring stale cancel for %s (current job is %s)", job_id, current)
        return False
    _cancel_event.set()
    _kill_current_job(current)
    return True


def register_with_backoff(client: ControllerClient, profiles: list[str], max_concurrent: int,
                          sleep_fn=time.sleep) -> bool:
    """Register with bounded backoff so a broken Controller cannot cause a hot loop."""
    for attempt, delay in enumerate((0, *REGISTER_BACKOFF_SECONDS), start=1):
        if delay:
            logger.warning("Worker registration retry %d/%d in %ss", attempt - 1, len(REGISTER_BACKOFF_SECONDS), delay)
            sleep_fn(delay)
        if client.register(profiles, max_concurrent):
            return True
        logger.warning("Worker registration attempt %d/%d failed", attempt, len(REGISTER_BACKOFF_SECONDS) + 1)
    return False


def main():
    global _current_job_id, _current_lease_token, _podman_binary

    config = load_config()
    if "worker_token" not in config:
        logger.error("CI_WORKER_TOKEN not found in /etc/private-ci/worker.env")
        sys.exit(1)

    worker_id = config["worker_id"]
    base_url = config["controller_url"]
    token = config["worker_token"]
    max_concurrent = config["max_concurrent_jobs"]
    profiles_list = config["supported_profiles"]
    _podman_binary = config.get("podman_binary", "/usr/bin/podman")
    workspace_root = config["workspace_root"]
    poll_interval = config["poll_interval_seconds"]
    heartbeat_interval = config["heartbeat_interval_seconds"]
    max_source_bytes = config["max_source_bytes"]

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    client = ControllerClient(base_url, worker_id, token)

    logger.info("Registering worker %s at %s", worker_id, base_url)
    if not register_with_backoff(client, profiles_list, max_concurrent):
        logger.error("Failed to register worker")
        sys.exit(1)

    logger.info("Worker registered. Supported profiles: %s", profiles_list)

    # Startup state reconciliation
    try:
        reconciler = Reconciler(base_url, worker_id, token)
        reconciler.reconcile()
    except Exception as e:
        logger.warning("Startup reconcile failed (non-fatal): %s", e)

    # Cleanup on start
    cleanup_containers(_podman_binary, [])
    workspace_mgr = WorkspaceManager(workspace_root)
    workspace_mgr.cleanup_stale([])

    executor = JobExecutor(client, config)
    podman_runner = PodmanRunner(_podman_binary)

    # Start heartbeat thread.  Cancellation is monitored here (and in the
    # lease loop) because podman run blocks the job thread: a cancel must be
    # able to stop containers without waiting for the current step to return.
    heartbeat_stop = threading.Event()

    def heartbeat_loop():
        while not heartbeat_stop.is_set():
            try:
                job_at_send = _current_job_id
                token_at_send = _current_lease_token
                response = client.heartbeat(job_at_send, token_at_send)
                if job_at_send and response.get("cancel_requested"):
                    logger.info("Cancel requested for job %s", job_at_send)
                    _request_cancel(job_at_send)
            except Exception as e:
                logger.warning("Heartbeat failed: %s", e)
            heartbeat_stop.wait(heartbeat_interval)

    heartbeat_thread = threading.Thread(target=heartbeat_loop, daemon=True)
    heartbeat_thread.start()

    try:
        while _running:
            try:
                if _current_job_id:
                    try:
                        hb = client.heartbeat(_current_job_id, _current_lease_token)
                        if hb.get("cancel_requested"):
                            logger.info("Cancel requested for job %s", _current_job_id)
                            _request_cancel(_current_job_id)
                            client.finish_job(_current_job_id, -1, "cancelled",
                                              summary={"cancelled": True})
                            _cleanup_job(_current_job_id, workspace_mgr)
                            _current_job_id = None
                            continue
                    except Exception:
                        pass

                if _current_job_id is None:
                    result = client.lease_job()
                    if result:
                        logger.info("Leased job: %s repo=%s profile=%s",
                                    result["job_id"], result["repository"], result["profile"])

                        job = Job(
                            job_id=result["job_id"],
                            repository=result["repository"],
                            branch=result["branch"],
                            commit_sha=result["commit_sha"],
                            profile=result["profile"],
                            timeout_seconds=result["timeout_seconds"],
                            lease_token=result["lease_token"],
                            lease_expires_at=result["lease_expires_at"],
                            base_sha=result.get("base_sha", ""),
                            changed_files=result.get("changed_files", []),
                            changed_files_total=int(
                                result.get("changed_files_total")
                                or len(result.get("changed_files", []))
                            ),
                            changed_files_truncated=bool(
                                result.get("changed_files_truncated")
                            ),
                        )
                        _current_job_id = job.job_id
                        _current_lease_token = job.lease_token
                        _cancel_event.clear()

                        try:
                            _execute_job(job, client, config, workspace_mgr,
                                         max_source_bytes, podman_runner)
                        except Exception as e:
                            logger.error("Job execution failed: %s", e)
                            try:
                                client.finish_job(job.job_id, -1, "internal_error",
                                                  error_message=str(e)[:500])
                            except Exception:
                                pass
                        finally:
                            _cleanup_job(job.job_id, workspace_mgr)
                            _current_job_id = None
                            _current_lease_token = None

                time.sleep(poll_interval)
            except Exception as e:
                logger.error("Main loop error: %s", e)
                time.sleep(poll_interval)

    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=5)

        if _current_job_id:
            try:
                client.release_job(_current_job_id)
            except Exception:
                pass

        cleanup_containers(_podman_binary, [])
        logger.info("CI Agent stopped")


def _execute_job(job: Job, client, config: dict, workspace_mgr: WorkspaceManager,
                 max_source_bytes: int, podman_runner: PodmanRunner):
    job_id = job.job_id
    logger.info("Executing job %s: repo=%s sha=%.12s", job_id, job.repository, job.commit_sha)

    workspace = workspace_mgr.create(job_id)
    source_dir = workspace_mgr.get_source_dir(job_id)
    job.workspace = workspace
    job.source_dir = source_dir

    client.update_job_status(job_id, "downloading")

    if config.get("source_mirror_enabled"):
        client.upload_log(job_id, "[source] independent bare mirror mode enabled\n")
        mirror_result = prepare_source_from_mirror(job.repository, job.commit_sha, source_dir, config.get("source_mirror_root", "/srv/private-ci/cache/git"))
        if not mirror_result.get("ok"):
            client.upload_log(job_id, f"[source] error_code={mirror_result.get('error_code')}\n")
            client.finish_job(job_id, -1, "failed", summary={"status": "failed", "exit_code": -1, "error_code": mirror_result.get("error_code"), "steps": []}, error_code=mirror_result.get("error_code"), error_message="source mirror preparation failed")
            return
        client.upload_log(job_id, f"[source] mirror_hit=true head={job.commit_sha}\n")
        executor = JobExecutor(client, config, cancel_event=_cancel_event)
        summary = executor.execute(job)
        logger.info("Job %s completed: %s", job_id, summary.get("status", "unknown"))
        return

    def upload_download_log(content: str):
        # downloading 阶段不能等到 preparing 才创建 LogManager，否则失败任务会是 0 bytes。
        client.upload_log(job_id, content)

    upload_download_log("[download] entered downloading\n")
    try:
        proxy_env = refresh_proxy_before_external_access()
        upload_download_log("[download] proxy config loaded\n")
        upload_download_log(f"[download] proxy_available={proxy_env.get('PROXY_AVAILABLE') == '1'}\n")
        upload_download_log(f"[download] proxy_protocol={proxy_env.get('PROXY_PROTOCOL', 'none')}\n")
        upload_download_log(f"[download] proxy_host={proxy_env.get('PROXY_HOST', '')}\n")
        upload_download_log(f"[download] proxy_port={proxy_env.get('PROXY_PORT', '')}\n")
        upload_download_log("[download] github route=proxy\n")
        upload_download_log("[download] controller route=direct\n")
        upload_download_log("[download] local_image_store route=local\n")
    except Exception:
        upload_download_log("[download] proxy_available=false\n[download] error_code=PROXY_UNAVAILABLE\n")
        client.finish_job(job_id, -1, "failed",
                          summary={"status": "failed", "exit_code": -1,
                                   "error_code": "PROXY_UNAVAILABLE", "steps": []},
                          error_code="PROXY_UNAVAILABLE",
                          error_message="Required proxy is unavailable")
        _cleanup_download(job_id, workspace, os.path.join(workspace, "source.tar.gz"))
        return

    archive_path = os.path.join(workspace, "source.tar.gz")
    logger.info("Downloading source for %s/%s...", job.repository, job.commit_sha[:12])

    download_start = time.time()
    try:
        sha256, size_bytes = download_source_archive(
            config["controller_url"], config["worker_id"], config["worker_token"],
            job_id, archive_path, max_source_bytes,
            log_callback=upload_download_log,
        )
        download_elapsed = time.time() - download_start
        logger.info("Downloaded %d bytes, SHA256=%s in %.1fs", size_bytes, sha256[:16], download_elapsed)
        upload_download_log("[download] commit_sha_verified=true\n")

    except SourceDownloadTimeout:
        logger.error("Source download timed out for job %s", job_id)
        timeout_code = _error_code(SourceDownloadTimeout())
        upload_download_log(f"[download] error_code={timeout_code}\n")
        _cleanup_download(job_id, workspace, archive_path)
        client.finish_job(job_id, -1, "failed",
                          summary={"status": "failed", "exit_code": -1,
                                   "error_code": timeout_code, "steps": []},
                          error_code=timeout_code,
                          error_message="Source download exceeded read/total time limit")
        return

    except ProxyUnavailableError as e:
        logger.error("Proxy unavailable for job %s: %s", job_id, e)
        upload_download_log("[download] error_code=PROXY_UNAVAILABLE\n")
        _cleanup_download(job_id, workspace, archive_path)
        client.finish_job(job_id, -1, "failed",
                          summary={"status": "failed", "exit_code": -1,
                                   "error_code": "PROXY_UNAVAILABLE", "steps": []},
                          error_code="PROXY_UNAVAILABLE", error_message="Required proxy is unavailable")
        return

    except DownloadError as e:
        logger.error("Download failed for job %s: %s", job_id, e)
        error_code = _error_code(e)
        upload_download_log(f"[download] error_code={error_code}\n")
        _cleanup_download(job_id, workspace, archive_path)
        client.finish_job(job_id, -1, "failed",
                          summary={"status": "failed", "exit_code": -1,
                                   "error_code": error_code, "steps": []},
                          error_code=error_code, error_message=str(e)[:200])
        return

    # Extract source
    try:
        total_bytes, file_count = extract_source(
            archive_path, source_dir, sha256, max_source_bytes
        )
    except Exception as e:
        logger.error("Extraction failed for job %s: %s", job_id, e)
        upload_download_log("[download] error_code=SOURCE_EXTRACT_FAILED\n")
        client.finish_job(job_id, -1, "failed",
                          summary={"status": "failed", "exit_code": -1,
                                   "error_code": "SOURCE_EXTRACT_FAILED", "steps": []},
                          error_code="SOURCE_EXTRACT_FAILED",
                          error_message=str(e)[:500])
        return

    # Verify commit SHA
    sha_file = os.path.join(source_dir, ".ci_commit_sha")
    with open(sha_file, "w") as f:
        f.write(job.commit_sha + "\n")

    # Execute the job
    executor = JobExecutor(client, config, cancel_event=_cancel_event)
    summary = executor.execute(job)
    logger.info("Job %s completed: %s", job_id, summary.get("status", "unknown"))


def _cleanup_download(job_id: str, workspace: str, archive_path: str):
    if os.path.exists(archive_path):
        try:
            os.remove(archive_path)
        except Exception:
            pass


def _kill_current_job(job_id: str | None = None):
    global _current_job_id, _podman_binary
    if job_id is None:
        job_id = _current_job_id
    if not job_id:
        return
    # Containers are named ci-<job_id>[:12]-<source-hash>; stop by job prefix
    # so every container owned by the job (main + per-workspace) is reclaimed.
    runner = PodmanRunner(_podman_binary)
    try:
        runner.kill_job(job_id)
    except Exception as exc:
        logger.error("Failed to force-stop job containers: %s", exc)


def _cleanup_job(job_id: str, workspace_mgr: WorkspaceManager):
    global _podman_binary
    try:
        # Worktrees belong to the independent mirror.  Remove stale worktree
        # metadata on every cleanup; this is harmless for archive jobs and
        # prevents a failed job from poisoning the next exact-SHA checkout.
        remove_source_worktree(
            os.path.join(workspace_mgr.workspace_root, job_id, "source"),
            os.environ.get("CI_SOURCE_MIRROR_ROOT", "/srv/private-ci/cache/git"),
        )
    except Exception:
        pass
    try:
        cleanup_job_services(_podman_binary, job_id, os.path.join(workspace_mgr.workspace_root, job_id))
    except Exception:
        pass
    try:
        PodmanRunner(_podman_binary).kill_job(job_id)
    except Exception:
        pass

    workspace_mgr.cleanup(job_id)


if __name__ == "__main__":
    main()
