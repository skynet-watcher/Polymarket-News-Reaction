from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


async def fetch_btc_usd_price() -> tuple[Optional[float], str]:
    """
    Return (BTC/USD price, provider).

    Binance can be unreachable from serverless egress, so smoke tests try a
    short provider chain before giving up.
    """
    providers = (
        ("binance", _fetch_binance),
        ("coinbase", _fetch_coinbase),
        ("kraken", _fetch_kraken),
    )
    async with httpx.AsyncClient(timeout=8.0) as client:
        for name, fn in providers:
            try:
                price = await fn(client)
                if price is not None and price > 0:
                    return price, name
            except Exception:
                logger.warning("BTC price provider failed: %s", name, exc_info=True)
    return None, "none"


async def _fetch_binance(client: httpx.AsyncClient) -> Optional[float]:
    r = await client.get("https://api.binance.com/api/v3/ticker/bookTicker", params={"symbol": "BTCUSDT"})
    r.raise_for_status()
    d: dict[str, Any] = r.json()
    return (float(d["bidPrice"]) + float(d["askPrice"])) / 2.0


async def _fetch_coinbase(client: httpx.AsyncClient) -> Optional[float]:
    r = await client.get("https://api.coinbase.com/v2/prices/BTC-USD/spot")
    r.raise_for_status()
    d: dict[str, Any] = r.json()
    return float(d["data"]["amount"])


async def _fetch_kraken(client: httpx.AsyncClient) -> Optional[float]:
    r = await client.get("https://api.kraken.com/0/public/Ticker", params={"pair": "XBTUSD"})
    r.raise_for_status()
    d: dict[str, Any] = r.json()
    result = d.get("result") or {}
    if not isinstance(result, dict) or not result:
        return None
    first = next(iter(result.values()))
    bid = float(first["b"][0])
    ask = float(first["a"][0])
    return (bid + ask) / 2.0
