"""执行受控 Profile 的多工作区 CI 任务。"""

import logging
import json
import os
import threading
import time
import hashlib
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

from private_ci_agent.models import Job
from private_ci_agent.profiles import (
    FAST_CHECK_COMMANDS,
    GO_COMMANDS,
    PYTHON_COMMANDS,
    discover_workspaces,
    go_commands_for_workspace,
    get_commands_for_profile,
    get_repository_overrides,
    node_commands_for_workspace,
    python_commands_for_workspace,
)
from private_ci_agent.podman import PodmanRunner
from private_ci_agent.logs import LogManager
from private_ci_agent.services import ServiceManager, ServiceSetupError

logger = logging.getLogger(__name__)

CACHE_MAP = {
    "go": "/srv/private-ci/cache/go",
    "pip": "/srv/private-ci/cache/pip",
    "npm": "/srv/private-ci/cache/npm",
    "playwright": "/srv/private-ci/cache/ms-playwright",
}
PROFILE_BY_STACK = {"go": "go-check", "python": "python-check", "node": "node-check"}


class JobExecutor:
    def __init__(self, controller_client, config: dict, cancel_event: threading.Event | None = None):
        self.client = controller_client
        self.config = config
        self.cancel_event = cancel_event
        self.podman = PodmanRunner(config.get("podman_binary", "/usr/bin/podman"))
        self.log_manager = LogManager(controller_client, config.get("max_log_bytes", 10485760))
        self.services = ServiceManager(config.get("podman_binary", "/usr/bin/podman"), config)

    def _cancelled(self) -> bool:
        cancel_event = getattr(self, "cancel_event", None)
        return cancel_event is not None and cancel_event.is_set()

    def _cancel_summary(self, job_id: str, metadata: dict | None = None) -> dict:
        summary = {"status": "cancelled", "exit_code": -1, "steps": [], "cancelled": True}
        if metadata:
            summary = {**metadata, **summary}
        self.log_manager.upload(job_id, "JOB_CANCELLED\n")
        return summary

    def execute(self, job: Job) -> dict:
        job_id = job.job_id
        self.log_manager.reset(job_id)
        self.log_manager.upload(job_id, f"[CI] repo={job.repository} sha={job.commit_sha[:12]} profile={job.profile}\n")
        self.client.update_job_status(job_id, "preparing")

        if self._cancelled():
            self.log_manager.flush(job_id)
            summary = self._cancel_summary(job_id)
            self.client.finish_job(job_id, -1, "cancelled", summary=summary)
            return summary

        repo_config = get_repository_overrides(job.repository, job.profile)
        if job.profile == "repo-fast-check":
            self.log_manager.upload(job_id, "[fast-check] entering isolated repo-fast-check path\n")
            summary = self._execute_fast_check(job)
            self.log_manager.flush(job_id)
            self.client.finish_job(
                job_id,
                summary["exit_code"],
                summary["status"],
                summary=summary,
                error_code=summary.get("error_code"),
                error_message=summary.get("error_message"),
            )
            return summary

        if job.profile == "repo-auto-check":
            plan = self._auto_plan(job.source_dir, repo_config)
        else:
            plan = self._explicit_plan(job.profile, job.source_dir)

        metadata = {
            "detected_stacks": plan.get("detected_stacks", []),
            "selected_profiles": plan.get("selected_profiles", []),
            "workspaces": self._public_workspaces(plan.get("workspaces", [])),
            "source_mirror_hit": bool(self.config.get("source_mirror_enabled")),
            "go_version": self._command_version("go", "version"),
            "node_version": self._command_version("node", "--version"),
            "npm_version": self._command_version("npm", "--version"),
            "cpu_count": os.cpu_count(),
        }
        tree = subprocess.run(["git", "-C", job.source_dir, "rev-parse", "HEAD^{tree}"], capture_output=True, text=True, timeout=20)
        metadata["git_tree_sha"] = tree.stdout.strip() if tree.returncode == 0 else None
        metadata["evidence"] = {
            "base_sha": job.base_sha,
            "changed_files": list(job.changed_files),
            "go_sum_sha256": self._hash_files(job.source_dir, ["go.sum"]),
            "admin_lock_sha256": self._hash_files(job.source_dir, ["h5/lenshub-admin/package-lock.json", "h5/lenshub-admin/pnpm-lock.yaml"]),
            "console_lock_sha256": self._hash_files(job.source_dir, ["h5/lenshub-console/package-lock.json", "h5/lenshub-console/pnpm-lock.yaml"]),
            "test_config_sha256": self._hash_files(job.source_dir, [".github/workflows/ci.yml", ".github/workflows/repo-auto-check.yml", "scripts/ci.yml"]),
        }
        go_workspace = next((item for item in plan.get("workspaces", []) if item.get("stack") == "go"), None)
        if go_workspace:
            go_commands = go_commands_for_workspace(job.source_dir, go_workspace["path"])
            if not go_commands.get("error"):
                metadata["requested_go_version"] = go_commands["requested_go_version"]
                metadata["selected_go_version"] = go_commands["selected_go_version"]
                metadata["source_image"] = go_commands["source_image"]
                metadata["selected_image"] = go_commands["selected_image"]
                metadata["image_digest"] = self.podman.image_digest(go_commands["selected_image"])
        self.log_manager.upload(job_id, f"detected_stacks={metadata['detected_stacks']}\n")
        self.log_manager.upload(job_id, f"selected_profiles={metadata['selected_profiles']}\n")
        self.log_manager.upload(job_id, f"workspaces={metadata['workspaces']}\n")

        if plan.get("error"):
            message = plan.get("message", plan["error"])
            self.log_manager.upload(job_id, f"CONFIGURATION_ERROR: {message}\n")
            summary = {"status": "failed", "exit_code": 2, **metadata, "steps": [], "log_truncated": self.log_manager.is_truncated(job_id)}
            self.log_manager.flush(job_id)
            self.client.finish_job(job_id, 2, "failed", summary=summary, error_code="CONFIGURATION_ERROR", error_message=message)
            return summary

        all_steps = []
        all_passed = True
        final_exit_code = 0
        total_start = time.time()
        self.client.update_job_status(job_id, "running")
        try:
            # Workspaces are isolated by source path/container/cache and can run
            # concurrently; each workspace keeps its own setup->check order.
            with ThreadPoolExecutor(max_workers=min(3, len(plan["workspaces"]))) as pool:
                futures = {pool.submit(self._execute_workspace, job, workspace): workspace for workspace in plan["workspaces"]}
                results = []
                for future in as_completed(futures):
                    results.append(future.result())
                    if self._cancelled():
                        # A cancel arrived while a workspace step was running:
                        # stop every container owned by the job and drain the
                        # remaining futures (they observe the same cancel flag
                        # and terminate their steps promptly).
                        self.podman.kill_job(job_id)
                        for pending in futures:
                            if pending is future or pending.done():
                                continue
                            try:
                                results.append(pending.result(timeout=30))
                            except Exception:
                                pass
                        break
                for result in results:
                    all_steps.extend(result["steps"])
                    if not result["passed"]:
                        all_passed = False
                        final_exit_code = final_exit_code or result["exit_code"]
        finally:
            self.log_manager.flush(job_id)
            self.services.cleanup(job_id, job.workspace)

        if self._cancelled():
            summary = self._cancel_summary(job_id, metadata)
            self.log_manager.flush(job_id)
            self.client.finish_job(job_id, -1, "cancelled", summary=summary)
            return summary

        summary = {
            "status": "passed" if all_passed else "failed",
            "exit_code": final_exit_code,
            **metadata,
            "steps": all_steps,
            "total_duration_seconds": time.time() - total_start,
            "performance": self._performance(all_steps, total_start),
            "log_truncated": self.log_manager.is_truncated(job_id),
        }
        self.log_manager.flush(job_id)
        self.client.finish_job(job_id, final_exit_code, summary["status"], summary=summary)
        return summary

    @staticmethod
    def _command_version(command, argument):
        try:
            result = subprocess.run([command, argument], capture_output=True, text=True, timeout=10)
            return result.stdout.strip() if result.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            return None

    @staticmethod
    def _hash_files(root, names):
        digest = hashlib.sha256(); found = False
        for name in names:
            path = os.path.join(root, name)
            if not os.path.isfile(path):
                continue
            found = True
            with open(path, "rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(name.encode() + b"\0" + block)
        return digest.hexdigest() if found else ""

    @staticmethod
    def _performance(steps, total_start):
        buckets = {"go_test_seconds": 0.0, "admin_test_seconds": 0.0, "console_test_seconds": 0.0,
                   "typecheck_seconds": 0.0, "build_seconds": 0.0}
        for step in steps:
            name = step.get("step_name", "")
            duration = float(step.get("duration_seconds") or 0)
            if "gotest" in name or ":test:run" in name:
                key = "console_test_seconds" if "lenshub-console" in name else "admin_test_seconds" if "lenshub-admin" in name else "go_test_seconds"
            elif "typecheck" in name:
                key = "typecheck_seconds"
            elif "build" in name:
                key = "build_seconds"
            else:
                continue
            buckets[key] += duration
        buckets["total_wall_seconds"] = round(time.time() - total_start, 3)
        return buckets

    @staticmethod
    def _public_workspaces(workspaces: list[dict]) -> list[dict]:
        public = []
        for item in workspaces:
            value = {"path": item["path"], "stack": item["stack"]}
            if item.get("framework"):
                value["framework"] = item["framework"]
            if item.get("package_manager"):
                value["package_manager"] = item["package_manager"]
            public.append(value)
        return public

    def _auto_plan(self, source_dir: str, repo_config: dict) -> dict:
        detected = discover_workspaces(source_dir, repo_config)
        allowed = set(self.config.get("supported_profiles") or PROFILE_BY_STACK.values())
        selected = []
        for workspace in detected["workspaces"]:
            profile = PROFILE_BY_STACK.get(workspace["stack"])
            if profile and profile not in selected:
                if profile not in allowed:
                    return {"error": "configuration_error", "message": f"Profile {profile} is not allowed for repository", **detected}
                selected.append(profile)
        if not detected["workspaces"]:
            return {"error": "unsupported", "message": "No supported project Manifest detected", **detected}
        return {**detected, "selected_profiles": selected}

    def _explicit_plan(self, profile: str, source_dir: str) -> dict:
        if profile == "node-check":
            detected = discover_workspaces(source_dir, {"workspaces": [{"path": ".", "type": "node"}]})
            workspaces = [item for item in detected["workspaces"] if item["stack"] == "node"]
        else:
            stack = profile.removesuffix("-check")
            detected = discover_workspaces(source_dir, {"workspaces": [{"path": ".", "type": stack}]})
            workspaces = [item for item in detected["workspaces"] if item["stack"] == stack]
        if not workspaces:
            return {"error": "unsupported", "message": f"No {profile} Manifest detected", **detected, "selected_profiles": [profile]}
        return {**detected, "workspaces": workspaces, "selected_profiles": [profile]}

    def _workspace_commands(self, workspace: dict, source_dir: str) -> dict:
        stack = workspace["stack"]
        if stack == "node":
            required_default = workspace.get("required_scripts") or []
            return node_commands_for_workspace(workspace, required_default, source_dir=source_dir)
        if stack == "go":
            return go_commands_for_workspace(source_dir)
        if stack == "python":
            return python_commands_for_workspace(source_dir)
        return {"error": "unsupported", "message": f"Unsupported stack: {stack}"}

    def _execute_workspace(self, job: Job, workspace: dict) -> dict:
        path = workspace["path"]
        source_dir = job.source_dir if path == "." else f"{job.source_dir}/{path}"
        commands = self._workspace_commands(workspace, source_dir)
        label = f"{workspace['stack']}:{path}"
        if commands.get("error"):
            return {"passed": False, "exit_code": 2, "steps": [{"step_name": label, "status": "configuration_error", "exit_code": 2}]}

        image = commands.get("image", "docker.io/library/alpine:latest")
        if not self.podman.image_available(image):
            message = f"CI image is unavailable: {image}"
            self.log_manager.upload(job.job_id, f"CONFIGURATION_ERROR: {message}\n")
            return {
                "passed": False,
                "exit_code": 2,
                "steps": [{"step_name": label, "status": "configuration_error", "exit_code": 2, "message": message}],
            }
        if workspace["stack"] == "go":
            # Go 模块缓存跨 job 共享（CACHE_MAP["go"]），避免每个 job 冷下载
            # 全部依赖；缓存目录在源码目录之外，不会进入 gofmt 的扫描范围。
            cache_root = CACHE_MAP["go"]
            if not os.path.isdir(cache_root):
                os.makedirs(cache_root, mode=0o700)
            caches = {"go": cache_root}
        elif workspace["stack"] == "python":
            # Python venv must persist across container steps.
            cache_root = os.path.join(job.workspace or os.path.dirname(job.source_dir), "python-venv")
            os.makedirs(cache_root, mode=0o700, exist_ok=True)
            os.chmod(cache_root, 0o700)
            caches = {"python_venv": cache_root}
            # Also include pip cache if configured
            for cache_name in commands.get("cache_dirs", {}):
                if cache_name == "pip" and cache_name in CACHE_MAP:
                    caches[cache_name] = CACHE_MAP[cache_name]
        else:
            caches = {name: CACHE_MAP[name] for name in commands.get("cache_dirs", {}) if name in CACHE_MAP}
        steps = []
        service_env = None
        if workspace["stack"] == "go":
            self.log_manager.upload(job.job_id, "[services:prepare] Starting isolated PostgreSQL/Redis/RabbitMQ\n")
            try:
                service_env = self.services.prepare(job.job_id, job.workspace)
                self.log_manager.upload(job.job_id, f"[services:ready] {service_env.public_summary()}\n")
            except ServiceSetupError as exc:
                self.log_manager.upload(job.job_id, f"[services:failed] {exc.diagnostic}\n")
                return {
                    "passed": False,
                    "exit_code": 1,
                    "steps": [{
                        "step_name": "services:prepare",
                        "status": "failed",
                        "exit_code": 1,
                        "error_code": exc.code,
                        "message": exc.diagnostic,
                    }],
                }
        passed = True
        exit_code = 0

        setup_failed = False
        for setup in commands.get("setup", []):
            if self._cancelled():
                step = {"step_name": f"{label}:{setup.get('name', 'setup') if isinstance(setup, dict) else 'setup'}",
                        "status": "cancelled", "exit_code": None, "duration_seconds": 0}
                steps.append(step)
                passed = False
                self.log_manager.upload(job.job_id, f"[{step['step_name']}] CANCELLED\n")
                continue
            if setup_failed:
                # A later setup step depends on the successful earlier one
                # (npm ci before browser preheat, go mod download before
                # migrate).  Do not run it after a previous setup failed;
                # report the dependency failure explicitly and keep every
                # dependent step blocked by setup.
                name = setup.get("name", "setup") if isinstance(setup, dict) else "setup"
                command = setup.get("command") if isinstance(setup, dict) else setup
                blocked_step = {
                    "step_name": f"{label}:{name}",
                    "command": command,
                    "status": "blocked_by_setup",
                    "exit_code": None,
                    "duration_seconds": 0,
                }
                steps.append(blocked_step)
                self.log_manager.upload(job.job_id, f"[{blocked_step['step_name']}] BLOCKED_BY_SETUP\n")
                continue
            name = setup.get("name", "setup") if isinstance(setup, dict) else "setup"
            command = setup.get("command") if isinstance(setup, dict) else setup
            # Dependency installation and migrations may need outbound access.
            # Podman receives proxies only through its controlled pass_proxy path.
            step = self._run_setup(job, label, image, source_dir, caches, name, command, service_env,
                                   pass_proxy=True)
            steps.append(step)
            if step["exit_code"] != 0:
                setup_failed = True
                passed = False
                exit_code = exit_code or step["exit_code"]

        for skipped in commands.get("skipped", []):
            step = {"step_name": f"{label}:{skipped['name']}", "status": skipped["status"], "exit_code": None}
            steps.append(step)
            if skipped["status"] == "configuration_error":
                passed = False
                exit_code = exit_code or 2
            self.log_manager.upload(job.job_id, f"[{step['step_name']}] {skipped['status']}: {skipped.get('reason')}\n")

        for check in commands.get("check", []):
            if self._cancelled():
                step = {"step_name": f"{label}:{check['name']}", "command": check["command"],
                        "status": "cancelled", "exit_code": None, "duration_seconds": 0}
                steps.append(step)
                passed = False
                self.log_manager.upload(job.job_id, f"[{step['step_name']}] CANCELLED\n")
                continue
            if setup_failed and (
                (workspace["stack"] == "go" and check["name"] in {"govet", "gotest", "gobuild"})
                or workspace["stack"] in {"node", "python"}
            ):
                step = {
                    "step_name": f"{label}:{check['name']}",
                    "command": check["command"],
                    "status": "blocked_by_setup",
                    "exit_code": None,
                    "duration_seconds": 0,
                }
                steps.append(step)
                self.log_manager.upload(job.job_id, f"[{step['step_name']}] BLOCKED_BY_SETUP\n")
                continue
            if setup_failed and workspace["stack"] == "python":
                step = {
                    "step_name": f"{label}:{check['name']}",
                    "command": check["command"],
                    "status": "blocked_by_setup",
                    "exit_code": None,
                    "duration_seconds": 0,
                }
                steps.append(step)
                self.log_manager.upload(job.job_id, f"[{step['step_name']}] BLOCKED_BY_SETUP\n")
                continue
            extra_env = None
            if check["name"] == "ai-integrity":
                extra_env = {
                    "AI_INTEGRITY_BASE_SHA": job.base_sha,
                    "AI_INTEGRITY_REPORT": "/tmp/ai-integrity-report.json",
                }
                if job.changed_files and not getattr(job, "changed_files_truncated", False):
                    extra_env["AI_INTEGRITY_CHANGED_FILES"] = "\n".join(job.changed_files)
            step = self._run_check(
                job,
                label,
                image,
                source_dir,
                caches,
                check["name"],
                check["command"],
                service_env,
                extra_env=extra_env,
                pass_proxy=False,
            )
            steps.append(step)
            if step["exit_code"] != 0:
                passed = False
                exit_code = exit_code or step["exit_code"]

        return {"passed": passed, "exit_code": exit_code, "steps": steps}

    def _run_setup(self, job, label, image, source_dir, caches, step_name, command, service_env=None, pass_proxy=False):
        name = f"{label}:{step_name}"
        self.log_manager.upload(job.job_id, f"[{name}] Starting: {command}\n")
        start = time.time()
        result = self.podman.run_command(image, job.job_id, source_dir, caches, command, self._setup_timeout(job), network=True,
                                         env=self._service_env(service_env), network_name=service_env.network if service_env else None,
                                         pass_proxy=pass_proxy, cancel_event=getattr(self, "cancel_event", None))
        self._upload_output(job.job_id, result)
        status = "passed" if result["exit_code"] == 0 else ("timed_out" if result["timed_out"] else ("cancelled" if result.get("cancelled") else "failed"))
        self.log_manager.upload(job.job_id, f"[{name}] {status.upper()} (exit={result['exit_code']})\n")
        return {"step_name": name, "command": command, "status": status, "exit_code": result["exit_code"], "duration_seconds": time.time() - start}

    def _setup_timeout(self, job: Job) -> int:
        """Bound setup by both the leased job deadline and the agent policy."""
        configured_maximum = int(getattr(self, "config", {}).get("max_job_seconds", job.timeout_seconds))
        return max(1, min(job.timeout_seconds, configured_maximum))

    def _run_check(self, job, label, image, source_dir, caches, name, command, service_env=None, extra_env=None, pass_proxy=False):
        step_name = f"{label}:{name}"
        self.log_manager.upload(job.job_id, f"[{step_name}] Starting: {command}\n")
        step_id = self.client.start_step(job.job_id, step_name)
        start = time.time()
        env = self._service_env(service_env) or {}
        if extra_env:
            env.update(extra_env)
        result = self.podman.run_command(image, job.job_id, source_dir, caches, command, job.timeout_seconds,
                                         network=True if service_env else False, env=env if env else None,
                                         network_name=service_env.network if service_env else None,
                                         pass_proxy=pass_proxy, cancel_event=getattr(self, "cancel_event", None))
        self._upload_output(job.job_id, result)
        status = "passed" if result["exit_code"] == 0 else ("timed_out" if result["timed_out"] else ("cancelled" if result.get("cancelled") else "failed"))
        duration = time.time() - start
        self.log_manager.upload(job.job_id, f"[{step_name}] {status.upper()} (exit={result['exit_code']}, {duration:.1f}s)\n")
        if step_id:
            self.client.finish_step(job.job_id, step_id, status, result["exit_code"], self.log_manager.get_total(job.job_id))
        return {"step_name": step_name, "command": command, "status": status, "exit_code": result["exit_code"], "duration_seconds": duration, "step_id": step_id}

    @staticmethod
    def _service_env(service_env):
        if not service_env:
            return None
        return {"DATABASE_URL": service_env.database_url, "REDIS_ADDR": service_env.redis_addr,
                "REDIS_PASSWORD": "", "REDIS_DB": service_env.redis_db, "RABBITMQ_URL": service_env.rabbitmq_url}

    def _upload_output(self, job_id, result):
        output = result.get("stdout", "")
        if result.get("stderr"):
            output += "\n" + result["stderr"]
        if output:
            self.log_manager.upload(job_id, output)
            if not output.endswith("\n"):
                self.log_manager.upload(job_id, "\n")

    FAST_CHECK_ERROR_CODES = {
        "missing_make": "FAST_CHECK_MAKE_MISSING",
        "entrypoint_missing": "FAST_CHECK_ENTRYPOINT_MISSING",
        "timeout": "FAST_CHECK_TIMEOUT",
        "integrity_failed": "FAST_CHECK_INTEGRITY_FAILED",
    }

    def _execute_fast_check(self, job: Job) -> dict:
        """Run the repository integrity target without network access."""
        job_id = job.job_id
        source_dir = job.source_dir
        started = time.time()
        self.client.update_job_status(job_id, "running")

        make = subprocess.run(["which", "make"], capture_output=True, text=True, timeout=10)
        if make.returncode != 0:
            return self._fast_check_configuration_error(
                job_id, "FAST_CHECK_MAKE_MISSING", "make is not installed"
            )

        target = subprocess.run(
            ["make", "-C", source_dir, "-n", "ai-integrity-check"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if target.returncode != 0:
            return self._fast_check_configuration_error(
                job_id,
                "FAST_CHECK_ENTRYPOINT_MISSING",
                "Make target ai-integrity-check is unavailable",
            )

        image = FAST_CHECK_COMMANDS["image"]
        if not self.podman.image_available(image, allow_pull=False):
            return self._fast_check_configuration_error(
                job_id, "FAST_CHECK_IMAGE_UNAVAILABLE", f"Image {image} is not prewarmed"
            )

        artifacts_dir = os.path.join(
            job.workspace or os.path.dirname(source_dir), "artifacts"
        )
        os.makedirs(artifacts_dir, mode=0o700, exist_ok=True)
        env = {
            "AI_INTEGRITY_BASE_SHA": job.base_sha,
            "AI_INTEGRITY_REPORT": "/ci-artifacts/ai-integrity-report.json",
        }
        if job.changed_files and not getattr(job, "changed_files_truncated", False):
            env["AI_INTEGRITY_CHANGED_FILES"] = "\n".join(job.changed_files)

        step_name = "repo-fast-check:ai-integrity"
        step_id = self.client.start_step(job_id, step_name)
        step_started = time.time()
        result = self.podman.run_command(
            image,
            job_id,
            source_dir,
            {},
            "make ai-integrity-check 2>&1",
            60,
            env=env,
            network=False,
            extra_mounts=[f"{artifacts_dir}:/ci-artifacts:Z"],
            pass_proxy=False,
            cancel_event=getattr(self, "cancel_event", None),
        )
        self._upload_output(job_id, result)
        timed_out = bool(result.get("timed_out"))
        cancelled = bool(result.get("cancelled"))
        exit_code = -1 if (timed_out or cancelled) else int(result.get("exit_code", -1))
        status = ("timed_out" if timed_out else ("cancelled" if cancelled else ("passed" if exit_code == 0 else "failed")))
        duration = time.time() - step_started
        if step_id:
            self.client.finish_step(
                job_id, step_id, status, exit_code, self.log_manager.get_total(job_id)
            )

        steps = [{
            "step_name": step_name,
            "command": "make ai-integrity-check",
            "status": status,
            "exit_code": exit_code,
            "duration_seconds": round(duration, 3),
        }]
        report_path = os.path.join(artifacts_dir, "ai-integrity-report.json")
        if os.path.isfile(report_path):
            try:
                with open(report_path, encoding="utf-8") as handle:
                    report = json.load(handle)
                steps.append({
                    "step_name": "repo-fast-check:report",
                    "status": "collected",
                    "exit_code": None,
                    "report_summary": {
                        key: report.get(key)
                        for key in ("status", "errors", "warnings", "details")
                        if isinstance(report, dict) and key in report
                    },
                })
            except (OSError, json.JSONDecodeError):
                pass

        summary = {
            "status": "passed" if exit_code == 0 else ("cancelled" if cancelled else "failed"),
            "exit_code": exit_code,
            "profile": "repo-fast-check",
            "base_sha": job.base_sha,
            "commit_sha": job.commit_sha,
            "image": image,
            "image_digest": self.podman.image_digest(image),
            "source_mirror_hit": bool(self.config.get("source_mirror_enabled")),
            "resource_policy": self.podman.resource_summary(),
            "ai_integrity_seconds": round(duration, 3),
            "total_duration_seconds": round(time.time() - started, 3),
            "steps": steps,
            "log_truncated": self.log_manager.is_truncated(job_id),
        }
        if cancelled:
            summary["cancelled"] = True
        if timed_out:
            summary["error_code"] = self.FAST_CHECK_ERROR_CODES["timeout"]
        elif cancelled:
            summary["error_code"] = "FAST_CHECK_CANCELLED"
        elif exit_code != 0:
            summary["error_code"] = self.FAST_CHECK_ERROR_CODES["integrity_failed"]
        return summary

    def _fast_check_configuration_error(self, job_id, error_code, message):
        self.log_manager.upload(job_id, f"[repo-fast-check] {error_code}: {message}\n")
        return {
            "status": "failed",
            "exit_code": 2,
            "profile": "repo-fast-check",
            "error_code": error_code,
            "error_message": message,
            "steps": [],
            "log_truncated": self.log_manager.is_truncated(job_id),
        }
