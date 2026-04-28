from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobs.compute_lag import _nearest_snapshot_after_before, _nearest_snapshot_before
from app.models import Market, NewsArticle, NewsSignal, SignalMetrics
from app.util import new_id, now_utc


WINDOWS_MIN = [1, 5, 15, 60, 240]


async def compute_for_signal(session: AsyncSession, *, signal: NewsSignal, article: NewsArticle, market: Market) -> int:
    """
    Store SignalMetrics rows for one ACT signal at fixed post-signal windows.
    Idempotent: deletes existing metrics for this signal first.
    """
    if signal.action != "ACT":
        return 0

    await session.execute(delete(SignalMetrics).where(SignalMetrics.signal_id == signal.id))

    signal_time = article.published_at
    if signal_time.tzinfo is None:
        signal_time = signal_time.replace(tzinfo=dt.timezone.utc)

    baseline = await _nearest_snapshot_before(session, market_id=market.id, at=signal_time)
    mid0 = float(baseline.mid_yes) if baseline is not None and baseline.mid_yes is not None else None
    spread0 = baseline.spread if baseline is not None else None
    if spread0 is None and baseline is not None and baseline.best_bid_yes is not None and baseline.best_ask_yes is not None:
        spread0 = float(baseline.best_ask_yes) - float(baseline.best_bid_yes)

    written = 0
    for w in WINDOWS_MIN:
        at = signal_time + dt.timedelta(minutes=w)
        snap = await _nearest_snapshot_after_before(
            session,
            market_id=market.id,
            at_or_before=at,
            strictly_after=signal_time,
        )
        mid = float(snap.mid_yes) if snap is not None and snap.mid_yes is not None else None
        spr: Optional[float] = None
        if snap is not None and snap.best_bid_yes is not None and snap.best_ask_yes is not None:
            spr = float(snap.best_ask_yes) - float(snap.best_bid_yes)
        elif snap is not None and snap.spread is not None:
            spr = float(snap.spread)

        dmid = (mid - mid0) if (mid is not None and mid0 is not None) else None
        dspr = (spr - spread0) if (spr is not None and spread0 is not None) else None

        session.add(
            SignalMetrics(
                id=new_id("smet"),
                signal_id=signal.id,
                market_id=market.id,
                window_minutes=w,
                signal_time=signal_time,
                snapshot_timestamp=snap.timestamp if snap is not None else None,
                mid_yes=mid,
                best_bid_yes=snap.best_bid_yes if snap is not None else None,
                best_ask_yes=snap.best_ask_yes if snap is not None else None,
                spread=spr,
                mid_at_signal=mid0,
                spread_at_signal=spread0,
                delta_mid_from_signal=dmid,
                delta_spread_from_signal=dspr,
                created_at=now_utc(),
            )
        )
        written += 1

    await session.flush()
    return written


async def run_backfill(session: AsyncSession, *, limit: int = 100) -> dict[str, Any]:
    """Compute metrics for recent ACT signals that lack rows (or recompute last N)."""
    q = (
        select(NewsSignal, NewsArticle, Market)
        .join(NewsArticle, NewsArticle.id == NewsSignal.article_id)
        .join(Market, Market.id == NewsSignal.market_id)
        .where(NewsSignal.action == "ACT")
        .where(Market.is_fixture.is_not(True))
        .order_by(NewsSignal.created_at.desc())
        .limit(limit)
    )
    rows = (await session.execute(q)).all()
    total = 0
    for sig, article, market in rows:
        n = await compute_for_signal(session, signal=sig, article=article, market=market)
        total += n
    await session.commit()
    return {"signals": len(rows), "metric_rows_written": total, "windows": len(WINDOWS_MIN)}
