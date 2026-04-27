"""Adaptive intervals when markets have open paper positions and resolution is near."""

from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Market, PaperTrade
from app.settings import settings
from app.util import now_utc, to_utc_aware


async def invested_market_ids(session: AsyncSession) -> set[str]:
    rows = (await session.execute(select(PaperTrade.market_id).where(PaperTrade.status == "OPEN").distinct())).all()
    return {str(r[0]) for r in rows if r[0]}


def _min_hours_to_resolution_for_markets(markets: list[Market], *, now: dt.datetime) -> Optional[float]:
    best: Optional[float] = None
    for m in markets:
        if m.end_date is None:
            continue
        end = to_utc_aware(m.end_date)
        if end <= now:
            continue
        h = (end - now).total_seconds() / 3600.0
        if best is None or h < best:
            best = h
    return best


async def invested_hours_to_resolution(session: AsyncSession) -> tuple[bool, Optional[float]]:
    """
    Returns (has_open_positions, min_hours_until_end_date among those markets).

    If no end_date on invested markets, has_open is True but hours is None (use calm cadence).
    """
    ids = await invested_market_ids(session)
    if not ids:
        return False, None
    markets = (await session.execute(select(Market).where(Market.id.in_(list(ids))))).scalars().all()
    h = _min_hours_to_resolution_for_markets(list(markets), now=now_utc())
    return True, h


def _urgent_factor(hours: Optional[float]) -> float:
    """0 = calm, 1 = most urgent (resolution very soon)."""
    if hours is None:
        return 0.0
    if hours >= 48:
        return 0.15
    if hours >= 24:
        return 0.35
    if hours >= 12:
        return 0.55
    if hours >= 6:
        return 0.7
    if hours >= 2:
        return 0.85
    return 1.0


def next_poll_news_sleep_seconds(*, base_seconds: int, has_open: bool, hours: Optional[float]) -> int:
    if not settings.realtime_adaptive_enabled or not has_open:
        return max(60, base_seconds)
    u = _urgent_factor(hours)
    # Scale base down toward floor as urgency increases.
    floor = max(settings.realtime_poll_min_seconds, 30)
    scaled = int(base_seconds * (1.0 - 0.65 * u))
    return max(floor, scaled)


def next_process_candidates_sleep_seconds(*, base_seconds: int, has_open: bool, hours: Optional[float]) -> int:
    if not settings.realtime_adaptive_enabled or not has_open:
        return max(60, base_seconds)
    u = _urgent_factor(hours)
    floor = max(settings.realtime_process_min_seconds, 15)
    scaled = int(base_seconds * (1.0 - 0.55 * u))
    return max(floor, scaled)


def next_snapshot_tick_sleep_seconds(*, base_seconds: int, has_open: bool, hours: Optional[float]) -> int:
    """Sleep between snapshot loop iterations (full or partial refresh)."""
    if not settings.realtime_adaptive_enabled or not has_open:
        return max(5, base_seconds)
    u = _urgent_factor(hours)
    floor = max(settings.realtime_snapshot_min_seconds, 10)
    scaled = int(base_seconds * (1.0 - 0.75 * u))
    return max(floor, scaled)
