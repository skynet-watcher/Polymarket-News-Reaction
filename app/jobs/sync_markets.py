from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.http_client import get_with_retry, polymarket_async_client
from app.models import Market, PaperTrade, PriceSnapshot, RuntimeSetting
from app.settings import settings
from app.util import new_id, now_utc

logger = logging.getLogger(__name__)

# One sync at a time: background snapshot loop + POST /api/jobs/sync_markets share SQLite.
_sync_markets_lock = asyncio.Lock()

RUNTIME_KEY_SYNC_MARKETS_SOURCE = "sync_markets_data_source"


def _on_vercel() -> bool:
    return bool(os.environ.get("VERCEL"))


def _is_binary(outcomes: list[str]) -> bool:
    if len(outcomes) != 2:
        return False
    norm = {o.strip().lower() for o in outcomes}
    return norm == {"yes", "no"} or len(norm) == 2


def _jsonish_list(value: Any) -> Optional[list[str]]:
    """Gamma often returns fields like clobTokenIds/outcomes as JSON strings."""
    if value is None:
        return None
    raw = value
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        try:
            raw = json.loads(s)
        except json.JSONDecodeError:
            raw = [item.strip() for item in s.split(",") if item.strip()]
    if not isinstance(raw, list):
        return None
    out = [str(item).strip() for item in raw if str(item).strip()]
    return out or None


def _gamma_token_ids(rm: dict[str, Any]) -> Optional[list[str]]:
    return _jsonish_list(rm.get("clobTokenIds") or rm.get("tokenIds") or rm.get("token_ids"))


def _normalize_events_payload(batch: Any) -> list[dict[str, Any]]:
    if isinstance(batch, list):
        return [x for x in batch if isinstance(x, dict)]
    if isinstance(batch, dict):
        for key in ("events", "data", "results"):
            v = batch.get(key)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
    return []


def _flatten_event_markets(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach event-level metadata to each nested market row."""
    out: list[dict[str, Any]] = []
    for ev in events:
        eid = str(ev.get("id") or "").strip()
        markets = ev.get("markets")
        if markets is None:
            continue
        if isinstance(markets, str):
            continue
        if not isinstance(markets, list):
            continue
        ev_resolution = ev.get("resolutionSource") or ev.get("resolution_source")
        ev_rules = ev.get("rules") or ev.get("description")
        for m in markets:
            if not isinstance(m, dict):
                continue
            mid = str(m.get("id") or m.get("market_id") or "").strip()
            if not mid:
                continue
            merged = dict(m)
            merged.setdefault("eventId", eid or merged.get("eventId") or merged.get("event_id"))
            if ev_resolution and not merged.get("resolutionSource") and not merged.get("resolution_source"):
                merged["resolutionSource"] = ev_resolution
            if ev_rules and not merged.get("rules"):
                merged["rules"] = ev_rules
            out.append(merged)
    return out


async def _fetch_gamma_events(
    client: httpx.AsyncClient,
    limit: int = 100,
    *,
    extra_params: Optional[Dict[str, str]] = None,
) -> list[dict[str, Any]]:
    url = f"{settings.polymarket_gamma_base_url}/events"
    events: list[dict[str, Any]] = []
    offset = 0
    extra_params = extra_params or {}
    max_offset = 0 if _on_vercel() else 2000
    while True:
        params: Dict[str, Any] = {"limit": limit, "offset": offset, **extra_params}
        r = await get_with_retry(client, url, params=params)
        r.raise_for_status()
        batch = _normalize_events_payload(r.json())
        events.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
        if offset >= max_offset:
            break
    return events


async def _fetch_gamma_markets(
    client: httpx.AsyncClient,
    limit: int = 200,
    *,
    extra_params: Optional[Dict[str, str]] = None,
) -> list[dict[str, Any]]:
    """Paginate Gamma /markets with caller-supplied filters (fallback path)."""
    markets: list[dict[str, Any]] = []
    offset = 0
    extra_params = extra_params or {}
    url = f"{settings.polymarket_gamma_base_url}/markets"
    max_offset = 0 if _on_vercel() else 2000
    while True:
        params: Dict[str, Any] = {"limit": limit, "offset": offset, **extra_params}
        r = await get_with_retry(client, url, params=params)
        r.raise_for_status()
        batch = r.json()
        if not isinstance(batch, list):
            break
        markets.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
        if offset >= max_offset:
            break
    return markets


async def _fetch_gamma_open_and_closed_via_events(client: httpx.AsyncClient, limit: int = 100) -> list[dict[str, Any]]:
    open_ev = await _fetch_gamma_events(
        client,
        limit,
        extra_params={
            "active": "true",
            "closed": "false",
            "order": "volume_24hr",
            "ascending": "false",
        },
    )
    closed_ev = await _fetch_gamma_events(
        client,
        limit,
        extra_params={
            "closed": "true",
            "order": "volume_24hr",
            "ascending": "false",
        },
    )
    by_id: dict[str, dict[str, Any]] = {}
    for m in _flatten_event_markets(open_ev):
        mid = str(m.get("id") or m.get("market_id") or "").strip()
        if mid:
            by_id[mid] = m
    for m in _flatten_event_markets(closed_ev):
        mid = str(m.get("id") or m.get("market_id") or "").strip()
        if mid:
            by_id[mid] = m
    return list(by_id.values())


async def _fetch_gamma_open_and_closed_markets_fallback(client: httpx.AsyncClient, limit: int = 200) -> list[dict[str, Any]]:
    open_rows = await _fetch_gamma_markets(
        client, limit, extra_params={"active": "true", "closed": "false"}
    )
    closed_rows = await _fetch_gamma_markets(client, limit, extra_params={"closed": "true"})
    by_id: dict[str, dict[str, Any]] = {}
    for rm in open_rows:
        mid = str(rm.get("id") or rm.get("market_id") or "").strip()
        if mid:
            by_id[mid] = rm
    for rm in closed_rows:
        mid = str(rm.get("id") or rm.get("market_id") or "").strip()
        if mid:
            by_id[mid] = rm
    return list(by_id.values())


async def _fetch_near_resolution_markets(
    client: httpx.AsyncClient, within_hours: int = 48, limit: int = 50
) -> list[dict[str, Any]]:
    """
    Fetch active markets whose end_date falls within the next `within_hours` hours.

    These are often thinly-traded but high-velocity: price can move large and fast
    right before resolution. They rank low in liquidity-sorted fetches and would
    otherwise be missed by the main universe sweep.
    """
    now = dt.datetime.now(dt.timezone.utc)
    end_before = now + dt.timedelta(hours=within_hours)
    try:
        return await _fetch_gamma_markets(
            client,
            limit,
            extra_params={
                "active": "true",
                "closed": "false",
                "end_date_min": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end_date_max": end_before.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        )
    except (httpx.HTTPError, httpx.RequestError):
        return []


async def _fetch_all_markets_unified(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """
    Always runs BOTH fetch paths and merges results.

    /events   → volume-sorted, includes resolutionSource / rules metadata,
                but does NOT return tokenIds (needed for CLOB price fetching).
    /markets  → includes tokenIds; used to fill that gap.

    Merge strategy:
      1. Seed from /events (richer metadata, good ordering).
      2. Overwrite/supplement with /markets data — adds tokenIds to existing
         entries and picks up any markets the events path missed.
      3. Union near-resolution sweep (markets ending within 48h) on top.
    """
    events_exc: Optional[Exception] = None
    markets_exc: Optional[Exception] = None

    try:
        from_events = await _fetch_gamma_open_and_closed_via_events(client, limit=25 if _on_vercel() else 100)
    except (httpx.HTTPError, httpx.RequestError) as e:
        from_events = []
        events_exc = e

    try:
        from_markets = await _fetch_gamma_open_and_closed_markets_fallback(client, limit=50 if _on_vercel() else 200)
    except (httpx.HTTPError, httpx.RequestError) as e:
        from_markets = []
        markets_exc = e

    # If BOTH primary paths failed, re-raise so the caller can trigger fixture fallback.
    if events_exc is not None and markets_exc is not None:
        raise markets_exc

    # Near-resolution sweep is optional — never blocks on failure.
    near_resolution = await _fetch_near_resolution_markets(client, within_hours=48, limit=25 if _on_vercel() else 50)

    by_id: dict[str, dict[str, Any]] = {}

    # Seed with events data (metadata-rich).
    for rm in from_events:
        mid = str(rm.get("id") or rm.get("market_id") or "").strip()
        if mid:
            by_id[mid] = rm

    # Merge /markets: adds tokenIds; for known markets, patch rather than replace
    # so we keep the richer events metadata (resolutionSource, rules, etc.).
    for rm in from_markets:
        mid = str(rm.get("id") or rm.get("market_id") or "").strip()
        if not mid:
            continue
        if mid in by_id:
            existing = by_id[mid]
            # Patch in CLOB token IDs if the events entry is missing them.
            if _gamma_token_ids(existing) is None:
                token_ids = _gamma_token_ids(rm)
                if token_ids is not None:
                    existing["clobTokenIds"] = token_ids
            # Patch in any other fields the events path left empty.
            for key in ("category", "slug", "description"):
                if not existing.get(key) and rm.get(key):
                    existing[key] = rm[key]
        else:
            by_id[mid] = rm

    # Near-resolution entries overwrite with the freshest data.
    for rm in near_resolution:
        mid = str(rm.get("id") or rm.get("market_id") or "").strip()
        if mid:
            by_id[mid] = rm

    return list(by_id.values())


async def _fetch_best_bid_ask_yes(
    client: httpx.AsyncClient, token_id_yes: Optional[str]
) -> Tuple[Optional[float], Optional[float]]:
    if not token_id_yes:
        return (None, None)
    for path in ("/book", "/orderbook"):
        try:
            url = f"{settings.polymarket_clob_base_url}{path}"
            r = await client.get(
                url,
                params={"token_id": token_id_yes},
                timeout=settings.clob_orderbook_timeout_seconds,
            )
            if r.status_code >= 400:
                continue
            data = r.json()
            if isinstance(data, dict):
                return parse_clob_best_prices(data)
        except Exception:
            continue
    return (None, None)


def parse_clob_best_prices(data: dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    bids = data.get("bids") or []
    asks = data.get("asks") or []

    def _price(level: Any) -> Optional[float]:
        try:
            if isinstance(level, (list, tuple)) and level:
                return float(level[0])
            if isinstance(level, dict) and "price" in level:
                return float(level["price"])
        except Exception:
            return None
        return None

    bid_prices = [p for p in (_price(lvl) for lvl in bids) if p is not None]
    ask_prices = [p for p in (_price(lvl) for lvl in asks) if p is not None]

    best_bid = max(bid_prices) if bid_prices else None
    best_ask = min(ask_prices) if ask_prices else None
    return best_bid, best_ask


def _normalize_binary_winner(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return "YES" if raw else "NO"
    s = str(raw).strip()
    if not s:
        return None
    up = s.upper()
    if up in {"YES", "NO"}:
        return up
    low = s.lower()
    if low in {"yes", "y", "true", "1"}:
        return "YES"
    if low in {"no", "n", "false", "0"}:
        return "NO"
    return None


def _str_or_none(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _gamma_resolution_source(rm: dict[str, Any]) -> Optional[str]:
    return _str_or_none(
        rm.get("resolutionSource")
        or rm.get("resolution_source")
        or rm.get("resolutionSources")
    )


def _gamma_rules_text(rm: dict[str, Any]) -> Optional[str]:
    r = rm.get("rules")
    if isinstance(r, str) and r.strip():
        return r.strip()
    if isinstance(r, dict):
        import json

        try:
            return json.dumps(r)[:8000]
        except Exception:
            return None
    return _str_or_none(rm.get("description"))


def _enable_orderbook_flag(rm: dict[str, Any]) -> bool:
    v = rm.get("enableOrderBook")
    if v is None:
        v = rm.get("enable_order_book")
    if v is None:
        return True
    return bool(v)


def _volume_24h(rm: dict[str, Any]) -> Optional[float]:
    raw = rm.get("volume24hr") or rm.get("volume_24hr") or rm.get("volume_24h") or rm.get("volume24Hr")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


async def run(session: AsyncSession) -> dict[str, Any]:
    async with _sync_markets_lock:
        return await _run_sync_markets(session)


async def refresh_open_position_markets(session: AsyncSession) -> dict[str, Any]:
    """
    CLOB-only refresh for markets with OPEN paper trades (full Gamma sync may be on a slower cadence).
    """
    async with _sync_markets_lock:
        return await _refresh_open_position_markets_unlocked(session)


async def _refresh_open_position_markets_unlocked(session: AsyncSession) -> dict[str, Any]:
    rows = (
        await session.execute(select(PaperTrade.market_id).where(PaperTrade.status == "OPEN").distinct())
    ).all()
    mids = [str(r[0]) for r in rows if r[0]]
    if not mids:
        return {"markets": 0, "snapshotted": 0}

    markets = (await session.execute(select(Market).where(Market.id.in_(mids)))).scalars().all()
    snapshotted = 0
    async with polymarket_async_client() as client:
        for m in markets:
            if not m.enable_orderbook:
                continue
            tids = m.token_ids_json
            if not isinstance(tids, list) or not tids:
                continue
            token_ids_yes = tids[0]
            best_bid, best_ask = await _fetch_best_bid_ask_yes(client, str(token_ids_yes))
            mid = None
            if best_bid is not None and best_ask is not None:
                mid = (best_bid + best_ask) / 2.0
            spread = (best_ask - best_bid) if (best_bid is not None and best_ask is not None) else None
            m.best_bid_yes = best_bid
            m.best_ask_yes = best_ask
            m.last_price_yes = mid
            if best_bid is not None or best_ask is not None:
                session.add(
                    PriceSnapshot(
                        id=new_id("snap"),
                        market_id=m.id,
                        timestamp=now_utc(),
                        best_bid_yes=best_bid,
                        best_ask_yes=best_ask,
                        mid_yes=mid,
                        last_price_yes=mid,
                        spread=spread,
                        liquidity=m.liquidity,
                        volume_24h=m.volume_24h,
                    )
                )
                snapshotted += 1

    await session.commit()
    return {"markets": len(markets), "snapshotted": snapshotted}


async def _runtime_set_str(session: AsyncSession, key: str, value: str) -> None:
    row = await session.get(RuntimeSetting, key)
    if row is None:
        session.add(RuntimeSetting(key=key, value=value))
    else:
        row.value = value


async def _run_sync_markets(session: AsyncSession) -> dict[str, Any]:
    markets_source = "live"
    async with polymarket_async_client() as client:
        try:
            raw_markets = await _fetch_all_markets_unified(client)
        except httpx.HTTPError as exc:
            # Gamma/CLOB may return 403/5xx or reset connection; fixture keeps the app usable offline.
            markets_source = "fixture"
            logger.warning(
                "Polymarket API unreachable (%s); using offline fixture markets only. "
                "Candidate matching will look synthetic until a sync succeeds against the real API.",
                type(exc).__name__,
            )
            raw_markets = _load_fixture_markets()

        upserted = 0
        snapshotted = 0
        clob_attempted = 0
        clob_skipped = 0
        is_fixture_market = markets_source == "fixture"

        for rm in raw_markets:
            market_id = str(rm.get("id") or rm.get("market_id") or "").strip()
            question = (rm.get("question") or rm.get("title") or "").strip()
            if not market_id or not question:
                continue

            outcomes = _jsonish_list(rm.get("outcomes") or rm.get("outcome"))
            if not outcomes:
                continue

            if not _is_binary(outcomes):
                continue

            active = bool(rm.get("active", True))
            closed = bool(rm.get("closed", False))

            liquidity = rm.get("liquidity")
            volume = rm.get("volume")
            volume_24h = _volume_24h(rm)

            resolution_source_text = _gamma_resolution_source(rm)
            rules_text = _gamma_rules_text(rm)
            enable_ob = _enable_orderbook_flag(rm)

            token_ids = _gamma_token_ids(rm)
            token_ids_yes = token_ids[0] if isinstance(token_ids, list) and token_ids else None

            best_bid: Optional[float] = None
            best_ask: Optional[float] = None
            clob_limit = min(settings.sync_clob_snapshot_limit, 10) if _on_vercel() else settings.sync_clob_snapshot_limit
            if enable_ob and token_ids_yes and clob_attempted < clob_limit:
                clob_attempted += 1
                best_bid, best_ask = await _fetch_best_bid_ask_yes(client, token_ids_yes)
            elif enable_ob and token_ids_yes:
                clob_skipped += 1
            mid = None
            if best_bid is not None and best_ask is not None:
                mid = (best_bid + best_ask) / 2.0
            spread = (best_ask - best_bid) if (best_bid is not None and best_ask is not None) else None

            winner_raw = rm.get("winner") or rm.get("resolved_outcome") or rm.get("resolvedOutcome")
            winning_outcome = _normalize_binary_winner(winner_raw)

            existing = (await session.execute(select(Market).where(Market.id == market_id))).scalar_one_or_none()
            if existing is None:
                m = Market(
                    id=market_id,
                    event_id=str(rm.get("eventId") or rm.get("event_id") or "") or None,
                    slug=rm.get("slug"),
                    question=question,
                    description=rm.get("description"),
                    category=rm.get("category"),
                    outcomes_json=outcomes,
                    token_ids_json=token_ids if isinstance(token_ids, list) else None,
                    active=active,
                    closed=closed,
                    end_date=_parse_dt(rm.get("endDate") or rm.get("end_date")),
                    liquidity=float(liquidity) if liquidity is not None else None,
                    volume=float(volume) if volume is not None else None,
                    volume_24h=volume_24h,
                    best_bid_yes=best_bid,
                    best_ask_yes=best_ask,
                    last_price_yes=mid,
                    winning_outcome=winning_outcome,
                    resolution_source_text=resolution_source_text,
                    rules_text=rules_text,
                    enable_orderbook=enable_ob,
                    is_fixture=is_fixture_market,
                )
                session.add(m)
                upserted += 1
            else:
                existing.event_id = str(rm.get("eventId") or rm.get("event_id") or "") or existing.event_id
                existing.slug = rm.get("slug") or existing.slug
                existing.question = question
                existing.description = rm.get("description") or existing.description
                existing.category = rm.get("category") or existing.category
                existing.outcomes_json = outcomes
                existing.token_ids_json = token_ids if isinstance(token_ids, list) else existing.token_ids_json
                existing.active = active
                existing.closed = closed
                existing.end_date = _parse_dt(rm.get("endDate") or rm.get("end_date")) or existing.end_date
                existing.liquidity = float(liquidity) if liquidity is not None else existing.liquidity
                existing.volume = float(volume) if volume is not None else existing.volume
                existing.volume_24h = volume_24h if volume_24h is not None else existing.volume_24h
                existing.best_bid_yes = best_bid
                existing.best_ask_yes = best_ask
                existing.last_price_yes = mid
                if winning_outcome is not None:
                    existing.winning_outcome = winning_outcome
                if resolution_source_text is not None:
                    existing.resolution_source_text = resolution_source_text
                if rules_text is not None:
                    existing.rules_text = rules_text
                existing.enable_orderbook = enable_ob
                existing.is_fixture = is_fixture_market
                upserted += 1

            if enable_ob and (best_bid is not None or best_ask is not None):
                snap = PriceSnapshot(
                    id=new_id("snap"),
                    market_id=market_id,
                    timestamp=now_utc(),
                    best_bid_yes=best_bid,
                    best_ask_yes=best_ask,
                    mid_yes=mid,
                    last_price_yes=mid,
                    spread=spread,
                    liquidity=float(liquidity) if liquidity is not None else None,
                    volume_24h=float(volume_24h) if volume_24h is not None else None,
                )
                session.add(snap)
                snapshotted += 1

        await _runtime_set_str(session, RUNTIME_KEY_SYNC_MARKETS_SOURCE, markets_source)
        await session.commit()
        return {
            "upserted": upserted,
            "snapshotted": snapshotted,
            "clob_attempted": clob_attempted,
            "clob_skipped": clob_skipped,
            "fetched": len(raw_markets),
            "markets_source": markets_source,
        }


def _parse_dt(value: Any) -> Optional[dt.datetime]:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    if isinstance(value, (int, float)):
        try:
            return dt.datetime.fromtimestamp(float(value), tz=dt.timezone.utc)
        except Exception:
            return None
    if isinstance(value, str):
        s = value.strip()
        try:
            d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
        except Exception:
            return None
    return None


def _load_fixture_markets() -> list[dict[str, Any]]:
    import json
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "fixtures" / "polymarket_markets.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
