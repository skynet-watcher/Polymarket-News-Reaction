"""
Billboard chart adapter — STUB.

FUTURE STATE: Fetch current chart positions from Billboard's public chart data
(billboard.com/charts). Chart data typically updates weekly (Tuesdays).

Config keys:
    chart (str): e.g. "hot-100" | "billboard-200".
    artist_or_title (str): Filter string to match a specific entry.
"""

from __future__ import annotations

from typing import Any, Optional

from app.resolution.base import ResolutionAdapter, ResolutionSignal


class BillboardAdapter(ResolutionAdapter):
    name = "billboard"

    def __init__(self, **config: Any) -> None:
        super().__init__(**config)

    async def fetch(self) -> Optional[ResolutionSignal]:
        raise NotImplementedError("BillboardAdapter is not yet implemented")
