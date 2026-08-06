"""Per-job isolated PostgreSQL, Redis and RabbitMQ services."""

import logging
import os
import re
import secrets
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit

logger = logging.getLogger(__name__)
JOB_ID_RE = re.compile(r"[^a-z0-9]+")
DIAGNOSTIC_REASON_LIMIT = 500
URL_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s]+")
ASSIGNMENT_RE = re.compile(
    r"(?i)\b([A-Za-z0-9_]*(?:password|passwd|token|secret|authorization|cookie))\b"
    r"\s*([=:])\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)


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
        self.diagnostic = message


def _redact_url(value: str) -> str:
    """Remove URL credentials and query data from a Podman error fragment."""
    try:
        parsed = urlsplit(value)
        if not parsed.scheme or not parsed.hostname:
            return "<redacted-url>"
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        netloc = host
        if parsed.port is not None:
            netloc = f"{netloc}:{parsed.port}"
        # Paths can contain opaque credentials in registry URLs, so omit them.
        return f"{parsed.scheme}://{netloc}"
    except ValueError:
        return "<redacted-url>"


def sanitize_service_stderr(stderr: str) -> str:
    """Return a bounded, credential-free reason suitable for CI logs."""
    value = str(stderr or "")
    value = URL_RE.sub(lambda match: _redact_url(match.group(0)), value)
    value = ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}<redacted>", value)
    value = re.sub(r"(?i)\b(bearer)\s+[^\s,;]+", r"\1 <redacted>", value)
    value = re.sub(r"\s+", " ", value).strip()
    if not value:
        return "unknown"
    return value[:DIAGNOSTIC_REASON_LIMIT]


class ServiceManager:
    """Own only resources named with the current job prefix."""

    def __init__(self, podman_binary: str = "/usr/bin/podman", config: dict | None = None):
        config = config or {}
        self.podman = podman_binary
        self.images = {
            "postgres": config.get("ci_postgres_image", "docker.io/library/postgres:16-alpine"),
            "redis": config.get("ci_redis_image", "docker.io/library/redis:7-alpine"),
            "rabbitmq": config.get("ci_rabbitmq_image", "docker.io/library/rabbitmq:3-management-alpine"),
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
                       "--add-host", "rabbitmq:127.0.0.1"], "SERVICE_SETUP_FAILED",
                      resource_type="pod", operation="create")
            self._run(["run", "-d", "--http-proxy=false", "--name", names["postgres"], "--pod", network, "--user", "0",
                       "-e", "POSTGRES_USER=lenshub",
                       "-e", f"POSTGRES_PASSWORD={db_password}", "-e", "POSTGRES_DB=postgres",
                       self.images["postgres"]], "POSTGRES_UNAVAILABLE", resource_type="postgres",
                      operation="start_container", image=self.images["postgres"])
            self._run(["run", "-d", "--http-proxy=false", "--name", names["redis"], "--pod", network, self.images["redis"], "redis-server",
                       "--save", "", "--appendonly", "no"], "REDIS_UNAVAILABLE", resource_type="redis",
                      operation="start_container", image=self.images["redis"])
            self._run(["run", "-d", "--http-proxy=false", "--name", names["rabbitmq"], "--pod", network,
                       "-e", f"RABBITMQ_DEFAULT_USER={rabbit_user}",
                       "-e", f"RABBITMQ_DEFAULT_PASS={rabbit_password}",
                       "-e", f"RABBITMQ_DEFAULT_VHOST={rabbit_vhost}", self.images["rabbitmq"]], "RABBITMQ_UNAVAILABLE",
                      resource_type="rabbitmq", operation="start_container", image=self.images["rabbitmq"])
            self._wait_ready(names)
            self._run(["exec", "-e", f"PGPASSWORD={db_password}", names["postgres"], "psql",
                       "-U", "lenshub", "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-c",
                       f"CREATE DATABASE {database}"], "POSTGRES_SETUP_FAILED", resource_type="postgres",
                      operation="create_database", image=self.images["postgres"])
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
            raise ServiceSetupError(
                "SERVICE_SETUP_FAILED",
                self._diagnostic(
                    "SERVICE_SETUP_FAILED", "prepare", "services", None, False,
                    type(exc).__name__,
                ),
            ) from exc

    def _wait_ready(self, names: dict) -> None:
        deadline = time.monotonic() + self.timeout
        probes = {
            "postgres": ["exec", names["postgres"], "pg_isready", "-U", "lenshub", "-d", "postgres"],
            "redis": ["exec", names["redis"], "redis-cli", "ping"],
            "rabbitmq": ["exec", names["rabbitmq"], "rabbitmq-diagnostics", "-q", "ping"],
        }
        while time.monotonic() < deadline:
            ready = {kind: self._try(command) for kind, command in probes.items()}
            if all(ready.values()):
                return
            for kind, name in names.items():
                state = self._container_state(name)
                if state and state["status"] in {"exited", "dead"}:
                    code = {
                        "postgres": "POSTGRES_UNAVAILABLE",
                        "redis": "REDIS_UNAVAILABLE",
                        "rabbitmq": "RABBITMQ_UNAVAILABLE",
                    }[kind]
                    reason = " ".join(item for item in (state["error"], state["logs"]) if item).strip()
                    raise ServiceSetupError(
                        code,
                        self._diagnostic(
                            code,
                            "readiness",
                            kind,
                            state["exit_code"],
                            False,
                            reason or f"container_status={state['status']}",
                            self.images[kind],
                        ),
                    )
            time.sleep(2)
        raise ServiceSetupError(
            "SERVICE_SETUP_TIMEOUT",
            self._diagnostic(
                "SERVICE_SETUP_TIMEOUT", "readiness", "services", -1, True,
                "postgres/redis/rabbitmq readiness timeout",
            ),
        )

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
            except OSError:
                pass

    @staticmethod
    def _diagnostic(code: str, operation: str, resource_type: str, exit_code: int | None,
                    timed_out: bool, reason: str, image: str | None = None) -> str:
        safe_image = sanitize_service_stderr(image or "-")
        return (
            f"code={code} operation={operation} exit_code={exit_code if exit_code is not None else '-'} "
            f"resource={resource_type} image={safe_image} timed_out={'true' if timed_out else 'false'} "
            f"reason={sanitize_service_stderr(reason)}"
        )

    def _run(self, args: list[str], error_code: str, *, resource_type: str | None = None,
             operation: str | None = None, image: str | None = None) -> None:
        attempts = 3 if args[:2] == ["pod", "create"] else 1
        resource_type = resource_type or ("pod" if args[:2] == ["pod", "create"] else "service")
        operation = operation or ("create" if args[:2] == ["pod", "create"] else "run")
        exit_code = None
        timed_out = False
        reason = "unknown"
        for attempt in range(attempts):
            try:
                result = subprocess.run([self.podman, *args], capture_output=True, text=True, timeout=20)
                exit_code = result.returncode
                timed_out = False
                reason = (result.stderr or "").strip() or f"podman_exit_{result.returncode}"
            except subprocess.TimeoutExpired:
                exit_code = -1
                timed_out = True
                reason = "podman command timeout"
                result = None
            except OSError as exc:
                exit_code = -1
                timed_out = False
                reason = type(exc).__name__
                result = None
            if result is not None and result.returncode == 0:
                return
            if attempt + 1 < attempts:
                time.sleep(1)
        raise ServiceSetupError(
            error_code,
            self._diagnostic(error_code, operation, resource_type, exit_code, timed_out, reason, image),
        )

    def _try(self, args: list[str]) -> bool:
        result = self._try_result(args)
        return result is not None and result.returncode == 0

    def _try_result(self, args: list[str]):
        try:
            return subprocess.run([self.podman, *args], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return None

    def _container_state(self, name: str) -> dict | None:
        try:
            inspected = subprocess.run(
                [self.podman, "inspect", "--format", "{{.State.Status}}|{{.State.ExitCode}}|{{.State.Error}}", name],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if inspected.returncode != 0:
                return None
            status, exit_code, error = (inspected.stdout.strip().split("|", 2) + ["", "", ""])[:3]
            try:
                parsed_exit_code = int(exit_code)
            except ValueError:
                parsed_exit_code = -1
            logs = ""
            if status in {"exited", "dead"}:
                log_result = subprocess.run(
                    [self.podman, "logs", "--tail", "20", name],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                logs = log_result.stderr.strip() or log_result.stdout.strip()
            return {"status": status, "exit_code": parsed_exit_code, "error": error, "logs": logs}
        except (OSError, subprocess.TimeoutExpired):
            return None


def cleanup_job_services(podman_binary: str, job_id: str, workspace: str = "") -> None:
    ServiceManager(podman_binary).cleanup(job_id, workspace)
