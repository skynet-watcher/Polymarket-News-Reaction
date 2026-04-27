from __future__ import annotations

import asyncio
from typing import Any, Optional

import httpx

from app.settings import settings


def polymarket_async_client(**kwargs: Any) -> httpx.AsyncClient:
    """
    Shared AsyncClient for Polymarket Gamma/CLOB and other ingestion.

    - trust_env=True respects HTTP(S)_PROXY when enabled (default).
    - http_disable_env_proxy=True forces trust_env=False to bypass broken system proxies.
    """
    trust = bool(settings.http_trust_env) and not bool(settings.http_disable_env_proxy)
    base_headers = {"User-Agent": settings.http_user_agent}
    extra_headers = kwargs.pop("headers", None)
    if extra_headers:
        base_headers.update(extra_headers)
    timeout = kwargs.pop("timeout", None) or httpx.Timeout(settings.http_timeout_seconds)
    return httpx.AsyncClient(
        trust_env=trust,
        timeout=timeout,
        headers=base_headers,
        **kwargs,
    )


async def get_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: Optional[dict[str, Any]] = None,
    max_retries: Optional[int] = None,
) -> httpx.Response:
    """GET with simple exponential backoff on 5xx and transient errors."""
    retries = max_retries if max_retries is not None else settings.http_max_retries
    delay = max(0.1, float(settings.http_retry_backoff_seconds))
    last_exc: Optional[BaseException] = None
    for attempt in range(retries + 1):
        try:
            r = await client.get(url, params=params)
            if r.status_code >= 500 and attempt < retries:
                await asyncio.sleep(delay * (2**attempt))
                continue
            return r
        except httpx.RequestError as e:
            last_exc = e
            if attempt < retries:
                await asyncio.sleep(delay * (2**attempt))
                continue
            raise
    assert last_exc is not None
    raise last_exc


async def fetch_clob_orderbook(client: httpx.AsyncClient, token_id: str) -> Optional[dict]:
    """Return raw CLOB book JSON for depth-based paper fills, or None."""
    for path in ("/book", "/orderbook"):
        try:
            url = f"{settings.polymarket_clob_base_url}{path}"
            r = await get_with_retry(client, url, params={"token_id": token_id})
            if r.status_code >= 400:
                continue
            data = r.json()
            if isinstance(data, dict):
                return data
        except Exception:
            continue
    return None
