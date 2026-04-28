"""
P&L truth tests for settle_trades: verifies that the settlement math is correct
for every combination of side (BUY_YES / BUY_NO) and outcome (YES / NO), and
that settlement_source is tagged correctly for each settlement path.

These are the authoritative correctness tests — if they fail, the P&L numbers
shown in the UI cannot be trusted regardless of what the dashboard says.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.jobs import settle_trades
from app.models import (
    Base,
    Market,
    NewsArticle,
    NewsSignal,
    NewsSource,
    PaperTrade,
    PriceSnapshot,
)
from app.paper_economics import entry_fee_usd, net_pnl_after_fees, settlement_fee_on_gross_profit
from app.settings import settings


def _utc(*args: int) -> dt.datetime:
    return dt.datetime(*args, tzinfo=dt.timezone.utc)


async def _make_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    return factory(), engine


async def _seed(
    session: AsyncSession,
    *,
    trade_id: str,
    market_id: str,
    side: str,
    fill_price: float,
    simulated_size: float,
    notional_usd: float | None,
    winning_outcome: str | None,
    article_age_hours: float = 30.0,
) -> None:
    session.add(NewsSource(
        name="src", domain="src.test", rss_url="https://src.test/rss",
        source_tier="SOFT", polling_interval_minutes=5, active=True,
    ))
    await session.flush()
    pub = _utc(2026, 1, 1, 0, 0, 0)
    session.add(Market(
        id=market_id, question="Q?", outcomes_json=["Yes", "No"],
        active=True, closed=(winning_outcome is not None),
        winning_outcome=winning_outcome,
        liquidity=5000.0, best_bid_yes=0.40, best_ask_yes=0.42,
    ))
    session.add(NewsArticle(
        id=f"art_{trade_id}", source_id=1, source_domain="src.test",
        source_tier="SOFT", url=f"https://src.test/{trade_id}",
        title="T", body="B", published_at=pub, content_hash=f"h_{trade_id}",
    ))
    session.add(NewsSignal(
        id=f"sig_{trade_id}", market_id=market_id, article_id=f"art_{trade_id}",
        interpreted_outcome="YES", evidence_type="DIRECT",
        confidence=0.95, verifier_agrees=True, verifier_confidence=0.95, action="ACT",
    ))
    session.add(PaperTrade(
        id=trade_id, market_id=market_id, signal_id=f"sig_{trade_id}",
        side=side, simulated_size=simulated_size, fill_price=fill_price,
        notional_usd=notional_usd,
        entry_fee_usd=(entry_fee_usd(notional_usd, settings.polymarket_entry_fee_rate)
                       if notional_usd is not None else None),
        status="OPEN",
    ))
    await session.commit()


# ── Gamma resolution path (winning_outcome set) ──────────────────────────────

def test_buy_yes_yes_resolution_is_a_win():
    """BUY_YES + market resolves YES → gross = (1.0 - fill) * size, net deducts fees."""

    async def _run():
        session, engine = await _make_session()
        try:
            fill, size, notional = 0.60, 16.6667, 10.0
            await _seed(session, trade_id="t1", market_id="m1", side="BUY_YES",
                        fill_price=fill, simulated_size=size,
                        notional_usd=notional, winning_outcome="YES")
            out = await settle_trades.run(session)
            assert out["settled"] == 1, out
            tr = await session.get(PaperTrade, "t1")
            assert tr.status == "SETTLED_RESOLVED"
            assert tr.settlement_source == "GAMMA_WINNING_OUTCOME"
            expected_gross = round((1.0 - fill) * size, 4)
            assert tr.gross_pnl_usd == pytest.approx(expected_gross, abs=0.0005)
            entry_f = entry_fee_usd(notional, settings.polymarket_entry_fee_rate)
            settle_f = settlement_fee_on_gross_profit(expected_gross, settings.polymarket_winning_profit_fee_rate)
            expected_net = net_pnl_after_fees(expected_gross, entry_f, settle_f)
            assert tr.net_pnl_usd == pytest.approx(expected_net, abs=0.0005)
            assert tr.pnl_final == pytest.approx(expected_net, abs=0.0005)
            assert tr.net_pnl_usd > 0, "a winning trade must have positive net P&L"
        finally:
            await session.close()
            await engine.dispose()

    asyncio.run(_run())


def test_buy_yes_no_resolution_is_a_loss():
    """BUY_YES + market resolves NO → gross = (0.0 - fill) * size (full loss of stake)."""

    async def _run():
        session, engine = await _make_session()
        try:
            fill, size = 0.60, 16.6667
            await _seed(session, trade_id="t2", market_id="m2", side="BUY_YES",
                        fill_price=fill, simulated_size=size,
                        notional_usd=None, winning_outcome="NO")
            out = await settle_trades.run(session)
            assert out["settled"] == 1, out
            tr = await session.get(PaperTrade, "t2")
            assert tr.status == "SETTLED_RESOLVED"
            assert tr.settlement_source == "GAMMA_WINNING_OUTCOME"
            expected_gross = round((0.0 - fill) * size, 4)
            assert tr.pnl_final == pytest.approx(expected_gross, abs=0.0005)
            assert tr.pnl_final < 0, "a losing trade must have negative P&L"
        finally:
            await session.close()
            await engine.dispose()

    asyncio.run(_run())


def test_buy_no_no_resolution_is_a_win():
    """BUY_NO + market resolves NO → resolution_price = 1.0, gross = (1.0 - fill) * size."""

    async def _run():
        session, engine = await _make_session()
        try:
            fill, size = 0.42, 23.8095
            await _seed(session, trade_id="t3", market_id="m3", side="BUY_NO",
                        fill_price=fill, simulated_size=size,
                        notional_usd=None, winning_outcome="NO")
            out = await settle_trades.run(session)
            assert out["settled"] == 1, out
            tr = await session.get(PaperTrade, "t3")
            assert tr.status == "SETTLED_RESOLVED"
            assert tr.settlement_source == "GAMMA_WINNING_OUTCOME"
            expected_gross = round((1.0 - fill) * size, 4)
            assert tr.pnl_final == pytest.approx(expected_gross, abs=0.0005)
            assert tr.pnl_final > 0
        finally:
            await session.close()
            await engine.dispose()

    asyncio.run(_run())


def test_buy_no_yes_resolution_is_a_loss():
    """BUY_NO + market resolves YES → resolution_price = 0.0, gross = (0.0 - fill) * size."""

    async def _run():
        session, engine = await _make_session()
        try:
            fill, size = 0.42, 23.8095
            await _seed(session, trade_id="t4", market_id="m4", side="BUY_NO",
                        fill_price=fill, simulated_size=size,
                        notional_usd=None, winning_outcome="YES")
            out = await settle_trades.run(session)
            assert out["settled"] == 1, out
            tr = await session.get(PaperTrade, "t4")
            assert tr.status == "SETTLED_RESOLVED"
            assert tr.pnl_final < 0
        finally:
            await session.close()
            await engine.dispose()

    asyncio.run(_run())


# ── T+24h mark-to-market path ────────────────────────────────────────────────

def test_t24h_settlement_tags_source_and_computes_pnl():
    """T+24h settlement sets settlement_source = T24H_MARK_TO_MARKET and correct PnL."""

    async def _run():
        session, engine = await _make_session()
        try:
            pub = _utc(2026, 1, 1, 0, 0, 0)
            fill, size = 0.50, 100.0
            # winning_outcome=None → T24H path
            await _seed(session, trade_id="t5", market_id="m5", side="BUY_YES",
                        fill_price=fill, simulated_size=size,
                        notional_usd=None, winning_outcome=None)
            snap_time = pub + dt.timedelta(hours=23)
            session.add(PriceSnapshot(
                id="snap_t5", market_id="m5", timestamp=snap_time,
                mid_yes=0.70, best_bid_yes=0.69, best_ask_yes=0.71,
                spread=0.02, liquidity=5000.0,
            ))
            await session.commit()

            out = await settle_trades.run(session)
            assert out["settled"] == 1, out
            tr = await session.get(PaperTrade, "t5")
            assert tr.status == "SETTLED_T24H"
            assert tr.settlement_source == "T24H_MARK_TO_MARKET"
            expected = round((0.70 - fill) * size, 4)
            assert tr.pnl_final == pytest.approx(expected, abs=0.0005)
        finally:
            await session.close()
            await engine.dispose()

    asyncio.run(_run())


# ── Sanity: unresolved open trade stays open ─────────────────────────────────

def test_open_trade_with_no_snapshot_stays_open():
    """A trade with no snapshot and no winning_outcome must remain OPEN."""

    async def _run():
        session, engine = await _make_session()
        try:
            await _seed(session, trade_id="t6", market_id="m6", side="BUY_YES",
                        fill_price=0.50, simulated_size=100.0,
                        notional_usd=None, winning_outcome=None)
            out = await settle_trades.run(session)
            assert out["settled"] == 0
            tr = await session.get(PaperTrade, "t6")
            assert tr.status == "OPEN"
            assert tr.settlement_source is None
        finally:
            await session.close()
            await engine.dispose()

    asyncio.run(_run())
