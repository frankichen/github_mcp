"""OpenAI runtime-file adapter backed by the generic immutable Artifact Plane."""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Any, Callable
from urllib.parse import urljoin

import httpx

from app import artifact_store


ALLOWED_HOST_SUFFIXES = ("oaiusercontent.com",)
MAX_REDIRECTS = 5


class RuntimeFileIngressError(Exception):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def _validate_destination(
    url: httpx.URL,
    *,
    label: str,
    resolver: Callable[..., Any],
) -> None:
    host = str(url.host or "").strip().rstrip(".")
    lowered = host.lower()
    if url.scheme != "https" or not host or url.port not in (None, 443):
        raise RuntimeFileIngressError(
            "INVALID_REFERENCE", f"{label} download_url must use HTTPS on port 443"
        )
    if not any(
        lowered == suffix or lowered.endswith("." + suffix)
        for suffix in ALLOWED_HOST_SUFFIXES
    ):
        raise RuntimeFileIngressError(
            "INVALID_REFERENCE",
            f"{label} download_url host is not allowlisted for OpenAI file delivery",
            {"host": host},
        )

    addresses: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    try:
        addresses.add(ipaddress.ip_address(host.split("%", 1)[0]))
    except ValueError:
        try:
            resolved = resolver(host, 443, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise RuntimeFileIngressError(
                "INVALID_REFERENCE",
                f"{label} download_url host could not be resolved",
                {"host": host, "cause_type": type(exc).__name__},
            ) from exc
        for item in resolved:
            try:
                addresses.add(ipaddress.ip_address(str(item[4][0]).split("%", 1)[0]))
            except (IndexError, ValueError, TypeError):
                continue
    if not addresses or any(not address.is_global for address in addresses):
        raise RuntimeFileIngressError(
            "INVALID_REFERENCE",
            f"{label} download_url must resolve only to public network addresses",
            {"host": host},
        )


def _runtime_file_descriptor(
    file_reference: dict[str, Any], label: str
) -> tuple[str, str, str, str]:
    if not isinstance(file_reference, dict):
        raise RuntimeFileIngressError(
            "INVALID_REFERENCE", f"{label} must be a runtime file reference"
        )
    download_url = file_reference.get("download_url")
    file_id = file_reference.get("file_id")
    if (
        not isinstance(download_url, str)
        or not download_url
        or not isinstance(file_id, str)
        or not file_id
    ):
        raise RuntimeFileIngressError(
            "INVALID_REFERENCE", f"{label} requires download_url and file_id"
        )
    file_name = file_reference.get("file_name")
    mime_type = file_reference.get("mime_type")
    return (
        download_url,
        file_id,
        file_name if isinstance(file_name, str) else "",
        mime_type if isinstance(mime_type, str) else "",
    )


async def ingest_runtime_artifact(
    file_reference: dict[str, Any],
    *,
    kind: str,
    max_bytes: int,
    label: str,
    repository_scope: str = "",
    principal_scope: str = "",
    session_scope: str = "",
    ttl_seconds: int = artifact_store.DEFAULT_ARTIFACT_TTL_SECONDS,
    resolver: Callable[..., Any] | None = None,
    client_factory: Callable[..., Any] | None = None,
) -> artifact_store.ArtifactRef:
    """Resolve one host-issued file capability into an immutable ArtifactRef.

    This adapter validates transport and stages exact bytes; it intentionally
    knows nothing about JSON, Git, Workspaces, or Development Sessions.
    """
    download_url, file_id, file_name, mime_type = _runtime_file_descriptor(
        file_reference, label
    )
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    try:
        current_url = httpx.URL(download_url)
    except (TypeError, ValueError) as exc:
        raise RuntimeFileIngressError(
            "INVALID_REFERENCE", f"{label} download_url is invalid"
        ) from exc

    resolve = resolver or socket.getaddrinfo
    make_client = client_factory or httpx.AsyncClient
    writer: artifact_store.ArtifactWriter | None = None
    try:
        timeout = httpx.Timeout(30.0, connect=10.0)
        async with make_client(follow_redirects=False, timeout=timeout) as client:
            for redirect_count in range(MAX_REDIRECTS + 1):
                await asyncio.to_thread(
                    _validate_destination,
                    current_url,
                    label=label,
                    resolver=resolve,
                )
                async with client.stream(
                    "GET", current_url, headers={"Accept-Encoding": "identity"}
                ) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location or redirect_count >= MAX_REDIRECTS:
                            raise RuntimeFileIngressError(
                                "INVALID_REFERENCE",
                                f"{label} redirect chain is invalid or too long",
                            )
                        try:
                            current_url = httpx.URL(
                                urljoin(str(response.url), location)
                            )
                        except (TypeError, ValueError) as exc:
                            raise RuntimeFileIngressError(
                                "INVALID_REFERENCE", f"{label} redirect URL is invalid"
                            ) from exc
                        continue
                    response.raise_for_status()
                    content_encoding = (
                        response.headers.get("content-encoding", "").strip().lower()
                    )
                    if content_encoding not in {"", "identity"}:
                        raise RuntimeFileIngressError(
                            "INVALID_REFERENCE",
                            f"{label} response must not use content encoding",
                            {"content_encoding": content_encoding},
                        )
                    declared_bytes: int | None = None
                    declared_size = response.headers.get("content-length")
                    if declared_size:
                        try:
                            declared_bytes = int(declared_size)
                        except ValueError as exc:
                            raise RuntimeFileIngressError(
                                "INVALID_REFERENCE",
                                f"{label} Content-Length is invalid",
                            ) from exc
                        if declared_bytes < 0:
                            raise RuntimeFileIngressError(
                                "INVALID_REFERENCE",
                                f"{label} Content-Length is invalid",
                            )
                        if declared_bytes > max_bytes:
                            raise RuntimeFileIngressError(
                                "TOO_LARGE",
                                f"{label} exceeds the ingress limit",
                                {
                                    "max_bytes": max_bytes,
                                    "declared_bytes": declared_bytes,
                                },
                            )
                    writer = artifact_store.ArtifactWriter(
                        kind=kind,
                        max_bytes=max_bytes,
                        file_name=file_name,
                        mime_type=mime_type,
                        source_transport="openai_file_param",
                        repository_scope=repository_scope,
                        principal_scope=principal_scope,
                        session_scope=session_scope,
                        ttl_seconds=ttl_seconds,
                    )
                    async for chunk in response.aiter_raw():
                        writer.write(chunk)
                        if declared_bytes is not None and writer.size_bytes > declared_bytes:
                            raise RuntimeFileIngressError(
                                "TRANSPORT_SIZE_MISMATCH",
                                f"{label} stream exceeded its declared byte count",
                                {
                                    "declared_bytes": declared_bytes,
                                    "received_bytes": writer.size_bytes,
                                },
                            )
                    if declared_bytes is not None and writer.size_bytes != declared_bytes:
                        raise RuntimeFileIngressError(
                            "TRANSPORT_SIZE_MISMATCH",
                            f"{label} stream ended before its declared byte count",
                            {
                                "declared_bytes": declared_bytes,
                                "received_bytes": writer.size_bytes,
                            },
                        )
                    return writer.commit()
    except RuntimeFileIngressError:
        raise
    except artifact_store.ArtifactStoreError as exc:
        code = "TOO_LARGE" if exc.code == "ARTIFACT_TOO_LARGE" else "STAGING_FAILED"
        raise RuntimeFileIngressError(code, exc.message, exc.details) from exc
    except httpx.HTTPError as exc:
        raise RuntimeFileIngressError(
            "DOWNLOAD_FAILED",
            f"{label} could not be downloaded",
            {"file_id": file_id, "cause_type": type(exc).__name__},
        ) from exc
    finally:
        if writer is not None:
            writer.abort()
    raise RuntimeFileIngressError(
        "INVALID_REFERENCE", f"{label} redirect limit exceeded"
    )
