from __future__ import annotations

import asyncio

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.live_feeds import DEFAULT_LIVE_FEEDS, ensure_live_news_sources
from app.models import Base, NewsSource


def test_ensure_live_feeds_deactivates_demo_and_inserts_defaults() -> None:
    async def go() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        async with Session() as session:
            session.add(
                NewsSource(
                    name="Demo Wire (fixture)",
                    domain="demo-wire.example",
                    rss_url="https://demo-wire.example/rss",
                    source_tier="SOFT",
                    polling_interval_minutes=5,
                    active=True,
                )
            )
            await session.commit()

        async with Session() as session:
            stats = await ensure_live_news_sources(session)
            assert stats["added"] == len(DEFAULT_LIVE_FEEDS)
            assert stats["feeds"] == len(DEFAULT_LIVE_FEEDS)

        async with Session() as session:
            demo = (
                await session.execute(select(NewsSource).where(NewsSource.domain == "demo-wire.example"))
            ).scalar_one()
            assert demo.active is False
            total = (await session.execute(select(func.count()).select_from(NewsSource))).scalar_one()
            assert total == 1 + len(DEFAULT_LIVE_FEEDS)

        async with Session() as session:
            stats2 = await ensure_live_news_sources(session)
            assert stats2["added"] == 0

        await engine.dispose()

    asyncio.run(go())
