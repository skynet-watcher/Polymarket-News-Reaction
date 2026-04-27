"""
ResolutionAdapter abstract base class and ResolutionSignal dataclass.

Each concrete adapter fetches a structured data point from an authoritative
external source and returns a ResolutionSignal. The poll_resolution_data job
(not yet hooked up) will iterate over enabled adapters and persist results.
"""

from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ResolutionSignal:
    """
    A single resolution data point returned by a ResolutionAdapter.

    `value` is always in the natural unit of the source (price in USD,
    temperature in °F, passenger count as integer, etc.).  The consumer is
    responsible for comparing it to the market's resolution criterion.
    """

    adapter_name: str
    market_type: str
    fetched_at: dt.datetime
    value: float
    unit: str  # e.g. "USD", "degF", "passengers", "approval_pct"
    source_url: Optional[str] = None
    raw: Optional[dict[str, Any]] = field(default=None)  # original API payload for audit
    notes: Optional[str] = None


class ResolutionAdapter(ABC):
    """
    Abstract base for structured-data resolution sources.

    Subclass and implement `fetch()`. The adapter is stateless; all
    configuration is passed at construction time via `**config`.
    """

    #: Unique name used as the key in ADAPTER_REGISTRY and ResolutionSourceConfig.adapter_name.
    name: str

    def __init__(self, **config: Any) -> None:
        self.config = config

    @abstractmethod
    async def fetch(self) -> Optional[ResolutionSignal]:
        """
        Fetch the current resolution data point.

        Returns None if the source is unavailable or the data is stale.
        Raises on unrecoverable errors (caller logs and skips).
        """
        ...

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={self.name!r})"
