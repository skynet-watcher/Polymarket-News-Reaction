"""
Box office results adapter — STUB.

FUTURE STATE: Fetch domestic / worldwide box office gross from The Numbers
(the-numbers.com) or Box Office Mojo (boxofficemojo.com).

Config keys:
    film_id (str): Provider-specific film identifier.
    metric (str): "domestic_gross" | "worldwide_gross" | "opening_weekend".
"""

from __future__ import annotations

from typing import Any, Optional

from app.resolution.base import ResolutionAdapter, ResolutionSignal


class BoxOfficeAdapter(ResolutionAdapter):
    name = "box_office"

    def __init__(self, **config: Any) -> None:
        super().__init__(**config)

    async def fetch(self) -> Optional[ResolutionSignal]:
        raise NotImplementedError("BoxOfficeAdapter is not yet implemented")
