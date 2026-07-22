"""Rootless Podman container management."""

import logging
import os
import subprocess
import hashlib
import time

logger = logging.getLogger(__name__)


class PodmanRunner:
    def __init__(self, podman_binary: str = "/usr/bin/podman"):
        self.podman = podman_binary

    def image_available(self, image: str) -> bool:
        """Refresh and verify the selected image instead of trusting a stale tag."""
        try:
            pull = [self.podman, "pull", "--platform", "linux/amd64"]
            if image.startswith("100.118.124.97:5555/"):
                pull.extend(["--tls-verify=false"])
            pull.append(image)
            refreshed = subprocess.run(pull, capture_output=True, text=True, timeout=180)
            if refreshed.returncode != 0:
                return False
            result = subprocess.run(
                [self.podman, "image", "exists", image],
                capture_output=True,
                text=True,
                timeout=15,
            )
            return result.returncode == 0
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
        go_cache = None
        for cache_name, cache_path in cache_dirs.items():
            if cache_name == "go":
                go_cache = os.path.realpath(cache_path)
                if os.path.exists(go_cache):
                    cache_mounts.extend(["--mount", f"type=bind,src={go_cache},dst=/ci-cache,rw"])
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
            "-v", f"{source_dir}:/workspace:Z",
            "--workdir", "/workspace",
            "--network=none",
        ] + cache_mounts + [
            "--entrypoint", "/bin/sh",
            image,
            "-c", " && ".join(commands),
        ]

        env_vars = env or {}
        inherited_proxy = {k: v for k, v in os.environ.items()
                           if k in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy"} and v}
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
                    network_name: str | None = None) -> dict:
        """Run a single command in a container. Supports optional network."""
        container_name = self._container_name(job_id, source_dir)

        cache_mounts = []
        go_cache = None
        for cache_name, cache_path in cache_dirs.items():
            if cache_name == "go":
                go_cache = os.path.realpath(cache_path)
                if os.path.exists(go_cache):
                    cache_mounts.extend(["--mount", f"type=bind,src={go_cache},dst=/ci-cache,rw"])
            elif os.path.exists(cache_path):
                cache_mounts.extend(["-v", f"{cache_path}:{cache_path}:Z"])

        net_arg = ["--pod", network_name] if network_name else ([] if network else ["--network=none"])

        userns_arg = [] if network_name else ["--userns=keep-id"]
        cmd = [
            self.podman, "run",
            "--rm",
            "--name", container_name,
        ] + userns_arg + [
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit=256",
            "--memory=2g",
            "--memory-swap=3g",
            "--cpus=2",
            "--read-only",
            "--tmpfs=/tmp:rw,noexec,nosuid,size=256m",
            "--tmpfs=/run:rw,noexec,nosuid,size=64m",
            "-v", f"{source_dir}:/workspace:Z",
            "--workdir", "/workspace",
        ] + net_arg + cache_mounts + [
            "--entrypoint", "/bin/sh",
            image,
            "-c", command,
        ]

        safe_env = {k: v for k, v in os.environ.items()
                    if k in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy"} and v}
        safe_env.update(self._go_cache_env(go_cache))
        if env:
            allowed = {"DATABASE_URL", "REDIS_ADDR", "REDIS_PASSWORD", "REDIS_DB", "RABBITMQ_URL"}
            safe_env.update({k: v for k, v in env.items() if k in allowed})
        if network_name:
            existing = safe_env.get("NO_PROXY", "")
            safe_env["NO_PROXY"] = ",".join(dict.fromkeys([item for item in existing.split(",") + ["postgres", "redis", "rabbitmq"] if item]))
            safe_env["no_proxy"] = safe_env["NO_PROXY"]
        env_args = []
        for k, v in safe_env.items():
            env_args.extend(["--env", f"{k}={v}"])
        cmd[cmd.index("--entrypoint"):cmd.index("--entrypoint")] = env_args

        try:
            process = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
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
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": "TIMEOUT",
                "timed_out": True,
            }
        except Exception as e:
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": f"ERROR: {str(e)}",
                "timed_out": False,
            }

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
