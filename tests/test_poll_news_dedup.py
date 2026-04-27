from __future__ import annotations

import asyncio
import datetime as dt
from unittest.mock import patch

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.jobs import poll_news
from app.models import Base, NewsArticle, NewsSource
from app.util import stable_article_id


def test_poll_news_skips_second_insert_when_url_exists_with_different_id() -> None:
    """Same URL + different parsed published_at → different stable id; DB enforces unique url."""

    async def fake_get(_client: httpx.AsyncClient, _url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(
            200,
            content=(
                b"""<?xml version="1.0"?>
        <rss version="2.0"><channel>
          <item>
            <title>Updated title</title>
            <link>https://www.theguardian.com/world/2024/jan/01/story</link>
            <pubDate>Mon, 01 Jan 2024 15:00:00 GMT</pubDate>
            <description>Hi</description>
          </item>
        </channel></rss>"""
            ),
        )

    async def go() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

        url = "https://www.theguardian.com/world/2024/jan/01/story"
        t_old = dt.datetime(2024, 1, 1, 12, 0, 0, tzinfo=dt.timezone.utc)

        async with Session() as session:
            session.add(
                NewsSource(
                    name="G",
                    domain="theguardian.com",
                    rss_url="https://theguardian.com/world/rss",
                    source_tier="SOFT",
                    polling_interval_minutes=5,
                    active=True,
                )
            )
            await session.commit()
            src = (await session.execute(select(NewsSource))).scalar_one()
            session.add(
                NewsArticle(
                    id=stable_article_id(url, t_old),
                    source_id=src.id,
                    source_domain=src.domain,
                    source_tier=src.source_tier,
                    url=url,
                    title="Old",
                    body="",
                    published_at=t_old,
                    fetched_at=dt.datetime.now(dt.timezone.utc),
                    content_hash="x",
                )
            )
            await session.commit()

        async with Session() as session:
            with patch("app.jobs.poll_news.get_with_retry", new=fake_get):
                out = await poll_news.run(session)
            assert out["inserted"] == 0

        async with Session() as session:
            n = (await session.execute(select(NewsArticle))).scalars().all()
            assert len(n) == 1
            assert n[0].url == url

        await engine.dispose()

    asyncio.run(go())
