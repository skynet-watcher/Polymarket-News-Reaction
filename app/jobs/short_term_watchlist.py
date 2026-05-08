from __future__ import annotations

import datetime as dt
import asyncio
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog, Market, PriceSnapshot
from app.settings import settings
from app.util import new_id, now_utc
from app.http_client import get_with_retry, polymarket_async_client
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
    "maxAsk": 0.98,
    "maxSpread": 0.15,
    "tradeWindowAfterSignalSeconds": 600,
    "requireSourceConfidence": "MEDIUM_OR_HIGH",
    "paperOnly": True,
}

FETCH_TIMEOUT_SECONDS = 6.0


SIGNAL_LOG_EVENTS = [
    "SIGNAL_FOUND",
    "SIGNAL_REJECTED_LOW_CONFIDENCE",
    "NO_TRADE_PRICE_ALREADY_MOVED",
    "NO_TRADE_NO_ORDERBOOK",
    "NO_TRADE_AMBIGUOUS_RULE",
    "PAPER_TRADE_CREATED",
]


WATCHLIST: list[dict[str, Any]] = [
    {
        "eventSlug": "what-will-be-said-on-the-next-all-in-podcast-may-8",
        "category": "Podcast / media / transcript",
        "expectedClose": "May 8, 2026",
        "sourceModel": "episode content / transcript / credible reporting",
        "sourcesToMonitor": [
            "official All-In Podcast episode",
            "YouTube transcript",
            "podcast RSS feed",
            "X posts from All-In hosts",
            "clips/transcripts from credible sources",
        ],
        "adapter": "PODCAST_TERM_ADAPTER",
        "signalTrigger": "official episode released and a tracked term is said",
        "whyGoodForTesting": "Short-term, many submarkets, requires text/audio interpretation rather than clean data feed.",
        "priority": 1,
        "autoTradeEligible": False,
        "selection": "all_active_binary_markets",
    },
    {
        "eventSlug": "donald-trump-tie-color-on-may-8",
        "category": "Trump / visual reporting",
        "expectedClose": "May 8, 2026",
        "sourceModel": "credible visual reporting",
        "sourcesToMonitor": ["AP photos", "Reuters photos", "Getty images if available", "White House video/photos", "C-SPAN"],
        "adapter": "VISUAL_EVIDENCE_ADAPTER",
        "signalTrigger": "credible photo/video evidence of Trump tie color on May 8",
        "whyGoodForTesting": "Short-term and requires visual/reporting confirmation rather than a structured data feed.",
        "priority": 2,
        "autoTradeEligible": False,
        "selection": "all_active_binary_markets",
    },
    {
        "eventSlug": "will-trump-publicly-insult-someone-on-312",
        "marketSlug": "will-donald-trump-publicly-insult-someone-on-may-8-2026",
        "category": "Trump / public statement / semantic interpretation",
        "expectedClose": "May 8, 2026",
        "sourceModel": "consensus credible reporting",
        "sourcesToMonitor": ["Truth Social @realDonaldTrump", "White House transcripts", "C-SPAN", "Reuters", "AP"],
        "adapter": "PUBLIC_STATEMENT_INTERPRETATION_ADAPTER",
        "signalTrigger": "Trump makes a public statement that clearly insults, mocks, or attacks a real person",
        "whyGoodForTesting": "Good LLM/human-review test: semantic interpretation matters and the rules are not a simple data table.",
        "priority": 3,
        "autoTradeEligible": False,
    },
    {
        "eventSlug": "will-trump-dance-on",
        "marketSlug": "will-donald-trump-dance-on-may-9-2026",
        "category": "Trump / video evidence",
        "expectedClose": "May 9, 2026",
        "sourceModel": "video footage",
        "sourcesToMonitor": ["White House video", "C-SPAN", "AP video", "Reuters video", "major news clips"],
        "adapter": "VIDEO_EVENT_ADAPTER",
        "signalTrigger": "credible video footage shows Trump dancing on May 9",
        "whyGoodForTesting": "Small short-term video/reporting test rather than a data-feed market.",
        "priority": 4,
        "autoTradeEligible": False,
    },
    {
        "eventSlug": "us-x-iran-permanent-peace-deal-by",
        "marketSlug": "us-x-iran-permanent-peace-deal-by-may-8-2026",
        "category": "Geopolitics / diplomacy",
        "expectedClose": "May 8, 2026 11:59 PM ET",
        "sourceModel": "official information plus credible reporting",
        "sourcesToMonitor": ["Reuters", "AP", "BBC", "Al Jazeera", "White House", "US State Department"],
        "adapter": "DIPLOMATIC_AGREEMENT_ADAPTER",
        "signalTrigger": "official or credible reporting of a permanent US-Iran peace deal by the deadline",
        "whyGoodForTesting": "Complex, geopolitical, and reporting-driven, but unlikely to produce a tradeable late signal.",
        "priority": 5,
        "autoTradeEligible": False,
    },
    {
        "eventSlug": "will-trump-visit-china-by",
        "marketSlug": "will-trump-visit-china-by-may-8-157",
        "category": "Trump / travel / diplomacy",
        "expectedClose": "May 8, 2026 11:59 PM ET",
        "sourceModel": "official information plus credible reporting",
        "sourcesToMonitor": ["Reuters", "AP", "White House travel pool", "Chinese Ministry of Foreign Affairs", "BBC"],
        "adapter": "TRAVEL_CONFIRMATION_ADAPTER",
        "signalTrigger": "official or credible reporting that Trump visits China by the deadline",
        "whyGoodForTesting": "Short-deadline political/travel market, but current price may already be settled.",
        "priority": 6,
        "autoTradeEligible": False,
    },
    {
        "eventSlug": "trump-announces-us-blockade-of-hormuz-lifted-by",
        "marketSlug": "will-donald-trump-announce-that-the-united-states-blockade-of-the-strait-of-hormuz-has-been-lifted-by-may-8-2026-522",
        "category": "Trump / geopolitics / official statement",
        "expectedClose": "May 8, 2026 11:59 PM ET",
        "sourceModel": "official public announcement",
        "sourcesToMonitor": ["White House", "Truth Social @realDonaldTrump", "US Department of Defense", "US Central Command", "Reuters", "AP"],
        "adapter": "OFFICIAL_ANNOUNCEMENT_ADAPTER",
        "signalTrigger": "official announcement that the US blockade of the Strait of Hormuz has ended",
        "whyGoodForTesting": "Official-statement driven and short-term, but current odds may imply near-no.",
        "priority": 7,
        "autoTradeEligible": False,
    },
    {
        "eventSlug": "will-the-white-house-call-a-full-lid-by-630-pm-may-4-9",
        "category": "White House / press pool / daily schedule",
        "expectedClose": "May 9, 2026",
        "sourceModel": "press pool / White House reporting",
        "sourcesToMonitor": ["White House press pool reports", "White House daily guidance", "political reporters on X", "Reuters politics", "AP politics"],
        "adapter": "WHITE_HOUSE_POOL_REPORT_ADAPTER",
        "signalTrigger": "credible report that the White House called a full lid by 6:30 PM",
        "whyGoodForTesting": "Useful reporting test, but per-day submarkets need date filtering.",
        "priority": 8,
        "autoTradeEligible": False,
        "marketSlugContainsAny": ["may-8", "may-9"],
    },
    {
        "eventSlug": "kbo-kt-kiw-2026-05-08",
        "category": "Less-watched sports / KBO baseball",
        "expectedClose": "May 8, 2026",
        "sourceModel": "official KBO plus credible reporting fallback",
        "sourcesToMonitor": ["KBO official scoreboard", "ESPN if available", "Flashscore/SofaScore as secondary", "credible sports reporting"],
        "adapter": "LESS_WATCHED_SPORTS_RESULT_ADAPTER",
        "signalTrigger": "final KBO game result",
        "whyGoodForTesting": "Smaller sports market, but skip if price has already moved to 0/1.",
        "priority": 9,
        "autoTradeEligible": True,
        "selection": "event_slug_market",
    },
    {
        "eventSlug": "pll-atl-cha-2026-05-08",
        "category": "Less-watched sports / PLL lacrosse",
        "expectedClose": "May 8, 2026 at 10:30 PM ET",
        "sourceModel": "official league/event organizer plus credible reporting fallback",
        "sourcesToMonitor": ["Premier Lacrosse League official scoreboard", "PLL official X account", "ESPN or broadcast scoreboard if available", "credible sports reporting"],
        "adapter": "LESS_WATCHED_SPORTS_RESULT_ADAPTER",
        "signalTrigger": "final PLL game result",
        "whyGoodForTesting": "Later-night smaller sports event, likely the cleanest auto-paper-trade candidate in this batch.",
        "priority": 10,
        "autoTradeEligible": True,
        "selection": "event_slug_market",
    },
]


def _to_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_binary(outcomes: list[str]) -> bool:
    return len(outcomes) == 2


async def _fetch_event(client, slug: str) -> dict[str, Any]:
    url = f"{settings.polymarket_gamma_base_url}/events/slug/{slug}"
    r = await get_with_retry(client, url, max_retries=0)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, dict):
        raise ValueError(f"Gamma event response for {slug} was not an object")
    return data


async def _fetch_events_by_slug() -> dict[str, Any]:
    async with polymarket_async_client() as client:
        pairs = await asyncio.gather(
            *(asyncio.wait_for(_fetch_event(client, str(config["eventSlug"])), timeout=FETCH_TIMEOUT_SECONDS) for config in WATCHLIST),
            return_exceptions=True,
        )
    return {str(config["eventSlug"]): result for config, result in zip(WATCHLIST, pairs)}


def _select_markets(config: dict[str, Any], event: dict[str, Any]) -> list[dict[str, Any]]:
    markets = event.get("markets")
    if not isinstance(markets, list):
        return []
    event_slug = str(event.get("slug") or config["eventSlug"])
    selected: list[dict[str, Any]] = []
    for raw in markets:
        if not isinstance(raw, dict):
            continue
        slug = str(raw.get("slug") or "")
        outcomes = _jsonish_list(raw.get("outcomes") or raw.get("outcome")) or []
        if not _is_binary(outcomes):
            continue
        market_slug = config.get("marketSlug")
        contains_any = config.get("marketSlugContainsAny") or []
        selection = config.get("selection")
        include = False
        if market_slug:
            include = slug == market_slug
        elif contains_any:
            include = any(str(part) in slug for part in contains_any)
        elif selection == "event_slug_market":
            include = slug == event_slug
        else:
            include = bool(raw.get("active", True)) and not bool(raw.get("closed", False))
        if include:
            merged = dict(raw)
            merged.setdefault("eventId", event.get("id"))
            merged.setdefault("eventSlug", event_slug)
            merged.setdefault("eventTitle", event.get("title"))
            merged.setdefault("resolutionSource", event.get("resolutionSource"))
            merged.setdefault("description", event.get("description"))
            selected.append(merged)
    return selected


def _readiness_checks(config: dict[str, Any], raw: dict[str, Any]) -> list[str]:
    checks: list[str] = []
    ask = _to_float(raw.get("bestAsk"))
    spread = _to_float(raw.get("spread"))
    if not _gamma_token_ids(raw) or ask is None:
        checks.append("NO_TRADE_NO_ORDERBOOK")
    if ask is not None and (ask >= float(EXPLORATORY_MODE["maxAsk"]) or ask <= 0.02):
        checks.append("NO_TRADE_PRICE_ALREADY_MOVED")
    if spread is not None and spread > float(EXPLORATORY_MODE["maxSpread"]):
        checks.append("NO_TRADE_AMBIGUOUS_RULE" if not config.get("autoTradeEligible") else "NO_TRADE_PRICE_ALREADY_MOVED")
    if not config.get("autoTradeEligible"):
        checks.append("NO_TRADE_AMBIGUOUS_RULE")
    return list(dict.fromkeys(checks))


def _test_fit(config: dict[str, Any], raw: dict[str, Any], checks: list[str]) -> dict[str, Any]:
    score = 100
    notes: list[str] = []
    adapter = str(config.get("adapter") or "")
    if adapter in {"PODCAST_TERM_ADAPTER", "LESS_WATCHED_SPORTS_RESULT_ADAPTER"}:
        score += 10
    if not config.get("autoTradeEligible"):
        score -= 20
        notes.append("manual/adapter confirmation required before paper trading")
    if "NO_TRADE_PRICE_ALREADY_MOVED" in checks:
        score -= 55
        notes.append("current price is already near a boundary or beyond test threshold")
    if "NO_TRADE_NO_ORDERBOOK" in checks:
        score -= 40
        notes.append("missing executable top-of-book or token mapping")
    if "NO_TRADE_AMBIGUOUS_RULE" in checks:
        score -= 15
        notes.append("rule/source interpretation is ambiguous enough to log-only by default")
    spread = _to_float(raw.get("spread"))
    if spread is not None and spread > float(EXPLORATORY_MODE["maxSpread"]):
        notes.append("spread is too wide for tonight's exploratory threshold")
    return {"score": max(0, min(100, score)), "notes": notes}


async def run(session: AsyncSession) -> dict[str, Any]:
    now = now_utc()
    events_out: list[dict[str, Any]] = []
    markets_seen = 0
    markets_upserted = 0
    snapshots_created = 0
    audit_rows = 0
    fetched_events = await _fetch_events_by_slug()

    for config in WATCHLIST:
        event_slug = str(config["eventSlug"])
        event_result = fetched_events.get(event_slug)
        if isinstance(event_result, Exception):
            events_out.append({"eventSlug": event_slug, "ok": False, "error": f"{type(event_result).__name__}: {event_result}", "markets": []})
            continue
        if not isinstance(event_result, dict):
            events_out.append({"eventSlug": event_slug, "ok": False, "error": "missing_event_response", "markets": []})
            continue
        event = event_result
        selected = _select_markets(config, event)

        event_markets: list[dict[str, Any]] = []
        for raw in selected:
            market_id = str(raw.get("id") or "").strip()
            question = str(raw.get("question") or raw.get("title") or "").strip()
            outcomes = _jsonish_list(raw.get("outcomes") or raw.get("outcome")) or []
            if not market_id or not question or not outcomes:
                continue
            markets_seen += 1
            token_ids = _gamma_token_ids(raw)
            best_bid = _to_float(raw.get("bestBid"))
            best_ask = _to_float(raw.get("bestAsk"))
            mid = ((best_bid + best_ask) / 2.0) if best_bid is not None and best_ask is not None else None
            spread = _to_float(raw.get("spread"))
            if spread is None and best_bid is not None and best_ask is not None:
                spread = best_ask - best_bid
            await _upsert_market(
                session,
                raw=raw,
                config=config,
                market_id=market_id,
                question=question,
                outcomes=outcomes,
                token_ids=token_ids,
                best_bid=best_bid,
                best_ask=best_ask,
                mid=mid,
            )
            markets_upserted += 1
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
                snapshots_created += 1

            checks = _readiness_checks(config, raw)
            fit = _test_fit(config, raw, checks)
            audit_payload = {
                "event": "SIGNAL_FOUND",
                "eventSlug": config["eventSlug"],
                "marketSlug": raw.get("slug"),
                "adapter": config.get("adapter"),
                "checks": checks,
                "testFit": fit,
            }
            session.add(
                AuditLog(
                    id=new_id("audit"),
                    event_type="WATCHLIST_SETUP_CHECK",
                    market_id=market_id,
                    signal_id=None,
                    payload_json=audit_payload,
                )
            )
            audit_rows += 1

            event_markets.append(
                {
                    "id": market_id,
                    "conditionId": raw.get("conditionId"),
                    "slug": raw.get("slug"),
                    "question": question,
                    "outcomes": outcomes,
                    "tokenIds": token_ids or [],
                    "bestBid": best_bid,
                    "bestAsk": best_ask,
                    "spread": spread,
                    "active": bool(raw.get("active", True)),
                    "closed": bool(raw.get("closed", False)),
                    "endDate": raw.get("endDate") or raw.get("end_date"),
                    "resolutionSource": _gamma_resolution_source(raw),
                    "checks": checks,
                    "testFit": fit,
                    "autoTradeEligible": bool(config.get("autoTradeEligible")),
                }
            )

        events_out.append(
            {
                "ok": True,
                "eventId": event.get("id"),
                "eventSlug": event.get("slug") or event_slug,
                "title": event.get("title"),
                "priority": config.get("priority"),
                "adapter": config.get("adapter"),
                "category": config.get("category"),
                "expectedClose": config.get("expectedClose"),
                "sourceModel": config.get("sourceModel"),
                "sourcesToMonitor": config.get("sourcesToMonitor") or [],
                "whyGoodForTesting": config.get("whyGoodForTesting"),
                "markets": event_markets,
            }
        )

    await session.commit()
    return {
        "ok": True,
        "events_configured": len(WATCHLIST),
        "events_fetched": sum(1 for e in events_out if e.get("ok")),
        "markets_seen": markets_seen,
        "markets_upserted": markets_upserted,
        "snapshots_created": snapshots_created,
        "audit_rows": audit_rows,
        "exploratory_mode": EXPLORATORY_MODE,
        "signal_log_events": SIGNAL_LOG_EVENTS,
        "events": events_out,
    }


async def _upsert_market(
    session: AsyncSession,
    *,
    raw: dict[str, Any],
    config: dict[str, Any],
    market_id: str,
    question: str,
    outcomes: list[str],
    token_ids: Optional[list[str]],
    best_bid: Optional[float],
    best_ask: Optional[float],
    mid: Optional[float],
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
        "category": config.get("category") or raw.get("category"),
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
        "market_type": "SHORT_TERM_WATCHLIST",
        "is_fixture": False,
    }
    if existing is None:
        session.add(Market(id=market_id, **values))
        return
    for key, value in values.items():
        if value is not None or key in {"active", "closed", "enable_orderbook", "is_fixture"}:
            setattr(existing, key, value)
