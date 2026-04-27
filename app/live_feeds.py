"""
Curated default RSS feeds for live news ingestion.

Each `domain` must match `domain_from_url(item["link"])` for items in that feed
(see `app/jobs/poll_news.py` guardrail).
"""

from __future__ import annotations

from typing import Any, Dict, FrozenSet, List

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import NewsSource


# fmt: off
# Reuters public RSS often returns 401/403 for automated clients; use Axios instead.
DEPRECATED_SOURCE_DOMAINS: FrozenSet[str] = frozenset({"reuters.com"})

DEFAULT_LIVE_FEEDS: List[Dict[str, Any]] = [
    {"name": "The Guardian — World", "domain": "theguardian.com", "rss_url": "https://www.theguardian.com/world/rss", "source_tier": "SOFT"},
    {"name": "BBC News — UK", "domain": "bbc.co.uk", "rss_url": "https://feeds.bbci.co.uk/news/rss.xml", "source_tier": "SOFT"},
    # World feed often uses www.bbc.com article URLs; separate row so domain guard matches.
    {"name": "BBC News — World (bbc.com)", "domain": "bbc.com", "rss_url": "https://feeds.bbci.co.uk/news/world/rss.xml", "source_tier": "SOFT"},
    {"name": "NPR — News", "domain": "npr.org", "rss_url": "https://feeds.npr.org/1001/rss.xml", "source_tier": "SOFT"},
    {"name": "Axios — Top stories", "domain": "axios.com", "rss_url": "https://api.axios.com/feed/", "source_tier": "SOFT"},
    {"name": "Politico — Politics", "domain": "politico.com", "rss_url": "https://rss.politico.com/politics-news.xml", "source_tier": "SOFT"},
    {"name": "Al Jazeera — All", "domain": "aljazeera.com", "rss_url": "https://www.aljazeera.com/xml/rss/all.xml", "source_tier": "SOFT"},
]
# fmt: on


async def ensure_live_news_sources(session: AsyncSession) -> dict[str, int]:
    """
    Upsert `DEFAULT_LIVE_FEEDS` and turn off the demo fixture source when real feeds exist.
    Safe to call on every startup.
    """
    added = 0
    updated = 0

    for feed in DEFAULT_LIVE_FEEDS:
        tier = feed.get("source_tier") or "SOFT"
        poll_m = int(feed.get("polling_interval_minutes") or 5)
        row = (await session.execute(select(NewsSource).where(NewsSource.domain == feed["domain"]))).scalar_one_or_none()
        if row is None:
            session.add(
                NewsSource(
                    name=feed["name"],
                    domain=feed["domain"],
                    rss_url=feed["rss_url"],
                    source_tier=tier,
                    polling_interval_minutes=poll_m,
                    active=True,
                )
            )
            added += 1
            continue

        changed = False
        if row.rss_url != feed["rss_url"]:
            row.rss_url = feed["rss_url"]
            changed = True
        if row.name != feed["name"]:
            row.name = feed["name"]
            changed = True
        if row.source_tier != tier:
            row.source_tier = tier
            changed = True
        if row.polling_interval_minutes != poll_m:
            row.polling_interval_minutes = poll_m
            changed = True
        if not row.active:
            row.active = True
            changed = True
        if changed:
            updated += 1

    for dead_domain in DEPRECATED_SOURCE_DOMAINS:
        dead = (await session.execute(select(NewsSource).where(NewsSource.domain == dead_domain))).scalar_one_or_none()
        if dead is not None and dead.active:
            dead.active = False
            updated += 1

    demo = (await session.execute(select(NewsSource).where(NewsSource.domain == "demo-wire.example"))).scalar_one_or_none()
    if demo is not None and demo.active:
        real_n = (
            await session.execute(
                select(func.count())
                .select_from(NewsSource)
                .where(NewsSource.domain != "demo-wire.example", NewsSource.active == True)  # noqa: E712
            )
        ).scalar_one()
        if int(real_n or 0) > 0:
            demo.active = False
            updated += 1

    await session.commit()
    return {"added": added, "updated": updated, "feeds": len(DEFAULT_LIVE_FEEDS)}
