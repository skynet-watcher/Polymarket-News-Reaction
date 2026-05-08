from __future__ import annotations

import time
from typing import Any, Optional

from sqlalchemy import and_, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.paper import maybe_paper_trade
from app.http_client import fetch_clob_orderbook, polymarket_async_client
from app.jobs import short_term_watchlist
from app.models import AuditLog, Market, NewsArticle, NewsSignal, NewsSource, PaperTrade, PriceSnapshot
from app.util import new_id, now_utc


SIGNAL_SOURCE_TYPE = "WATCHLIST_OVERNIGHT_PROBE"


def _spread(market: Market, snapshot: Optional[PriceSnapshot]) -> Optional[float]:
    if snapshot and snapshot.spread is not None:
        return float(snapshot.spread)
    bid = snapshot.best_bid_yes if snapshot and snapshot.best_bid_yes is not None else market.best_bid_yes
    ask = snapshot.best_ask_yes if snapshot and snapshot.best_ask_yes is not None else market.best_ask_yes
    if bid is None or ask is None:
        return None
    return float(ask) - float(bid)


async def _ensure_source(session: AsyncSession) -> NewsSource:
    source = (
        await session.execute(select(NewsSource).where(NewsSource.domain == "watchlist-monitor.internal"))
    ).scalar_one_or_none()
    if source is not None:
        return source
    source = NewsSource(
        name="Watchlist Monitor (internal)",
        domain="watchlist-monitor.internal",
        rss_url="https://watchlist-monitor.internal/rss",
        source_tier="HARD",
        polling_interval_minutes=5,
        active=True,
    )
    session.add(source)
    await session.flush()
    return source


async def _latest_snapshot(session: AsyncSession, market_id: str) -> Optional[PriceSnapshot]:
    return (
        await session.execute(
            select(PriceSnapshot)
            .where(PriceSnapshot.market_id == market_id)
            .order_by(PriceSnapshot.timestamp.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _already_traded(session: AsyncSession, market_id: str) -> bool:
    existing = (
        await session.execute(
            select(PaperTrade)
            .join(NewsSignal)
            .where(PaperTrade.market_id == market_id)
            .where(NewsSignal.signal_source_type == SIGNAL_SOURCE_TYPE)
            .limit(1)
        )
    ).scalar_one_or_none()
    return existing is not None


async def _probe_trade(session: AsyncSession, market: Market, snapshot: Optional[PriceSnapshot]) -> Optional[PaperTrade]:
    source = await _ensure_source(session)
    now = now_utc()
    article_id = new_id("watch_art")
    primary_outcome = (market.outcomes_json or ["YES"])[0]
    article = NewsArticle(
        id=article_id,
        source_id=source.id,
        source_domain=source.domain,
        source_tier="HARD",
        url=f"https://watchlist-monitor.internal/probe/{article_id}",
        title=f"[WATCHLIST PROBE] Exploratory paper entry for {market.question}",
        body=(
            "Automated overnight watchlist probe. This is paper-only and exists to "
            "exercise the signal-to-trade pipeline on a direct, auto-eligible short-term market. "
            f"The signal buys the first listed outcome: {primary_outcome}."
        ),
        published_at=now,
        fetched_at=now,
        content_hash=new_id("hash"),
    )
    session.add(article)
    await session.flush()

    signal = NewsSignal(
        id=new_id("watch_sig"),
        market_id=market.id,
        article_id=article_id,
        relevance_score=1.0,
        interpreted_outcome="YES",
        evidence_type="DIRECT",
        supporting_excerpt=article.title[:200],
        confidence=0.80,
        verifier_agrees=True,
        verifier_confidence=0.80,
        action="ACT",
        rejection_reason=None,
        signal_source_type=SIGNAL_SOURCE_TYPE,
        raw_interpretation={
            "source": SIGNAL_SOURCE_TYPE,
            "mode": "overnight_probe",
            "primary_outcome": primary_outcome,
            "paper_only": True,
        },
        raw_verifier={"verifier_agrees": True, "confidence": 0.80, "source": SIGNAL_SOURCE_TYPE},
        created_at=now,
    )
    session.add(signal)
    await session.flush()

    orderbook: Optional[dict[str, Any]] = None
    if market.enable_orderbook and market.token_ids_json:
        async with polymarket_async_client() as client:
            orderbook = await fetch_clob_orderbook(client, str(market.token_ids_json[0]))
    trade = maybe_paper_trade(market=market, signal=signal, snapshot=snapshot, orderbook=orderbook)
    if trade is not None:
        trade.trade_source = "LIVE"
        execution_context = dict(trade.execution_context_json or {})
        execution_context.update(
            {
                "source": SIGNAL_SOURCE_TYPE,
                "mode": "overnight_probe",
                "primary_outcome": primary_outcome,
                "paper_only": True,
            }
        )
        trade.execution_context_json = execution_context
        session.add(trade)
    return trade


async def run(session: AsyncSession, *, max_trades: int = 3) -> dict[str, Any]:
    t0 = time.perf_counter()
    setup = await short_term_watchlist.run(session)
    await session.flush()

    markets = (
        await session.execute(
            select(Market)
            .where(
                and_(
                    Market.market_type == "SHORT_TERM_WATCHLIST",
                    Market.active == True,  # noqa: E712
                    Market.closed == False,  # noqa: E712
                    Market.is_fixture.is_not(True),
                    Market.best_ask_yes.is_not(None),
                )
            )
            .order_by(Market.best_ask_yes.asc())
        )
    ).scalars().all()

    created = 0
    skipped: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    direct_categories = {"Less-watched sports / PLL lacrosse", "Less-watched sports / KBO baseball"}

    for market in markets:
        if created >= max_trades:
            break
        snapshot = await _latest_snapshot(session, market.id)
        ask = float(snapshot.best_ask_yes if snapshot and snapshot.best_ask_yes is not None else market.best_ask_yes)
        spread = _spread(market, snapshot)
        reason: Optional[str] = None
        if market.category not in direct_categories:
            reason = "NO_TRADE_AMBIGUOUS_RULE"
        elif ask >= float(short_term_watchlist.EXPLORATORY_MODE["maxAsk"]) or ask <= 0.02:
            reason = "NO_TRADE_PRICE_ALREADY_MOVED"
        elif spread is None or spread > float(short_term_watchlist.EXPLORATORY_MODE["maxSpread"]):
            reason = "NO_TRADE_AMBIGUOUS_RULE"
        elif await _already_traded(session, market.id):
            reason = "NO_TRADE_ALREADY_PROBED"

        if reason:
            skipped.append({"market_id": market.id, "slug": market.slug, "reason": reason, "ask": ask, "spread": spread})
            session.add(
                AuditLog(
                    id=new_id("audit"),
                    event_type="WATCHLIST_MONITOR_NO_TRADE",
                    market_id=market.id,
                    payload_json={"reason": reason, "ask": ask, "spread": spread, "slug": market.slug},
                )
            )
            continue

        trade = await _probe_trade(session, market, snapshot)
        if trade is None:
            skipped.append({"market_id": market.id, "slug": market.slug, "reason": "NO_TRADE_NO_ORDERBOOK", "ask": ask, "spread": spread})
            session.add(
                AuditLog(
                    id=new_id("audit"),
                    event_type="WATCHLIST_MONITOR_NO_TRADE",
                    market_id=market.id,
                    payload_json={"reason": "NO_TRADE_NO_ORDERBOOK", "ask": ask, "spread": spread, "slug": market.slug},
                )
            )
            continue

        created += 1
        trades.append({"trade_id": trade.id, "market_id": market.id, "slug": market.slug, "side": trade.side, "fill_price": trade.fill_price})
        session.add(
            AuditLog(
                id=new_id("audit"),
                event_type="PAPER_TRADE_CREATED",
                market_id=market.id,
                signal_id=trade.signal_id,
                payload_json={"trade_id": trade.id, "slug": market.slug, "side": trade.side, "fill_price": trade.fill_price},
            )
        )

    await session.commit()
    return {
        "ok": True,
        "setup": {
            "events_fetched": setup.get("events_fetched"),
            "markets_seen": setup.get("markets_seen"),
            "snapshots_created": setup.get("snapshots_created"),
        },
        "trades_created": created,
        "trades": trades,
        "skipped_count": len(skipped),
        "skipped_sample": skipped[:20],
        "duration_ms": int((time.perf_counter() - t0) * 1000),
    }
