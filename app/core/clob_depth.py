from __future__ import annotations

from typing import Any, Optional, Tuple

# Minimum fraction of target size that must be filled from the book, else reject.
MIN_DEPTH_FILL_RATIO = 0.5


def _level_price_size(level: Any) -> Optional[Tuple[float, float]]:
    try:
        if isinstance(level, (list, tuple)) and len(level) >= 2:
            return float(level[0]), float(level[1])
        if isinstance(level, dict):
            if "price" in level and "size" in level:
                return float(level["price"]), float(level["size"])
    except (TypeError, ValueError):
        return None
    return None


def orderbook_levels_from_payload(data: dict[str, Any]) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """Return bids (descending price) and asks (ascending price) as (price, size)."""
    raw_bids = data.get("bids") or []
    raw_asks = data.get("asks") or []
    bids: list[tuple[float, float]] = []
    asks: list[tuple[float, float]] = []
    for lvl in raw_bids:
        ps = _level_price_size(lvl)
        if ps and ps[1] > 0:
            bids.append(ps)
    for lvl in raw_asks:
        ps = _level_price_size(lvl)
        if ps and ps[1] > 0:
            asks.append(ps)
    bids.sort(key=lambda x: -x[0])
    asks.sort(key=lambda x: x[0])
    return bids, asks


def walk_asks_buy(asks_asc: list[tuple[float, float]], target_shares: float) -> tuple[Optional[float], float, dict[str, Any]]:
    """Buy by lifting asks. Returns (vwap_price, filled_shares, meta)."""
    cost = 0.0
    filled = 0.0
    rem = max(0.0, target_shares)
    ladder: list[dict[str, float]] = []
    for price, sz in asks_asc:
        if rem <= 1e-12:
            break
        take = min(rem, sz)
        if take <= 0:
            continue
        cost += take * price
        filled += take
        rem -= take
        ladder.append({"price": price, "size": take})
    if filled <= 0:
        return None, 0.0, {"ladder": [], "partial": True, "reject": "EMPTY_BOOK"}
    vwap = cost / filled
    return vwap, filled, {"ladder": ladder, "partial": rem > 1e-9, "remaining": rem}


def walk_bids_sell(bids_desc: list[tuple[float, float]], target_shares: float) -> tuple[Optional[float], float, dict[str, Any]]:
    """Sell into bids (e.g. YES sell / NO buy proxy). Returns (vwap_sell_price, filled_shares, meta)."""
    proceeds = 0.0
    filled = 0.0
    rem = max(0.0, target_shares)
    ladder: list[dict[str, float]] = []
    for price, sz in bids_desc:
        if rem <= 1e-12:
            break
        take = min(rem, sz)
        if take <= 0:
            continue
        proceeds += take * price
        filled += take
        rem -= take
        ladder.append({"price": price, "size": take})
    if filled <= 0:
        return None, 0.0, {"ladder": [], "partial": True, "reject": "EMPTY_BOOK"}
    vwap = proceeds / filled
    return vwap, filled, {"ladder": ladder, "partial": rem > 1e-9, "remaining": rem}
