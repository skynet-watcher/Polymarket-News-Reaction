from __future__ import annotations

from typing import Any, Optional

from app.core.clob_depth import MIN_DEPTH_FILL_RATIO, orderbook_levels_from_payload, walk_asks_buy, walk_bids_sell
from app.models import Market, NewsSignal, PaperTrade, PriceSnapshot
from app.paper_economics import contracts_for_notional, entry_fee_usd
from app.settings import settings
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
    Simulate a paper trade sized to ``settings.paper_trade_notional_usd`` (× ``paper_size_multiplier``),
    with entry/settlement fee metadata (see ``app/paper_economics.py``).

    Prefer CLOB depth when ``orderbook`` is provided; else top-of-book + slippage.
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

    mult = max(0.25, min(3.0, float(paper_size_multiplier or 1.0)))
    notional_usd = round(float(settings.paper_trade_notional_usd) * mult, 4)
    entry_fee = entry_fee_usd(notional_usd, settings.polymarket_entry_fee_rate)

    execution_context: dict[str, Any] = {"notional_usd": notional_usd, "entry_fee_usd": entry_fee, "mode": "top_of_book"}

    if orderbook and isinstance(orderbook, dict) and (orderbook.get("bids") or orderbook.get("asks")):
        bids, asks = orderbook_levels_from_payload(orderbook)
        if outcome == "YES":
            est = float(asks[0][0]) if asks else (ask or 0.5)
            target_contracts = contracts_for_notional(notional_usd, est)
            if target_contracts <= 0:
                return None
            vwap, filled, meta = walk_asks_buy(asks, target_contracts)
            if vwap is None or filled < MIN_DEPTH_FILL_RATIO * target_contracts:
                execution_context.update({"reject": "THIN_DEPTH_OR_EMPTY", "meta": meta})
                return None
            fill = min(0.999, vwap + 0.01)
            side = "BUY_YES"
            contracts = float(filled)
            cash_spent = round(contracts * float(vwap) + entry_fee, 4)
            execution_context = {
                "mode": "depth_v1",
                "side": side,
                "notional_usd": notional_usd,
                "entry_fee_usd": entry_fee,
                "target_contracts": target_contracts,
                "filled_size": filled,
                "partial": meta.get("partial", False),
                "vwap": vwap,
                "ladder": meta.get("ladder", []),
                "cash_spent_usd": cash_spent,
                "polymarket_entry_fee_rate": settings.polymarket_entry_fee_rate,
                "polymarket_winning_profit_fee_rate": settings.polymarket_winning_profit_fee_rate,
            }
        else:
            est_yes = float(bids[0][0]) if bids else (bid or 0.5)
            est_no_px = max(1e-6, 1.0 - est_yes)
            target_contracts = contracts_for_notional(notional_usd, est_no_px)
            if target_contracts <= 0:
                return None
            vwap_sell, filled, meta = walk_bids_sell(bids, target_contracts)
            if vwap_sell is None or filled < MIN_DEPTH_FILL_RATIO * target_contracts:
                execution_context.update({"reject": "THIN_DEPTH_OR_EMPTY", "meta": meta})
                return None
            fill = min(0.999, (1.0 - vwap_sell) + 0.01)
            side = "BUY_NO"
            contracts = float(filled)
            no_vwap_px = 1.0 - float(vwap_sell)
            cash_spent = round(contracts * no_vwap_px + entry_fee, 4)
            execution_context = {
                "mode": "depth_v1",
                "side": side,
                "notional_usd": notional_usd,
                "entry_fee_usd": entry_fee,
                "target_contracts": target_contracts,
                "filled_size": filled,
                "partial": meta.get("partial", False),
                "vwap_yes_sell": vwap_sell,
                "ladder": meta.get("ladder", []),
                "cash_spent_usd": cash_spent,
                "polymarket_entry_fee_rate": settings.polymarket_entry_fee_rate,
                "polymarket_winning_profit_fee_rate": settings.polymarket_winning_profit_fee_rate,
            }
        return PaperTrade(
            id=new_id("trade"),
            market_id=market.id,
            signal_id=signal.id,
            hypothesis_id=None,
            side=side,
            simulated_size=contracts,
            fill_price=float(fill),
            best_bid_at_signal=bid,
            best_ask_at_signal=ask,
            mid_at_signal=mid,
            max_slippage=0.02,
            confidence=float(signal.confidence or 0.0),
            status="OPEN",
            created_at=now_utc(),
            notional_usd=notional_usd,
            entry_fee_usd=entry_fee,
            cash_spent_usd=cash_spent,
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

    contracts = round(contracts_for_notional(notional_usd, float(fill)), 6)
    if contracts <= 0:
        return None
    cash_spent = round(contracts * float(fill) + entry_fee, 4)
    execution_context = {
        "mode": "top_of_book",
        "side": side,
        "notional_usd": notional_usd,
        "entry_fee_usd": entry_fee,
        "contracts": contracts,
        "filled_size": contracts,
        "cash_spent_usd": cash_spent,
        "polymarket_entry_fee_rate": settings.polymarket_entry_fee_rate,
        "polymarket_winning_profit_fee_rate": settings.polymarket_winning_profit_fee_rate,
    }

    return PaperTrade(
        id=new_id("trade"),
        market_id=market.id,
        signal_id=signal.id,
        hypothesis_id=None,
        side=side,
        simulated_size=float(contracts),
        fill_price=float(fill),
        best_bid_at_signal=bid,
        best_ask_at_signal=ask,
        mid_at_signal=mid,
        max_slippage=0.02,
        confidence=float(signal.confidence or 0.0),
        status="OPEN",
        created_at=now_utc(),
        notional_usd=notional_usd,
        entry_fee_usd=entry_fee,
        cash_spent_usd=cash_spent,
        execution_context_json=execution_context,
    )
