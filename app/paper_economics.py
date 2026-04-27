"""
USD sizing and fee model for paper trades (Polymarket-style, simplified).

- **Notional**: target ~`paper_trade_notional_usd` dollars of *position* at entry
  (`contracts = notional / fill_price` for YES/NO binary tokens priced in [0,1]).
- **Entry fee**: taker-style fee on notional (config rate × notional).
- **Settlement fee**: fee on *positive* settlement PnL only (config rate × max(0, gross)).

Rates are approximations; tune via settings / `.env`. See Polymarket fee docs for live trading.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PaperTrade


def contracts_for_notional(notional_usd: float, fill_price: float, *, max_contracts: float = 50_000.0) -> float:
    """Shares/contracts so that entry notional ≈ fill_price × contracts (binary USDC semantics)."""
    fp = float(fill_price)
    if fp <= 0.0 or fp >= 1.0:
        return 0.0
    n = max(0.01, float(notional_usd))
    c = n / fp
    return min(max_contracts, c)


def entry_fee_usd(notional_usd: float, taker_fee_rate: float) -> float:
    return round(max(0.0, float(notional_usd)) * max(0.0, float(taker_fee_rate)), 4)


def settlement_fee_on_gross_profit(gross_profit_usd: float, winning_profit_fee_rate: float) -> float:
    """Fee charged on winning settlement PnL (zero if flat or loss)."""
    return round(max(0.0, float(gross_profit_usd)) * max(0.0, float(winning_profit_fee_rate)), 4)


def net_pnl_after_fees(gross_pnl_usd: float, entry_fee_usd: float, settlement_fee_usd: float) -> float:
    return round(float(gross_pnl_usd) - float(entry_fee_usd) - float(settlement_fee_usd), 4)


def gross_unrealized_usd(*, side: str, fill_price: float, contracts: float, yes_mid: float) -> float:
    if side == "BUY_YES":
        return (float(yes_mid) - float(fill_price)) * float(contracts)
    if side == "BUY_NO":
        no_mid = 1.0 - float(yes_mid)
        return (no_mid - float(fill_price)) * float(contracts)
    return 0.0


def live_net_mark_usd(
    *,
    side: str,
    fill_price: float,
    contracts: float,
    yes_mid: float,
    entry_fee_usd: float,
    winning_profit_fee_rate: float,
) -> float:
    """
    Mark-to-market *economic* PnL if we closed at ``yes_mid`` now: gross MTM minus entry fee
    minus a fee on positive MTM (proxy for redemption / winning fee).
    """
    gross = gross_unrealized_usd(side=side, fill_price=fill_price, contracts=contracts, yes_mid=yes_mid)
    settle_proxy = settlement_fee_on_gross_profit(gross, winning_profit_fee_rate)
    return round(gross - float(entry_fee_usd) - settle_proxy, 4)


async def aggregate_portfolio(session: AsyncSession) -> dict[str, Any]:
    """
    Roll-up for /trades header: spend (cash to open), fees, realized net, open count.

    Legacy rows without ``notional_usd`` are excluded from USD rollups but included in counts.
    """
    open_n = (
        await session.execute(select(func.count()).select_from(PaperTrade).where(PaperTrade.status == "OPEN"))
    ).scalar_one()
    settled_n = (
        await session.execute(
            select(func.count()).select_from(PaperTrade).where(PaperTrade.status != "OPEN"))
    ).scalar_one()

    tracked = (
        await session.execute(
            select(
                func.coalesce(func.sum(PaperTrade.notional_usd), 0.0),
                func.coalesce(func.sum(PaperTrade.entry_fee_usd), 0.0),
                func.coalesce(func.sum(PaperTrade.settlement_fee_usd), 0.0),
                func.coalesce(func.sum(PaperTrade.cash_spent_usd), 0.0),
            ).where(PaperTrade.notional_usd.is_not(None))
        )
    ).one()

    realized_row = (
        await session.execute(
            select(func.coalesce(func.sum(PaperTrade.net_pnl_usd), 0.0)).where(
                PaperTrade.status.in_(["SETTLED_RESOLVED", "SETTLED_T24H"]),
                PaperTrade.net_pnl_usd.is_not(None),
            )
        )
    ).scalar_one()

    notional_sum = float(tracked[0] or 0.0)
    entry_fees = float(tracked[1] or 0.0)
    settlement_fees = float(tracked[2] or 0.0)
    cash_spent = float(tracked[3] or 0.0)
    realized_net = float(realized_row or 0.0)

    return {
        "open_trades": int(open_n or 0),
        "settled_trades": int(settled_n or 0),
        "tracked_trades": int((open_n or 0) + (settled_n or 0)),
        "sum_notional_usd": round(notional_sum, 2),
        "sum_entry_fees_usd": round(entry_fees, 4),
        "sum_settlement_fees_usd": round(settlement_fees, 4),
        "sum_cash_spent_usd": round(cash_spent, 4),
        "sum_realized_net_pnl_usd": round(realized_net, 4),
    }
