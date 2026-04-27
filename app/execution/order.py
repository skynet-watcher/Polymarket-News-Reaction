"""
FUTURE STATE — not implemented.

This module will handle live order placement on Polymarket CLOB v3.

Requires: wallet private key, EIP-712 signing, authenticated CLOB endpoints.

Do not implement until paper trade validation is complete.
"""

from __future__ import annotations

from app.models import Market, NewsSignal


class LiveOrderNotImplementedError(NotImplementedError):
    pass


async def place_order(*, market: Market, signal: NewsSignal, side: str, size: float) -> None:
    """
    Placeholder for live order placement.

    Will sign and submit a limit order to Polymarket CLOB v3.
    """
    raise LiveOrderNotImplementedError("Live trading is not implemented. Set TRADING_ENABLED=false.")
