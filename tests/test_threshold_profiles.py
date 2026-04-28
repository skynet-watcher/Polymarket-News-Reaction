from __future__ import annotations

import asyncio
import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.gating import decide_action
from app.models import (
    Base,
    Market,
    NewsArticle,
    NewsSignal,
    NewsSource,
    RuntimeSetting,
    ThresholdProfile,
)
from app.threshold_context import RUNTIME_KEY_THRESHOLD_PROFILE, resolve_trading_thresholds
from app.threshold_profiles_seed import DEFAULT_PROFILES, ensure_default_threshold_profiles


def test_ensure_default_inserts_three_profiles() -> None:
    async def go() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        async with Session() as session:
            await ensure_default_threshold_profiles(session)
            n = (await session.execute(select(func.count()).select_from(ThresholdProfile))).scalar_one()
            assert int(n) == len(DEFAULT_PROFILES)
            ids = (await session.execute(select(ThresholdProfile.id))).scalars().all()
            assert set(ids) == {"conservative", "balanced", "aggressive", "research"}
        async with Session() as session:
            await ensure_default_threshold_profiles(session)
            n2 = (await session.execute(select(func.count()).select_from(ThresholdProfile))).scalar_one()
            assert int(n2) == len(DEFAULT_PROFILES)
        await engine.dispose()

    asyncio.run(go())


def test_resolve_trading_thresholds_follows_runtime_setting() -> None:
    async def go() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        async with Session() as session:
            await ensure_default_threshold_profiles(session)
            session.add(RuntimeSetting(key=RUNTIME_KEY_THRESHOLD_PROFILE, value="aggressive"))
            await session.commit()

        async with Session() as session:
            ctx = await resolve_trading_thresholds(session)
            assert ctx.profile_id == "aggressive"
            assert ctx.min_relevance == 0.40
            assert ctx.paper_size_multiplier == 1.25

        await engine.dispose()

    asyncio.run(go())


def test_decide_action_evidence_rules() -> None:
    now = dt.datetime.now(dt.timezone.utc)
    market = Market(
        id="m1",
        question="Q?",
        outcomes_json=["YES", "NO"],
        liquidity=5000.0,
        best_bid_yes=0.4,
        best_ask_yes=0.42,
    )
    article = NewsArticle(
        id="a1",
        source_id=1,
        source_domain="x.test",
        url="https://x.test/1",
        title="t",
        body="b",
        published_at=now,
        content_hash="h1",
    )
    sig_base = dict(
        id="s1",
        market_id="m1",
        article_id="a1",
        interpreted_outcome="YES",
        confidence=0.95,
        verifier_agrees=True,
        verifier_confidence=0.9,
    )

    def sig(**kw: object) -> NewsSignal:
        return NewsSignal(**{**sig_base, **kw})

    a, _ = decide_action(
        market=market,
        article=article,
        signal=sig(evidence_type="INDIRECT"),
        max_spread=0.2,
        min_liquidity=100.0,
        min_confidence=0.5,
        min_verifier_confidence=0.5,
        max_article_age_minutes=60,
        allow_indirect_evidence=False,
    )
    assert a != "ACT"

    a2, _ = decide_action(
        market=market,
        article=article,
        signal=sig(evidence_type="INDIRECT"),
        max_spread=0.2,
        min_liquidity=100.0,
        min_confidence=0.5,
        min_verifier_confidence=0.5,
        max_article_age_minutes=60,
        allow_indirect_evidence=True,
    )
    assert a2 == "ACT"

    a3, r3 = decide_action(
        market=market,
        article=article,
        signal=sig(evidence_type="SPECULATIVE"),
        max_spread=0.2,
        min_liquidity=100.0,
        min_confidence=0.5,
        min_verifier_confidence=0.5,
        max_article_age_minutes=60,
        allow_indirect_evidence=True,
    )
    assert a3 == "ABSTAIN" and r3 == "WEAK_EVIDENCE"


def test_resolve_falls_back_when_profile_row_missing() -> None:
    async def go() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        async with Session() as session:
            session.add(NewsSource(name="S", domain="s.test", rss_url="https://s.test/rss"))
            await session.commit()

        async with Session() as session:
            ctx = await resolve_trading_thresholds(session)
            assert ctx.profile_id == "conservative"
            assert "fallback" in ctx.profile_label.lower()

        await engine.dispose()

    asyncio.run(go())
