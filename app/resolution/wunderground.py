"""
Weather Underground adapter — STUB.

FUTURE STATE: Fetch observed weather data (temperature, precipitation, etc.)
from the Weather Underground Personal Weather Station API.

Config keys:
    station_id (str): PWS station ID.
    api_key (str): WU API key.
    metric (str): "temp_f" | "precip_in" | etc.
"""

from __future__ import annotations

from typing import Any, Optional

from app.resolution.base import ResolutionAdapter, ResolutionSignal


class WundergroundAdapter(ResolutionAdapter):
    name = "wunderground"

    def __init__(self, **config: Any) -> None:
        super().__init__(**config)

    async def fetch(self) -> Optional[ResolutionSignal]:
        raise NotImplementedError("WundergroundAdapter is not yet implemented")
