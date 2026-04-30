from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Any

from lxml import etree
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.http_client import get_with_retry, polymarket_async_client
from app.models import NewsArticle, NewsSource
from app.security import validate_public_https_url
from app.util import hostname_from_url, hostname_matches_source, now_utc, sha256_hex, stable_article_id

logger = logging.getLogger(__name__)

_EMPTY_RSS_BYTES = b"<?xml version='1.0'?><rss version='2.0'><channel></channel></rss>"


def _text(el: Any) -> str:
    if el is None:
        return ""
    if isinstance(el, str):
        return el
    return "".join(el.itertext()).strip()


def _rss_item_link(item: Any) -> str:
    """Many feeds leave <link/> empty and put the URL in <guid isPermaLink=\"true\">."""
    link = _text(item.find("link")).strip()
    if link:
        return link
    guid_el = item.find("guid")
    if guid_el is None:
        return ""
    raw = _text(guid_el).strip()
    flag = (guid_el.get("isPermaLink") or "").strip().lower()
    if flag == "true" or raw.startswith("http://") or raw.startswith("https://"):
        return raw
    return ""


def _atom_entry_link(entry: Any) -> str:
    best = ""
    for link_el in entry.xpath("./*[local-name()='link']"):
        href = (link_el.get("href") or "").strip()
        if not href:
            continue
        rel = (link_el.get("rel") or "alternate").lower()
        if rel in ("alternate", "http://www.iana.org/assignments/relation/alternate"):
            return href
        if not best:
            best = href
    return best


def _parse_rss(xml_bytes: bytes) -> list[dict[str, Any]]:
    # Minimal RSS/Atom parsing for MVP (lxml is available).
    items: list[dict[str, Any]] = []
    parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=True, huge_tree=False)
    root = etree.fromstring(xml_bytes, parser=parser)

    # RSS 2.0: channel/item
    for item in root.xpath("//channel/item"):
        title = _text(item.find("title"))
        link = _rss_item_link(item)
        pub = _text(item.find("pubDate"))
        desc = _text(item.find("description"))
        items.append({"title": title, "link": link, "published_raw": pub, "summary": desc})

    # Atom: entry
    for entry in root.xpath("//*[local-name()='feed']/*[local-name()='entry']"):
        title = _text(entry.find("{*}title"))
        link = _atom_entry_link(entry)
        updated = _text(entry.find("{*}updated")) or _text(entry.find("{*}published"))
        summary = _text(entry.find("{*}summary")) or _text(entry.find("{*}content"))
        items.append({"title": title, "link": link, "published_raw": updated, "summary": summary})

    # Dedup by link
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for it in items:
        link = (it.get("link") or "").strip()
        if not link or link in seen:
            continue
        seen.add(link)
        out.append(it)
    return out


def _parse_rss_safe(xml_bytes: bytes) -> list[dict[str, Any]]:
    try:
        return _parse_rss(xml_bytes)
    except Exception:
        return []


def _parse_published(published_raw: str) -> dt.datetime:
    s = (published_raw or "").strip()
    if not s:
        return now_utc()

    # Try RFC 2822-ish quickly (RSS)
    for fmt in (
        "%a, %d %b %Y %H:%M:%S %Z",
        "%a, %d %b %Y %H:%M:%S %z",
        "%d %b %Y %H:%M:%S %z",
    ):
        try:
            d = dt.datetime.strptime(s, fmt)
            return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
        except Exception:
            pass

    # Try ISO (Atom)
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except Exception:
        return now_utc()


async def run(session: AsyncSession) -> dict[str, Any]:
    t0 = time.perf_counter()
    sources = (await session.execute(select(NewsSource).where(NewsSource.active == True))).scalars().all()  # noqa: E712
    if not sources:
        return {"sources": 0, "inserted": 0, "duration_ms": int((time.perf_counter() - t0) * 1000)}

    inserted = 0
    async with polymarket_async_client() as client:
        for src in sources:
            xml = _EMPTY_RSS_BYTES
            try:
                rss_url = validate_public_https_url(src.rss_url)
                r = await get_with_retry(client, rss_url)
                if r.status_code < 400 and r.content:
                    xml = r.content
            except Exception:
                pass

            items = _parse_rss_safe(xml)

            for it in items[:50]:
                url = (it.get("link") or "").strip()
                if not url:
                    continue

                host = hostname_from_url(url)
                if not hostname_matches_source(host, src.domain):
                    # hard guardrail: only ingest URLs on the source domain or its subdomains
                    continue

                title = (it.get("title") or "").strip()
                body = (it.get("summary") or "").strip()
                published_at = _parse_published(it.get("published_raw") or "")

                content_hash = sha256_hex(f"{title}\n{body}")
                article_id = stable_article_id(url, published_at)

                existing = (await session.execute(select(NewsArticle).where(NewsArticle.id == article_id))).scalar_one_or_none()
                if existing is not None:
                    continue

                # URL is unique in DB; if published_at parsing shifts between polls, article_id changes
                # but the same URL must not be inserted twice.
                existing_url = (
                    await session.execute(select(NewsArticle).where(NewsArticle.url == url))
                ).scalar_one_or_none()
                if existing_url is not None:
                    continue

                session.add(
                    NewsArticle(
                        id=article_id,
                        source_id=src.id,
                        source_domain=src.domain,
                        source_tier=src.source_tier,
                        url=url,
                        title=title,
                        body=body,
                        published_at=published_at,
                        fetched_at=now_utc(),
                        content_hash=content_hash,
                    )
                )
                inserted += 1

        await session.commit()
        duration_ms = int((time.perf_counter() - t0) * 1000)
        logger.info("poll_news done sources=%s inserted=%s duration_ms=%s", len(sources), inserted, duration_ms)
        return {"sources": len(sources), "inserted": inserted, "duration_ms": duration_ms}
