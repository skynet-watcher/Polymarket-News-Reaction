"""
Binance spot price adapter.

Fetches the current spot mid-price for a symbol from the Binance public REST
API (no API key required). Used for CRYPTO_HOURLY market types.

Config keys (passed via ResolutionSourceConfig.adapter_config_json):
    symbol (str): Binance symbol, e.g. "BTCUSDT". Required.
    base_url (str): Override API base. Default: https://api.binance.com
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Optional

import httpx

from app.resolution.base import ResolutionAdapter, ResolutionSignal
from app.settings import settings

_DEFAULT_BASE = "https://api.binance.com"


class BinanceAdapter(ResolutionAdapter):
    name = "binance"

    def __init__(self, **config: Any) -> None:
        super().__init__(**config)
        self.symbol: str = config["symbol"]  # required
        self.base_url: str = config.get("base_url", _DEFAULT_BASE).rstrip("/")

    async def fetch(self) -> Optional[ResolutionSignal]:
        url = f"{self.base_url}/api/v3/ticker/bookTicker"
        params = {"symbol": self.symbol}

        async with httpx.AsyncClient(
            timeout=settings.http_timeout_seconds,
            trust_env=settings.http_trust_env,
            headers={"User-Agent": settings.http_user_agent},
        ) as client:
            resp = client.build_request("GET", url, params=params)
            response = await client.send(resp)
            response.raise_for_status()
            payload: dict[str, Any] = response.json()

        bid = float(payload["bidPrice"])
        ask = float(payload["askPrice"])
        mid = (bid + ask) / 2.0

        return ResolutionSignal(
            adapter_name=self.name,
            market_type="CRYPTO_HOURLY",
            fetched_at=dt.datetime.now(dt.timezone.utc),
            value=mid,
            unit="USD",
            source_url=url,
            raw=payload,
            notes=f"symbol={self.symbol} bid={bid} ask={ask}",
        )
