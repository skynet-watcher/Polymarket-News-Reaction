"""
Sports results adapter — STUB.

FUTURE STATE: Fetch game scores / outcomes from a sports data API
(e.g. ESPN undocumented API, The Odds API, or MySportsFeeds).

Config keys:
    provider (str): "espn" | "odds_api" | etc.
    sport (str): "nfl" | "nba" | "mlb" | etc.
    game_id (str): Provider-specific game identifier.
"""

from __future__ import annotations

from typing import Any, Optional

from app.resolution.base import ResolutionAdapter, ResolutionSignal


class SportsAdapter(ResolutionAdapter):
    name = "sports"

    def __init__(self, **config: Any) -> None:
        super().__init__(**config)

    async def fetch(self) -> Optional[ResolutionSignal]:
        raise NotImplementedError("SportsAdapter is not yet implemented")
