"""Ownership-aware lifecycle management for Private CI Podman resources."""

import json
import logging
import os
import subprocess
from collections import Counter
from typing import Callable

logger = logging.getLogger(__name__)

JOB_LABEL = "private-ci.job"
WORKER_LABEL = "private-ci.worker"
RESOURCE_LABEL = "private-ci.resource"

ACTIVE_JOB_STATES = {"leased", "downloading", "preparing", "running"}
STALE_JOB_STATES = {
    "queued", "passed", "failed", "cancelled", "timed_out",
    "internal_error", "superseded", "worker_lost",
}
SHARED_RESOURCE_TYPES = {"cache", "shared-cache"}


class PodmanCleanupError(RuntimeError):
    """A Podman cleanup operation could not be proven safe and complete."""


class PodmanResourceManager:
    """Inventory and remove only resources with exact Private CI ownership."""

    def __init__(self, podman_binary: str, worker_id: str):
        self.podman = podman_binary
        self.worker_id = worker_id

    def label_args(self, job_id: str, resource_type: str) -> list[str]:
        return [
            "--label", f"{WORKER_LABEL}={self.worker_id}",
            "--label", f"{JOB_LABEL}={job_id}",
            "--label", f"{RESOURCE_LABEL}={resource_type}",
        ]

    def _command(self, args: list[str], *, timeout: int = 10):
        try:
            return subprocess.run(
                [self.podman, *args], capture_output=True, text=True, timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PodmanCleanupError(
                f"podman command failed: operation={args[:2]} error={type(exc).__name__}"
            ) from exc

    def _exists(self, kind: str, name: str) -> bool:
        result = self._command(["container" if kind == "container" else kind, "exists", name])
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        raise PodmanCleanupError(
            f"podman exists failed: kind={kind} name={name} exit_code={result.returncode}"
        )

    def _inspect_labels(self, kind: str, name: str) -> dict[str, str]:
        if kind == "pod":
            args = ["pod", "inspect", "--format", "{{json .Labels}}", name]
        elif kind == "volume":
            args = ["volume", "inspect", "--format", "{{json .Labels}}", name]
        else:
            args = ["inspect", "--format", "{{json .Config.Labels}}", name]
        result = self._command(args)
        if result.returncode != 0:
            raise PodmanCleanupError(
                f"podman inspect failed: kind={kind} name={name} exit_code={result.returncode}"
            )
        try:
            labels = json.loads(result.stdout.strip() or "{}")
        except json.JSONDecodeError as exc:
            raise PodmanCleanupError(
                f"podman inspect labels invalid: kind={kind} name={name}"
            ) from exc
        return labels if isinstance(labels, dict) else {}

    def _verify_owned_labels(self, labels: dict[str, str], job_id: str, resource_type: str) -> None:
        expected = {
            WORKER_LABEL: self.worker_id,
            JOB_LABEL: job_id,
            RESOURCE_LABEL: resource_type,
        }
        mismatches = [key for key, value in expected.items() if labels.get(key) != value]
        if mismatches:
            raise PodmanCleanupError(
                "podman ownership mismatch: "
                f"worker={self.worker_id} job={job_id} resource={resource_type} "
                f"labels_missing_or_mismatched={','.join(mismatches)}"
            )

    def _classification_for_labels(
        self,
        labels: dict[str, str],
        *,
        anonymous: bool,
        job_state_resolver: Callable[[str], dict | None] | None,
        state_cache: dict[str, dict | None] | None = None,
    ) -> tuple[str, str]:
        worker_id = labels.get(WORKER_LABEL)
        job_id = labels.get(JOB_LABEL)
        resource_type = labels.get(RESOURCE_LABEL)
        if not worker_id or not job_id or not resource_type:
            if anonymous and not labels:
                return "LEGACY_UNOWNED", "anonymous_volume_without_ownership_labels"
            return "UNKNOWN", "missing_ownership_labels"
        if resource_type in SHARED_RESOURCE_TYPES or resource_type.startswith("shared-"):
            return "SHARED", "shared_resource_type"
        if worker_id != self.worker_id:
            return "UNKNOWN", "other_worker"
        if job_state_resolver is None:
            return "UNKNOWN", "job_state_unavailable"

        cache = state_cache if state_cache is not None else {}
        if job_id not in cache:
            try:
                cache[job_id] = job_state_resolver(job_id)
            except Exception as exc:
                logger.warning(
                    "Podman inventory could not resolve job state: worker=%s job=%s error=%s",
                    self.worker_id, job_id, type(exc).__name__,
                )
                cache[job_id] = None
        state = cache.get(job_id)
        if not isinstance(state, dict):
            return "UNKNOWN", "job_not_found_or_unavailable"
        status = str(state.get("status") or "").lower()
        active_worker = state.get("worker_id")
        if status in ACTIVE_JOB_STATES and active_worker == self.worker_id:
            return "ACTIVE", f"job_status={status}"
        if status in ACTIVE_JOB_STATES:
            return "SAFE_STALE", f"job_active_on_other_worker={active_worker or '-'}"
        if status in STALE_JOB_STATES:
            return "SAFE_STALE", f"job_status={status}"
        return "UNKNOWN", f"unrecognized_job_status={status or '-'}"

    def remove_verified(
        self,
        kind: str,
        name: str,
        job_id: str,
        resource_type: str,
        *,
        require_stale: bool = False,
        job_state_resolver: Callable[[str], dict | None] | None = None,
    ) -> bool:
        if not self._exists(kind, name):
            return False
        labels = self._inspect_labels(kind, name)
        self._verify_owned_labels(labels, job_id, resource_type)
        if require_stale:
            classification, reason = self._classification_for_labels(
                labels, anonymous=False, job_state_resolver=job_state_resolver,
            )
            if classification != "SAFE_STALE":
                raise PodmanCleanupError(
                    "podman stale cleanup refused: "
                    f"kind={kind} name={name} classification={classification} reason={reason}"
                )
        if kind == "pod":
            args = ["pod", "rm", "-f", name]
        elif kind == "volume":
            args = ["volume", "rm", "-f", name]
        elif kind == "container":
            args = ["rm", "-f", "-v", name]
        else:
            raise PodmanCleanupError(f"unsupported Podman resource kind: {kind}")

        logger.info(
            "podman cleanup requested worker=%s job=%s resource_type=%s resource_name=%s",
            self.worker_id, job_id, resource_type, name,
        )
        result = self._command(args)
        if result.returncode != 0:
            logger.error(
                "podman cleanup failed worker=%s job=%s resource_type=%s resource_name=%s exit_code=%s",
                self.worker_id, job_id, resource_type, name, result.returncode,
            )
            raise PodmanCleanupError(
                f"podman remove failed: kind={kind} name={name} exit_code={result.returncode}"
            )
        if self._exists(kind, name):
            logger.error(
                "podman cleanup failed worker=%s job=%s resource_type=%s resource_name=%s post_condition=still_exists",
                self.worker_id, job_id, resource_type, name,
            )
            raise PodmanCleanupError(
                f"podman remove post-condition failed: kind={kind} name={name}"
            )
        logger.info(
            "podman cleanup succeeded worker=%s job=%s resource_type=%s resource_name=%s",
            self.worker_id, job_id, resource_type, name,
        )
        return True

    def remove_current_job_container_verified(self, name: str, job_id: str, legacy_name_prefix: str) -> bool:
        if not self._exists("container", name):
            return False
        labels = self._inspect_labels("container", name)
        if labels.get(WORKER_LABEL) is None and name.startswith(legacy_name_prefix):
            resource_type = labels.get(RESOURCE_LABEL) or "legacy-current-job-container"
        else:
            if labels.get(WORKER_LABEL) != self.worker_id or labels.get(JOB_LABEL) != job_id:
                raise PodmanCleanupError(f"current job container ownership mismatch: name={name}")
            resource_type = labels.get(RESOURCE_LABEL) or "build-container"

        result = self._command(["rm", "-f", "-v", name])
        if result.returncode != 0:
            raise PodmanCleanupError(
                f"podman current-job container remove failed: name={name} exit_code={result.returncode}"
            )
        if self._exists("container", name):
            raise PodmanCleanupError(
                f"podman current-job container post-condition failed: name={name}"
            )
        logger.info(
            "podman cleanup succeeded worker=%s job=%s resource_type=%s resource_name=%s",
            self.worker_id, job_id, resource_type, name,
        )
        return True

    def _list_json(self, args: list[str]) -> list[dict]:
        result = self._command(args)
        if result.returncode != 0:
            raise PodmanCleanupError(
                f"podman inventory command failed: operation={args[:2]} exit_code={result.returncode}"
            )
        try:
            value = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise PodmanCleanupError(
                f"podman inventory returned invalid JSON: operation={args[:2]}"
            ) from exc
        return value if isinstance(value, list) else []

    def _volume_references(self, name: str, mount_count: int) -> list[str]:
        if mount_count <= 0:
            return []
        result = self._command(
            ["ps", "-a", "--filter", f"volume={name}", "--format", "{{.Names}}"]
        )
        if result.returncode != 0:
            return ["<unavailable>"]
        return [line for line in result.stdout.splitlines() if line]

    def _volume_mountpoint(self, name: str) -> str:
        result = self._command(
            ["volume", "inspect", "--format", "{{.Mountpoint}}", name]
        )
        if result.returncode != 0:
            logger.warning(
                "podman volume size inventory could not inspect mountpoint: "
                "worker=%s volume=%s exit_code=%s",
                self.worker_id,
                name,
                result.returncode,
            )
            return ""
        return result.stdout.strip()

    @staticmethod
    def _estimate_size_bytes(mountpoint: str) -> int | None:
        if not mountpoint or not os.path.isdir(mountpoint):
            return None
        try:
            result = subprocess.run(
                ["du", "-sb", "--", mountpoint],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        try:
            return int(result.stdout.split()[0])
        except (IndexError, ValueError):
            return None

    def inventory_resources(
        self,
        job_state_resolver: Callable[[str], dict | None] | None = None,
        *,
        include_sizes: bool = False,
    ) -> list[dict]:
        state_cache: dict[str, dict | None] = {}
        items: list[dict] = []

        for raw in self._list_json(["pod", "ps", "--format", "json"]):
            labels = raw.get("Labels") or {}
            classification, reason = self._classification_for_labels(
                labels, anonymous=False, job_state_resolver=job_state_resolver,
                state_cache=state_cache,
            )
            items.append({
                "kind": "pod",
                "name": raw.get("Name") or raw.get("Id") or "",
                "labels": labels,
                "worker": labels.get(WORKER_LABEL),
                "job": labels.get(JOB_LABEL),
                "resource_type": labels.get(RESOURCE_LABEL),
                "created_at": raw.get("Created"),
                "referenced_by": [
                    item.get("Names") for item in (raw.get("Containers") or [])
                    if item.get("Names")
                ],
                "estimated_size_bytes": None,
                "classification": classification,
                "reason": reason,
            })

        for raw in self._list_json(["ps", "-a", "--format", "json"]):
            labels = raw.get("Labels") or {}
            classification, reason = self._classification_for_labels(
                labels, anonymous=False, job_state_resolver=job_state_resolver,
                state_cache=state_cache,
            )
            names = raw.get("Names") or []
            name = names if isinstance(names, str) else (names[0] if names else raw.get("Id") or "")
            items.append({
                "kind": "container",
                "name": name,
                "labels": labels,
                "worker": labels.get(WORKER_LABEL),
                "job": labels.get(JOB_LABEL),
                "resource_type": labels.get(RESOURCE_LABEL),
                "created_at": raw.get("CreatedAt") or raw.get("Created"),
                "referenced_by": [raw.get("Pod")] if raw.get("Pod") else [],
                "estimated_size_bytes": None,
                "classification": classification,
                "reason": reason,
            })

        for raw in self._list_json(["volume", "ls", "--format", "json"]):
            labels = raw.get("Labels") or {}
            anonymous = bool(raw.get("Anonymous"))
            classification, reason = self._classification_for_labels(
                labels, anonymous=anonymous, job_state_resolver=job_state_resolver,
                state_cache=state_cache,
            )
            try:
                mount_count = int(raw.get("MountCount") or 0)
            except (TypeError, ValueError):
                mount_count = 0
            name = raw.get("Name") or ""
            mountpoint = raw.get("Mountpoint") or (
                self._volume_mountpoint(name) if include_sizes else ""
            )
            estimated_size = (
                self._estimate_size_bytes(mountpoint) if include_sizes else None
            )
            items.append({
                "kind": "volume",
                "name": name,
                "labels": labels,
                "worker": labels.get(WORKER_LABEL),
                "job": labels.get(JOB_LABEL),
                "resource_type": labels.get(RESOURCE_LABEL),
                "created_at": raw.get("CreatedAt"),
                "referenced_by": self._volume_references(name, mount_count),
                "estimated_size": estimated_size,
                "estimated_size_bytes": estimated_size,
                "anonymous": anonymous,
                "classification": classification,
                "reason": reason,
            })
        return items

    def reconcile_stale(self, job_state_resolver: Callable[[str], dict | None]) -> dict:
        inventory = self.inventory_resources(job_state_resolver)
        counts = Counter(item["classification"] for item in inventory)
        logger.info(
            "podman reconcile inventory worker=%s active=%d safe_stale=%d "
            "unknown=%d shared=%d legacy_unowned=%d",
            self.worker_id,
            counts.get("ACTIVE", 0),
            counts.get("SAFE_STALE", 0),
            counts.get("UNKNOWN", 0),
            counts.get("SHARED", 0),
            counts.get("LEGACY_UNOWNED", 0),
        )
        order = {"pod": 0, "container": 1, "volume": 2}
        failures: list[str] = []
        removed = 0
        for item in sorted(inventory, key=lambda entry: order.get(entry["kind"], 99)):
            if item["classification"] != "SAFE_STALE" or item.get("worker") != self.worker_id:
                continue
            try:
                if self.remove_verified(
                    item["kind"],
                    item["name"],
                    item["job"],
                    item["resource_type"],
                    require_stale=True,
                    job_state_resolver=job_state_resolver,
                ):
                    removed += 1
            except Exception as exc:
                logger.error(
                    "podman stale cleanup failed worker=%s job=%s resource_type=%s "
                    "resource_name=%s error=%s",
                    self.worker_id,
                    item.get("job"),
                    item.get("resource_type"),
                    item.get("name"),
                    type(exc).__name__,
                )
                failures.append(f"{item.get('kind')}:{item.get('name')}:{type(exc).__name__}")
        if failures:
            raise PodmanCleanupError(
                "podman startup reconciliation incomplete: " + ", ".join(failures)
            )
        return {
            "worker_id": self.worker_id,
            "removed": removed,
            "classifications": dict(counts),
        }
