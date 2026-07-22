"""执行受控 Profile 的多工作区 CI 任务。"""

import logging
import os
import time
import hashlib
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

from private_ci_agent.models import Job
from private_ci_agent.profiles import (
    GO_COMMANDS,
    PYTHON_COMMANDS,
    discover_workspaces,
    go_commands_for_workspace,
    get_commands_for_profile,
    get_repository_overrides,
    node_commands_for_workspace,
)
from private_ci_agent.podman import PodmanRunner
from private_ci_agent.logs import LogManager
from private_ci_agent.services import ServiceManager, ServiceSetupError

logger = logging.getLogger(__name__)

CACHE_MAP = {"go": "/srv/private-ci/cache/go", "python": "/srv/private-ci/cache/pip", "node": "/srv/private-ci/cache/npm"}
PROFILE_BY_STACK = {"go": "go-check", "python": "python-check", "node": "node-check"}


class JobExecutor:
    def __init__(self, controller_client, config: dict):
        self.client = controller_client
        self.config = config
        self.podman = PodmanRunner(config.get("podman_binary", "/usr/bin/podman"))
        self.log_manager = LogManager(controller_client, config.get("max_log_bytes", 10485760))
        self.services = ServiceManager(config.get("podman_binary", "/usr/bin/podman"), config)

    def execute(self, job: Job) -> dict:
        job_id = job.job_id
        self.log_manager.reset(job_id)
        self.log_manager.upload(job_id, f"[CI] repo={job.repository} sha={job.commit_sha[:12]} profile={job.profile}\n")
        self.client.update_job_status(job_id, "preparing")

        repo_config = get_repository_overrides(job.repository, job.profile)
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
                for result in results:
                    all_steps.extend(result["steps"])
                    if not result["passed"]:
                        all_passed = False
                        final_exit_code = final_exit_code or result["exit_code"]
        finally:
            self.log_manager.flush(job_id)
            self.services.cleanup(job_id, job.workspace)

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
        allowed = set(repo_config.get("allowed_profiles") or ("repo-auto-check",))
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
            return node_commands_for_workspace(workspace, required_default)
        if stack == "go":
            return go_commands_for_workspace(source_dir)
        if stack == "python":
            return PYTHON_COMMANDS
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
            # Go 缓存必须和源码目录分离，并按 job 隔离，避免第三方模块进入 gofmt 的源码扫描范围。
            cache_root = os.path.join(job.workspace or os.path.dirname(job.source_dir), "go-cache")
            os.makedirs(cache_root, mode=0o700, exist_ok=True)
            os.chmod(cache_root, 0o700)
            caches = {"go": cache_root}
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
                self.log_manager.upload(job.job_id, f"[services:failed] {exc.code}\n")
                return {"passed": False, "exit_code": 1, "steps": [{"step_name": "services:prepare", "status": "failed", "exit_code": 1, "error_code": exc.code}]}
        passed = True
        exit_code = 0

        setup_failed = False
        for setup in commands.get("setup", []):
            name = setup.get("name", "setup") if isinstance(setup, dict) else "setup"
            command = setup.get("command") if isinstance(setup, dict) else setup
            step = self._run_setup(job, label, image, source_dir, caches, name, command, service_env)
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
            if setup_failed and workspace["stack"] == "go" and check["name"] in {"govet", "gotest", "gobuild"}:
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
            step = self._run_check(job, label, image, source_dir, caches, check["name"], check["command"], service_env)
            steps.append(step)
            if step["exit_code"] != 0:
                passed = False
                exit_code = exit_code or step["exit_code"]

        return {"passed": passed, "exit_code": exit_code, "steps": steps}

    def _run_setup(self, job, label, image, source_dir, caches, step_name, command, service_env=None):
        name = f"{label}:{step_name}"
        self.log_manager.upload(job.job_id, f"[{name}] Starting: {command}\n")
        start = time.time()
        result = self.podman.run_command(image, job.job_id, source_dir, caches, command, 300, network=True,
                                         env=self._service_env(service_env), network_name=service_env.network if service_env else None)
        self._upload_output(job.job_id, result)
        status = "passed" if result["exit_code"] == 0 else ("timed_out" if result["timed_out"] else "failed")
        self.log_manager.upload(job.job_id, f"[{name}] {status.upper()} (exit={result['exit_code']})\n")
        return {"step_name": name, "command": command, "status": status, "exit_code": result["exit_code"], "duration_seconds": time.time() - start}

    def _run_check(self, job, label, image, source_dir, caches, name, command, service_env=None):
        step_name = f"{label}:{name}"
        self.log_manager.upload(job.job_id, f"[{step_name}] Starting: {command}\n")
        step_id = self.client.start_step(job.job_id, step_name)
        start = time.time()
        result = self.podman.run_command(image, job.job_id, source_dir, caches, command, job.timeout_seconds,
                                         network=True if service_env else False, env=self._service_env(service_env),
                                         network_name=service_env.network if service_env else None)
        self._upload_output(job.job_id, result)
        status = "passed" if result["exit_code"] == 0 else ("timed_out" if result["timed_out"] else "failed")
        duration = time.time() - start
        self.log_manager.upload(job.job_id, f"[{step_name}] {status.upper()} (exit={result['exit_code']}, {duration:.1f}s)\n")
        if step_id:
            self.client.finish_step(job.job_id, step_id, status, result["exit_code"], self.log_manager.get_total(job.job_id))
        return {"step_name": step_name, "command": command, "status": status, "exit_code": result["exit_code"], "duration_seconds": duration}

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
