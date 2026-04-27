from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Market, NewsArticle, NewsSignal, PaperTrade, PriceSnapshot
from app.settings import settings
from app.util import now_utc


async def run(session: AsyncSession) -> dict[str, Any]:
    """
    Settles OPEN paper trades.

    - If the market has a known binary resolution, settle immediately at 0/1 in outcome-space.
    - Otherwise, settle at T+24h from article publish time using the latest PriceSnapshot at/before
      that time, only if the snapshot is strictly after the article time and within
      ``settle_t24_snapshot_max_skew_seconds`` of the nominal settle time (avoids stale pre-signal prices).
    """
    cutoff = now_utc() - dt.timedelta(hours=24)

    result = await session.execute(
        select(PaperTrade)
        .join(NewsSignal, NewsSignal.id == PaperTrade.signal_id)
        .join(NewsArticle, NewsArticle.id == NewsSignal.article_id)
        .join(Market, Market.id == PaperTrade.market_id)
        .where(
            and_(
                PaperTrade.status == "OPEN",
                or_(Market.winning_outcome.is_not(None), NewsArticle.published_at <= cutoff),
            )
        )
    )
    trades = result.scalars().all()

    settled = 0
    skipped = 0

    for trade in trades:
        signal = await session.get(NewsSignal, trade.signal_id)
        if signal is None:
            skipped += 1
            continue
        article = await session.get(NewsArticle, signal.article_id)
        market = await session.get(Market, trade.market_id)
        if article is None or market is None:
            skipped += 1
            continue

        signal_time = article.published_at
        if signal_time.tzinfo is None:
            signal_time = signal_time.replace(tzinfo=dt.timezone.utc)
        settle_time = signal_time + dt.timedelta(hours=24)

        if market.winning_outcome is not None:
            if trade.side == "BUY_YES":
                resolution_price = 1.0 if market.winning_outcome == "YES" else 0.0
            else:  # BUY_NO
                resolution_price = 1.0 if market.winning_outcome == "NO" else 0.0
            pnl = (resolution_price - trade.fill_price) * trade.simulated_size
            trade.pnl_final = round(pnl, 4)
            trade.pnl_current = round(pnl, 4)
            trade.status = "SETTLED_RESOLVED"
            settled += 1
            continue

        snap = (
            await session.execute(
                select(PriceSnapshot)
                .where(PriceSnapshot.market_id == trade.market_id)
                .where(PriceSnapshot.timestamp <= settle_time)
                .order_by(PriceSnapshot.timestamp.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        if snap is None or snap.mid_yes is None:
            skipped += 1
            continue

        st = snap.timestamp
        if st.tzinfo is None:
            st = st.replace(tzinfo=dt.timezone.utc)
        if st <= signal_time:
            skipped += 1
            continue
        skew = (settle_time - st).total_seconds()
        if skew < 0 or skew > settings.settle_t24_snapshot_max_skew_seconds:
            skipped += 1
            continue

        settle_mid = float(snap.mid_yes)
        if trade.side == "BUY_YES":
            pnl = (settle_mid - trade.fill_price) * trade.simulated_size
        else:  # BUY_NO
            settle_no = 1.0 - settle_mid
            pnl = (settle_no - trade.fill_price) * trade.simulated_size

        trade.pnl_final = round(pnl, 4)
        trade.pnl_current = round(pnl, 4)
        trade.status = "SETTLED_T24H"
        settled += 1

    await session.commit()
    return {"settled": settled, "skipped": skipped, "total_open": len(trades)}
