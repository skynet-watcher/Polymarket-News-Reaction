"""Arithmetic checks for paper trade settlement (resolution + T+24h mid). No DB/async."""


def test_buy_yes_resolution_win():
    pnl = (1.0 - 0.55) * 50.0  # fill=0.55, size=50, YES wins
    assert round(pnl, 4) == 22.5


def test_buy_yes_resolution_loss():
    pnl = (0.0 - 0.55) * 50.0
    assert round(pnl, 4) == -27.5


def test_buy_no_resolution_win():
    pnl = (1.0 - 0.40) * 50.0  # fill=0.40 (NO ask), NO wins
    assert round(pnl, 4) == 30.0


def test_t24h_settlement_buy_yes():
    fill = 0.52
    settle_mid = 0.67
    size = 100.0
    pnl = (settle_mid - fill) * size
    assert round(pnl, 2) == 15.0
