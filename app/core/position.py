"""
FUTURE STATE — not implemented.

Position limit enforcement for live trading.

Will check per-market exposure and total portfolio notional before allowing order placement.
"""

from __future__ import annotations


class PositionLimitError(Exception):
    pass


async def check_limits(*, market_id: str, side: str, size: float) -> None:
    """
    Placeholder. Will enforce:
    - Max exposure per market
    - Max total open notional
    - Max single trade size

    Raises PositionLimitError if any limit is breached.
    """
    raise NotImplementedError("Position limits not yet implemented.")
