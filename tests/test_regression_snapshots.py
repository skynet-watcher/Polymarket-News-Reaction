"""
Regression tests for Chad review: T+24 settlement guards, post-signal drift snapshots,
and snapshot-aligned paper fills. Uses in-memory SQLite (no project data.db).
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from app.core.paper import maybe_paper_trade
from app.jobs import settle_trades
from app.jobs.compute_lag import _nearest_snapshot_after_before
from app.models import (
    Base,
    Market,
    NewsArticle,
    NewsSignal,
    NewsSource,
    PaperTrade,
    PriceSnapshot,
)


def _utc(*args: int) -> dt.datetime:
    return dt.datetime(*args, tzinfo=dt.timezone.utc)


def _aware(d: dt.datetime) -> dt.datetime:
    if d.tzinfo is None:
        return d.replace(tzinfo=dt.timezone.utc)
    return d


async def _memory_session() -> tuple[AsyncSession, object]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    session = factory()
    return session, engine


async def _seed_minimal_open_trade(
    session: AsyncSession,
    *,
    article_time: dt.datetime,
    snap_time: dt.datetime,
    snap_mid: float,
) -> PaperTrade:
    session.add(
        NewsSource(
            name="test-src",
            domain="test-src.example",
            rss_url="https://test-src.example/rss",
            source_tier="SOFT",
            polling_interval_minutes=5,
            active=True,
        )
    )
    await session.flush()

    session.add(
        Market(
            id="mkt_r1",
            question="Test market?",
            outcomes_json=["Yes", "No"],
            active=True,
            closed=False,
            winning_outcome=None,
            liquidity=5000.0,
            best_bid_yes=0.40,
            best_ask_yes=0.42,
        )
    )
    session.add(
        NewsArticle(
            id="art_r1",
            source_id=1,
            source_domain="test-src.example",
            source_tier="SOFT",
            url="https://test-src.example/a1",
            title="Headline",
            body="Body",
            published_at=article_time,
            content_hash="hash_r1",
        )
    )
    session.add(
        NewsSignal(
            id="sig_r1",
            market_id="mkt_r1",
            article_id="art_r1",
            interpreted_outcome="YES",
            evidence_type="DIRECT",
            confidence=0.95,
            verifier_agrees=True,
            verifier_confidence=0.95,
            action="ACT",
        )
    )
    session.add(
        PaperTrade(
            id="tr_r1",
            market_id="mkt_r1",
            signal_id="sig_r1",
            hypothesis_id=None,
            side="BUY_YES",
            simulated_size=100.0,
            fill_price=0.50,
            best_bid_at_signal=0.40,
            best_ask_at_signal=0.42,
            mid_at_signal=0.41,
            status="OPEN",
        )
    )
    session.add(
        PriceSnapshot(
            id="snap_r1",
            market_id="mkt_r1",
            timestamp=snap_time,
            best_bid_yes=0.40,
            best_ask_yes=0.42,
            mid_yes=snap_mid,
            spread=0.02,
            liquidity=5000.0,
        )
    )
    await session.commit()

    res = await session.get(PaperTrade, "tr_r1")
    assert res is not None
    return res


def test_settle_t24_skips_presignal_snapshot_only():
    """Only snapshot before article time → no settlement (stale / wrong window)."""

    async def _run() -> None:
        session, engine = await _memory_session()
        try:
            t0 = _utc(2020, 1, 1, 12, 0, 0)
            await _seed_minimal_open_trade(
                session,
                article_time=t0,
                snap_time=t0 - dt.timedelta(hours=2),
                snap_mid=0.99,
            )
            out = await settle_trades.run(session)
            assert out["settled"] == 0
            assert out["skipped"] >= 1
            tr = await session.get(PaperTrade, "tr_r1")
            assert tr is not None
            assert tr.status == "OPEN"
        finally:
            await session.close()
            await engine.dispose()

    asyncio.run(_run())


def test_settle_t24_skips_snapshot_too_far_before_nominal_settle():
    """Post-signal snapshot but > max skew before T+24 → skip."""

    async def _run() -> None:
        session, engine = await _memory_session()
        try:
            t0 = _utc(2020, 1, 1, 12, 0, 0)
            # settle_time = t0 + 24h; snap at t0 + 20h → 4h skew > default 7200s
            await _seed_minimal_open_trade(
                session,
                article_time=t0,
                snap_time=t0 + dt.timedelta(hours=20),
                snap_mid=0.60,
            )
            out = await settle_trades.run(session)
            assert out["settled"] == 0
            assert out["skipped"] >= 1
            tr = await session.get(PaperTrade, "tr_r1")
            assert tr is not None
            assert tr.status == "OPEN"
        finally:
            await session.close()
            await engine.dispose()

    asyncio.run(_run())


def test_settle_t24_settles_when_snapshot_near_settle_after_signal():
    """Post-signal snapshot within skew of T+24 → SETTLED_T24H and PnL updated."""

    async def _run() -> None:
        session, engine = await _memory_session()
        try:
            t0 = _utc(2020, 1, 1, 12, 0, 0)
            await _seed_minimal_open_trade(
                session,
                article_time=t0,
                snap_time=t0 + dt.timedelta(hours=23),
                snap_mid=0.60,
            )
            out = await settle_trades.run(session)
            assert out["settled"] == 1
            assert out["skipped"] == 0
            tr = await session.get(PaperTrade, "tr_r1")
            assert tr is not None
            assert tr.status == "SETTLED_T24H"
            assert tr.pnl_final is not None
            assert round(float(tr.pnl_final), 4) == round((0.60 - 0.50) * 100.0, 4)
        finally:
            await session.close()
            await engine.dispose()

    asyncio.run(_run())


def test_post_window_nearest_snapshot_must_be_after_signal():
    """POST drift helper must not return a snapshot at or before signal_time."""

    async def _run() -> None:
        session, engine = await _memory_session()
        try:
            t0 = _utc(2026, 1, 1, 12, 0, 0)
            session.add(
                Market(
                    id="mkt_p1",
                    question="M?",
                    outcomes_json=["Yes", "No"],
                    active=True,
                    closed=False,
                )
            )
            # Pre-signal only — should not be selected for POST target after signal
            session.add(
                PriceSnapshot(
                    id="pre_only",
                    market_id="mkt_p1",
                    timestamp=t0 - dt.timedelta(hours=1),
                    mid_yes=0.50,
                    best_bid_yes=0.49,
                    best_ask_yes=0.51,
                )
            )
            await session.commit()

            snap = await _nearest_snapshot_after_before(
                session,
                market_id="mkt_p1",
                at_or_before=t0 + dt.timedelta(minutes=15),
                strictly_after=t0,
            )
            assert snap is None

            session.add(
                PriceSnapshot(
                    id="post_ok",
                    market_id="mkt_p1",
                    timestamp=t0 + dt.timedelta(minutes=10),
                    mid_yes=0.55,
                    best_bid_yes=0.54,
                    best_ask_yes=0.56,
                )
            )
            await session.commit()

            snap2 = await _nearest_snapshot_after_before(
                session,
                market_id="mkt_p1",
                at_or_before=t0 + dt.timedelta(minutes=15),
                strictly_after=t0,
            )
            assert snap2 is not None
            assert snap2.id == "post_ok"
            assert _aware(snap2.timestamp) > t0
        finally:
            await session.close()
            await engine.dispose()

    asyncio.run(_run())


def test_maybe_paper_trade_prefers_snapshot_bid_ask_over_stale_market():
    """Fill prices match snapshot when Market row has no usable book but snapshot does."""

    market = Market(
        id="m_x",
        question="Q?",
        outcomes_json=["Yes", "No"],
        active=True,
        closed=False,
        best_bid_yes=None,
        best_ask_yes=None,
        liquidity=5000.0,
    )
    snap = PriceSnapshot(
        id="s_x",
        market_id="m_x",
        timestamp=_utc(2026, 1, 1, 12, 0, 0),
        best_bid_yes=0.40,
        best_ask_yes=0.42,
        mid_yes=0.41,
        liquidity=8000.0,
    )
    signal = NewsSignal(
        id="sig_x",
        market_id="m_x",
        article_id="a_x",
        interpreted_outcome="YES",
        evidence_type="DIRECT",
        confidence=0.95,
        verifier_agrees=True,
        verifier_confidence=0.95,
        action="ACT",
    )
    trade = maybe_paper_trade(market=market, signal=signal, snapshot=snap)
    assert trade is not None
    assert trade.best_bid_at_signal == 0.40
    assert trade.best_ask_at_signal == 0.42
    assert trade.fill_price == pytest.approx(min(0.999, 0.42 + 0.01))
    # size uses snapshot liquidity 8000 → min(100, max(10, 8000/500)) = min(100, 16) = 16
    assert trade.simulated_size == pytest.approx(16.0)


def test_maybe_paper_trade_buy_no_uses_snapshot_bid():
    market = Market(
        id="m_y",
        question="Q?",
        outcomes_json=["Yes", "No"],
        active=True,
        closed=False,
        best_bid_yes=None,
        best_ask_yes=None,
        liquidity=500.0,
    )
    snap = PriceSnapshot(
        id="s_y",
        market_id="m_y",
        timestamp=_utc(2026, 1, 1, 12, 0, 0),
        best_bid_yes=0.60,
        best_ask_yes=0.62,
        mid_yes=0.61,
        liquidity=500.0,
    )
    signal = NewsSignal(
        id="sig_y",
        market_id="m_y",
        article_id="a_y",
        interpreted_outcome="NO",
        evidence_type="DIRECT",
        confidence=0.95,
        verifier_agrees=True,
        verifier_confidence=0.95,
        action="ACT",
    )
    trade = maybe_paper_trade(market=market, signal=signal, snapshot=snap)
    assert trade is not None
    assert trade.side == "BUY_NO"
    # fill = min(0.999, (1 - 0.60) + 0.01) = 0.41
    assert trade.fill_price == pytest.approx(0.41)
