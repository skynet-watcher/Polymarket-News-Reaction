"""
Silver Bulletin polling average adapter — STUB.

FUTURE STATE: Fetch the latest presidential approval rating from the Silver
Bulletin model average (fivethirtyeight successor; silverbb.substack.com or
equivalent public endpoint).

Config keys:
    president (str): e.g. "trump". Used to filter the correct series.
"""

from __future__ import annotations

from typing import Any, Optional

from app.resolution.base import ResolutionAdapter, ResolutionSignal


class SilverBulletinAdapter(ResolutionAdapter):
    name = "silver_bulletin"

    def __init__(self, **config: Any) -> None:
        super().__init__(**config)

    async def fetch(self) -> Optional[ResolutionSignal]:
        raise NotImplementedError("SilverBulletinAdapter is not yet implemented")
