"""Rootless Podman container management.

Provides config-driven resource limits, proxy-aware image prewarming,
and persistent image reuse with --pull=never.
"""

import logging
import os
import re
import subprocess
import hashlib
import time

logger = logging.getLogger(__name__)

# Patterns to redact from log output: proxy credentials, tokens, etc.
_PROXY_REDACT_RE = re.compile(
    r"(https?://)[^@:]*:[^@]*@|"
    r"(token|password|secret|api[_-]?key|authorization)\s*[:=]\s*\S+|"
    r"(Proxy-Authorization|Authorization)\s*:\s*\S+",
    re.IGNORECASE,
)

DEFAULT_RESOURCE_POLICY = {
    "mode": "dedicated_worker",
    "reserve_cpus": 1,
    "reserve_memory_mb": 2048,
    "pids_limit": 2048,
    "max_parallel_workspaces": 3,
}


class PodmanRunner:
    def __init__(self, podman_binary: str = "/usr/bin/podman",
                 resource_policy: dict | None = None,
                 prewarm_images: list[str] | None = None):
        self.podman = podman_binary
        self.resource_policy = resource_policy or dict(DEFAULT_RESOURCE_POLICY)
        self.prewarm_images = prewarm_images or []
        self._image_digest_cache: dict[str, str] = {}

    # ---- Resource limit construction ----

    def _build_resource_args(self, overrides: dict | None = None) -> list[str]:
        """Build Podman resource args from config-driven policy.

        dedicated_worker mode: omit CPU/memory caps; rely on systemd cgroup.
        shared_worker mode: apply explicit limits from policy or overrides.
        Values of 0 or None are omitted.
        """
        policy = dict(self.resource_policy)
        if overrides:
            policy.update(overrides)

        args = []
        mode = policy.get("mode", "dedicated_worker")

        if mode == "shared_worker":
            for key, flag in [("cpus", "--cpus"), ("memory", "--memory"),
                              ("memory_swap", "--memory-swap")]:
                val = policy.get(key)
                if val and str(val) != "0":
                    args.extend([flag, str(val)])
        # dedicated_worker: no CPU/memory caps — systemd slice controls total

        pids = policy.get("pids_limit", 2048)
        if pids:
            args.extend(["--pids-limit", str(pids)])

        return args

    def resource_summary(self) -> dict:
        """Public-safe summary of the active resource policy."""
        p = dict(self.resource_policy)
        return {
            "mode": p.get("mode", "dedicated_worker"),
            "pids_limit": p.get("pids_limit"),
            "reserve_cpus": p.get("reserve_cpus") if p.get("mode") == "dedicated_worker" else None,
            "reserve_memory_mb": p.get("reserve_memory_mb") if p.get("mode") == "dedicated_worker" else None,
        }

    # ---- Image prewarming ----

    def prewarm(self) -> dict[str, str | None]:
        """Ensure prewarm images exist locally. Records digests.

        Uses proxy env vars for pull when image is missing.
        Does NOT delete existing images.
        """
        digests: dict[str, str | None] = {}
        for image in self.prewarm_images:
            try:
                exists = subprocess.run(
                    [self.podman, "image", "exists", image],
                    capture_output=True, text=True, timeout=15,
                )
                if exists.returncode == 0:
                    digest = self.image_digest(image)
                    digests[image] = digest
                    logger.info("Prewarm cache hit: %s digest=%s", image, digest)
                    continue
            except (OSError, subprocess.TimeoutExpired):
                pass

            # Pull with proxy
            logger.info("Prewarm pull: %s", image)
            try:
                pull_env = self._sanitized_proxy_env()
                pull = subprocess.run(
                    [self.podman, "pull", "--platform", "linux/amd64", image],
                    capture_output=True, text=True, timeout=300,
                    env={**os.environ, **pull_env},
                )
                if pull.returncode == 0:
                    digest = self.image_digest(image)
                    digests[image] = digest
                    self._image_digest_cache[image] = digest or ""
                    logger.info("Prewarm pulled: %s digest=%s", image, digest)
                else:
                    digests[image] = None
                    logger.warning("Prewarm pull failed for %s: %s", image, _PROXY_REDACT_RE.sub("***", pull.stderr[-200:]))
            except (OSError, subprocess.TimeoutExpired) as exc:
                digests[image] = None
                logger.warning("Prewarm pull error for %s: %s", image, exc)
        return digests

    # ---- Proxy helpers ----

    @staticmethod
    def _sanitized_proxy_env() -> dict[str, str]:
        """Return proxy env vars for host-level operations (pull, fetch).

        Includes HTTP_PROXY, HTTPS_PROXY, ALL_PROXY, NO_PROXY and lowercase
        variants.  Credentials are NOT stripped here — callers must redact
        before logging.
        """
        result = {}
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                    "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
            val = os.environ.get(key)
            if val:
                result[key] = val
        return result

    @staticmethod
    def _container_proxy_env() -> dict[str, str]:
        """Pass host-loopback proxies through rootless Podman containers."""
        proxy_keys = {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"}
        result = {
            key: value for key, value in os.environ.items()
            if key in proxy_keys and value
        }
        container_proxy_host = os.environ.get("PRIVATE_CI_CONTAINER_PROXY_HOST", "host.containers.internal")
        for key, value in list(result.items()):
            if "127.0.0.1" in value or "localhost" in value:
                result[key] = value.replace("127.0.0.1", container_proxy_host).replace("localhost", container_proxy_host)
        no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy")
        if no_proxy:
            result["NO_PROXY"] = no_proxy
            result["no_proxy"] = no_proxy
        return result

    def image_available(self, image: str, allow_pull: bool = False) -> bool:
        """Verify the selected image exists locally.

        When allow_pull=False (default for job execution): checks local
        storage only — images must be prewarmed.
        When allow_pull=True: pulls missing images via proxy.
        """
        try:
            exists = subprocess.run(
                [self.podman, "image", "exists", image],
                capture_output=True, text=True, timeout=15,
            )
            if exists.returncode == 0:
                return True
        except (OSError, subprocess.TimeoutExpired):
            if not allow_pull:
                return False

        if not allow_pull:
            return False

        # Slow path: pull missing image from registry via proxy
        try:
            pull_env = self._sanitized_proxy_env()
            pull = [self.podman, "pull", "--platform", "linux/amd64"]
            if image.startswith("100.118.124.97:5555/"):
                pull.extend(["--tls-verify=false"])
            pull.append(image)
            refreshed = subprocess.run(pull, capture_output=True, text=True, timeout=180,
                                       env={**os.environ, **pull_env})
            if refreshed.returncode != 0:
                return False
            verify = subprocess.run(
                [self.podman, "image", "exists", image],
                capture_output=True, text=True, timeout=15,
            )
            if verify.returncode == 0:
                digest = self.image_digest(image)
                if digest:
                    self._image_digest_cache[image] = digest
                return True
            return False
        except (OSError, subprocess.TimeoutExpired):
            return False

    def image_digest(self, image: str) -> str | None:
        """Return the local image digest without exposing registry credentials."""
        try:
            result = subprocess.run(
                [self.podman, "image", "inspect", "--format", "{{index .RepoDigests 0}}", image],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode != 0:
                return None
            value = result.stdout.strip()
            return value.split("@", 1)[1] if "@" in value else None
        except (OSError, subprocess.TimeoutExpired):
            return None

    def run(
        self,
        image: str,
        job_id: str,
        source_dir: str,
        cache_dirs: dict,
        commands: list[str],
        timeout_seconds: int,
        env: dict = None,
    ) -> dict:
        """Run commands in an isolated rootless Podman container."""
        container_name = self._container_name(job_id, source_dir)

        # Map caches to container paths
        cache_mounts = []
        project_root = os.path.abspath(os.path.join(source_dir, os.pardir, os.pardir))
        go_cache = None
        for cache_name, cache_path in cache_dirs.items():
            if cache_name == "go":
                go_cache = os.path.realpath(cache_path)
                if os.path.exists(go_cache):
                    cache_mounts.extend(["--mount", f"type=bind,src={go_cache},dst=/ci-cache,rw"])
            elif cache_name == "python_venv":
                if os.path.exists(cache_path):
                    cache_mounts.extend(["-v", f"{cache_path}:/ci-venv:Z"])
            elif cache_name == "pip":
                if os.path.exists(cache_path):
                    cache_mounts.extend(["-v", f"{cache_path}:/ci-cache/pip:Z"])
            elif os.path.exists(cache_path):
                cache_mounts.extend(["-v", f"{cache_path}:{cache_path}:Z"])

        cmd = [
            self.podman, "run",
            "--rm",
            "--name", container_name,
            "--userns=keep-id",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit=256",
            "--memory=2g",
            "--memory-swap=3g",
            "--cpus=2",
            "--read-only",
            "--tmpfs=/tmp:rw,noexec,nosuid,size=256m",
            "--tmpfs=/run:rw,noexec,nosuid,size=64m",
            "--tmpfs=/data:rw,noexec,nosuid,size=64m",
            "-v", f"{source_dir}:/workspace:Z",
            "-v", f"{project_root}:/repo:ro",
            "--workdir", "/workspace",
            "--network=none",
        ] + cache_mounts + [
            "--entrypoint", "/bin/sh",
            image,
            "-c", " && ".join(commands),
        ]

        env_vars = env or {}
        inherited_proxy = self._container_proxy_env()
        safe_env = {**inherited_proxy, **self._go_cache_env(go_cache)}
        if go_cache:
            safe_env.update({
                key: value for key, value in env_vars.items()
                if key in self._go_cache_env(go_cache)
            })
        else:
            safe_env.update(env_vars)
        safe_env = {k: v for k, v in safe_env.items()
                    if not any(f.lower() in k.lower()
                               for f in ["TOKEN", "SECRET", "PASSWORD", "KEY", "AUTH"])}

        env_args = []
        for k, v in safe_env.items():
            env_args.extend(["--env", f"{k}={v}"])
        cmd[cmd.index("--entrypoint"):cmd.index("--entrypoint")] = env_args

        logger.info("Running podman container: %s image=%s", container_name, image)
        logger.debug("Podman command: %s", " ".join(cmd))

        try:
            process = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_seconds + 60,  # Extra padding
            )

            return {
                "exit_code": process.returncode,
                "stdout": process.stdout,
                "stderr": process.stderr,
                "timed_out": False,
            }
        except subprocess.TimeoutExpired:
            logger.warning("Container timed out: %s", container_name)
            self._kill_container(container_name)
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": "TIMEOUT: Job exceeded maximum execution time",
                "timed_out": True,
            }
        except Exception as e:
            logger.error("Container execution failed: %s", e)
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": f"ERROR: {str(e)}",
                "timed_out": False,
            }

    def run_command(self, image: str, job_id: str, source_dir: str,
                    cache_dirs: dict, command: str, timeout_seconds: int,
                    env: dict = None, network: bool = False,
                    network_name: str | None = None,
                    extra_mounts: list[str] | None = None,
                    resource_limits: dict | None = None,
                    pass_proxy: bool = False) -> dict:
        """Run a single command in a container with config-driven resource limits.

        Args:
            extra_mounts: additional `-v src:dst` mount pairs.
            resource_limits: overrides for resource_policy (cpus, memory, etc.).
            pass_proxy: if True, pass proxy env to container (for dep download).
        """
        container_name = self._container_name(job_id, source_dir)

        cache_mounts = []
        project_root = os.path.abspath(os.path.join(source_dir, os.pardir, os.pardir))
        go_cache = None
        for cache_name, cache_path in cache_dirs.items():
            if cache_name == "go":
                go_cache = os.path.realpath(cache_path)
                if os.path.exists(go_cache):
                    cache_mounts.extend(["--mount", f"type=bind,src={go_cache},dst=/ci-cache,rw"])
            elif cache_name == "python_venv":
                if os.path.exists(cache_path):
                    cache_mounts.extend(["-v", f"{cache_path}:/ci-venv:Z"])
            elif cache_name == "pip":
                if os.path.exists(cache_path):
                    cache_mounts.extend(["-v", f"{cache_path}:/ci-cache/pip:Z"])
            elif os.path.exists(cache_path):
                cache_mounts.extend(["-v", f"{cache_path}:{cache_path}:Z"])

        # Extra mounts (e.g. artifacts dir)
        extra = extra_mounts or []

        net_arg = (["--pod", network_name] if network_name
                   else ([] if network else ["--network=none"]))

        userns_arg = [] if network_name else ["--userns=keep-id"]
        resource_args = self._build_resource_args(resource_limits)

        cmd = [
            self.podman, "run",
            "--rm",
            "--pull=never",
            "--name", container_name,
        ] + userns_arg + [
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--read-only",
            "--tmpfs=/tmp:rw,noexec,nosuid,size=256m",
            "--tmpfs=/run:rw,noexec,nosuid,size=64m",
            "-v", f"{source_dir}:/workspace:Z",
            "-v", f"{project_root}:/repo:ro",
            "--workdir", "/workspace",
        ] + net_arg + resource_args + cache_mounts + extra + [
            "--entrypoint", "/bin/sh",
            image,
            "-c", command,
        ]

        safe_env = {}
        if pass_proxy:
            safe_env.update(self._container_proxy_env())
        safe_env.update(self._go_cache_env(go_cache))
        if env:
            allowed = {"DATABASE_URL", "REDIS_ADDR", "REDIS_PASSWORD", "REDIS_DB", "RABBITMQ_URL",
                       "AI_INTEGRITY_BASE_SHA", "AI_INTEGRITY_REPORT", "AI_INTEGRITY_CHANGED_FILES"}
            safe_env.update({k: v for k, v in env.items() if k in allowed})
        if network_name:
            existing = safe_env.get("NO_PROXY", "")
            safe_env["NO_PROXY"] = ",".join(dict.fromkeys(
                [item for item in existing.split(",") + ["postgres", "redis", "rabbitmq"] if item]))
            safe_env["no_proxy"] = safe_env["NO_PROXY"]
        env_args = []
        for k, v in safe_env.items():
            env_args.extend(["--env", f"{k}={v}"])
        if env_args:
            cmd[cmd.index("--entrypoint"):cmd.index("--entrypoint")] = env_args

        try:
            process = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=timeout_seconds + 60,
            )
            return {
                "exit_code": process.returncode,
                "stdout": process.stdout,
                "stderr": process.stderr,
                "timed_out": False,
            }
        except subprocess.TimeoutExpired:
            self._kill_container(container_name)
            return {"exit_code": -1, "stdout": "", "stderr": "TIMEOUT", "timed_out": True}
        except Exception as e:
            return {"exit_code": -1, "stdout": "", "stderr": f"ERROR: {type(e).__name__}", "timed_out": False}

    @staticmethod
    def _container_name(job_id: str, source_dir: str = "") -> str:
        suffix = hashlib.sha1(str(source_dir).encode()).hexdigest()[:6] if source_dir else "main"
        return f"ci-{job_id[:12]}-{suffix}"

    @staticmethod
    def _go_cache_env(go_cache: str | None) -> dict:
        if not go_cache:
            return {}
        return {
            "HOME": "/ci-cache/home",
            "GOPATH": "/ci-cache/gopath",
            "GOMODCACHE": "/ci-cache/gomod",
            "GOCACHE": "/ci-cache/gobuild",
            "GOENV": "/ci-cache/config/go/env",
            "GOTMPDIR": "/ci-cache/tmp",
            "XDG_CACHE_HOME": "/ci-cache/xdg-cache",
            "XDG_CONFIG_HOME": "/ci-cache/xdg-config",
            "GOTOOLCHAIN": "local",
        }

    def kill(self, job_id: str):
        self._kill_container(f"ci-{job_id[:12]}")

    def _kill_container(self, container_name: str):
        try:
            subprocess.run(
                [self.podman, "stop", "--time=10", container_name],
                capture_output=True, timeout=15,
            )
        except Exception:
            try:
                subprocess.run(
                    [self.podman, "kill", container_name],
                    capture_output=True, timeout=10,
                )
            except Exception:
                pass

    def cleanup_stale(self, job_id_prefixes: list):
        """Remove stale containers not matching current jobs."""
        try:
            result = subprocess.run(
                [self.podman, "ps", "-a", "--format", "{{.Names}}"],
                capture_output=True, text=True, timeout=10,
            )
            for name in result.stdout.strip().split("\n"):
                if name.startswith("ci-") and not any(name.startswith(f"ci-{p[:12]}") for p in job_id_prefixes):
                    logger.info("Removing stale container: %s", name)
                    try:
                        subprocess.run([self.podman, "rm", "-f", name],
                                       capture_output=True, timeout=10)
                    except Exception:
                        pass
        except Exception as e:
            logger.warning("Cleanup stale containers failed: %s", e)
