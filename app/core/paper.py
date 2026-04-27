from __future__ import annotations

from typing import Any, Optional

from app.core.clob_depth import MIN_DEPTH_FILL_RATIO, orderbook_levels_from_payload, walk_asks_buy, walk_bids_sell
from app.models import Market, NewsSignal, PaperTrade, PriceSnapshot
from app.util import new_id, now_utc


def maybe_paper_trade(
    *,
    market: Market,
    signal: NewsSignal,
    snapshot: Optional[PriceSnapshot] = None,
    orderbook: Optional[dict[str, Any]] = None,
    paper_size_multiplier: float = 1.0,
) -> Optional[PaperTrade]:
    """
    Simulate a paper trade. Prefer CLOB depth when `orderbook` is provided; else top-of-book + slippage.

    BUY_NO uses YES bid ladder as a sell-YES proxy (complement price), consistent with the MVP model.
    """
    bid = (
        snapshot.best_bid_yes
        if snapshot is not None and snapshot.best_bid_yes is not None
        else market.best_bid_yes
    )
    ask = (
        snapshot.best_ask_yes
        if snapshot is not None and snapshot.best_ask_yes is not None
        else market.best_ask_yes
    )
    mid = None
    if bid is not None and ask is not None:
        mid = (bid + ask) / 2.0
    elif snapshot is not None and snapshot.mid_yes is not None:
        mid = float(snapshot.mid_yes)

    outcome = signal.interpreted_outcome
    if outcome not in ("YES", "NO"):
        return None

    liq = (
        float(snapshot.liquidity)
        if snapshot is not None and snapshot.liquidity is not None
        else (market.liquidity or 0.0)
    )
    mult = max(0.25, min(3.0, float(paper_size_multiplier or 1.0)))
    target_size = min(100.0, max(10.0, liq / 500.0)) * mult

    execution_context: dict[str, Any] = {"target_size": target_size, "mode": "top_of_book"}

    if orderbook and isinstance(orderbook, dict) and (orderbook.get("bids") or orderbook.get("asks")):
        bids, asks = orderbook_levels_from_payload(orderbook)
        if outcome == "YES":
            vwap, filled, meta = walk_asks_buy(asks, target_size)
            if vwap is None or filled < MIN_DEPTH_FILL_RATIO * target_size:
                execution_context.update({"reject": "THIN_DEPTH_OR_EMPTY", "meta": meta})
                return None
            fill = min(0.999, vwap + 0.01)
            side = "BUY_YES"
            execution_context = {
                "mode": "depth_v1",
                "side": side,
                "target_size": target_size,
                "filled_size": filled,
                "partial": meta.get("partial", False),
                "vwap": vwap,
                "ladder": meta.get("ladder", []),
            }
        else:
            vwap_sell, filled, meta = walk_bids_sell(bids, target_size)
            if vwap_sell is None or filled < MIN_DEPTH_FILL_RATIO * target_size:
                execution_context.update({"reject": "THIN_DEPTH_OR_EMPTY", "meta": meta})
                return None
            fill = min(0.999, (1.0 - vwap_sell) + 0.01)
            side = "BUY_NO"
            execution_context = {
                "mode": "depth_v1",
                "side": side,
                "target_size": target_size,
                "filled_size": filled,
                "partial": meta.get("partial", False),
                "vwap_yes_sell": vwap_sell,
                "ladder": meta.get("ladder", []),
            }
        return PaperTrade(
            id=new_id("trade"),
            market_id=market.id,
            signal_id=signal.id,
            hypothesis_id=None,
            side=side,
            simulated_size=float(filled),
            fill_price=float(fill),
            best_bid_at_signal=bid,
            best_ask_at_signal=ask,
            mid_at_signal=mid,
            max_slippage=0.02,
            confidence=float(signal.confidence or 0.0),
            status="OPEN",
            created_at=now_utc(),
            execution_context_json=execution_context,
        )

    # Legacy top-of-book path
    if outcome == "YES":
        if ask is None:
            return None
        fill = min(0.999, ask + 0.01)
        side = "BUY_YES"
    else:
        if bid is None:
            return None
        fill = min(0.999, (1.0 - bid) + 0.01)
        side = "BUY_NO"

    execution_context = {"mode": "top_of_book", "side": side, "target_size": target_size, "filled_size": target_size}

    return PaperTrade(
        id=new_id("trade"),
        market_id=market.id,
        signal_id=signal.id,
        hypothesis_id=None,
        side=side,
        simulated_size=float(target_size),
        fill_price=float(fill),
        best_bid_at_signal=bid,
        best_ask_at_signal=ask,
        mid_at_signal=mid,
        max_slippage=0.02,
        confidence=float(signal.confidence or 0.0),
        status="OPEN",
        created_at=now_utc(),
        execution_context_json=execution_context,
    )
