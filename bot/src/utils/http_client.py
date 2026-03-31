"""Shared HTTP client with connection pooling and retry for the bot service.

Provides a singleton httpx.AsyncClient that retries once on connection
errors or timeouts (e.g. when wa-service restarts during deploy).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None

RETRY_DELAY = 2  # seconds before retry


def get_client() -> httpx.AsyncClient:
    """Return shared AsyncClient with connection pooling."""
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(30, connect=10),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=5),
        )
    return _client


async def request(
    method: str,
    url: str,
    *,
    timeout: float | None = None,
    **kwargs: Any,
) -> httpx.Response:
    """Make an HTTP request with 1 retry on ConnectError/TimeoutException."""
    client = get_client()
    req_kwargs = dict(**kwargs)
    if timeout is not None:
        req_kwargs["timeout"] = timeout

    try:
        return await client.request(method, url, **req_kwargs)
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        logger.warning("HTTP %s %s failed (%s), retrying in %ds", method, url, exc, RETRY_DELAY)
        await asyncio.sleep(RETRY_DELAY)
        return await client.request(method, url, **req_kwargs)


async def get(url: str, **kwargs: Any) -> httpx.Response:
    return await request("GET", url, **kwargs)


async def post(url: str, **kwargs: Any) -> httpx.Response:
    return await request("POST", url, **kwargs)
