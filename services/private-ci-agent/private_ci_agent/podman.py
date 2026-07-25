"""Rootless Podman container management."""

import hashlib
import logging
import os
import subprocess

logger = logging.getLogger(__name__)


class PodmanRunner:
    def __init__(self, podman_binary: str = "/usr/bin/podman"):
        self.podman = podman_binary

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

    def image_available(self, image: str) -> bool:
        """Verify the selected image exists locally, pulling only when missing."""
        try:
            exists = subprocess.run(
                [self.podman, "image", "exists", image],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if exists.returncode == 0:
                return True

            pull = [self.podman, "pull", "--platform", "linux/amd64"]
            if image.startswith("100.118.124.97:5555/"):
                pull.extend(["--tls-verify=false"])
            pull.append(image)
            refreshed = subprocess.run(pull, capture_output=True, text=True, timeout=180)
            if refreshed.returncode != 0:
                return False
            verify = subprocess.run(
                [self.podman, "image", "exists", image],
                capture_output=True,
                text=True,
                timeout=15,
            )
            return verify.returncode == 0
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

    @staticmethod
    def _cache_mounts(cache_dirs: dict) -> tuple[list[str], str | None]:
        mounts: list[str] = []
        go_cache = None
        for cache_name, cache_path in cache_dirs.items():
            if cache_name == "go":
                go_cache = os.path.realpath(cache_path)
                if os.path.exists(go_cache):
                    mounts.extend(["--mount", f"type=bind,src={go_cache},dst=/ci-cache,rw"])
            elif cache_name == "python_venv":
                if os.path.exists(cache_path):
                    mounts.extend(["-v", f"{cache_path}:/ci-venv:Z"])
            elif cache_name == "pip":
                if os.path.exists(cache_path):
                    mounts.extend(["-v", f"{cache_path}:/ci-cache/pip:Z"])
            elif os.path.exists(cache_path):
                mounts.extend(["-v", f"{cache_path}:{cache_path}:Z"])
        return mounts, go_cache

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
        cache_mounts, go_cache = self._cache_mounts(cache_dirs)
        project_root = os.path.abspath(os.path.join(source_dir, os.pardir, os.pardir))

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
        if "pip" in cache_dirs:
            safe_env.update({
                "PIP_CACHE_DIR": "/ci-cache/pip",
                "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            })
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
        for key, value in safe_env.items():
            env_args.extend(["--env", f"{key}={value}"])
        cmd[cmd.index("--entrypoint"):cmd.index("--entrypoint")] = env_args

        logger.info("Running podman container: %s image=%s", container_name, image)
        logger.debug("Podman command: %s", " ".join(cmd))

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
            logger.warning("Container timed out: %s", container_name)
            self._kill_container(container_name)
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": "TIMEOUT: Job exceeded maximum execution time",
                "timed_out": True,
            }
        except Exception as exc:
            logger.error("Container execution failed: %s", exc)
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": f"ERROR: {str(exc)}",
                "timed_out": False,
            }

    def run_command(
        self,
        image: str,
        job_id: str,
        source_dir: str,
        cache_dirs: dict,
        command: str,
        timeout_seconds: int,
        env: dict = None,
        network: bool = False,
        network_name: str | None = None,
        pass_proxy: bool = False,
    ) -> dict:
        """Run one command with explicit network and proxy boundaries."""
        container_name = self._container_name(job_id, source_dir)
        cache_mounts, go_cache = self._cache_mounts(cache_dirs)
        project_root = os.path.abspath(os.path.join(source_dir, os.pardir, os.pardir))
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
            "--tmpfs=/data:rw,noexec,nosuid,size=64m",
            "-v", f"{source_dir}:/workspace:Z",
            "-v", f"{project_root}:/repo:ro",
            "--workdir", "/workspace",
        ] + net_arg + cache_mounts + [
            "--entrypoint", "/bin/sh",
            image,
            "-c", command,
        ]

        safe_env = self._container_proxy_env() if (pass_proxy or network) else {}
        safe_env.update(self._go_cache_env(go_cache))
        if "pip" in cache_dirs:
            safe_env.update({
                "PIP_CACHE_DIR": "/ci-cache/pip",
                "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            })
        safe_env.update({
            "ACTION_API_KEY": "test_api_key_32_bytes_long",
            "GITHUB_TOKEN": "test_token_value",
            "ALLOWED_REPOSITORIES": "owner/allowed-repo",
            "ALLOW_DEFAULT_BRANCH_WRITE": "false",
            "MAX_FILE_CHARACTERS": "5000",
            "MAX_TOTAL_CHARACTERS": "10000",
            "MAX_FILES_PER_COMMIT": "5",
            "REPOSITORY_POLICY_FILE": "/workspace/tests/repository_policies_test.yml",
        })
        if env:
            allowed = {"DATABASE_URL", "REDIS_ADDR", "REDIS_PASSWORD", "REDIS_DB", "RABBITMQ_URL"}
            safe_env.update({key: value for key, value in env.items() if key in allowed})
        if network_name:
            existing = safe_env.get("NO_PROXY", "")
            safe_env["NO_PROXY"] = ",".join(dict.fromkeys(
                [item for item in existing.split(",") + ["postgres", "redis", "rabbitmq"] if item]
            ))
            safe_env["no_proxy"] = safe_env["NO_PROXY"]

        env_args = []
        for key, value in safe_env.items():
            env_args.extend(["--env", f"{key}={value}"])
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
        except Exception as exc:
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": f"ERROR: {str(exc)}",
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
                if name.startswith("ci-") and not any(name.startswith(f"ci-{prefix[:12]}") for prefix in job_id_prefixes):
                    logger.info("Removing stale container: %s", name)
                    try:
                        subprocess.run([self.podman, "rm", "-f", name],
                                       capture_output=True, timeout=10)
                    except Exception:
                        pass
        except Exception as exc:
            logger.warning("Cleanup stale containers failed: %s", exc)
