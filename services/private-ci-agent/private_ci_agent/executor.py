"""执行受控 Profile 的多工作区 CI 任务。"""

import logging
import json
import os
import threading
import time
import hashlib
import subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from private_ci_agent.models import Job
from private_ci_agent.affected_selection import select_affected
from private_ci_agent.contract_integrity import verify_product_contract_integrity
from private_ci_agent.environment_cache import (
    DependencyEnvironmentCache,
    EnvironmentCacheError,
)
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
    apply_workspace_hooks,
)
from private_ci_agent.podman import PodmanRunner
from private_ci_agent.logs import LogManager
from private_ci_agent.services import MultiDataPlaneServiceManager, ServiceSetupError, service_evidence_name

logger = logging.getLogger(__name__)

WRITABLE_CACHE_NAMES = ("go", "pip", "npm", "cargo", "maven", "gradle", "nuget")


def build_cache_map(config: dict) -> dict:
    writable_root = config.get("writable_cache_root", config.get("cache_root", "/srv/private-ci/cache"))
    shared_root = config.get("shared_cache_root", "/srv/private-ci/cache")
    result = {name: os.path.join(writable_root, name) for name in WRITABLE_CACHE_NAMES}
    result["playwright"] = config.get("shared_playwright_cache", os.path.join(shared_root, "ms-playwright"))
    return result


# Backward-compatible module-level defaults for tests/importers. Runtime jobs
# always use the instance map derived from the fixed Worker configuration.
CACHE_MAP = build_cache_map({})
PROFILE_BY_STACK = {
    "go": "go-check",
    "python": "python-check",
    "node": "node-check",
    "rust": "rust-check",
    "maven": "maven-check",
    "gradle": "gradle-check",
    "dotnet": "dotnet-check",
}


class JobExecutor:
    def __init__(self, controller_client, config: dict, cancel_event: threading.Event | None = None):
        self.client = controller_client
        self.config = config
        self.cancel_event = cancel_event
        self.cache_map = build_cache_map(config)
        self.podman = PodmanRunner(
            config.get("podman_binary", "/usr/bin/podman"), config.get("worker_id")
        )
        self.log_manager = LogManager(controller_client, config.get("max_log_bytes", 10485760))
        self.services = MultiDataPlaneServiceManager(config.get("podman_binary", "/usr/bin/podman"), config)
        self.environment_cache = DependencyEnvironmentCache(
            config.get("environment_cache_root", "/srv/private-ci/cache/environments")
        )

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

        if job.profile in {"repo-fast-check", "repo-auto-check"}:
            contract_gate = verify_product_contract_integrity(
                job.repository, job.source_dir, job.base_sha, job.commit_sha
            )
            if contract_gate.get("applicable"):
                self.log_manager.upload(
                    job_id,
                    "[contract-integrity] checked_entries="
                    f"{contract_gate.get('checked_entries', 0)}\n",
                )
                if not contract_gate.get("ok"):
                    for finding in contract_gate.get("errors", []):
                        self.log_manager.upload(
                            job_id,
                            "[contract-integrity] "
                            f"{finding.get('code')} {finding.get('path')}: "
                            f"{finding.get('message')}\n",
                        )
                    summary = {
                        "status": "failed",
                        "exit_code": 2,
                        "steps": [],
                        "contract_integrity": contract_gate,
                    }
                    self.log_manager.flush(job_id)
                    self.client.finish_job(
                        job_id,
                        2,
                        "failed",
                        summary=summary,
                        error_code="CONTRACT_INTEGRITY_FAILED",
                        error_message="trusted product contract integrity gate failed",
                    )
                    return summary
                job.contract_integrity_attested = True

        repo_config = get_repository_overrides(job.repository, job.profile)
        if job.profile == "openapi-check":
            self.log_manager.upload(job_id, "[openapi-check] entering fixed controlled profile\n")
            summary = self._execute_openapi_check(job)
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
            plan = self._explicit_plan(job.profile, job.source_dir, repo_config)

        affected = select_affected(
            job.changed_files,
            plan.get("workspaces", []),
            truncated=job.changed_files_truncated,
            affected_only=False,
        )

        metadata = {
            "detected_stacks": plan.get("detected_stacks", []),
            "selected_profiles": plan.get("selected_profiles", []),
            "workspaces": self._public_workspaces(plan.get("workspaces", [])),
            "source_mirror_hit": bool(self.config.get("source_mirror_enabled")),
            "go_version": self._command_version("go", "version"),
            "node_version": self._command_version("node", "--version"),
            "npm_version": self._command_version("npm", "--version"),
            "cpu_count": os.cpu_count(),
            "affected_selection": affected,
        }
        tree = subprocess.run(["git", "-C", job.source_dir, "rev-parse", "HEAD^{tree}"], capture_output=True, text=True, timeout=20)
        metadata["git_tree_sha"] = tree.stdout.strip() if tree.returncode == 0 else None
        metadata["evidence"] = {
            "base_sha": job.base_sha,
            "changed_files": list(job.changed_files),
            "changed_files_total": job.changed_files_total or len(job.changed_files),
            "changed_files_truncated": job.changed_files_truncated,
            "dependency_manifest_sha256": self._hash_dependency_manifests(
                job.source_dir, plan.get("workspaces", [])
            ),
            "test_config_sha256": self._hash_test_config(job.source_dir),
        }
        go_workspace = next((item for item in plan.get("workspaces", []) if item.get("stack") == "go"), None)
        if go_workspace:
            go_commands = go_commands_for_workspace(job.source_dir, go_workspace["path"])
            if not go_commands.get("error"):
                metadata["requested_go_version"] = go_commands["requested_go_version"]
                metadata["selected_go_version"] = go_commands["selected_go_version"]
                metadata["source_image"] = go_commands["source_image"]
                metadata["selected_image"] = go_commands["selected_image"]
        self.log_manager.upload(job_id, f"detected_stacks={metadata['detected_stacks']}\n")
        self.log_manager.upload(job_id, f"selected_profiles={metadata['selected_profiles']}\n")
        self.log_manager.upload(job_id, f"workspaces={metadata['workspaces']}\n")
        self.log_manager.upload(
            job_id, f"affected_selection={metadata['affected_selection']['reasons']}\n"
        )

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
                metadata["environment_cache"] = [
                    result["environment_cache"]
                    for result in results
                    if result.get("environment_cache")
                ]
        finally:
            self.log_manager.flush(job_id)
            self.services.cleanup(job_id, job.workspace)

        if self._cancelled():
            summary = self._cancel_summary(job_id, metadata)
            self.log_manager.flush(job_id)
            self.client.finish_job(job_id, -1, "cancelled", summary=summary)
            return summary

        images = self._workspace_images(plan.get("workspaces", []), job.source_dir, job=job)
        metadata["images"] = images
        image_identity_ok = self._images_have_immutable_identity(images)
        metadata["image_digest"] = self._hash_json(images) if images and image_identity_ok else ""
        if not image_identity_ok:
            all_passed = False
            final_exit_code = final_exit_code or 2
            all_steps.append({
                "step_name": "images:identity",
                "status": "failed",
                "exit_code": 2,
                "error_code": "IMAGE_DIGEST_UNAVAILABLE",
            })
            self.log_manager.upload(
                job_id,
                "IMAGE_DIGEST_UNAVAILABLE: every runtime/service image must have an immutable local identity\n",
            )

        source_ok, source_error = self._verify_source_immutable(job)
        metadata["source_immutable"] = source_ok
        metadata["evidence"]["source_immutable"] = source_ok
        if not source_ok:
            all_passed = False
            final_exit_code = final_exit_code or 2
            all_steps.append({
                "step_name": "source:immutable",
                "status": "failed",
                "exit_code": 2,
                "error_code": "SOURCE_MUTATED",
            })
            self.log_manager.upload(job_id, f"SOURCE_MUTATED: {source_error}\n")

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
    def _hash_json(value) -> str:
        return hashlib.sha256(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @classmethod
    def _hash_paths(cls, root: str, names: list[str]) -> str:
        records = []
        for name in sorted(set(names)):
            normalized = name.replace("\\", "/")
            while normalized.startswith("./"):
                normalized = normalized[2:]
            path = os.path.join(root, *normalized.split("/"))
            if not os.path.isfile(path):
                continue
            digest = hashlib.sha256()
            with open(path, "rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
            records.append([normalized, digest.hexdigest()])
        return cls._hash_json(records)

    @classmethod
    def _hash_dependency_manifests(cls, root: str, workspaces: list[dict]) -> str:
        fixed = {
            "go": ("go.mod", "go.sum", "go.work", "go.work.sum", "vendor/modules.txt"),
            "node": (
                "package.json", "package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml",
                "yarn.lock", "bun.lock", "bun.lockb",
            ),
            "python": (
                "pyproject.toml", "requirements.txt", "requirements-dev.txt", "setup.py",
                "setup.cfg", "Pipfile", "Pipfile.lock", "poetry.lock", "uv.lock",
            ),
            "rust": ("Cargo.toml", "Cargo.lock", ".cargo/config", ".cargo/config.toml"),
            "maven": (
                "pom.xml", ".mvn/extensions.xml", ".mvn/maven.config",
                ".mvn/wrapper/maven-wrapper.properties",
            ),
            "gradle": (
                "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts",
                "gradle.properties", "gradle.lockfile", "gradle/libs.versions.toml",
                "gradle/wrapper/gradle-wrapper.properties", "gradle/verification-metadata.xml",
            ),
            "dotnet": (
                "Directory.Build.props", "Directory.Build.targets", "Directory.Packages.props",
                "NuGet.Config", "global.json", "packages.lock.json",
            ),
        }
        names = []
        for workspace in workspaces:
            rel = "" if workspace.get("path") in ("", ".") else workspace["path"].strip("/") + "/"
            stack = workspace.get("stack")
            for name in fixed.get(stack, ()):
                names.append(rel + name)

            directory = Path(root) / rel
            try:
                if stack == "python":
                    for path in directory.glob("requirements*.txt"):
                        if path.is_file():
                            names.append(rel + path.name)
                elif stack == "gradle":
                    for pattern in ("gradle/dependency-locks/*.lockfile", "*.lockfile"):
                        for path in directory.glob(pattern):
                            if path.is_file():
                                names.append(path.relative_to(root).as_posix())
                elif stack == "dotnet":
                    for pattern in ("*.sln", "*.csproj", "*.fsproj", "*.vbproj"):
                        for path in directory.glob(pattern):
                            if path.is_file():
                                names.append(path.relative_to(root).as_posix())
            except (OSError, ValueError):
                pass
        return cls._hash_paths(root, names)

    @classmethod
    def _hash_test_config(cls, root: str) -> str:
        names = [
            "Makefile", "Taskfile.yml", "Taskfile.yaml", "justfile", "tox.ini",
            "pytest.ini", "ruff.toml", ".golangci.yml", ".golangci.yaml",
        ]
        workflows = os.path.join(root, ".github", "workflows")
        if os.path.isdir(workflows):
            for name in sorted(os.listdir(workflows)):
                if name.endswith((".yml", ".yaml")):
                    names.append(f".github/workflows/{name}")
        return cls._hash_paths(root, names)

    @staticmethod
    def _images_have_immutable_identity(images: list[dict]) -> bool:
        return all(bool(item.get("digest")) for item in images)

    def _workspace_images(
        self, workspaces: list[dict], source_dir: str, *, job: Job | None = None
    ) -> list[dict]:
        records = []
        service_images = getattr(self.services, "images", {}) or {}
        for workspace in workspaces:
            workspace_path = workspace.get("path", ".")
            workspace_source = (
                source_dir
                if workspace_path in ("", ".")
                else os.path.join(source_dir, workspace_path)
            )
            commands = self._workspace_commands(workspace, workspace_source, job=job)
            if commands.get("error"):
                continue
            image = commands.get("image", "")
            records.append({
                "workspace": workspace_path,
                "stack": workspace.get("stack", ""),
                "image": image,
                "digest": self.podman.image_digest(image) or "",
            })
            for service in workspace.get("services") or []:
                image = service_images.get(service, "")
                if not image:
                    continue
                records.append({
                    "workspace": workspace_path,
                    "stack": f"service:{service_evidence_name(service)}",
                    "image": image,
                    "digest": self.podman.image_digest(image) or "",
                })
        return sorted(
            records, key=lambda item: (item["workspace"], item["stack"], item["image"])
        )

    @staticmethod
    def _verify_source_immutable(job: Job) -> tuple[bool, str]:
        try:
            head = subprocess.run(
                ["git", "-C", job.source_dir, "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=20,
            )
            if head.returncode != 0 or head.stdout.strip() != job.commit_sha:
                return False, "head_sha_changed"
            worktree = subprocess.run(
                ["git", "-C", job.source_dir, "diff", "--quiet", "HEAD", "--"],
                timeout=20,
            )
            index = subprocess.run(
                ["git", "-C", job.source_dir, "diff", "--cached", "--quiet", "HEAD", "--"],
                timeout=20,
            )
            if worktree.returncode != 0 or index.returncode != 0:
                return False, "tracked_files_changed"
            return True, ""
        except (OSError, subprocess.TimeoutExpired):
            return False, "immutability_check_failed"

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

    def _explicit_plan(self, profile: str, source_dir: str, repo_config: dict | None = None) -> dict:
        stack = profile.removesuffix("-check")
        configured_workspaces = (repo_config or {}).get("workspaces") or []
        if configured_workspaces:
            discovery_config = repo_config
        else:
            discovery_config = {"workspaces": [{"path": ".", "type": stack}]}
        detected = discover_workspaces(source_dir, discovery_config)
        workspaces = [item for item in detected["workspaces"] if item["stack"] == stack]
        if not workspaces:
            return {"error": "unsupported", "message": f"No {profile} Manifest detected", **detected, "selected_profiles": [profile]}
        return {**detected, "workspaces": workspaces, "selected_profiles": [profile]}

    def _workspace_commands(
        self, workspace: dict, source_dir: str, *, job: Job | None = None
    ) -> dict:
        if workspace.get("configuration_error"):
            return {"error": "configuration_error", "message": workspace["configuration_error"]}
        stack = workspace["stack"]
        if stack == "node":
            required_default = workspace.get("required_scripts") or []
            commands = node_commands_for_workspace(workspace, required_default, source_dir=source_dir)
        elif stack == "go":
            commands = go_commands_for_workspace(source_dir)
        elif stack == "python":
            commands = python_commands_for_workspace(
                source_dir,
                repository=job.repository if job else "",
                logical_workspace=workspace.get("path", "."),
                profile=job.profile if job else "python-check",
            )
        elif stack in {"rust", "maven", "gradle", "dotnet"}:
            commands = get_commands_for_profile(PROFILE_BY_STACK[stack], source_dir)
        else:
            return {"error": "unsupported", "message": f"Unsupported stack: {stack}"}
        if commands.get("error"):
            return commands
        return apply_workspace_hooks(commands, workspace)

    def _restore_python_environment(
        self,
        job: Job,
        workspace: dict,
        commands: dict,
        image: str,
        destination: str,
    ) -> dict:
        cache = getattr(self, "environment_cache", None)
        if cache is None:
            return {"enabled": False, "reason": "not_configured"}
        image_digest = self.podman.image_digest(image) or ""
        if not image_digest:
            return {"enabled": False, "reason": "image_digest_unavailable"}
        key = cache.build_key(
            repository=job.repository,
            workspace=workspace.get("path", "."),
            stack="python",
            profile=job.profile,
            manifest_sha256=commands["manifest_sha256"],
            image_digest=image_digest,
            runtime_identity=commands["runtime_identity"],
        )
        try:
            restored = cache.restore(key, destination)
        except (EnvironmentCacheError, OSError) as exc:
            return {
                "enabled": True,
                "key": key,
                "hit": False,
                "reason": type(exc).__name__,
            }
        return {
            "enabled": True,
            "key": key,
            "workspace": workspace.get("path", "."),
            "manifest_sha256": commands["manifest_sha256"],
            "image_digest": image_digest,
            **restored,
        }

    def _publish_python_environment(
        self, cache_state: dict, source: str, commands: dict
    ) -> dict:
        cache = getattr(self, "environment_cache", None)
        if cache is None or not cache_state.get("enabled") or cache_state.get("hit"):
            return cache_state
        try:
            published = cache.publish(
                cache_state["key"],
                source,
                {
                    "workspace": cache_state.get("workspace", "."),
                    "manifest_sha256": commands["manifest_sha256"],
                    "image_digest": cache_state.get("image_digest", ""),
                    "runtime_identity": commands["runtime_identity"],
                },
            )
        except (EnvironmentCacheError, OSError) as exc:
            return {**cache_state, "published": False, "publish_error": type(exc).__name__}
        return {**cache_state, **published}

    def _execute_workspace(self, job: Job, workspace: dict) -> dict:
        path = workspace["path"]
        source_dir = job.source_dir if path == "." else f"{job.source_dir}/{path}"
        commands = self._workspace_commands(workspace, source_dir, job=job)
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
            # Go 下载缓存只在当前 Worker 内共享；跨 Worker 仅共享 sealed/只读资产。
            # 缓存目录在源码目录之外，不会进入 gofmt 的扫描范围。
            cache_root = self.cache_map["go"]
            if not os.path.isdir(cache_root):
                os.makedirs(cache_root, mode=0o700)
            caches = {"go": cache_root}
        elif workspace["stack"] == "python":
            # Every job restores a sealed environment into a private writable
            # directory. Multiple Python workspaces never share that directory.
            cache_root = os.path.join(
                job.workspace or os.path.dirname(job.source_dir),
                "python-environments",
                commands["workspace_key"],
            )
            os.makedirs(cache_root, mode=0o700, exist_ok=True)
            os.chmod(cache_root, 0o700)
            caches = {"python_venv": cache_root}
            # Also include pip cache if configured
            for cache_name in commands.get("cache_dirs", {}):
                if cache_name == "pip" and cache_name in self.cache_map:
                    caches[cache_name] = self.cache_map[cache_name]
        else:
            caches = {name: self.cache_map[name] for name in commands.get("cache_dirs", {}) if name in self.cache_map}
        steps = []
        for preflight in commands.get("preflight", []):
            step = self._run_check(
                job,
                label,
                image,
                source_dir,
                caches,
                preflight["name"],
                preflight["command"],
                pass_proxy=False,
            )
            steps.append(step)
            if step["exit_code"] != 0:
                return {
                    "passed": False,
                    "exit_code": step["exit_code"],
                    "steps": steps,
                    "environment_cache": None,
                }
        environment_cache = None
        if workspace["stack"] == "python":
            environment_cache = self._restore_python_environment(
                job, workspace, commands, image, cache_root
            )
            if environment_cache.get("hit"):
                steps.append(
                    {
                        "step_name": f"{label}:environment-cache",
                        "status": "restored",
                        "exit_code": None,
                        "cache_key": environment_cache["key"],
                    }
                )
                self.log_manager.upload(
                    job.job_id,
                    f"[{label}:environment-cache] HIT key={environment_cache['key'][:16]}\n",
                )
            else:
                self.log_manager.upload(
                    job.job_id,
                    f"[{label}:environment-cache] MISS reason="
                    f"{environment_cache.get('reason', 'missing')}\n",
                )
        service_env = None
        requested_services = list(workspace.get("services") or [])
        if requested_services:
            self.log_manager.upload(job.job_id, f"[services:prepare] Starting isolated services={requested_services}\n")
            try:
                service_env = self.services.prepare(job.job_id, job.workspace, requested_services)
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
        setup_commands = [] if environment_cache and environment_cache.get("hit") else commands.get("setup", [])
        for setup in setup_commands:
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

        if environment_cache and not setup_failed and not environment_cache.get("hit"):
            environment_cache = self._publish_python_environment(
                environment_cache, cache_root, commands
            )
            if environment_cache.get("published"):
                self.log_manager.upload(
                    job.job_id,
                    f"[{label}:environment-cache] PUBLISHED "
                    f"key={environment_cache['key'][:16]}\n",
                )

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
                or workspace["stack"] in {"node", "python", "rust", "maven", "gradle", "dotnet"}
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
                if job.contract_integrity_attested:
                    extra_env["AI_INTEGRITY_CONTRACT_GATE_ATTESTED"] = "1"
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

        return {
            "passed": passed,
            "exit_code": exit_code,
            "steps": steps,
            "environment_cache": environment_cache,
        }

    def _run_setup(self, job, label, image, source_dir, caches, step_name, command, service_env=None, pass_proxy=False):
        name = f"{label}:{step_name}"
        self.log_manager.upload(job.job_id, f"[{name}] Starting: {command}\n")
        step_id = self.client.start_step(job.job_id, name)
        start = time.time()
        result = self.podman.run_command(image, job.job_id, source_dir, caches, command, self._setup_timeout(job), network=True,
                                         env=self._service_env(service_env), network_name=service_env.network if service_env else None,
                                         pass_proxy=pass_proxy, cancel_event=getattr(self, "cancel_event", None))
        self._upload_output(job.job_id, result)
        status = "passed" if result["exit_code"] == 0 else ("timed_out" if result["timed_out"] else ("cancelled" if result.get("cancelled") else "failed"))
        duration = time.time() - start
        self.log_manager.upload(job.job_id, f"[{name}] {status.upper()} (exit={result['exit_code']}, {duration:.1f}s)\n")
        if step_id:
            self.client.finish_step(job.job_id, step_id, status, result["exit_code"], self.log_manager.get_total(job.job_id))
        return {"step_name": name, "command": command, "status": status, "exit_code": result["exit_code"], "duration_seconds": duration, "step_id": step_id}

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
        env["CI_COMMIT_SHA"] = job.commit_sha
        env["CI_REPOSITORY_ROOT"] = "/repo"
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
        env = {"DATABASE_URL": service_env.database_url, "REDIS_ADDR": service_env.redis_addr,
               "REDIS_PASSWORD": "", "REDIS_DB": service_env.redis_db, "RABBITMQ_URL": service_env.rabbitmq_url}
        for key, attribute in (
            ("CI_GLOBAL_DATABASE_URL", "global_database_url"),
            ("CI_REGIONAL_CN_DATABASE_URL", "regional_cn_database_url"),
            ("CI_REGIONAL_DE_DATABASE_URL", "regional_de_database_url"),
        ):
            value = getattr(service_env, attribute, "")
            if value:
                env[key] = value
        return env

    def _upload_output(self, job_id, result):
        output = result.get("stdout", "")
        if result.get("stderr"):
            output += "\n" + result["stderr"]
        if output:
            self.log_manager.upload(job_id, output)
            if not output.endswith("\n"):
                self.log_manager.upload(job_id, "\n")

    @staticmethod
    def _verify_openapi_source_identity(job: Job) -> dict:
        """Verify this checkout is the exact immutable commit/tree requested by the job."""
        def rev_parse(revision: str) -> tuple[str | None, str | None]:
            try:
                result = subprocess.run(
                    ["git", "-C", job.source_dir, "rev-parse", revision],
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return None, type(exc).__name__
            if result.returncode != 0:
                return None, (result.stderr or "git rev-parse failed")[-300:]
            return result.stdout.strip(), None

        actual_head, error = rev_parse("HEAD")
        if error:
            return {"ok": False, "error_code": "SOURCE_IDENTITY_UNAVAILABLE", "error_message": error}
        if actual_head != job.commit_sha:
            return {
                "ok": False,
                "error_code": "SOURCE_HEAD_MISMATCH",
                "error_message": "source HEAD does not match requested commit_sha",
                "actual_head_sha": actual_head,
            }

        expected_tree, error = rev_parse(f"{job.commit_sha}^{{tree}}")
        if error:
            return {"ok": False, "error_code": "SOURCE_IDENTITY_UNAVAILABLE", "error_message": error}
        actual_tree, error = rev_parse("HEAD^{tree}")
        if error:
            return {"ok": False, "error_code": "SOURCE_IDENTITY_UNAVAILABLE", "error_message": error}
        if actual_tree != expected_tree:
            return {
                "ok": False,
                "error_code": "SOURCE_TREE_MISMATCH",
                "error_message": "source Tree SHA does not match requested commit tree",
                "expected_tree_sha": expected_tree,
                "actual_tree_sha": actual_tree,
            }

        try:
            status = subprocess.run(
                ["git", "-C", job.source_dir, "status", "--porcelain", "--untracked-files=all"],
                capture_output=True,
                text=True,
                timeout=20,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "error_code": "SOURCE_IDENTITY_UNAVAILABLE", "error_message": type(exc).__name__}
        if status.returncode != 0:
            return {"ok": False, "error_code": "SOURCE_IDENTITY_UNAVAILABLE", "error_message": "git status failed"}
        if status.stdout.strip():
            return {"ok": False, "error_code": "SOURCE_WORKTREE_DIRTY", "error_message": "source worktree is not clean"}
        return {"ok": True, "head_sha": actual_head, "tree_sha": actual_tree}

    def _execute_openapi_check(self, job: Job) -> dict:
        """Run the repository-owned OpenAPI validator in one fixed Node 22 profile."""
        job_id = job.job_id
        started = time.time()
        self.client.update_job_status(job_id, "running")
        identity = self._verify_openapi_source_identity(job)
        if not identity.get("ok"):
            self.log_manager.upload(job_id, f"[openapi-check] {identity['error_code']}\n")
            return {
                "status": "failed", "exit_code": 2, "profile": "openapi-check",
                "commit_sha": job.commit_sha, "git_tree_sha": identity.get("actual_tree_sha"),
                "error_code": identity["error_code"], "error_message": identity.get("error_message"),
                "detected_stacks": ["openapi"], "selected_profiles": ["openapi-check"],
                "workspaces": [{"path": ".", "stack": "openapi"}], "steps": [],
            }

        commands = get_commands_for_profile("openapi-check", job.source_dir)
        image = commands["image"]
        if not self.podman.image_available(image):
            message = f"CI image is unavailable: {image}"
            self.log_manager.upload(job_id, f"CONFIGURATION_ERROR: {message}\n")
            return {
                "status": "failed", "exit_code": 2, "profile": "openapi-check",
                "commit_sha": job.commit_sha, "git_tree_sha": identity["tree_sha"],
                "image": image, "error_code": "OPENAPI_CHECK_IMAGE_UNAVAILABLE", "error_message": message,
                "detected_stacks": ["openapi"], "selected_profiles": ["openapi-check"],
                "workspaces": [{"path": ".", "stack": "openapi"}], "steps": [],
            }

        spec = commands["setup"][0]
        caches = {name: self.cache_map[name] for name in commands.get("cache_dirs", {}) if name in self.cache_map}
        step = self._run_setup(
            job, "openapi:.", image, job.source_dir, caches,
            spec["name"], spec["command"], pass_proxy=True,
        )
        if step["status"] == "cancelled":
            status, exit_code, error_code = "cancelled", -1, "OPENAPI_CHECK_CANCELLED"
        elif step["status"] == "timed_out":
            status, exit_code, error_code = "timed_out", step["exit_code"], "OPENAPI_CHECK_TIMEOUT"
        elif step["exit_code"] == 0:
            status, exit_code, error_code = "passed", 0, None
        elif step["exit_code"] == 2:
            status, exit_code, error_code = "failed", 2, "OPENAPI_CHECK_CONFIGURATION_ERROR"
        else:
            status, exit_code, error_code = "failed", step["exit_code"], "OPENAPI_CHECK_FAILED"
        summary = {
            "status": status, "exit_code": exit_code, "profile": "openapi-check",
            "commit_sha": job.commit_sha, "git_tree_sha": identity["tree_sha"],
            "image": image, "source_mirror_hit": bool(self.config.get("source_mirror_enabled")),
            "detected_stacks": ["openapi"], "selected_profiles": ["openapi-check"],
            "workspaces": [{"path": ".", "stack": "openapi"}], "steps": [step],
            "total_duration_seconds": round(time.time() - started, 3),
            "log_truncated": self.log_manager.is_truncated(job_id),
        }
        if error_code:
            summary["error_code"] = error_code
        return summary

    FAST_CHECK_ERROR_CODES = {
        "missing_make": "FAST_CHECK_MAKE_MISSING",
        "entrypoint_missing": "FAST_CHECK_ENTRYPOINT_MISSING",
        "timeout": "FAST_CHECK_TIMEOUT",
        "integrity_failed": "FAST_CHECK_INTEGRITY_FAILED",
    }

    def _execute_fast_check(self, job: Job) -> dict:
        """Run repository-controlled Make behavior only inside isolated Podman."""
        job_id = job.job_id
        source_dir = job.source_dir
        started = time.time()
        self.client.update_job_status(job_id, "running")

        image = FAST_CHECK_COMMANDS["image"]
        if not self.podman.image_available(image, allow_pull=False):
            return self._fast_check_configuration_error(
                job_id, "FAST_CHECK_IMAGE_UNAVAILABLE", f"Image {image} is not prewarmed"
            )
        image_digest = self.podman.image_digest(image)
        if not image_digest:
            return self._fast_check_configuration_error(
                job_id,
                "FAST_CHECK_IMAGE_IDENTITY_UNAVAILABLE",
                "Fast-check image has no immutable local identity",
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
        if job.contract_integrity_attested:
            env["AI_INTEGRITY_CONTRACT_GATE_ATTESTED"] = "1"

        affected = select_affected(
            job.changed_files,
            [{"path": ".", "stack": "integrity"}],
            truncated=job.changed_files_truncated,
            affected_only=True,
        )

        step_name = "repo-fast-check:ai-integrity"
        step_id = self.client.start_step(job_id, step_name)
        step_started = time.time()
        result = self.podman.run_command(
            image,
            job_id,
            source_dir,
            {},
            "command -v make >/dev/null 2>&1 || exit 127; "
            "make -n ai-integrity-check >/dev/null 2>&1 || exit 42; "
            "make ai-integrity-check 2>&1",
            60,
            env=env,
            network=False,
            extra_mounts=[f"{artifacts_dir}:/ci-artifacts:Z"],
            pass_proxy=False,
            cancel_event=getattr(self, "cancel_event", None),
            source_read_only=True,
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
            "image_digest": image_digest,
            "source_mirror_hit": bool(self.config.get("source_mirror_enabled")),
            "affected_selection": affected,
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
        elif exit_code == 127:
            summary["error_code"] = self.FAST_CHECK_ERROR_CODES["missing_make"]
        elif exit_code == 42:
            summary["error_code"] = self.FAST_CHECK_ERROR_CODES["entrypoint_missing"]
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
