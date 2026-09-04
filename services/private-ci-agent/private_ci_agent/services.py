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

from private_ci_agent.config import resolve_worker_id

logger = logging.getLogger(__name__)
JOB_ID_RE = re.compile(r"[^a-z0-9]+")
DIAGNOSTIC_REASON_LIMIT = 500
URL_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s]+")
ASSIGNMENT_RE = re.compile(
    r"(?i)\b([A-Za-z0-9_]*(?:password|passwd|token|secret|authorization|cookie))\b"
    r"\s*([=:])\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
JOB_LABEL = "private-ci.job"
RESOURCE_LABEL = "private-ci.resource"


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
        parts = []
        if self.database_url:
            db = self.database_url.rsplit("/", 1)[-1].split("?", 1)[0]
            parts.append(f"DATABASE_HOST=postgres DATABASE_PORT=5432 DATABASE_NAME={db}")
        if self.redis_addr:
            parts.append(f"REDIS_HOST=redis REDIS_PORT=6379 REDIS_DB={self.redis_db}")
        if self.rabbitmq_url:
            parts.append("RABBITMQ_HOST=rabbitmq RABBITMQ_PORT=5672")
        return " ".join(parts)


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
    """Own only explicitly requested per-job service resources."""

    SUPPORTED_SERVICES = ("postgres", "redis", "rabbitmq")

    def __init__(self, podman_binary: str = "/usr/bin/podman", config: dict | None = None):
        config = config or {}
        self.podman = podman_binary
        self.worker_id = resolve_worker_id(config.get("worker_id"))
        self.images = {
            "postgres": config.get("ci_postgres_image", "docker.io/library/postgres:16-alpine"),
            "redis": config.get("ci_redis_image", "docker.io/library/redis:7-alpine"),
            "rabbitmq": config.get("ci_rabbitmq_image", "docker.io/library/rabbitmq:3-management-alpine"),
        }
        self.timeout = min(int(config.get("ci_services_timeout_seconds", 90)), 90)

    def prepare(
        self,
        job_id: str,
        workspace: str,
        services: list[str] | tuple[str, ...] | None = None,
    ) -> ServiceEnvironment:
        requested = tuple(dict.fromkeys(services or ()))
        invalid = [name for name in requested if name not in self.SUPPORTED_SERVICES]
        if invalid or not requested:
            raise ServiceSetupError(
                "SERVICE_CONFIGURATION_INVALID",
                f"unsupported or empty service request: {invalid or requested}",
            )

        suffix = safe_job_suffix(job_id)
        network = f"ci-svc-{self.worker_id}-{suffix}"
        names = {kind: f"ci-{self.worker_id}-{suffix}-{kind}" for kind in requested}
        database = f"lenshub_ci_{suffix}"
        db_password = secrets.token_urlsafe(24)
        rabbit_password = secrets.token_urlsafe(24)
        rabbit_user = f"ci_{suffix[:40]}"
        rabbit_vhost = f"ci_{suffix[:40]}"
        env_file = os.path.join(workspace, "runtime", "services.env")
        try:
            # Job execution stays side-effect bounded: service images must have
            # been preloaded by controlled maintenance, and only explicitly
            # requested services are inspected or started.
            for kind in requested:
                self._run(
                    ["image", "exists", self.images[kind]],
                    f"{kind.upper()}_UNAVAILABLE",
                    resource_type=kind,
                    operation="inspect",
                    image=self.images[kind],
                )

            pod_args = [
                "pod", "create", "--name", network,
                "--label", f"{JOB_LABEL}={suffix}",
                "--label", f"{RESOURCE_LABEL}=pod",
                "--userns=keep-id",
                "--network", "slirp4netns:allow_host_loopback=true",
            ]
            for kind in requested:
                pod_args += ["--add-host", f"{kind}:127.0.0.1"]
            self._run(
                pod_args,
                "SERVICE_SETUP_FAILED",
                resource_type="pod",
                operation="create",
                resource_name=network,
            )

            if "postgres" in requested:
                self._run([
                    "run", "-d", "--http-proxy=false",
                    "--name", names["postgres"],
                    "--label", f"{JOB_LABEL}={suffix}",
                    "--label", f"{RESOURCE_LABEL}=postgres",
                    "--pod", network, "--user", "0",
                    "-e", "POSTGRES_USER=lenshub",
                    "-e", f"POSTGRES_PASSWORD={db_password}",
                    "-e", "POSTGRES_DB=postgres",
                    self.images["postgres"],
                ], "POSTGRES_UNAVAILABLE", resource_type="postgres",
                    operation="start_container", image=self.images["postgres"],
                    resource_name=names["postgres"])

            if "redis" in requested:
                self._run([
                    "run", "-d", "--http-proxy=false",
                    "--name", names["redis"],
                    "--label", f"{JOB_LABEL}={suffix}",
                    "--label", f"{RESOURCE_LABEL}=redis",
                    "--pod", network,
                    self.images["redis"], "redis-server", "--save", "", "--appendonly", "no",
                ], "REDIS_UNAVAILABLE", resource_type="redis",
                    operation="start_container", image=self.images["redis"],
                    resource_name=names["redis"])

            if "rabbitmq" in requested:
                self._run([
                    "run", "-d", "--http-proxy=false",
                    "--name", names["rabbitmq"],
                    "--label", f"{JOB_LABEL}={suffix}",
                    "--label", f"{RESOURCE_LABEL}=rabbitmq",
                    "--pod", network,
                    "-e", f"RABBITMQ_DEFAULT_USER={rabbit_user}",
                    "-e", f"RABBITMQ_DEFAULT_PASS={rabbit_password}",
                    "-e", f"RABBITMQ_DEFAULT_VHOST={rabbit_vhost}",
                    self.images["rabbitmq"],
                ], "RABBITMQ_UNAVAILABLE", resource_type="rabbitmq",
                    operation="start_container", image=self.images["rabbitmq"],
                    resource_name=names["rabbitmq"])

            self._wait_ready(names)

            if "postgres" in requested:
                self._run([
                    "exec", "-e", f"PGPASSWORD={db_password}", names["postgres"],
                    "psql", "-U", "lenshub", "-d", "postgres",
                    "-v", "ON_ERROR_STOP=1", "-c", f"CREATE DATABASE {database}",
                ], "POSTGRES_SETUP_FAILED", resource_type="postgres",
                    operation="create_database", image=self.images["postgres"],
                    resource_name=names["postgres"])

            env = ServiceEnvironment(
                network=network,
                database_url=(
                    f"postgres://lenshub:{quote(db_password, safe='')}@postgres:5432/{database}?sslmode=disable"
                    if "postgres" in requested else ""
                ),
                redis_addr="redis:6379" if "redis" in requested else "",
                redis_db="0" if "redis" in requested else "",
                rabbitmq_url=(
                    f"amqp://{quote(rabbit_user, safe='')}:{quote(rabbit_password, safe='')}@rabbitmq:5672/{quote(rabbit_vhost, safe='')}"
                    if "rabbitmq" in requested else ""
                ),
                env_file=env_file,
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
        probes = {}
        if "postgres" in names:
            probes["postgres"] = ["exec", names["postgres"], "pg_isready", "-U", "lenshub", "-d", "postgres"]
        if "redis" in names:
            probes["redis"] = ["exec", names["redis"], "redis-cli", "ping"]
        if "rabbitmq" in names:
            probes["rabbitmq"] = ["exec", names["rabbitmq"], "rabbitmq-diagnostics", "-q", "ping"]

        attempts = 0
        last_probe_reasons = {}
        while time.monotonic() < deadline:
            attempts += 1
            probe_results = {kind: self._try_result(command) for kind, command in probes.items()}
            ready = {kind: result is not None and result.returncode == 0 for kind, result in probe_results.items()}
            last_probe_reasons = {
                kind: sanitize_service_stderr((result.stderr or result.stdout or "").strip())
                for kind, result in probe_results.items()
                if result is not None and result.returncode != 0
            }
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
                            code, "readiness", kind, state["exit_code"], False,
                            f"attempts={attempts} state={state['status']} health={state.get('health', 'unknown')} "
                            f"observed=inspect,logs {reason or last_probe_reasons.get(kind, 'probe_failed')}",
                            self.images[kind],
                            resource_name=name,
                        ),
                    )
            time.sleep(2)

        raise ServiceSetupError(
            "SERVICE_SETUP_TIMEOUT",
            self._diagnostic(
                "SERVICE_SETUP_TIMEOUT", "readiness", "services", -1, True,
                f"attempts={attempts} service readiness timeout "
                + " ".join(f"{kind}={reason}" for kind, reason in sorted(last_probe_reasons.items())),
                resource_name=",".join(names.values()),
            ),
        )

    def _write_env(self, env: ServiceEnvironment) -> None:
        path = Path(env.env_file)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(f"DATABASE_URL={env.database_url}\nREDIS_ADDR={env.redis_addr}\nREDIS_PASSWORD=\nREDIS_DB={env.redis_db}\nRABBITMQ_URL={env.rabbitmq_url}\n", encoding="utf-8")
        os.chmod(path, 0o600)

    def cleanup(self, job_id: str, workspace: str = "") -> None:
        # 资源名绑定 Worker + Job，只处理当前实例自己的精确名称。
        suffix = safe_job_suffix(job_id)
        pod_name = f"ci-svc-{self.worker_id}-{suffix}"
        self._cleanup_resource(["pod", "rm", "-f", pod_name], "pod", pod_name)
        for kind in self.images:
            name = f"ci-{self.worker_id}-{suffix}-{kind}"
            self._cleanup_resource(["rm", "-f", name], kind, name)
        if workspace:
            try:
                os.remove(os.path.join(workspace, "runtime", "services.env"))
            except OSError:
                pass

    @staticmethod
    def _diagnostic(code: str, operation: str, resource_type: str, exit_code: int | None,
                    timed_out: bool, reason: str, image: str | None = None,
                    resource_name: str | None = None) -> str:
        safe_image = sanitize_service_stderr(image or "-")
        safe_name = sanitize_service_stderr(resource_name or "-")
        return (
            f"code={code} operation={operation} exit_code={exit_code if exit_code is not None else '-'} "
            f"resource={resource_type} name={safe_name} image={safe_image} timed_out={'true' if timed_out else 'false'} "
            f"reason={sanitize_service_stderr(reason)}"
        )

    def _run(self, args: list[str], error_code: str, *, resource_type: str | None = None,
             operation: str | None = None, image: str | None = None,
             resource_name: str | None = None) -> None:
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
                logger.info(
                    "services operation=%s resource=%s name=%s exit_code=0 image=%s",
                    operation, resource_type, resource_name or "-", image or "-",
                )
                return
            if attempt + 1 < attempts:
                time.sleep(1)
        context = self._container_failure_context(resource_name) if resource_name else ""
        reason = " ".join(item for item in (reason, context) if item).strip()
        raise ServiceSetupError(
            error_code,
            self._diagnostic(
                error_code, operation, resource_type, exit_code, timed_out, reason, image, resource_name
            ),
        )

    def _try(self, args: list[str]) -> bool:
        result = self._try_result(args)
        return result is not None and result.returncode == 0

    def _try_result(self, args: list[str]):
        try:
            return subprocess.run([self.podman, *args], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return None

    def _cleanup_resource(self, args: list[str], resource_type: str, name: str) -> None:
        result = self._try_result(args)
        logger.info(
            "services operation=cleanup resource=%s name=%s exit_code=%s",
            resource_type,
            name,
            result.returncode if result is not None else -1,
        )

    def _container_state(self, name: str) -> dict | None:
        try:
            inspected = subprocess.run(
                [
                    self.podman,
                    "inspect",
                    "--format",
                    "{{.State.Status}}|{{.State.ExitCode}}|{{.State.Error}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}unknown{{end}}",
                    name,
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if inspected.returncode != 0:
                return None
            status, exit_code, error, health = (inspected.stdout.strip().split("|", 3) + ["", "", "", ""])[:4]
            try:
                parsed_exit_code = int(exit_code)
            except ValueError:
                parsed_exit_code = -1
            logger.info(
                "services operation=inspect resource=container name=%s exit_code=%s state=%s health=%s",
                name,
                parsed_exit_code,
                sanitize_service_stderr(status),
                sanitize_service_stderr(health or "unknown"),
            )
            logs = ""
            if status in {"exited", "dead"}:
                log_result = subprocess.run(
                    [self.podman, "logs", "--tail", "20", name],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                logs = log_result.stderr.strip() or log_result.stdout.strip()
                logger.info(
                    "services operation=logs resource=container name=%s exit_code=%s",
                    name,
                    log_result.returncode,
                )
            return {
                "status": status,
                "exit_code": parsed_exit_code,
                "error": sanitize_service_stderr(error),
                "health": sanitize_service_stderr(health or "unknown"),
                "logs": sanitize_service_stderr(logs),
            }
        except (OSError, subprocess.TimeoutExpired):
            return None

    def _container_failure_context(self, name: str) -> str:
        state = self._container_state(name)
        if not state:
            return ""
        return sanitize_service_stderr(
            f"state={state['status']} health={state.get('health', 'unknown')} "
            f"exit={state['exit_code']} {state['error']} {state['logs']}"
        )



@dataclass
class MultiDataPlaneServiceEnvironment(ServiceEnvironment):
    global_database_url: str = ""
    regional_cn_database_url: str = ""
    regional_de_database_url: str = ""
    service_evidence: tuple[str, ...] = ()

    def public_summary(self) -> str:
        return " ".join(f"{item}=ready" for item in self.service_evidence)


_MULTI_POSTGRES = {
    "postgres-global": {
        "role": "global",
        "host": "postgres",
        "port": 5432,
        "error_code": "CI_INFRA_POSTGRES_GLOBAL_UNAVAILABLE",
    },
    "postgres-regional-cn": {
        "role": "regional-cn",
        "host": "postgres-regional-cn",
        "port": 5433,
        "error_code": "CI_INFRA_POSTGRES_REGIONAL_CN_UNAVAILABLE",
    },
    "postgres-regional-de": {
        "role": "regional-de",
        "host": "postgres-regional-de",
        "port": 5434,
        "error_code": "CI_INFRA_POSTGRES_REGIONAL_DE_UNAVAILABLE",
    },
}
_MULTI_POSTGRES_SERVICES = tuple(_MULTI_POSTGRES)
_MULTI_POSTGRES_LIMITS = (
    "--pids-limit=128", "--memory=160m", "--memory-swap=192m", "--cpus=0.30",
)
_MULTI_REDIS_LIMITS = (
    "--pids-limit=96", "--memory=64m", "--memory-swap=96m", "--cpus=0.15",
)
_MULTI_RABBITMQ_LIMITS = (
    "--pids-limit=192", "--memory=256m", "--memory-swap=320m", "--cpus=0.30",
)


def service_evidence_name(service: str) -> str:
    spec = _MULTI_POSTGRES.get(service)
    return f"postgres:{spec['role']}" if spec else service


class MultiDataPlaneServiceManager(ServiceManager):
    """Add a fixed three-PostgreSQL topology without changing legacy service behavior."""

    SUPPORTED_SERVICES = ServiceManager.SUPPORTED_SERVICES + _MULTI_POSTGRES_SERVICES

    def __init__(self, podman_binary: str = "/usr/bin/podman", config: dict | None = None):
        super().__init__(podman_binary, config)
        postgres_image = self.images["postgres"]
        self.images.update({name: postgres_image for name in _MULTI_POSTGRES_SERVICES})

    def _volume_name(self, suffix: str, service: str) -> str:
        return f"ci-{self.worker_id}-{suffix}-{service}-data"

    def prepare(
        self,
        job_id: str,
        workspace: str = "",
        services: list[str] | tuple[str, ...] | None = None,
    ) -> ServiceEnvironment:
        requested = tuple(dict.fromkeys(services or ()))
        invalid = sorted(set(requested) - set(self.SUPPORTED_SERVICES))
        if invalid or not requested:
            raise ServiceSetupError(
                "SERVICE_CONFIGURATION_INVALID",
                f"code=SERVICE_CONFIGURATION_INVALID operation=validate resource=services invalid={invalid or ['none']}",
            )
        multi_requested = tuple(name for name in requested if name in _MULTI_POSTGRES)
        if not multi_requested:
            return super().prepare(job_id, workspace, requested)
        if "postgres" in requested or set(multi_requested) != set(_MULTI_POSTGRES_SERVICES):
            raise ServiceSetupError(
                "SERVICE_CONFIGURATION_INVALID",
                "code=SERVICE_CONFIGURATION_INVALID operation=validate resource=postgres-multidataplane reason=fixed_three_postgres_required",
            )

        suffix = safe_job_suffix(job_id)
        network = f"ci-svc-{self.worker_id}-{suffix}"
        names = {kind: f"ci-{self.worker_id}-{suffix}-{kind}" for kind in requested}
        volumes = {kind: self._volume_name(suffix, kind) for kind in _MULTI_POSTGRES_SERVICES}
        database = f"lenshub_ci_{suffix}"[:63]
        passwords = {kind: secrets.token_urlsafe(24) for kind in _MULTI_POSTGRES_SERVICES}
        rabbit_password = secrets.token_urlsafe(24)
        rabbit_vhost = f"ci_{suffix}"[:48]
        created_pod = False
        created_volumes = []

        try:
            for kind in requested:
                image = self.images[kind]
                error_code = _MULTI_POSTGRES.get(kind, {}).get(
                    "error_code",
                    "REDIS_UNAVAILABLE" if kind == "redis" else "RABBITMQ_UNAVAILABLE",
                )
                self._run(
                    ["image", "exists", image], error_code,
                    resource_type=kind, operation="inspect", image=image,
                )

            for kind in _MULTI_POSTGRES_SERVICES:
                volume = volumes[kind]
                self._run(
                    [
                        "volume", "create",
                        "--label", f"{JOB_LABEL}={suffix}",
                        "--label", f"{RESOURCE_LABEL}={kind}-data",
                        volume,
                    ],
                    _MULTI_POSTGRES[kind]["error_code"],
                    resource_type=kind,
                    operation="create_volume",
                    resource_name=volume,
                )
                created_volumes.append(volume)

            pod_command = [
                "pod", "create", "--name", network,
                "--label", f"{JOB_LABEL}={suffix}",
                "--label", f"{RESOURCE_LABEL}=pod",
                "--userns=keep-id",
                "--network", "slirp4netns:allow_host_loopback=true",
            ]
            aliases = list(requested)
            if "postgres-global" in requested:
                aliases.append("postgres")
            for alias in dict.fromkeys(aliases):
                pod_command.extend(["--add-host", f"{alias}:127.0.0.1"])
            self._run(
                pod_command,
                "SERVICE_NETWORK_UNAVAILABLE",
                resource_type="pod", operation="create", resource_name=network,
            )
            created_pod = True

            for kind in _MULTI_POSTGRES_SERVICES:
                spec = _MULTI_POSTGRES[kind]
                self._run(
                    [
                        "run", "-d", "--name", names[kind],
                        "--label", f"{JOB_LABEL}={suffix}",
                        "--label", f"{RESOURCE_LABEL}={kind}",
                        "--http-proxy=false", "--pod", network, "--user", "0",
                        *_MULTI_POSTGRES_LIMITS,
                        "-v", f"{volumes[kind]}:/var/lib/postgresql/data:Z",
                        "-e", "POSTGRES_USER=lenshub",
                        "-e", f"POSTGRES_PASSWORD={passwords[kind]}",
                        "-e", "POSTGRES_DB=postgres",
                        self.images[kind],
                        "-c", f"port={spec['port']}",
                        "-c", "shared_buffers=32MB",
                        "-c", "max_connections=30",
                    ],
                    spec["error_code"],
                    resource_type=kind, operation="start_container",
                    image=self.images[kind], resource_name=names[kind],
                )

            if "redis" in requested:
                self._run(
                    [
                        "run", "-d", "--name", names["redis"],
                        "--label", f"{JOB_LABEL}={suffix}",
                        "--label", f"{RESOURCE_LABEL}=redis",
                        "--http-proxy=false", "--pod", network,
                        *_MULTI_REDIS_LIMITS,
                        self.images["redis"],
                    ],
                    "REDIS_UNAVAILABLE", resource_type="redis", operation="start_container",
                    image=self.images["redis"], resource_name=names["redis"],
                )

            if "rabbitmq" in requested:
                self._run(
                    [
                        "run", "-d", "--name", names["rabbitmq"],
                        "--label", f"{JOB_LABEL}={suffix}",
                        "--label", f"{RESOURCE_LABEL}=rabbitmq",
                        "--http-proxy=false", "--pod", network,
                        *_MULTI_RABBITMQ_LIMITS,
                        "-e", "RABBITMQ_DEFAULT_USER=lenshub",
                        "-e", f"RABBITMQ_DEFAULT_PASS={rabbit_password}",
                        "-e", f"RABBITMQ_DEFAULT_VHOST={rabbit_vhost}",
                        self.images["rabbitmq"],
                    ],
                    "RABBITMQ_UNAVAILABLE", resource_type="rabbitmq", operation="start_container",
                    image=self.images["rabbitmq"], resource_name=names["rabbitmq"],
                )

            self._wait_ready_multi(names)
            for kind in _MULTI_POSTGRES_SERVICES:
                spec = _MULTI_POSTGRES[kind]
                self._run(
                    [
                        "exec", names[kind], "psql", "-U", "lenshub", "-d", "postgres",
                        "-p", str(spec["port"]), "-v", "ON_ERROR_STOP=1", "-c",
                        f"CREATE DATABASE {database};",
                    ],
                    spec["error_code"],
                    resource_type=kind, operation="create_database", resource_name=names[kind],
                )

            urls = {}
            for kind in _MULTI_POSTGRES_SERVICES:
                spec = _MULTI_POSTGRES[kind]
                urls[kind] = (
                    f"postgres://lenshub:{quote(passwords[kind])}@{spec['host']}:{spec['port']}/"
                    f"{database}?sslmode=disable"
                )
            env = MultiDataPlaneServiceEnvironment(
                network=network,
                database_url=urls["postgres-global"],
                redis_addr="redis:6379" if "redis" in requested else "",
                redis_db="0" if "redis" in requested else "",
                rabbitmq_url=(
                    f"amqp://lenshub:{quote(rabbit_password)}@rabbitmq:5672/{quote(rabbit_vhost, safe='')}"
                    if "rabbitmq" in requested else ""
                ),
                env_file=str(Path(workspace or "/tmp") / "runtime" / "services.env"),
                global_database_url=urls["postgres-global"],
                regional_cn_database_url=urls["postgres-regional-cn"],
                regional_de_database_url=urls["postgres-regional-de"],
                service_evidence=tuple(
                    [f"service:{service_evidence_name(kind)}" for kind in _MULTI_POSTGRES_SERVICES]
                    + (["service:redis"] if "redis" in requested else [])
                    + (["service:rabbitmq"] if "rabbitmq" in requested else [])
                ),
            )
            self._write_multi_env(env)
            logger.info("services:ready job=%s %s", suffix, env.public_summary())
            return env
        except ServiceSetupError:
            if created_pod or created_volumes:
                self.cleanup(job_id, workspace)
            raise
        except Exception as exc:
            if created_pod or created_volumes:
                self.cleanup(job_id, workspace)
            raise ServiceSetupError(
                "SERVICE_SETUP_FAILED",
                self._diagnostic(
                    "SERVICE_SETUP_FAILED", "prepare", "services", None, False,
                    type(exc).__name__,
                ),
            ) from exc

    def _wait_ready_multi(self, names: dict[str, str]) -> None:
        deadline = time.monotonic() + self.timeout
        probes = {
            kind: ["exec", names[kind], "pg_isready", "-U", "lenshub", "-p", str(_MULTI_POSTGRES[kind]["port"])]
            for kind in _MULTI_POSTGRES_SERVICES
        }
        if "redis" in names:
            probes["redis"] = ["exec", names["redis"], "redis-cli", "ping"]
        if "rabbitmq" in names:
            probes["rabbitmq"] = ["exec", names["rabbitmq"], "rabbitmq-diagnostics", "-q", "ping"]
        ready = set()
        attempts = 0
        while time.monotonic() < deadline:
            attempts += 1
            for kind, command in probes.items():
                if kind in ready:
                    continue
                result = self._try_result(command)
                if result is not None and result.returncode == 0:
                    ready.add(kind)
                    continue
                state = self._container_state(names[kind])
                if state and state.get("status") not in {"running", "paused"}:
                    error_code = _MULTI_POSTGRES.get(kind, {}).get(
                        "error_code",
                        "REDIS_UNAVAILABLE" if kind == "redis" else "RABBITMQ_UNAVAILABLE",
                    )
                    raise ServiceSetupError(
                        error_code,
                        self._diagnostic(
                            error_code, "readiness", kind,
                            int(state.get("exit_code", -1)), False,
                            f"attempts={attempts} state={state.get('status', 'unknown')} "
                            f"health={state.get('health', 'unknown')}",
                            resource_name=names[kind],
                        ),
                    )
            if len(ready) == len(probes):
                return
            time.sleep(1)
        missing = next(kind for kind in probes if kind not in ready)
        error_code = _MULTI_POSTGRES.get(missing, {}).get(
            "error_code",
            "REDIS_UNAVAILABLE" if missing == "redis" else "RABBITMQ_UNAVAILABLE",
        )
        raise ServiceSetupError(
            error_code,
            self._diagnostic(
                error_code, "readiness", missing, -1, True,
                f"attempts={attempts} health=not_ready",
                resource_name=names[missing],
            ),
        )

    def _write_multi_env(self, env: MultiDataPlaneServiceEnvironment) -> None:
        path = Path(env.env_file)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(
            "\n".join([
                f"DATABASE_URL={env.database_url}",
                f"CI_GLOBAL_DATABASE_URL={env.global_database_url}",
                f"CI_REGIONAL_CN_DATABASE_URL={env.regional_cn_database_url}",
                f"CI_REGIONAL_DE_DATABASE_URL={env.regional_de_database_url}",
                f"REDIS_ADDR={env.redis_addr}",
                "REDIS_PASSWORD=",
                f"REDIS_DB={env.redis_db}",
                f"RABBITMQ_URL={env.rabbitmq_url}",
                "",
            ]),
            encoding="utf-8",
        )
        os.chmod(path, 0o600)

    def cleanup(self, job_id: str, workspace: str = "") -> None:
        suffix = safe_job_suffix(job_id)
        super().cleanup(job_id, workspace)
        for kind in _MULTI_POSTGRES_SERVICES:
            volume = self._volume_name(suffix, kind)
            self._cleanup_resource(["volume", "rm", "-f", volume], kind, volume)

def cleanup_job_services(podman_binary: str, job_id: str, workspace: str = "") -> None:
    MultiDataPlaneServiceManager(podman_binary).cleanup(job_id, workspace)
