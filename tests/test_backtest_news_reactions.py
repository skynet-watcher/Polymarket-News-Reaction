from __future__ import annotations

import asyncio
import datetime as dt

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.jobs import backtest_news_reactions
from app.models import BacktestCase, BacktestEventLog, BacktestRun, Base, Market, NewsArticle, NewsSignal, NewsSource, PriceSnapshot


def test_backtest_news_reactions_records_timing_and_price_moves() -> None:
    async def _run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        try:
            async with Session() as session:
                published = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=30)
                fetched = published + dt.timedelta(minutes=4)
                session.add(
                    NewsSource(
                        id=1,
                        name="Test Wire",
                        domain="example.com",
                        rss_url="https://example.com/rss",
                        source_tier="SOFT",
                    )
                )
                session.add(
                    Market(
                        id="m1",
                        question="Will the thing happen?",
                        outcomes_json=["YES", "NO"],
                        end_date=published + dt.timedelta(hours=2),
                        liquidity=5000,
                        active=True,
                        closed=False,
                    )
                )
                session.add(
                    NewsArticle(
                        id="a1",
                        source_id=1,
                        source_domain="example.com",
                        source_tier="SOFT",
                        url="https://example.com/a1",
                        title="Thing happened",
                        body="Thing happened.",
                        published_at=published,
                        fetched_at=fetched,
                        content_hash="h1",
                    )
                )
                session.add(
                    NewsSignal(
                        id="s1",
                        market_id="m1",
                        article_id="a1",
                        relevance_score=0.9,
                        interpreted_outcome="YES",
                        evidence_type="DIRECT",
                        confidence=0.9,
                        verifier_agrees=True,
                        verifier_confidence=0.9,
                        action="ACT",
                        created_at=published + dt.timedelta(minutes=6),
                    )
                )
                for minutes, mid in [(-1, 0.50), (1, 0.53), (5, 0.56), (15, 0.64), (60, 0.62)]:
                    session.add(
                        PriceSnapshot(
                            id=f"p{minutes}",
                            market_id="m1",
                            timestamp=published + dt.timedelta(minutes=minutes),
                            mid_yes=mid,
                            best_bid_yes=mid - 0.01,
                            best_ask_yes=mid + 0.01,
                            liquidity=5000,
                        )
                    )
                await session.commit()

                out = await backtest_news_reactions.run(session, since_hours=2, max_articles=10, min_snapshot_coverage=3)
                assert out["ok"] is True
                assert out["cases"] == 1
                assert out["coverage_good"] == 1

                run = (await session.execute(select(BacktestRun))).scalar_one()
                assert run.status == "SUCCESS"
                case = (await session.execute(select(BacktestCase))).scalar_one()
                assert case.polling_delay_seconds == 240
                assert case.signal_delay_seconds == 360
                assert case.hours_to_resolution == 2.0
                assert case.coverage_status == "GOOD"
                assert case.first_5pt_move_seconds == 300
                assert case.first_10pt_move_seconds == 900
                assert case.move_before_fetch is False
                assert case.price_windows_json["5m"]["price"] == 0.56
                logs = (await session.execute(select(BacktestEventLog))).scalars().all()
                assert {l.event_type for l in logs} >= {"RUN_STARTED", "CASE_RECORDED", "RUN_FINISHED"}
        finally:
            await engine.dispose()

    asyncio.run(_run())
