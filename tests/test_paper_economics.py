"""USD notional, fees, and settlement PnL helpers."""

import pytest

from app.jobs.settle_trades import _apply_settlement_pnl
from app.models import PaperTrade
from app.paper_economics import (
    contracts_for_notional,
    entry_fee_usd,
    gross_unrealized_usd,
    live_net_mark_usd,
    net_pnl_after_fees,
    settlement_fee_on_gross_profit,
)
from app.util import now_utc


def test_contracts_for_notional() -> None:
    assert contracts_for_notional(10.0, 0.5) == pytest.approx(20.0)
    assert contracts_for_notional(10.0, 0.25) == pytest.approx(40.0)


def test_entry_and_settlement_fees() -> None:
    assert entry_fee_usd(10.0, 0.003) == pytest.approx(0.03)
    assert settlement_fee_on_gross_profit(50.0, 0.02) == pytest.approx(1.0)
    assert settlement_fee_on_gross_profit(-5.0, 0.02) == 0.0


def test_net_pnl_after_fees() -> None:
    assert net_pnl_after_fees(10.0, 0.03, 0.2) == pytest.approx(9.77)


def test_gross_unrealized_buy_no() -> None:
    g = gross_unrealized_usd(side="BUY_NO", fill_price=0.41, contracts=100.0, yes_mid=0.60)
    # no_mid=0.40 → (0.40 - 0.41)*100 = -1
    assert g == pytest.approx(-1.0)


def test_live_net_mark_includes_fees() -> None:
    v = live_net_mark_usd(
        side="BUY_YES",
        fill_price=0.5,
        contracts=20.0,
        yes_mid=0.6,
        entry_fee_usd=0.03,
        winning_profit_fee_rate=0.02,
    )
    # gross = 2.0, settle proxy on 2 = 0.04, net = 2 - 0.03 - 0.04
    assert v == pytest.approx(1.93)


def test_apply_settlement_pnl_with_fees() -> None:
    t = PaperTrade(
        id="t_fee",
        market_id="m1",
        signal_id="s1",
        hypothesis_id=None,
        side="BUY_YES",
        simulated_size=20.0,
        fill_price=0.5,
        max_slippage=0.02,
        confidence=0.9,
        status="OPEN",
        created_at=now_utc(),
        notional_usd=10.0,
        entry_fee_usd=0.03,
    )
    _apply_settlement_pnl(t, gross_pnl_usd=10.0)
    assert t.gross_pnl_usd == pytest.approx(10.0)
    assert t.settlement_fee_usd == pytest.approx(0.2)
    assert t.net_pnl_usd == pytest.approx(9.77)
    assert t.pnl_final == pytest.approx(9.77)


def test_apply_settlement_pnl_legacy_no_notional() -> None:
    t = PaperTrade(
        id="t_old",
        market_id="m1",
        signal_id="s1",
        hypothesis_id=None,
        side="BUY_YES",
        simulated_size=100.0,
        fill_price=0.5,
        max_slippage=0.02,
        confidence=0.9,
        status="OPEN",
        created_at=now_utc(),
    )
    _apply_settlement_pnl(t, gross_pnl_usd=25.0)
    assert t.pnl_final == pytest.approx(25.0)
    assert t.net_pnl_usd is None