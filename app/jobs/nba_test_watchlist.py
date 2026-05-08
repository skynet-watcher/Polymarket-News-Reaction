from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clob_depth import orderbook_levels_from_payload
from app.http_client import fetch_clob_orderbook, get_with_retry, polymarket_async_client
from app.models import Market, PriceSnapshot
from app.settings import settings
from app.util import new_id, now_utc
from app.jobs.sync_markets import (
    _enable_orderbook_flag,
    _gamma_resolution_source,
    _gamma_rules_text,
    _gamma_token_ids,
    _jsonish_list,
    _parse_dt,
    _volume_24h,
)


EXPLORATORY_MODE: dict[str, Any] = {
    "maxFinalAsk": 0.98,
    "maxHalftimeAsk": 0.97,
    "maxSpread": 0.15,
    "tradeWindowAfterSignalSeconds": 300,
    "requireNbaOfficialFinal": True,
    "requireBackupConfirmation": False,
    "paperOnly": True,
}


WATCHLIST: list[dict[str, Any]] = [
    {
        "label": "Knicks vs 76ers full-game moneyline",
        "polymarketEventSlug": "nba-nyk-phi-2026-05-08",
        "nbaGameId": "0042500213",
        "nbaGameUrl": "https://www.nba.com/game/0042500213",
        "priority": 2,
        "trigger": "NBA_FINAL",
        "recommendedMode": "exploratory",
        "marketSlugContains": None,
    },
    {
        "label": "Knicks vs 76ers first-half moneyline",
        "polymarketEventSlug": "nba-nyk-phi-2026-05-08",
        "nbaGameId": "0042500213",
        "nbaGameUrl": "https://www.nba.com/game/0042500213",
        "priority": 1,
        "trigger": "HALFTIME_SCORE",
        "recommendedMode": "exploratory",
        "marketSlugContains": "1h-moneyline",
    },
    {
        "label": "Spurs vs Timberwolves full-game moneyline",
        "polymarketEventSlug": "nba-sas-min-2026-05-08",
        "nbaGameId": "0042500233",
        "nbaGameUrl": "https://www.nba.com/game/sas-vs-min-0042500233",
        "priority": 2,
        "trigger": "NBA_FINAL",
        "recommendedMode": "exploratory",
        "marketSlugContains": None,
    },
    {
        "label": "Spurs vs Timberwolves first-half moneyline",
        "polymarketEventSlug": "nba-sas-min-2026-05-08",
        "nbaGameId": "0042500233",
        "nbaGameUrl": "https://www.nba.com/game/sas-vs-min-0042500233",
        "priority": 1,
        "trigger": "HALFTIME_SCORE",
        "recommendedMode": "exploratory",
        "marketSlugContains": "1h-moneyline",
    },
]


NO_TRADE_REASONS = [
    "NO_TRADE_PRICE_ALREADY_SETTLED",
    "NO_TRADE_NO_ORDERBOOK",
    "NO_TRADE_SPREAD_TOO_WIDE",
    "NO_TRADE_MARKET_NOT_LINKED",
    "NO_TRADE_SCORE_TIED",
    "NO_TRADE_TOO_LATE",
]


def _event_slugs() -> list[str]:
    return sorted({str(item["polymarketEventSlug"]) for item in WATCHLIST})


def _event_meta(slug: str) -> dict[str, Any]:
    item = next((x for x in WATCHLIST if x["polymarketEventSlug"] == slug), {})
    return {
        "nbaGameId": item.get("nbaGameId"),
        "nbaGameUrl": item.get("nbaGameUrl"),
    }


def _to_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_markets(event: dict[str, Any]) -> list[dict[str, Any]]:
    event_slug = str(event.get("slug") or event.get("ticker") or "").strip()
    event_id = str(event.get("id") or "").strip()
    markets = event.get("markets")
    if not isinstance(markets, list):
        return []

    out: list[dict[str, Any]] = []
    for market in markets:
        if not isinstance(market, dict):
            continue
        slug = str(market.get("slug") or "").strip()
        question = str(market.get("question") or market.get("title") or "").strip()
        if not _include_market(event_slug=event_slug, market_slug=slug, question=question):
            continue
        merged = dict(market)
        merged.setdefault("eventId", event_id)
        merged.setdefault("eventSlug", event_slug)
        merged.setdefault("eventTitle", event.get("title"))
        merged.setdefault("resolutionSource", event.get("resolutionSource"))
        merged.setdefault("description", event.get("description"))
        out.append(merged)
    return out


def _include_market(*, event_slug: str, market_slug: str, question: str) -> bool:
    q = question.lower()
    return "1h-moneyline" in market_slug or market_slug == event_slug or "win" in q


def _watchlist_labels(event_slug: str, market_slug: str) -> list[str]:
    labels: list[str] = []
    for item in WATCHLIST:
        if item["polymarketEventSlug"] != event_slug:
            continue
        contains = item.get("marketSlugContains")
        if contains:
            if contains in market_slug:
                labels.append(str(item["label"]))
        elif market_slug == event_slug:
            labels.append(str(item["label"]))
    return labels


def _best_bid_ask_from_book(book: Optional[dict[str, Any]]) -> tuple[Optional[float], Optional[float]]:
    if not book:
        return None, None
    bids, asks = orderbook_levels_from_payload(book)
    best_bid = bids[0][0] if bids else None
    best_ask = asks[0][0] if asks else None
    return best_bid, best_ask


async def _token_books(token_ids: Optional[list[str]]) -> list[dict[str, Any]]:
    if not token_ids:
        return []
    rows: list[dict[str, Any]] = []
    async with polymarket_async_client() as client:
        for token_id in token_ids:
            book = await fetch_clob_orderbook(client, token_id)
            bid, ask = _best_bid_ask_from_book(book)
            rows.append({"token_id": token_id, "best_bid": bid, "best_ask": ask})
    return rows


def _fallback_token_books(token_ids: Optional[list[str]], market: dict[str, Any]) -> list[dict[str, Any]]:
    if not token_ids:
        return []
    rows: list[dict[str, Any]] = []
    for i, token_id in enumerate(token_ids):
        bid = _to_float(market.get("bestBid")) if i == 0 else None
        ask = _to_float(market.get("bestAsk")) if i == 0 else None
        rows.append({"token_id": token_id, "best_bid": bid, "best_ask": ask})
    return rows


def _no_trade_reasons(market: dict[str, Any], token_rows: list[dict[str, Any]], *, trigger: str) -> list[str]:
    reasons: list[str] = []
    max_ask = EXPLORATORY_MODE["maxHalftimeAsk"] if trigger == "HALFTIME_SCORE" else EXPLORATORY_MODE["maxFinalAsk"]
    spread = _to_float(market.get("spread"))
    if not token_rows or all(row.get("best_ask") is None for row in token_rows):
        reasons.append("NO_TRADE_NO_ORDERBOOK")
    if spread is not None and spread > float(EXPLORATORY_MODE["maxSpread"]):
        reasons.append("NO_TRADE_SPREAD_TOO_WIDE")
    if any(row.get("best_ask") is not None and float(row["best_ask"]) > max_ask for row in token_rows):
        reasons.append("NO_TRADE_PRICE_ALREADY_SETTLED")
    if not _gamma_token_ids(market):
        reasons.append("NO_TRADE_MARKET_NOT_LINKED")
    return reasons


async def _fetch_event(slug: str) -> dict[str, Any]:
    async with polymarket_async_client() as client:
        url = f"{settings.polymarket_gamma_base_url}/events/slug/{slug}"
        r = await get_with_retry(client, url)
        r.raise_for_status()
        data = r.json()
    if not isinstance(data, dict):
        raise ValueError(f"Gamma event response for {slug} was not an object")
    return data


async def run(session: AsyncSession, *, fetch_orderbooks: bool = True) -> dict[str, Any]:
    now = now_utc()
    events: list[dict[str, Any]] = []
    markets_seen = 0
    upserted = 0
    snapshotted = 0

    for slug in _event_slugs():
        event = await _fetch_event(slug)
        event_slug = str(event.get("slug") or slug)
        event_meta = _event_meta(event_slug)
        event_markets = _extract_markets(event)
        market_rows: list[dict[str, Any]] = []

        for raw in event_markets:
            markets_seen += 1
            market_id = str(raw.get("id") or "").strip()
            question = str(raw.get("question") or raw.get("title") or "").strip()
            if not market_id or not question:
                continue

            outcomes = _jsonish_list(raw.get("outcomes") or raw.get("outcome")) or []
            token_ids = _gamma_token_ids(raw)
            token_books = await _token_books(token_ids) if fetch_orderbooks else []
            if not token_books:
                token_books = _fallback_token_books(token_ids, raw)

            yes_book = token_books[0] if token_books else {}
            best_bid = yes_book.get("best_bid")
            best_ask = yes_book.get("best_ask")
            mid = ((float(best_bid) + float(best_ask)) / 2.0) if best_bid is not None and best_ask is not None else None
            spread = _to_float(raw.get("spread"))
            if spread is None and best_bid is not None and best_ask is not None:
                spread = float(best_ask) - float(best_bid)

            await _upsert_market(
                session,
                raw=raw,
                market_id=market_id,
                question=question,
                outcomes=outcomes,
                token_ids=token_ids,
                best_bid=best_bid,
                best_ask=best_ask,
                mid=mid,
                event_slug=event_slug,
            )
            upserted += 1

            if best_bid is not None or best_ask is not None:
                session.add(
                    PriceSnapshot(
                        id=new_id("snap"),
                        market_id=market_id,
                        timestamp=now,
                        best_bid_yes=best_bid,
                        best_ask_yes=best_ask,
                        mid_yes=mid,
                        last_price_yes=mid,
                        spread=spread,
                        liquidity=_to_float(raw.get("liquidity") or raw.get("liquidityNum") or raw.get("liquidityClob")),
                        volume_24h=_volume_24h(raw),
                    )
                )
                snapshotted += 1

            trigger = "HALFTIME_SCORE" if "1h-moneyline" in str(raw.get("slug") or "") else "NBA_FINAL"
            market_rows.append(
                {
                    "id": market_id,
                    "conditionId": raw.get("conditionId"),
                    "slug": raw.get("slug"),
                    "question": question,
                    "outcomes": outcomes,
                    "tokenMappings": [
                        {
                            "outcome": outcomes[i] if i < len(outcomes) else f"Outcome {i + 1}",
                            "tokenId": token_id,
                            "bestBid": token_books[i].get("best_bid") if i < len(token_books) else None,
                            "bestAsk": token_books[i].get("best_ask") if i < len(token_books) else None,
                        }
                        for i, token_id in enumerate(token_ids or [])
                    ],
                    "bestBid": best_bid,
                    "bestAsk": best_ask,
                    "spread": spread,
                    "resolutionSource": _gamma_resolution_source(raw),
                    "trigger": trigger,
                    "watchlistLabels": _watchlist_labels(event_slug, str(raw.get("slug") or "")),
                    "noTradeReasons": _no_trade_reasons(raw, token_books, trigger=trigger),
                    "polymarketUrl": f"https://polymarket.com/event/{event_slug}",
                }
            )

        events.append(
            {
                "id": event.get("id"),
                "slug": event_slug,
                "title": event.get("title"),
                "nbaGameId": event_meta.get("nbaGameId"),
                "nbaGameUrl": event_meta.get("nbaGameUrl"),
                "markets": market_rows,
            }
        )

    await session.commit()
    return {
        "ok": True,
        "events_configured": len(events),
        "markets_seen": markets_seen,
        "markets_upserted": upserted,
        "snapshots_created": snapshotted,
        "exploratory_mode": EXPLORATORY_MODE,
        "no_trade_reasons": NO_TRADE_REASONS,
        "events": events,
    }


async def _upsert_market(
    session: AsyncSession,
    *,
    raw: dict[str, Any],
    market_id: str,
    question: str,
    outcomes: list[str],
    token_ids: Optional[list[str]],
    best_bid: Optional[float],
    best_ask: Optional[float],
    mid: Optional[float],
    event_slug: str,
) -> None:
    existing = (await session.execute(select(Market).where(Market.id == market_id))).scalar_one_or_none()
    liquidity = _to_float(raw.get("liquidity") or raw.get("liquidityNum") or raw.get("liquidityClob"))
    volume = _to_float(raw.get("volume") or raw.get("volumeNum") or raw.get("volumeClob"))
    values = {
        "event_id": str(raw.get("eventId") or raw.get("event_id") or "") or None,
        "condition_id": str(raw.get("conditionId") or raw.get("condition_id") or "") or None,
        "slug": raw.get("slug"),
        "question": question,
        "description": raw.get("description"),
        "category": raw.get("category") or "sports",
        "outcomes_json": outcomes,
        "token_ids_json": token_ids if isinstance(token_ids, list) else None,
        "active": bool(raw.get("active", True)),
        "closed": bool(raw.get("closed", False)),
        "end_date": _parse_dt(raw.get("endDate") or raw.get("end_date")),
        "liquidity": liquidity,
        "volume": volume,
        "volume_24h": _volume_24h(raw),
        "best_bid_yes": best_bid,
        "best_ask_yes": best_ask,
        "last_price_yes": mid,
        "resolution_source_text": _gamma_resolution_source(raw),
        "rules_text": _gamma_rules_text(raw),
        "enable_orderbook": _enable_orderbook_flag(raw),
        "market_type": "NBA_TEST_MONEYLINE",
        "is_fixture": False,
    }
    if existing is None:
        session.add(Market(id=market_id, **values))
        return
    for key, value in values.items():
        if value is not None or key in {"active", "closed", "enable_orderbook", "is_fixture"}:
            setattr(existing, key, value)
