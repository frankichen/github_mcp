"""Per-job isolated PostgreSQL, Redis and RabbitMQ services."""

import logging
import os
import re
import secrets
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

logger = logging.getLogger(__name__)
JOB_ID_RE = re.compile(r"[^a-z0-9]+")


def safe_job_suffix(job_id: str) -> str:
    return (JOB_ID_RE.sub("_", job_id.lower()).strip("_") or "job")[:48]


@dataclass
class ServiceEnvironment:
    network: str
    database_url: str
    redis_addr: str
    redis_db: str
    rabbitmq_url: str
    env_file: str

    def public_summary(self) -> str:
        db = self.database_url.rsplit("/", 1)[-1].split("?", 1)[0]
        return (f"DATABASE_HOST=postgres DATABASE_PORT=5432 DATABASE_NAME={db} "
                f"REDIS_HOST=redis REDIS_PORT=6379 REDIS_DB={self.redis_db} "
                "RABBITMQ_HOST=rabbitmq RABBITMQ_PORT=5672")


class ServiceSetupError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ServiceManager:
    """Own only resources named with the current job prefix."""

    def __init__(self, podman_binary: str = "/usr/bin/podman", config: dict | None = None):
        config = config or {}
        self.podman = podman_binary
        self.images = {
            "postgres": config.get("ci_postgres_image", "localhost/postgres:16-alpine"),
            "redis": config.get("ci_redis_image", "localhost/redis:7-alpine"),
            "rabbitmq": config.get("ci_rabbitmq_image", "localhost/rabbitmq:3-management-alpine"),
        }
        self.timeout = min(int(config.get("ci_services_timeout_seconds", 90)), 90)

    def prepare(self, job_id: str, workspace: str) -> ServiceEnvironment:
        suffix = safe_job_suffix(job_id)
        network = f"ci-svc-{suffix}"
        names = {kind: f"ci-{suffix}-{kind}" for kind in self.images}
        database = f"lenshub_ci_{suffix}"
        db_password = secrets.token_urlsafe(24)
        rabbit_password = secrets.token_urlsafe(24)
        rabbit_user = f"ci_{suffix[:40]}"
        rabbit_vhost = f"ci_{suffix[:40]}"
        env_file = os.path.join(workspace, "runtime", "services.env")
        try:
            self._run(["pod", "create", "--name", network, "--userns=keep-id", "--network", "slirp4netns:allow_host_loopback=true",
                       "--add-host", "postgres:127.0.0.1", "--add-host", "redis:127.0.0.1",
                       "--add-host", "rabbitmq:127.0.0.1"], "SERVICE_SETUP_FAILED")
            self._run(["run", "-d", "--name", names["postgres"], "--pod", network, "--user", "0",
                       "-e", "POSTGRES_USER=lenshub",
                       "-e", f"POSTGRES_PASSWORD={db_password}", "-e", "POSTGRES_DB=postgres",
                       self.images["postgres"]], "POSTGRES_UNAVAILABLE")
            self._run(["run", "-d", "--name", names["redis"], "--pod", network, self.images["redis"], "redis-server",
                       "--save", "", "--appendonly", "no"], "REDIS_UNAVAILABLE")
            self._run(["run", "-d", "--name", names["rabbitmq"], "--pod", network,
                       "-e", f"RABBITMQ_DEFAULT_USER={rabbit_user}",
                       "-e", f"RABBITMQ_DEFAULT_PASS={rabbit_password}",
                       "-e", f"RABBITMQ_DEFAULT_VHOST={rabbit_vhost}", self.images["rabbitmq"]], "RABBITMQ_UNAVAILABLE")
            self._wait_ready(names)
            self._run(["exec", "-e", f"PGPASSWORD={db_password}", names["postgres"], "psql",
                       "-U", "lenshub", "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-c",
                       f"CREATE DATABASE {database}"], "POSTGRES_SETUP_FAILED")
            env = ServiceEnvironment(
                network,
                f"postgres://lenshub:{quote(db_password, safe='')}@postgres:5432/{database}?sslmode=disable",
                "redis:6379", "0",
                f"amqp://{quote(rabbit_user, safe='')}:{quote(rabbit_password, safe='')}@rabbitmq:5672/{quote(rabbit_vhost, safe='')}",
                env_file,
            )
            self._write_env(env)
            logger.info("services:ready job=%s %s", suffix, env.public_summary())
            return env
        except ServiceSetupError:
            self.cleanup(job_id, workspace)
            raise
        except Exception as exc:
            self.cleanup(job_id, workspace)
            raise ServiceSetupError("SERVICE_SETUP_FAILED", str(exc)) from exc

    def _wait_ready(self, names: dict) -> None:
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            ready = (self._try(["exec", names["postgres"], "pg_isready", "-U", "lenshub", "-d", "postgres"])
                     and self._try(["exec", names["redis"], "redis-cli", "ping"])
                     and self._try(["exec", names["rabbitmq"], "rabbitmq-diagnostics", "-q", "ping"]))
            if ready:
                return
            time.sleep(2)
        raise ServiceSetupError("SERVICE_SETUP_TIMEOUT", "services did not become ready within 90 seconds")

    def _write_env(self, env: ServiceEnvironment) -> None:
        path = Path(env.env_file)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(f"DATABASE_URL={env.database_url}\nREDIS_ADDR={env.redis_addr}\nREDIS_PASSWORD=\nREDIS_DB={env.redis_db}\nRABBITMQ_URL={env.rabbitmq_url}\n", encoding="utf-8")
        os.chmod(path, 0o600)

    def cleanup(self, job_id: str, workspace: str = "") -> None:
        suffix = safe_job_suffix(job_id)
        self._try(["pod", "rm", "-f", f"ci-svc-{suffix}"])
        for kind in self.images:
            self._try(["rm", "-f", f"ci-{suffix}-{kind}"])
        if workspace:
            try:
                os.remove(os.path.join(workspace, "runtime", "services.env"))
            except FileNotFoundError:
                pass

    def _run(self, args: list[str], error_code: str) -> None:
        attempts = 3 if args[:2] == ["pod", "create"] else 1
        last_error = None
        for attempt in range(attempts):
            try:
                result = subprocess.run([self.podman, *args], capture_output=True, text=True, timeout=20)
            except (OSError, subprocess.TimeoutExpired) as exc:
                last_error = exc
                result = None
            if result is not None and result.returncode == 0:
                return
            if result is not None and result.stderr.strip():
                last_error = RuntimeError(result.stderr.strip()[-500:])
            if attempt + 1 < attempts:
                time.sleep(1)
        raise ServiceSetupError(error_code, error_code) from last_error

    def _try(self, args: list[str]) -> bool:
        try:
            return subprocess.run([self.podman, *args], capture_output=True, text=True, timeout=10).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False


def cleanup_job_services(podman_binary: str, job_id: str, workspace: str = "") -> None:
    ServiceManager(podman_binary).cleanup(job_id, workspace)
