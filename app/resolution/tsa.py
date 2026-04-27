"""
TSA throughput adapter — STUB.

FUTURE STATE: Fetch daily TSA checkpoint traveler numbers from the TSA public
data feed (tsa.gov/travel/passenger-volumes).

Config keys:
    lookback_days (int): How many days of history to request. Default 1.
"""

from __future__ import annotations

from typing import Any, Optional

from app.resolution.base import ResolutionAdapter, ResolutionSignal


class TSAAdapter(ResolutionAdapter):
    name = "tsa"

    def __init__(self, **config: Any) -> None:
        super().__init__(**config)

    async def fetch(self) -> Optional[ResolutionSignal]:
        raise NotImplementedError("TSAAdapter is not yet implemented")
