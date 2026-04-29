"""
BTC smoke-test signal generator.

Bypasses the news pipeline and LLM entirely.

How it works
------------
1. Fetch the current BTCUSDT mid-price from Binance (no API key required).
2. Compare against the last stored price (RuntimeSetting "btc_test_ref_price" /
   "btc_test_ref_at").  If this is the first run, save the reference and exit
   — nothing to compare yet.
3. If the price has moved >= ``move_threshold_pct`` percent since the reference
   was saved:
   a. Pick the most-liquid open non-fixture market in the DB.
   b. Create a synthetic NewsArticle and NewsSignal with ``action = "ACT"``
      (no LLM gating), pointing the signal direction at whichever way BTC moved.
   c. Call ``maybe_paper_trade`` to size and record the trade.
4. Always update the reference price so the next call compares fresh.

Purpose
-------
Prove that the signal → paper_trade → settlement pipeline is wired up correctly
without depending on RSS feeds or OpenAI being configured.

Usage
-----
POST /api/jobs/btc_signal_test?move_threshold_pct=0.5&force=false
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Optional

import httpx
from sqlalchemy import and_, desc, select

from app.db import SessionLocal
from app.models import Market, NewsArticle, NewsSignal, NewsSource, PaperTrade, PriceSnapshot, RuntimeSetting
from app.core.paper import maybe_paper_trade
from app.settings import settings
from app.util import new_id, now_utc

logger = logging.getLogger(__name__)

_BINANCE_URL = "https://api.binance.com/api/v3/ticker/bookTicker"
_SYMBOL = "BTCUSDT"
_REF_PRICE_KEY = "btc_test_ref_price"
_REF_AT_KEY = "btc_test_ref_at"


async def _fetch_btc_mid() -> Optional[float]:
    """Return current BTCUSDT mid-price from Binance, or None on any error."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(_BINANCE_URL, params={"symbol": _SYMBOL})
            r.raise_for_status()
            d = r.json()
            bid = float(d["bidPrice"])
            ask = float(d["askPrice"])
            return (bid + ask) / 2.0
    except Exception:
        logger.exception("btc_signal_test: failed to fetch Binance price")
        return None


async def run(
    *,
    move_threshold_pct: float = 0.5,
    force: bool = False,
) -> dict[str, Any]:
    """
    Main entry point.  Returns a result dict describing what happened.

    Parameters
    ----------
    move_threshold_pct:
        Minimum BTC price move (%) needed to fire a trade.  Default 0.5%.
    force:
        If True, fire a trade regardless of how much BTC moved (useful for
        one-off pipeline smoke tests).
    """
    async with SessionLocal() as session:
        # ── 1. Fetch current price ────────────────────────────────────────────
        current_price = await _fetch_btc_mid()
        if current_price is None:
            return {"ok": False, "reason": "binance_fetch_failed", "trade_created": False}

        now = now_utc()

        # ── 2. Load stored reference price ───────────────────────────────────
        ref_row = await session.get(RuntimeSetting, _REF_PRICE_KEY)
        ref_at_row = await session.get(RuntimeSetting, _REF_AT_KEY)

        ref_price: Optional[float] = None
        ref_at: Optional[dt.datetime] = None

        if ref_row is not None and ref_row.value:
            try:
                ref_price = float(ref_row.value)
            except ValueError:
                pass
        if ref_at_row is not None and ref_at_row.value:
            try:
                ref_at = dt.datetime.fromisoformat(ref_at_row.value)
            except ValueError:
                pass

        # First run — store reference and exit.
        if ref_price is None or ref_at is None:
            if ref_row is None:
                session.add(RuntimeSetting(key=_REF_PRICE_KEY, value=str(current_price)))
            else:
                ref_row.value = str(current_price)
            if ref_at_row is None:
                session.add(RuntimeSetting(key=_REF_AT_KEY, value=now.isoformat()))
            else:
                ref_at_row.value = now.isoformat()
            await session.commit()
            return {
                "ok": True,
                "reason": "first_run_reference_saved",
                "current_price": current_price,
                "trade_created": False,
            }

        # ── 3. Evaluate price move ────────────────────────────────────────────
        pct_move = ((current_price - ref_price) / ref_price) * 100.0
        abs_move = abs(pct_move)
        direction = "UP" if pct_move >= 0 else "DOWN"
        outcome = "YES" if pct_move >= 0 else "NO"  # YES = price went up

        threshold_met = abs_move >= move_threshold_pct or force

        logger.info(
            "btc_signal_test: current=%.2f ref=%.2f move=%.3f%% direction=%s threshold_met=%s",
            current_price, ref_price, pct_move, direction, threshold_met,
        )

        # Always update the reference price for the next call.
        if ref_row is not None:
            ref_row.value = str(current_price)
        if ref_at_row is not None:
            ref_at_row.value = now.isoformat()

        if not threshold_met:
            await session.commit()
            return {
                "ok": True,
                "reason": "move_below_threshold",
                "current_price": current_price,
                "ref_price": ref_price,
                "pct_move": round(pct_move, 4),
                "direction": direction,
                "threshold_pct": move_threshold_pct,
                "trade_created": False,
            }

        # ── 4. Find a target market ───────────────────────────────────────────
        # Prefer a BTC/crypto market; fall back to any open market.
        btc_market: Optional[Market] = None
        crypto_q = (
            select(Market)
            .where(
                and_(
                    Market.active == True,  # noqa: E712
                    Market.closed == False,  # noqa: E712
                    Market.is_fixture.is_not(True),
                )
            )
            .where(Market.category == "crypto")
            .order_by(desc(Market.liquidity))
            .limit(1)
        )
        btc_market = (await session.execute(crypto_q)).scalar_one_or_none()

        if btc_market is None:
            # Widen search — any open market with BTC/bitcoin in the question.
            from sqlalchemy import func
            kw_q = (
                select(Market)
                .where(
                    and_(
                        Market.active == True,  # noqa: E712
                        Market.closed == False,  # noqa: E712
                        Market.is_fixture.is_not(True),
                        func.lower(Market.question).contains("bitcoin")
                        | func.lower(Market.question).contains("btc"),
                    )
                )
                .order_by(desc(Market.liquidity))
                .limit(1)
            )
            btc_market = (await session.execute(kw_q)).scalar_one_or_none()

        if btc_market is None:
            # Last resort: any open market.
            any_q = (
                select(Market)
                .where(
                    and_(
                        Market.active == True,  # noqa: E712
                        Market.closed == False,  # noqa: E712
                        Market.is_fixture.is_not(True),
                    )
                )
                .order_by(desc(Market.liquidity))
                .limit(1)
            )
            btc_market = (await session.execute(any_q)).scalar_one_or_none()

        if btc_market is None:
            await session.commit()
            return {
                "ok": False,
                "reason": "no_open_markets_in_db",
                "hint": "Run 'Sync Markets' first to populate market data.",
                "trade_created": False,
            }

        # ── 5. Ensure we have a NewsSource to attach the article to ──────────
        source = (await session.execute(select(NewsSource).limit(1))).scalar_one_or_none()
        if source is None:
            await session.commit()
            return {"ok": False, "reason": "no_news_source_in_db", "trade_created": False}

        # ── 6. Create synthetic article ───────────────────────────────────────
        title = (
            f"[BTC SMOKE TEST] Bitcoin {direction} {abs_move:.2f}% "
            f"to ${current_price:,.0f} (was ${ref_price:,.0f})"
        )
        body = (
            f"Automated smoke-test signal. BTCUSDT moved {pct_move:+.3f}% "
            f"from ${ref_price:,.2f} to ${current_price:,.2f} between "
            f"{ref_at.strftime('%H:%M UTC')} and {now.strftime('%H:%M UTC')}.\n"
            "This article was generated by btc_signal_test — not a real news source."
        )
        article_id = new_id("btc_art")
        article = NewsArticle(
            id=article_id,
            source_id=source.id,
            source_domain="binance-smoke-test.internal",
            source_tier="SOFT",
            url=f"https://binance-smoke-test.internal/btc/{article_id}",
            title=title,
            body=body,
            published_at=now,
            fetched_at=now,
            content_hash=new_id("hash"),
        )
        session.add(article)
        await session.flush()

        # ── 7. Create synthetic signal — bypass all LLM gating ───────────────
        signal = NewsSignal(
            id=new_id("btc_sig"),
            market_id=btc_market.id,
            article_id=article_id,
            relevance_score=1.0,
            interpreted_outcome=outcome,
            evidence_type="DIRECT",
            supporting_excerpt=title[:200],
            confidence=0.85,
            verifier_agrees=True,
            verifier_confidence=0.85,
            action="ACT",          # Skip all gating — this is a smoke test.
            rejection_reason=None,
            signal_source_type="BINANCE_PRICE_MOVE",
            raw_interpretation={
                "source": "binance_smoke_test",
                "symbol": _SYMBOL,
                "ref_price": ref_price,
                "current_price": current_price,
                "pct_move": round(pct_move, 4),
                "direction": direction,
                "threshold_pct": move_threshold_pct,
                "forced": force,
            },
            created_at=now,
        )
        session.add(signal)
        await session.flush()

        # ── 8. Pull latest price snapshot for fill price ──────────────────────
        latest_snap = (
            await session.execute(
                select(PriceSnapshot)
                .where(PriceSnapshot.market_id == btc_market.id)
                .order_by(PriceSnapshot.timestamp.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        # ── 9. Place the paper trade ──────────────────────────────────────────
        trade = maybe_paper_trade(
            market=btc_market,
            signal=signal,
            snapshot=latest_snap,
            orderbook=None,  # top-of-book mode is fine for smoke test
        )

        trade_id: Optional[str] = None
        if trade is not None:
            session.add(trade)
            trade_id = trade.id

        await session.commit()

        result = {
            "ok": True,
            "trade_created": trade is not None,
            "trade_id": trade_id,
            "market_id": btc_market.id,
            "market_question": btc_market.question[:120],
            "signal_id": signal.id,
            "article_id": article_id,
            "direction": direction,
            "outcome": outcome,
            "pct_move": round(pct_move, 4),
            "current_price": current_price,
            "ref_price": ref_price,
        }

        if trade is None:
            result["trade_skipped_reason"] = (
                "No bid/ask price available on market — run 'Sync Markets' to refresh prices."
            )

        logger.info("btc_signal_test: %s", result)
        return result
