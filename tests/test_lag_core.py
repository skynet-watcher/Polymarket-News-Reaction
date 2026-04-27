import datetime as dt

from app.core.lag import compute_baseline, eventual_move_thresholds, first_crossing_after, p_implied, zscore
from app.models import PriceSnapshot


def _snap(ts: dt.datetime, mid_yes: float) -> PriceSnapshot:
    return PriceSnapshot(
        id="x",
        market_id="m",
        timestamp=ts,
        best_bid_yes=None,
        best_ask_yes=None,
        mid_yes=mid_yes,
        last_price_yes=mid_yes,
        spread=None,
        liquidity=None,
        volume_24h=None,
    )


def test_p_implied_yes_and_no():
    assert p_implied(yes_mid=0.7, implied_outcome="YES") == 0.7
    assert round(p_implied(yes_mid=0.7, implied_outcome="NO"), 10) == 0.3


def test_first_crossing_after_yes():
    t0 = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    snaps = [
        _snap(t0 + dt.timedelta(seconds=10), 0.51),
        _snap(t0 + dt.timedelta(seconds=20), 0.61),
        _snap(t0 + dt.timedelta(seconds=30), 0.55),
    ]
    crossed = first_crossing_after(snaps, start_time=t0, implied_outcome="YES", threshold_value=0.60)
    assert crossed == t0 + dt.timedelta(seconds=20)


def test_first_crossing_after_no_direction():
    t0 = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    snaps = [
        _snap(t0 + dt.timedelta(seconds=10), 0.70),  # implied(NO)=0.30
        _snap(t0 + dt.timedelta(seconds=20), 0.60),  # implied(NO)=0.40
        _snap(t0 + dt.timedelta(seconds=30), 0.50),  # implied(NO)=0.50
    ]
    crossed = first_crossing_after(snaps, start_time=t0, implied_outcome="NO", threshold_value=0.45)
    assert crossed == t0 + dt.timedelta(seconds=30)


def test_eventual_move_thresholds():
    t0 = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    snaps = [
        _snap(t0 + dt.timedelta(minutes=1), 0.55),
        _snap(t0 + dt.timedelta(minutes=2), 0.65),
        _snap(t0 + dt.timedelta(minutes=3), 0.60),
    ]
    eventual_move, thr50, thr90 = eventual_move_thresholds(
        snaps, start_time=t0, implied_outcome="YES", p0=0.50
    )
    assert round(eventual_move, 4) == 0.15
    assert round(thr50, 4) == 0.575
    assert round(thr90, 4) == 0.635


def test_eventual_move_thresholds_small_move_returns_none_thresholds():
    t0 = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    snaps = [
        _snap(t0 + dt.timedelta(minutes=1), 0.52),
        _snap(t0 + dt.timedelta(minutes=2), 0.53),
    ]
    eventual_move, thr50, thr90 = eventual_move_thresholds(
        snaps, start_time=t0, implied_outcome="YES", p0=0.50
    )
    assert eventual_move is not None and eventual_move < 0.05
    assert thr50 is None
    assert thr90 is None


def test_zscore_edge_cases():
    assert zscore([]) == []
    assert zscore([1.0]) == [None]
    assert zscore([2.0, 2.0, 2.0]) == [None, None, None]


def test_compute_baseline_without_snapshot_is_none():
    b = compute_baseline(None, implied_outcome="YES")
    assert b.p0 is None
    assert b.yes_mid is None


def test_compute_baseline_with_snapshot_yes_and_no():
    t0 = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    snap = PriceSnapshot(
        id="x",
        market_id="m",
        timestamp=t0,
        best_bid_yes=0.49,
        best_ask_yes=0.51,
        mid_yes=0.50,
        last_price_yes=0.50,
        spread=None,
        liquidity=3000.0,
        volume_24h=123.0,
    )
    b_yes = compute_baseline(snap, implied_outcome="YES")
    assert b_yes.p0 == 0.50
    assert round(b_yes.spread, 10) == 0.02
    b_no = compute_baseline(snap, implied_outcome="NO")
    assert round(b_no.p0, 10) == 0.50

