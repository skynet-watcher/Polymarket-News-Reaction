"""
Bulk smoke-test trade generator.

Places up to ``count`` paper trades across ``count`` different markets in one
shot — one trade per market, no duplicates.

Market selection strategy
-------------------------
1. Markets with ``end_date`` soonest (resolves fastest → P&L appears fastest).
2. Fall back to most-liquid open markets when fewer than ``count`` near-expiry
   markets exist.

Bet direction per market
------------------------
If the market has a current mid price:
  - mid_yes > 0.55  → BUY_YES  (market leans YES, follow it)
  - mid_yes < 0.45  → BUY_NO   (market leans NO, follow it)
  - 0.45–0.55       → BUY_YES  (coin-flip; default to YES)

This produces a realistic mix of YES/NO trades and will show both winning
and losing settlements once markets close.

Usage
-----
POST /api/jobs/bulk_smoke_test?count=20
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Optional

import httpx
from sqlalchemy import and_, asc, desc, select

from app.db import SessionLocal
from app.models import (
    Market,
    NewsArticle,
    NewsSignal,
    NewsSource,
    PaperTrade,
    PriceSnapshot,
    RuntimeSetting,
)
from app.core.paper import maybe_paper_trade
from app.settings import settings
from app.util import new_id, now_utc

logger = logging.getLogger(__name__)

_BINANCE_URL = "https://api.binance.com/api/v3/ticker/bookTicker"
_SYMBOL = "BTCUSDT"


async def _fetch_btc_price() -> Optional[float]:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(_BINANCE_URL, params={"symbol": _SYMBOL})
            r.raise_for_status()
            d = r.json()
            return (float(d["bidPrice"]) + float(d["askPrice"])) / 2.0
    except Exception:
        logger.warning("bulk_smoke_test: Binance fetch failed; BTC context unavailable")
        return None


def _pick_direction(market: Market, latest_snap: Optional[PriceSnapshot]) -> str:
    """Return 'YES' or 'NO' based on current market mid price."""
    mid: Optional[float] = None
    if latest_snap is not None and latest_snap.mid_yes is not None:
        mid = float(latest_snap.mid_yes)
    elif market.best_bid_yes is not None and market.best_ask_yes is not None:
        mid = (float(market.best_bid_yes) + float(market.best_ask_yes)) / 2.0
    elif market.last_price_yes is not None:
        mid = float(market.last_price_yes)

    if mid is None:
        return "YES"          # default
    return "NO" if mid < 0.45 else "YES"


def _synthetic_snap(market_id: str, now: dt.datetime) -> PriceSnapshot:
    """In-memory snapshot with neutral 50/50 prices — not persisted."""
    return PriceSnapshot(
        id="smoke_synthetic",
        market_id=market_id,
        timestamp=now,
        mid_yes=0.50,
        best_bid_yes=0.48,
        best_ask_yes=0.52,
        spread=0.04,
        liquidity=1000.0,
        data_quality="SMOKE_TEST",
    )


async def run(*, count: int = 20) -> dict[str, Any]:
    """
    Place up to ``count`` paper trades across ``count`` different markets.
    Returns a summary dict.
    """
    count = max(1, min(50, count))
    now = now_utc()

    async with SessionLocal() as session:
        # ── Fetch BTC price for context note ─────────────────────────────────
        btc_price = await _fetch_btc_price()
        btc_note = f"BTCUSDT ≈ ${btc_price:,.0f}" if btc_price else "BTC price unavailable"

        # ── Get a NewsSource to attach articles to ────────────────────────────
        source = (await session.execute(select(NewsSource).limit(1))).scalar_one_or_none()
        if source is None:
            return {"ok": False, "reason": "no_news_source_in_db", "trades_created": 0}

        # ── Select markets — soonest end_date first ───────────────────────────
        base_filter = and_(
            Market.active == True,        # noqa: E712
            Market.closed == False,       # noqa: E712
            Market.is_fixture.is_not(True),
        )

        # Bucket 1: markets with a known end_date, soonest first.
        near_expiry = (
            await session.execute(
                select(Market)
                .where(and_(base_filter, Market.end_date.is_not(None)))
                .order_by(asc(Market.end_date))
                .limit(count)
            )
        ).scalars().all()

        # Bucket 2: top-liquidity markets to fill remaining slots.
        needed = count - len(near_expiry)
        near_ids = {m.id for m in near_expiry}
        if needed > 0:
            extra = (
                await session.execute(
                    select(Market)
                    .where(and_(base_filter, Market.id.not_in(list(near_ids)) if near_ids else base_filter))
                    .order_by(desc(Market.liquidity))
                    .limit(needed)
                )
            ).scalars().all()
        else:
            extra = []

        markets = list(near_expiry) + list(extra)

        if not markets:
            return {
                "ok": False,
                "reason": "no_open_markets_in_db",
                "hint": "Run 'Sync Markets' first.",
                "trades_created": 0,
            }

        # ── One shared article for the whole batch ────────────────────────────
        article_id = new_id("bulk_art")
        article = NewsArticle(
            id=article_id,
            source_id=source.id,
            source_domain="bulk-smoke-test.internal",
            source_tier="SOFT",
            url=f"https://bulk-smoke-test.internal/{article_id}",
            title=f"[BULK SMOKE TEST] {btc_note} — pipeline validation batch",
            body=(
                f"Automated bulk smoke-test. {btc_note}. "
                f"Placing {len(markets)} paper trades across {len(markets)} markets "
                f"to validate the end-to-end trade and settlement pipeline. "
                "Not a real news signal."
            ),
            published_at=now,
            fetched_at=now,
            content_hash=new_id("hash"),
        )
        session.add(article)
        await session.flush()

        # ── Place one trade per market ────────────────────────────────────────
        trades_created = 0
        skipped = 0
        results: list[dict[str, Any]] = []

        for market in markets:
            # Latest snapshot for this market
            latest_snap = (
                await session.execute(
                    select(PriceSnapshot)
                    .where(PriceSnapshot.market_id == market.id)
                    .order_by(PriceSnapshot.timestamp.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()

            snap_has_prices = (
                latest_snap is not None
                and latest_snap.best_bid_yes is not None
                and latest_snap.best_ask_yes is not None
            )
            market_has_prices = (
                market.best_bid_yes is not None
                and market.best_ask_yes is not None
            )
            if not snap_has_prices and not market_has_prices:
                latest_snap = _synthetic_snap(market.id, now)

            outcome = _pick_direction(market, latest_snap)

            signal = NewsSignal(
                id=new_id("bulk_sig"),
                market_id=market.id,
                article_id=article_id,
                relevance_score=1.0,
                interpreted_outcome=outcome,
                evidence_type="DIRECT",
                supporting_excerpt=market.question[:200],
                confidence=0.85,
                verifier_agrees=True,
                verifier_confidence=0.85,
                action="ACT",
                rejection_reason=None,
                signal_source_type="BULK_SMOKE_TEST",
                raw_interpretation={
                    "source": "bulk_smoke_test",
                    "btc_price": btc_price,
                    "direction_basis": "mid_price",
                    "outcome": outcome,
                },
                created_at=now,
            )
            session.add(signal)
            await session.flush()

            trade = maybe_paper_trade(
                market=market,
                signal=signal,
                snapshot=latest_snap,
                orderbook=None,
            )

            if trade is not None:
                session.add(trade)
                trades_created += 1
                results.append({
                    "market_id": market.id,
                    "question": market.question[:80],
                    "outcome": outcome,
                    "end_date": market.end_date.isoformat() if market.end_date else None,
                    "trade_id": trade.id,
                })
            else:
                skipped += 1

        await session.commit()

        # Work out how soon the earliest trade might settle
        dated = [r for r in results if r["end_date"]]
        dated.sort(key=lambda x: x["end_date"])
        next_settlement = dated[0]["end_date"] if dated else None

        logger.info(
            "bulk_smoke_test: placed=%d skipped=%d markets=%d",
            trades_created, skipped, len(markets),
        )

        return {
            "ok": True,
            "trades_created": trades_created,
            "skipped": skipped,
            "markets_targeted": len(markets),
            "next_expected_settlement": next_settlement,
            "btc_context": btc_note,
            "results": results,
        }
