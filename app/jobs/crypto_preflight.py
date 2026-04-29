"""
Crypto Market Preflight Scanner.

Fetches active Polymarket crypto markets from Gamma, classifies each by rule
family, parses structured fields for CRYPTO_INTRAPERIOD_UP_DOWN markets,
verifies the Binance kline, checks YES/NO orderbooks, and stores everything
in CryptoMarketProfile for display in the UI.

POST /api/jobs/crypto_preflight?market_limit=25
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import math
import re
from typing import Any, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.http_client import fetch_clob_orderbook, polymarket_async_client
from app.models import CryptoMarketProfile
from app.settings import settings
from app.util import new_id, now_utc

logger = logging.getLogger(__name__)

_GAMMA_BASE = settings.polymarket_gamma_base_url
_BINANCE_BASE = "https://api.binance.com"

# ── Asset aliases ──────────────────────────────────────────────────────────────
_ASSET_PATTERNS: dict[str, list[str]] = {
    "BTC":  ["bitcoin", r"\bbtc\b"],
    "ETH":  ["ethereum", r"\beth\b"],
    "SOL":  ["solana", r"\bsol\b"],
    "BNB":  [r"\bbnb\b", "binance coin"],
    "XRP":  [r"\bxrp\b", "ripple"],
    "DOGE": ["dogecoin", r"\bdoge\b"],
    "ADA":  ["cardano", r"\bada\b"],
    "AVAX": ["avalanche", r"\bavax\b"],
    "LINK": ["chainlink", r"\blink\b"],
    "MATIC": ["polygon", r"\bmatic\b"],
    "DOT":  ["polkadot", r"\bdot\b"],
    "LTC":  ["litecoin", r"\bltc\b"],
}

# ── Interval aliases (label → seconds) ────────────────────────────────────────
_INTERVAL_MAP: dict[str, tuple[str, int]] = {
    r"1[\s\-]?min(?:ute)?s?|\b1m\b":  ("1m",  60),
    r"5[\s\-]?min(?:ute)?s?|\b5m\b":  ("5m",  300),
    r"15[\s\-]?min(?:ute)?s?|\b15m\b": ("15m", 900),
    r"30[\s\-]?min(?:ute)?s?|\b30m\b": ("30m", 1800),
    r"1[\s\-]?hour?|hourly|\b1h\b":   ("1h",  3600),
    r"4[\s\-]?hour?|\b4h\b":          ("4h",  14400),
    r"daily|1[\s\-]?day|\b1d\b":      ("1d",  86400),
}

# ── Up/Down signal words ───────────────────────────────────────────────────────
_UP_DOWN_RE = re.compile(
    r"up\s+or\s+down|higher\s+or\s+lower|close[sd]?\s+up|close[sd]?\s+down|up\/down|"
    r"be\s+up|go\s+up|end\s+up|finish\s+(?:up|higher)|pump\s+or\s+dump",
    re.I,
)
_DAILY_COMP_RE = re.compile(r"higher\s+than\s+yesterday|yesterday.{0,15}close|previous\s+day", re.I)
_PRICE_THRESH_RE = re.compile(r"above\s+\$[\d,]+|below\s+\$[\d,]+|reach\s+\$[\d,]+|hit\s+\$[\d,]+", re.I)
_HIGH_LOW_RE = re.compile(r"all[\s\-]?time\s+high|\bath\b|new\s+high|highest\s+ever", re.I)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _jsonish_list(value: Any) -> Optional[list[str]]:
    if value is None:
        return None
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
        except json.JSONDecodeError:
            pass
        return [item.strip() for item in s.split(",") if item.strip()]
    return None


def _detect_asset(text: str) -> tuple[Optional[str], float]:
    """Return (base_asset, confidence). Higher confidence = more specific match."""
    text_lower = text.lower()
    for asset, patterns in _ASSET_PATTERNS.items():
        for pat in patterns:
            if re.search(pat, text_lower, re.I):
                conf = 0.95 if r"\b" in pat else 0.8
                return asset, conf
    return None, 0.0


def _detect_interval(text: str) -> tuple[Optional[str], int, float]:
    """Return (label, seconds, confidence)."""
    for pattern, (label, secs) in _INTERVAL_MAP.items():
        if re.search(pattern, text, re.I):
            return label, secs, 0.9
    return None, 0, 0.0


def _classify(title: str, rule_text: str, resolution_source: str) -> tuple[str, float]:
    """Return (rule_family, confidence)."""
    combined = f"{title} {rule_text} {resolution_source}".lower()

    # Strongest signal: explicit up/down + crypto asset + interval
    asset, _ = _detect_asset(combined)
    interval, _, _ = _detect_interval(combined)
    has_up_down = bool(_UP_DOWN_RE.search(combined))

    if has_up_down and asset:
        confidence = 0.7
        if interval:
            confidence += 0.2
        if "binance" in combined:
            confidence += 0.1
        return "CRYPTO_INTRAPERIOD_UP_DOWN", min(confidence, 1.0)

    if _DAILY_COMP_RE.search(combined) and asset:
        return "CRYPTO_DAILY_COMPARISON", 0.75

    if _PRICE_THRESH_RE.search(combined) and asset:
        return "CRYPTO_PRICE_ABOVE_BELOW", 0.75

    if _HIGH_LOW_RE.search(combined) and asset:
        return "CRYPTO_HIT_HIGH_LOW", 0.75

    if asset:
        return "UNKNOWN", 0.3

    return "UNKNOWN", 0.1


def _parse_intraperiod(
    title: str,
    rule_text: str,
    resolution_source: str,
    end_date: Optional[dt.datetime],
    token_ids: Optional[list[str]],
) -> dict[str, Any]:
    """
    Extract structured fields for a CRYPTO_INTRAPERIOD_UP_DOWN market.
    Returns a dict of parsed fields + parser_confidence + parser_notes.
    """
    combined = f"{title} {rule_text} {resolution_source}"
    notes: list[str] = []
    confidence = 0.0

    # Asset
    base_asset, asset_conf = _detect_asset(combined)
    if base_asset:
        confidence += 0.30
    else:
        notes.append("asset not detected")

    # Interval
    interval_label, interval_secs, interval_conf = _detect_interval(combined)
    if interval_label:
        confidence += 0.30
    else:
        notes.append("interval not detected")

    # Candle close time = end_date (Polymarket end_date IS the candle close time)
    # Candle start time = end_date - interval
    candle_close: Optional[dt.datetime] = end_date
    candle_start: Optional[dt.datetime] = None

    if candle_close and interval_secs:
        candle_start = candle_close - dt.timedelta(seconds=interval_secs)
        confidence += 0.25
    else:
        notes.append("candle start time could not be computed — end_date or interval missing")

    # Binance symbol
    binance_symbol: Optional[str] = None
    if base_asset:
        quote = "USDT"
        # check if PERP / futures mentioned
        if re.search(r"perp|future|perpetual|coinm|um_perp", combined, re.I):
            quote = "USDT"  # most Binance perps are USDT-margined
        binance_symbol = f"{base_asset}{quote}"

    # Token IDs — YES is index 0, NO is index 1 (Polymarket convention)
    yes_token_id: Optional[str] = None
    no_token_id: Optional[str] = None
    if token_ids and len(token_ids) >= 2:
        yes_token_id = token_ids[0]
        no_token_id = token_ids[1]
        confidence += 0.15
    elif token_ids and len(token_ids) == 1:
        yes_token_id = token_ids[0]
        notes.append("only one token ID found — NO token unavailable")
        confidence += 0.05
    else:
        notes.append("no token IDs found")

    parser_status = "PARSED" if confidence >= 0.75 else "PARSER_REVIEW_REQUIRED"

    return {
        "base_asset": base_asset,
        "quote_asset": "USDT",
        "binance_symbol": binance_symbol,
        "candle_interval": interval_label,
        "candle_interval_seconds": interval_secs or None,
        "candle_start_time_utc": candle_start,
        "candle_close_time_utc": candle_close,
        "yes_token_id": yes_token_id,
        "no_token_id": no_token_id,
        "parser_confidence": round(confidence, 3),
        "parser_notes": "; ".join(notes) if notes else None,
        "parser_status": parser_status,
    }


async def _fetch_crypto_candidates(
    limit: int,
    include_resolved: bool,
    min_liquidity: Optional[float],
) -> list[dict[str, Any]]:
    """Pull candidate markets from Gamma. Returns raw market dicts."""
    candidates: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    # Keyword searches that should surface Up/Down crypto markets
    keyword_queries = ["up or down", "higher or lower", "up in the next", "crypto up"]

    async with polymarket_async_client() as client:
        # First: crypto category, flat markets endpoint (has tokenIds)
        try:
            params: dict[str, Any] = {
                "category": "crypto",
                "limit": limit,
                "active": "true",
            }
            if not include_resolved:
                params["closed"] = "false"
            if min_liquidity:
                params["liquidity_min"] = min_liquidity

            r = await client.get(f"{_GAMMA_BASE}/markets", params=params)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list):
                    for m in data:
                        mid = str(m.get("id") or "").strip()
                        if mid and mid not in seen_ids:
                            seen_ids.add(mid)
                            candidates.append(m)
        except Exception:
            logger.exception("crypto_preflight: Gamma /markets category=crypto failed")

        # Second: keyword search passes
        for kw in keyword_queries:
            if len(candidates) >= limit * 2:
                break
            try:
                r = await client.get(
                    f"{_GAMMA_BASE}/markets",
                    params={"_c": kw, "limit": min(50, limit), "active": "true"},
                )
                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, list):
                        for m in data:
                            mid = str(m.get("id") or "").strip()
                            if mid and mid not in seen_ids:
                                seen_ids.add(mid)
                                candidates.append(m)
            except Exception:
                logger.warning("crypto_preflight: keyword query '%s' failed", kw)

    logger.info("crypto_preflight: fetched %d candidate markets", len(candidates))
    return candidates[:limit * 3]  # cap before filtering


def _is_crypto_updown_candidate(m: dict[str, Any]) -> bool:
    """Quick heuristic filter before full classification."""
    title = str(m.get("question") or m.get("title") or "").lower()
    slug = str(m.get("slug") or "").lower()
    combined = f"{title} {slug}"
    asset, _ = _detect_asset(combined)
    if not asset:
        return False
    # Must have some binary up/down signal
    return bool(_UP_DOWN_RE.search(combined)) or bool(_DAILY_COMP_RE.search(combined)) or bool(_PRICE_THRESH_RE.search(combined))


async def _verify_binance_kline(
    symbol: str,
    interval: str,
    start_time_utc: dt.datetime,
    now: dt.datetime,
) -> dict[str, Any]:
    """
    Fetch the specific Binance kline and verify openTime matches expected start.
    Returns dict with verified, open_time, open_price, close_price, notes.
    """
    if start_time_utc > now:
        return {"verified": False, "notes": "candle is in the future — cannot verify yet"}

    start_ms = int(start_time_utc.timestamp() * 1000)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{_BINANCE_BASE}/api/v3/klines",
                params={"symbol": symbol, "interval": interval, "startTime": start_ms, "limit": 1},
            )
            r.raise_for_status()
            data = r.json()
            if not data or not isinstance(data, list) or not data[0]:
                return {"verified": False, "notes": "Binance returned empty kline data"}

            kline = data[0]
            open_time_ms = int(kline[0])
            open_price = float(kline[1])
            close_price = float(kline[4])
            kline_open_utc = dt.datetime.fromtimestamp(open_time_ms / 1000, tz=dt.timezone.utc)

            # Allow up to 5-second tolerance for timestamp rounding
            delta_secs = abs((kline_open_utc - start_time_utc).total_seconds())
            if delta_secs <= 5:
                return {
                    "verified": True,
                    "open_time_utc": kline_open_utc,
                    "open_price": open_price,
                    "close_price": close_price,
                    "notes": f"openTime matched within {delta_secs:.1f}s",
                }
            else:
                return {
                    "verified": False,
                    "open_time_utc": kline_open_utc,
                    "open_price": open_price,
                    "close_price": close_price,
                    "notes": f"openTime mismatch: expected {start_time_utc.isoformat()} got {kline_open_utc.isoformat()} (delta={delta_secs:.0f}s)",
                }
    except Exception as e:
        return {"verified": False, "notes": f"Binance kline fetch failed: {e}"}


async def _check_orderbooks(
    yes_token_id: Optional[str],
    no_token_id: Optional[str],
    min_liquidity: float = 500.0,
) -> dict[str, Any]:
    """Check YES and NO orderbooks. Returns usability flags and top-of-book prices."""
    result: dict[str, Any] = {
        "yes_book_usable": False,
        "no_book_usable": False,
        "yes_best_ask": None,
        "no_best_ask": None,
        "yes_liquidity": None,
        "no_liquidity": None,
        "notes": None,
    }
    notes: list[str] = []

    async with polymarket_async_client(timeout=httpx.Timeout(8.0)) as client:
        for side, token_id, key in [
            ("YES", yes_token_id, "yes"),
            ("NO", no_token_id, "no"),
        ]:
            if not token_id:
                notes.append(f"{side} token ID missing")
                continue
            try:
                book = await fetch_clob_orderbook(client, token_id)
                if book is None:
                    notes.append(f"{side} orderbook: no response")
                    continue

                asks = book.get("asks") or []
                bids = book.get("bids") or []

                # Best ask = lowest ask price
                best_ask: Optional[float] = None
                if asks:
                    try:
                        prices = [float(a.get("price", a[0]) if isinstance(a, dict) else a[0]) for a in asks[:5]]
                        best_ask = min(prices)
                    except Exception:
                        pass

                # Rough liquidity = sum of bid sizes
                liquidity: float = 0.0
                for b in bids[:10]:
                    try:
                        sz = float(b.get("size", b[1]) if isinstance(b, dict) else b[1])
                        px = float(b.get("price", b[0]) if isinstance(b, dict) else b[0])
                        liquidity += sz * px
                    except Exception:
                        pass

                usable = best_ask is not None and liquidity >= min_liquidity
                result[f"{key}_book_usable"] = usable
                result[f"{key}_best_ask"] = best_ask
                result[f"{key}_liquidity"] = round(liquidity, 2)
                if not usable:
                    notes.append(f"{side}: ask={best_ask} liq=${liquidity:.0f} (below ${min_liquidity:.0f} threshold)")

            except Exception as e:
                notes.append(f"{side} book error: {e}")

    result["notes"] = "; ".join(notes) if notes else None
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

async def run(
    market_limit: int = 25,
    include_resolved: bool = False,
    min_liquidity: Optional[float] = None,
) -> dict[str, Any]:
    """
    Run the full preflight scan. Upserts CryptoMarketProfile rows.
    Returns a summary dict for the API response.
    """
    now = now_utc()
    market_limit = max(1, min(100, market_limit))

    # 1. Fetch candidates
    raw_candidates = await _fetch_crypto_candidates(market_limit, include_resolved, min_liquidity)

    # 2. Filter to likely Up/Down crypto markets
    filtered = [m for m in raw_candidates if _is_crypto_updown_candidate(m)]
    logger.info("crypto_preflight: %d candidates → %d after Up/Down filter", len(raw_candidates), len(filtered))

    # Cap to requested limit
    filtered = filtered[:market_limit]

    summary = {
        "total_fetched": len(raw_candidates),
        "total_processed": len(filtered),
        "ready": 0,
        "parser_review_required": 0,
        "unsupported": 0,
        "unknown": 0,
        "binance_verified": 0,
        "both_books_usable": 0,
        "markets": [],
    }

    async with SessionLocal() as session:
        for raw in filtered:
            try:
                profile_data = await _process_one(raw, now, min_liquidity)
                await _upsert_profile(session, profile_data, now)

                status = profile_data.get("monitor_status", "UNKNOWN")
                if status == "READY":
                    summary["ready"] += 1
                elif status == "PARSER_REVIEW_REQUIRED":
                    summary["parser_review_required"] += 1
                elif status == "UNSUPPORTED":
                    summary["unsupported"] += 1
                else:
                    summary["unknown"] += 1

                if profile_data.get("binance_verified"):
                    summary["binance_verified"] += 1
                if profile_data.get("yes_book_usable") and profile_data.get("no_book_usable"):
                    summary["both_books_usable"] += 1

                summary["markets"].append({
                    "market_id": profile_data["market_id"],
                    "title": profile_data["title"][:80],
                    "rule_family": profile_data.get("rule_family", "UNKNOWN"),
                    "binance_symbol": profile_data.get("binance_symbol"),
                    "candle_interval": profile_data.get("candle_interval"),
                    "monitor_status": status,
                    "parser_confidence": profile_data.get("parser_confidence"),
                })
            except Exception:
                logger.exception("crypto_preflight: failed processing market %s", raw.get("id"))

        await session.commit()

    logger.info(
        "crypto_preflight done: processed=%d ready=%d review=%d",
        summary["total_processed"], summary["ready"], summary["parser_review_required"],
    )
    return summary


async def _process_one(raw: dict[str, Any], now: dt.datetime, min_liquidity: Optional[float]) -> dict[str, Any]:
    """Process one raw Gamma market dict → profile data dict."""
    market_id = str(raw.get("id") or "").strip()
    title = str(raw.get("question") or raw.get("title") or "").strip()
    slug = str(raw.get("slug") or "").strip()
    rule_text = str(raw.get("description") or raw.get("rules") or "").strip()
    resolution_source = str(raw.get("resolutionSource") or raw.get("resolution_source") or "").strip()
    token_ids = _jsonish_list(raw.get("clobTokenIds") or raw.get("tokenIds") or raw.get("token_ids"))
    outcomes = _jsonish_list(raw.get("outcomes"))

    # Parse end_date
    end_date: Optional[dt.datetime] = None
    raw_end = raw.get("endDate") or raw.get("end_date")
    if raw_end:
        try:
            end_date = dt.datetime.fromisoformat(str(raw_end).replace("Z", "+00:00"))
        except Exception:
            pass

    # Classify
    rule_family, class_conf = _classify(title, rule_text, resolution_source)

    profile: dict[str, Any] = {
        "market_id": market_id,
        "slug": slug or None,
        "title": title,
        "rule_text": rule_text or None,
        "resolution_source_text": resolution_source or None,
        "end_date": end_date,
        "outcomes_json": outcomes,
        "token_ids_json": token_ids,
        "raw_gamma_json": raw,
        "rule_family": rule_family,
        "classification_confidence": class_conf,
        # defaults
        "base_asset": None,
        "quote_asset": None,
        "binance_symbol": None,
        "candle_interval": None,
        "candle_interval_seconds": None,
        "candle_start_time_utc": None,
        "candle_close_time_utc": None,
        "yes_token_id": None,
        "no_token_id": None,
        "parser_confidence": None,
        "parser_notes": None,
        "parser_status": "N_A",
        "binance_verified": False,
        "binance_open_time_utc": None,
        "binance_open_price": None,
        "binance_close_price": None,
        "binance_verification_notes": None,
        "yes_book_usable": False,
        "no_book_usable": False,
        "yes_best_ask": None,
        "no_best_ask": None,
        "yes_liquidity": None,
        "no_liquidity": None,
        "orderbook_notes": None,
        "monitor_status": "UNKNOWN",
        "monitor_ready": False,
    }

    if rule_family != "CRYPTO_INTRAPERIOD_UP_DOWN":
        profile["parser_status"] = "UNSUPPORTED"
        profile["monitor_status"] = "UNSUPPORTED"
        return profile

    # Parse
    parsed = _parse_intraperiod(title, rule_text, resolution_source, end_date, token_ids)
    profile.update(parsed)

    if profile["parser_status"] == "PARSER_REVIEW_REQUIRED":
        profile["monitor_status"] = "PARSER_REVIEW_REQUIRED"
        return profile  # don't check Binance/orderbook for low-confidence parses

    # Binance kline verification
    if profile.get("binance_symbol") and profile.get("candle_interval") and profile.get("candle_start_time_utc"):
        bv = await _verify_binance_kline(
            profile["binance_symbol"],
            profile["candle_interval"],
            profile["candle_start_time_utc"],
            now,
        )
        profile["binance_verified"] = bv.get("verified", False)
        profile["binance_open_time_utc"] = bv.get("open_time_utc")
        profile["binance_open_price"] = bv.get("open_price")
        profile["binance_close_price"] = bv.get("close_price")
        profile["binance_verification_notes"] = bv.get("notes")

        if not profile["binance_verified"] and "future" in (bv.get("notes") or ""):
            profile["monitor_status"] = "FUTURE_CANDLE"
    elif profile.get("candle_start_time_utc") and profile["candle_start_time_utc"] > now:
        profile["binance_verification_notes"] = "candle opens in the future"
        profile["monitor_status"] = "FUTURE_CANDLE"

    # Orderbook check
    ob = await _check_orderbooks(
        profile.get("yes_token_id"),
        profile.get("no_token_id"),
        min_liquidity=min_liquidity or 500.0,
    )
    profile.update(ob)

    # Final readiness decision
    if profile["monitor_status"] not in ("FUTURE_CANDLE", "PARSER_REVIEW_REQUIRED", "UNSUPPORTED"):
        has_binance = profile["binance_verified"] or profile["monitor_status"] == "FUTURE_CANDLE"
        has_books = profile["yes_book_usable"] and profile["no_book_usable"]
        parser_ok = profile.get("parser_confidence", 0.0) >= 0.75

        if parser_ok and has_books:
            profile["monitor_status"] = "READY"
            profile["monitor_ready"] = True
        elif not has_books:
            profile["monitor_status"] = "NO_ORDERBOOK"
        elif not parser_ok:
            profile["monitor_status"] = "PARSER_REVIEW_REQUIRED"

    return profile


async def _upsert_profile(session: AsyncSession, data: dict[str, Any], now: dt.datetime) -> None:
    """Insert or update a CryptoMarketProfile row."""
    market_id = data["market_id"]
    existing = (
        await session.execute(
            select(CryptoMarketProfile).where(CryptoMarketProfile.market_id == market_id)
        )
    ).scalar_one_or_none()

    if existing is not None:
        for k, v in data.items():
            if k != "market_id" and hasattr(existing, k):
                setattr(existing, k, v)
        existing.updated_at = now
    else:
        row = CryptoMarketProfile(id=new_id("cmp"), **data)
        session.add(row)
