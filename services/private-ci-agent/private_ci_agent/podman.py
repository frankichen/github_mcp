"""Rootless Podman container management."""

import hashlib
import logging
import os
import re
import subprocess
import threading
import time
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)

PROXY_ENV_NAMES = {
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "all_proxy",
}
REQUIRED_NO_PROXY = ("postgres", "redis", "rabbitmq", "localhost", "127.0.0.1", "::1")
LOOPBACK_PROXY_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}
CONTAINER_PROXY_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]*$")
ROOTLESS_OUTBOUND_NETWORK = "slirp4netns:allow_host_loopback=true"
LOCAL_ONLY_IMAGE_PREFIXES = ("localhost/node-chromium:",)
GO_CACHE_SUBDIRECTORIES = (
    "home", "gopath", "gomod", "gobuild", "config/go",
    "xdg-cache", "xdg-config", "tmp", ".tool-bin",
)


class PodmanRunner:
    def __init__(self, podman_binary: str = "/usr/bin/podman"):
        self.podman = podman_binary
        self._validated_proxy_contexts: set[tuple[str, str, str]] = set()

    @staticmethod
    def _container_proxy_env() -> dict[str, str]:
        """Return the explicitly controlled proxy environment for a container."""
        result = {
            key: value for key, value in os.environ.items()
            if key in PROXY_ENV_NAMES and value
        }
        if not result:
            return PodmanRunner._no_proxy_env()
        container_proxy_host = os.environ.get("PRIVATE_CI_CONTAINER_PROXY_HOST", "host.containers.internal")
        if not CONTAINER_PROXY_HOST_RE.fullmatch(container_proxy_host) or container_proxy_host.lower() in LOOPBACK_PROXY_HOSTS:
            raise ValueError("PRIVATE_CI_CONTAINER_PROXY_HOST must be a non-loopback hostname or IPv4 address")
        for key, value in list(result.items()):
            result[key] = PodmanRunner._rewrite_loopback_proxy_url(value, container_proxy_host)
        result.update(PodmanRunner._no_proxy_env())
        return result

    @staticmethod
    def _rewrite_loopback_proxy_url(value: str, container_proxy_host: str) -> str:
        """Replace only a loopback URL hostname, preserving scheme and credentials."""
        try:
            parsed = urlsplit(value)
            if parsed.hostname not in LOOPBACK_PROXY_HOSTS:
                return value
            port = parsed.port
        except ValueError:
            return value
        userinfo = f"{parsed.netloc.rsplit('@', 1)[0]}@" if "@" in parsed.netloc else ""
        netloc = f"{userinfo}{container_proxy_host}"
        if port is not None:
            netloc = f"{netloc}:{port}"
        return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))

    @staticmethod
    def _no_proxy_env() -> dict[str, str]:
        """Keep internal service names and the existing bypass allowlist intact."""
        values = []
        for name in ("NO_PROXY", "no_proxy"):
            values.extend(item.strip() for item in os.environ.get(name, "").split(",") if item.strip())
        values.extend(REQUIRED_NO_PROXY)
        merged = ",".join(dict.fromkeys(values))
        return {"NO_PROXY": merged, "no_proxy": merged}

    @staticmethod
    def _ensure_cache_dir(cache_path: str) -> bool:
        try:
            os.makedirs(cache_path, mode=0o700, exist_ok=True)
            return True
        except OSError as exc:
            logger.error("Unable to prepare controlled cache directory: %s", type(exc).__name__)
            return False

    @staticmethod
    def _network_args(network: bool, network_name: str | None) -> list[str]:
        """Select the same explicit Rootless network for probes and CI commands."""
        if network_name:
            return ["--pod", network_name]
        if network:
            return ["--network", ROOTLESS_OUTBOUND_NETWORK]
        return ["--network=none"]

    def _validate_container_proxy(
        self,
        image: str,
        container_name: str,
        network: bool,
        network_name: str | None,
        proxy_env: dict[str, str],
    ) -> bool:
        """Verify the rewritten proxy from an equally isolated container once."""
        active_proxy = {key: value for key, value in proxy_env.items() if key in PROXY_ENV_NAMES}
        if not active_proxy:
            return True
        if not network and not network_name:
            logger.warning("Refusing proxy injection into a network-isolated container")
            return False
        fingerprint = hashlib.sha256("\0".join(f"{key}={value}" for key, value in sorted(active_proxy.items())).encode()).hexdigest()
        context = network_name or ROOTLESS_OUTBOUND_NETWORK
        cache_key = (image, context, fingerprint)
        if cache_key in self._validated_proxy_contexts:
            return True

        network_args = self._network_args(network, network_name)
        userns_args = [] if network_name else ["--userns=keep-id"]
        command = [
            self.podman, "run", "--rm", "--pull=never", "--http-proxy=false",
            "--name", f"{container_name}-proxycheck",
        ] + userns_args + [
            "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--read-only", "--tmpfs=/tmp:rw,noexec,nosuid,size=64m",
        ] + network_args
        for key, value in proxy_env.items():
            if key in PROXY_ENV_NAMES or key in {"NO_PROXY", "no_proxy"}:
                command.extend(["--env", f"{key}={value}"])
        command.extend([
            "--entrypoint", "/bin/sh", image, "-c",
            'proxy=${HTTPS_PROXY:-${HTTP_PROXY:-${ALL_PROXY:-}}}; test -n "$proxy" && if command -v curl >/dev/null; then curl --proxy "$proxy" --connect-timeout 5 --max-time 15 -fsS -o /dev/null https://github.com; elif command -v python >/dev/null; then python -c \'import urllib.request; urllib.request.urlopen("https://github.com", timeout=15).close()\'; else exit 127; fi',
        ])
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("Container proxy validation failed: %s", type(exc).__name__)
            return False
        if result.returncode != 0:
            logger.warning("Container proxy validation failed with exit=%s", result.returncode)
            return False
        self._validated_proxy_contexts.add(cache_key)
        return True

    @staticmethod
    def _proxy_failure(code: str) -> dict:
        return {"exit_code": -1, "stdout": "", "stderr": code, "timed_out": False}

    def image_available(self, image: str, allow_pull: bool = True) -> bool:
        """Verify an image exists, never pulling local-only shared runtimes."""
        try:
            exists = subprocess.run(
                [self.podman, "image", "exists", image],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if exists.returncode == 0:
                return True
            if not allow_pull or any(image.startswith(prefix) for prefix in LOCAL_ONLY_IMAGE_PREFIXES):
                if any(image.startswith(prefix) for prefix in LOCAL_ONLY_IMAGE_PREFIXES):
                    logger.error("Local shared image is not prewarmed: %s", image)
                return False

            pull = [self.podman, "pull", "--platform", "linux/amd64", image]
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

    def resource_summary(self) -> dict:
        return {
            "mode": "shared_worker",
            "pids_limit": 256,
            "cpus": 2,
            "memory": "2g",
        }

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
        needs_cache_parent = False
        for cache_name, cache_path in cache_dirs.items():
            if cache_name == "go":
                go_cache = os.path.realpath(cache_path)
                if os.path.exists(go_cache):
                    try:
                        for subdirectory in GO_CACHE_SUBDIRECTORIES:
                            os.makedirs(os.path.join(go_cache, subdirectory), mode=0o700, exist_ok=True)
                    except OSError as exc:
                        logger.warning("Unable to prepare Go cache layout: %s", type(exc).__name__)
                    mounts.extend(["--mount", f"type=bind,src={go_cache},dst=/ci-cache,rw"])
            elif cache_name == "python_venv":
                if os.path.exists(cache_path):
                    mounts.extend(["-v", f"{cache_path}:/ci-venv:Z"])
            elif cache_name == "pip":
                needs_cache_parent = True
                if PodmanRunner._ensure_cache_dir(cache_path):
                    mounts.extend(["-v", f"{cache_path}:/ci-cache/pip:rw,Z"])
            elif cache_name == "npm":
                needs_cache_parent = True
                if PodmanRunner._ensure_cache_dir(cache_path):
                    # :z allows concurrent rootless containers to share only the
                    # download cache; node_modules remains workspace-local.
                    mounts.extend(["-v", f"{cache_path}:/ci-cache/npm:rw,z"])
            elif cache_name == "playwright":
                needs_cache_parent = True
                if PodmanRunner._ensure_cache_dir(cache_path):
                    # Browser binaries are a shared, immutable maintenance cache.
                    # Use shared SELinux relabeling (:z), never the exclusive :Z.
                    mounts.extend(["-v", f"{cache_path}:/ci-cache/ms-playwright:rw,z"])
            elif os.path.exists(cache_path):
                mounts.extend(["-v", f"{cache_path}:{cache_path}:Z"])
        if needs_cache_parent:
            # Minimal read-write parent for bind mounts in images that do not
            # ship /ci-cache.  The actual persistent data remains in the
            # explicitly mounted child directories above.
            mounts.insert(0, "--tmpfs=/ci-cache:rw,nosuid,size=64m")
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
        pass_proxy: bool = False,
        cancel_event: threading.Event | None = None,
    ) -> dict:
        """Run commands in an isolated rootless Podman container."""
        container_name = self._container_name(job_id, source_dir)
        cache_mounts, go_cache = self._cache_mounts(cache_dirs)
        project_root = os.path.abspath(os.path.join(source_dir, os.pardir, os.pardir))

        cmd = [
            self.podman, "run",
            "--rm",
            "--pull=never",
            "--http-proxy=false",
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

        env_vars = {key: value for key, value in (env or {}).items() if key not in PROXY_ENV_NAMES}
        try:
            proxy_env = self._container_proxy_env() if pass_proxy else {}
        except ValueError:
            logger.error("Container proxy configuration is invalid")
            return self._proxy_failure("PROXY_CONFIGURATION_INVALID")
        if pass_proxy and not self._validate_container_proxy(image, container_name, False, None, proxy_env):
            return self._proxy_failure("PROXY_VALIDATION_FAILED")
        safe_env = self._no_proxy_env()
        safe_env.update(proxy_env)
        safe_env.update(self._go_cache_env(go_cache))
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
        if "npm" in cache_dirs:
            safe_env["NPM_CONFIG_CACHE"] = "/ci-cache/npm"
        if "playwright" in cache_dirs:
            safe_env["PLAYWRIGHT_BROWSERS_PATH"] = "/ci-cache/ms-playwright"
        safe_env = {k: v for k, v in safe_env.items()
                    if not any(f.lower() in k.lower()
                               for f in ["TOKEN", "SECRET", "PASSWORD", "KEY", "AUTH"])}

        env_args = []
        for key, value in safe_env.items():
            env_args.extend(["--env", f"{key}={value}"])
        cmd[cmd.index("--entrypoint"):cmd.index("--entrypoint")] = env_args

        logger.info("Running podman container: %s image=%s proxy=%s", container_name, image, pass_proxy)

        return self._run_process(cmd, container_name, timeout_seconds, cancel_event)

    def _run_process(self, cmd: list[str], container_name: str, timeout_seconds: int,
                     cancel_event: threading.Event | None = None) -> dict:
        """Run a container command, honoring both the deadline and an external cancel.

        With no cancel_event this keeps the historical subprocess.run behavior.
        With a cancel_event we use Popen so a cancel request can stop the
        container and reap the process immediately instead of waiting for the
        network request inside the container to finish.
        """
        if cancel_event is None:
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
                logger.error("Container execution failed: %s", type(exc).__name__)
                return {
                    "exit_code": -1,
                    "stdout": "",
                    "stderr": f"ERROR: {type(exc).__name__}",
                    "timed_out": False,
                }

        deadline = time.monotonic() + timeout_seconds + 60
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except Exception as exc:
            logger.error("Container start failed: %s", type(exc).__name__)
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": f"ERROR: {type(exc).__name__}",
                "timed_out": False,
            }
        try:
            while True:
                if cancel_event.is_set():
                    logger.warning("Cancel requested, stopping container: %s", container_name)
                    self._kill_container(container_name)
                    try:
                        process.terminate()
                    except Exception:
                        pass
                    try:
                        stdout, stderr = process.communicate(timeout=10)
                    except subprocess.TimeoutExpired:
                        try:
                            process.kill()
                        except Exception:
                            pass
                        try:
                            stdout, stderr = process.communicate(timeout=10)
                        except Exception:
                            stdout, stderr = "", "CANCELLED"
                    except Exception:
                        stdout, stderr = "", "CANCELLED"
                    return {
                        "exit_code": -1,
                        "stdout": stdout or "",
                        "stderr": stderr or "CANCELLED",
                        "timed_out": False,
                        "cancelled": True,
                    }
                try:
                    stdout, stderr = process.communicate(timeout=0.5)
                    return {
                        "exit_code": process.returncode,
                        "stdout": stdout,
                        "stderr": stderr,
                        "timed_out": False,
                    }
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= deadline:
                        logger.warning("Container timed out: %s", container_name)
                        self._kill_container(container_name)
                        try:
                            process.kill()
                        except Exception:
                            pass
                        try:
                            stdout, stderr = process.communicate(timeout=10)
                        except Exception:
                            stdout, stderr = "", ""
                        return {
                            "exit_code": -1,
                            "stdout": stdout or "",
                            "stderr": "TIMEOUT: Job exceeded maximum execution time",
                            "timed_out": True,
                        }
        except Exception as exc:
            logger.error("Container execution failed: %s", type(exc).__name__)
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": f"ERROR: {type(exc).__name__}",
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
        extra_mounts: list[str] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict:
        """Run one command with explicit network and proxy boundaries."""
        container_name = self._container_name(job_id, source_dir)
        cache_mounts, go_cache = self._cache_mounts(cache_dirs)
        project_root = os.path.abspath(os.path.join(source_dir, os.pardir, os.pardir))
        net_arg = self._network_args(network, network_name)
        userns_arg = [] if network_name else ["--userns=keep-id"]

        cmd = [
            self.podman, "run",
            "--rm",
            "--pull=never",
            "--http-proxy=false",
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
        ] + net_arg + cache_mounts
        for mount in extra_mounts or []:
            cmd.extend(["-v", mount])
        cmd += [
            "--entrypoint", "/bin/sh",
            image,
            "-c", command,
        ]

        try:
            proxy_env = self._container_proxy_env() if pass_proxy else {}
        except ValueError:
            logger.error("Container proxy configuration is invalid")
            return self._proxy_failure("PROXY_CONFIGURATION_INVALID")
        if pass_proxy and not self._validate_container_proxy(image, container_name, network, network_name, proxy_env):
            return self._proxy_failure("PROXY_VALIDATION_FAILED")
        safe_env = self._no_proxy_env()
        safe_env.update(proxy_env)
        safe_env.update(self._go_cache_env(go_cache))
        if "pip" in cache_dirs:
            safe_env.update({
                "PIP_CACHE_DIR": "/ci-cache/pip",
                "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            })
        if "npm" in cache_dirs:
            safe_env["NPM_CONFIG_CACHE"] = "/ci-cache/npm"
        if "playwright" in cache_dirs:
            safe_env["PLAYWRIGHT_BROWSERS_PATH"] = "/ci-cache/ms-playwright"
        if env:
            allowed = {
                "DATABASE_URL", "REDIS_ADDR", "REDIS_PASSWORD", "REDIS_DB", "RABBITMQ_URL",
                "AI_INTEGRITY_BASE_SHA", "AI_INTEGRITY_REPORT", "AI_INTEGRITY_CHANGED_FILES",
            }
            safe_env.update({key: value for key, value in env.items() if key in allowed})
        env_args = []
        for key, value in safe_env.items():
            env_args.extend(["--env", f"{key}={value}"])
        cmd[cmd.index("--entrypoint"):cmd.index("--entrypoint")] = env_args

        return self._run_process(cmd, container_name, timeout_seconds, cancel_event)

    @staticmethod
    def _container_name(job_id: str, source_dir: str = "") -> str:
        suffix = hashlib.sha1(str(source_dir).encode()).hexdigest()[:6] if source_dir else "main"
        return f"ci-{job_id[:12]}-{suffix}"

    @staticmethod
    def _job_container_prefix(job_id: str) -> str:
        """Prefix shared by every container owned by a job (main + per-workspace)."""
        return f"ci-{job_id[:12]}"

    def kill_job(self, job_id: str) -> int:
        """Force-stop and remove every container owned by the job."""
        prefix = self._job_container_prefix(job_id)
        names = self._container_names_matching(prefix)
        for name in names:
            self._kill_container(name)
            try:
                subprocess.run([self.podman, "rm", "-f", name],
                               capture_output=True, timeout=10)
            except Exception:
                pass
        if names:
            logger.warning("Cancelled job %s: reclaimed %d container(s)", job_id[:12], len(names))
        return len(names)

    def _container_names_matching(self, prefix: str) -> list[str]:
        try:
            result = subprocess.run(
                [self.podman, "ps", "-a", "--format", "{{.Names}}"],
                capture_output=True, text=True, timeout=10,
            )
            return [name for name in result.stdout.strip().split("\n") if name.startswith(prefix)]
        except Exception as exc:
            logger.warning("Listing containers failed: %s", exc)
            return []

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
        self.kill_job(job_id)

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
