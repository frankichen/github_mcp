import asyncio
from pathlib import Path

import httpx
import socksio


def test_httpx_socks_runtime_dependency_is_locked():
    root = Path(__file__).parents[1]
    requirements = (root / "requirements.txt").read_text(encoding="utf-8")
    constraints = (root / "constraints.txt").read_text(encoding="utf-8")

    assert "httpx[socks]==0.28.1" in requirements
    assert "socksio==1.0.0" in constraints
    assert socksio is not None


def test_httpx_async_client_accepts_socks_proxy_from_environment(monkeypatch):
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:9")

    async def probe():
        async with httpx.AsyncClient(timeout=0.1) as client:
            assert client is not None

    asyncio.run(probe())
