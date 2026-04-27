"""
Adapter registry.

Maps adapter_name strings (as stored in ResolutionSourceConfig.adapter_name)
to their concrete ResolutionAdapter subclasses.

To add a new adapter: implement it in its own module, import here, add to
ADAPTER_REGISTRY. No other file needs to change.
"""

from __future__ import annotations

from typing import Any, Type

from app.resolution.base import ResolutionAdapter
from app.resolution.billboard import BillboardAdapter
from app.resolution.binance import BinanceAdapter
from app.resolution.box_office import BoxOfficeAdapter
from app.resolution.silver_bulletin import SilverBulletinAdapter
from app.resolution.sports import SportsAdapter
from app.resolution.tsa import TSAAdapter
from app.resolution.truth_social import TruthSocialAdapter
from app.resolution.wunderground import WundergroundAdapter

ADAPTER_REGISTRY: dict[str, Type[ResolutionAdapter]] = {
    BinanceAdapter.name: BinanceAdapter,
    WundergroundAdapter.name: WundergroundAdapter,
    TSAAdapter.name: TSAAdapter,
    SilverBulletinAdapter.name: SilverBulletinAdapter,
    SportsAdapter.name: SportsAdapter,
    BoxOfficeAdapter.name: BoxOfficeAdapter,
    BillboardAdapter.name: BillboardAdapter,
    TruthSocialAdapter.name: TruthSocialAdapter,
}


def build_adapter(adapter_name: str, **config: Any) -> ResolutionAdapter:
    """Instantiate an adapter by name. Raises KeyError for unknown names."""
    cls = ADAPTER_REGISTRY[adapter_name]
    return cls(**config)
