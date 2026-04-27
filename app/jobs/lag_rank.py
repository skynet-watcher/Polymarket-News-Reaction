from __future__ import annotations

import math
import statistics
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import LagMeasurement, LagThresholdCrossing, Market, MarketLagScore
from app.settings import settings
from app.util import now_utc


def _z_score(value: float, population: list[float]) -> float:
    if not population:
        return 0.0
    mu = statistics.mean(population)
    sd = statistics.pstdev(population) if len(population) > 1 else 0.0
    if sd == 0:
        return 0.0
    return (value - mu) / sd


async def run(session: AsyncSession) -> dict[str, Any]:
    """
    Recompute MarketLagScore rows from LagMeasurement + 10PT crossings + closure lag.
    Higher combined_score => slower-to-update (more interesting for research).
    """
    market_ids = list(
        (await session.execute(select(LagMeasurement.market_id).distinct())).scalars().all()
    )
    if not market_ids:
        return {"markets_scored": 0}

    rows: list[tuple[str, Optional[float], Optional[float], int, Optional[float], Optional[float], Optional[str]]] = []
    price_vals: list[float] = []
    res_vals: list[float] = []

    for mid in market_ids:
        mkt = await session.get(Market, mid)
        if mkt is None:
            continue
        if (mkt.liquidity or 0.0) < settings.lag_rank_min_liquidity:
            continue

        lm_ids = (
            await session.execute(select(LagMeasurement.id).where(LagMeasurement.market_id == mid))
        ).scalars().all()
        if not lm_ids:
            continue

        lag10_list = (
            await session.execute(
                select(LagThresholdCrossing.lag_seconds)
                .where(
                    LagThresholdCrossing.lag_measurement_id.in_(lm_ids),
                    LagThresholdCrossing.threshold_label == "10PT",
                    LagThresholdCrossing.lag_seconds.is_not(None),
                    LagThresholdCrossing.crossed == True,  # noqa: E712
                )
            )
        ).scalars().all()

        median_price: Optional[float] = None
        if lag10_list:
            median_price = float(statistics.median([float(x) for x in lag10_list]))

        closure_list = (
            await session.execute(
                select(LagMeasurement.closure_lag_seconds).where(
                    LagMeasurement.market_id == mid,
                    LagMeasurement.closure_lag_seconds.is_not(None),
                )
            )
        ).scalars().all()
        median_res: Optional[float] = None
        if closure_list:
            median_res = float(statistics.median([float(x) for x in closure_list]))

        n_cross = (
            await session.execute(
                select(func.count())
                .select_from(LagMeasurement)
                .where(
                    LagMeasurement.market_id == mid,
                    LagMeasurement.price_lag_status == "CROSSED",
                )
            )
        ).scalar_one()

        if median_price is not None:
            price_vals.append(math.log1p(max(0.0, median_price)))
        if median_res is not None:
            res_vals.append(math.log1p(max(0.0, median_res)))

        rows.append(
            (
                mid,
                median_price,
                median_res,
                n_cross,
                mkt.volume_24h,
                mkt.liquidity,
                mkt.category,
            )
        )

    for mid, mp, mr, n_cross, v24, liq, cat in rows:
        zp = (
            _z_score(math.log1p(max(0.0, mp)), price_vals)
            if mp is not None and price_vals
            else 0.0
        )
        zr = (
            _z_score(math.log1p(max(0.0, mr)), res_vals)
            if mr is not None and res_vals
            else 0.0
        )
        combined = settings.lag_rank_weight_price * zp + settings.lag_rank_weight_resolution * zr

        existing = await session.get(MarketLagScore, mid)
        if existing is None:
            session.add(
                MarketLagScore(
                    market_id=mid,
                    median_price_lag_seconds=mp,
                    median_resolution_lag_seconds=mr,
                    combined_score=combined,
                    signal_count=n_cross,
                    volume_24h=v24,
                    liquidity=liq,
                    category=cat,
                    updated_at=now_utc(),
                )
            )
        else:
            existing.median_price_lag_seconds = mp
            existing.median_resolution_lag_seconds = mr
            existing.combined_score = combined
            existing.signal_count = n_cross
            existing.volume_24h = v24
            existing.liquidity = liq
            existing.category = cat
            existing.updated_at = now_utc()

    await session.commit()
    return {"markets_scored": len(rows)}
