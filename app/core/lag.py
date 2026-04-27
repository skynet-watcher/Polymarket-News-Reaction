from __future__ import annotations

import datetime as dt
import math
import statistics
from dataclasses import dataclass
from typing import Iterable, Optional

from app.models import PriceSnapshot


def p_implied(*, yes_mid: float, implied_outcome: str) -> float:
    if implied_outcome == "YES":
        return yes_mid
    if implied_outcome == "NO":
        return 1.0 - yes_mid
    raise ValueError(f"Unsupported implied_outcome: {implied_outcome}")


@dataclass(frozen=True)
class Baseline:
    p0: Optional[float]
    yes_mid: Optional[float]
    yes_bid: Optional[float]
    yes_ask: Optional[float]
    spread: Optional[float]
    liquidity: Optional[float]
    volume_24h: Optional[float]


def compute_baseline(snapshot: Optional[PriceSnapshot], *, implied_outcome: str) -> Baseline:
    if snapshot is None or snapshot.mid_yes is None:
        return Baseline(
            p0=None,
            yes_mid=None,
            yes_bid=None,
            yes_ask=None,
            spread=None,
            liquidity=None,
            volume_24h=None,
        )

    spread = snapshot.spread
    if spread is None and snapshot.best_bid_yes is not None and snapshot.best_ask_yes is not None:
        spread = snapshot.best_ask_yes - snapshot.best_bid_yes

    return Baseline(
        p0=p_implied(yes_mid=float(snapshot.mid_yes), implied_outcome=implied_outcome),
        yes_mid=float(snapshot.mid_yes),
        yes_bid=float(snapshot.best_bid_yes) if snapshot.best_bid_yes is not None else None,
        yes_ask=float(snapshot.best_ask_yes) if snapshot.best_ask_yes is not None else None,
        spread=float(spread) if spread is not None else None,
        liquidity=float(snapshot.liquidity) if snapshot.liquidity is not None else None,
        volume_24h=float(snapshot.volume_24h) if getattr(snapshot, "volume_24h", None) is not None else None,
    )


def first_crossing_after(
    snapshots: Iterable[PriceSnapshot],
    *,
    start_time: dt.datetime,
    implied_outcome: str,
    threshold_value: float,
) -> Optional[dt.datetime]:
    for s in snapshots:
        if s.timestamp <= start_time:
            continue
        if s.mid_yes is None:
            continue
        if p_implied(yes_mid=float(s.mid_yes), implied_outcome=implied_outcome) >= threshold_value:
            return s.timestamp
    return None


def eventual_move_thresholds(
    snapshots: Iterable[PriceSnapshot],
    *,
    start_time: dt.datetime,
    implied_outcome: str,
    p0: float,
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """
    Returns (eventual_move, thr50, thr90) where thresholds are p0 + {0.5,0.9}*eventual_move.
    Only defined when eventual_move >= 0.05.
    """
    max_p = None
    for s in snapshots:
        if s.timestamp <= start_time or s.mid_yes is None:
            continue
        pv = p_implied(yes_mid=float(s.mid_yes), implied_outcome=implied_outcome)
        if max_p is None or pv > max_p:
            max_p = pv
    if max_p is None:
        return (None, None, None)
    eventual_move = max_p - p0
    if eventual_move < 0.05:
        return (eventual_move, None, None)
    return (eventual_move, p0 + 0.5 * eventual_move, p0 + 0.9 * eventual_move)


def zscore(values: list[float]) -> list[Optional[float]]:
    if len(values) < 2:
        return [None] * len(values)
    mu = statistics.mean(values)
    sd = statistics.stdev(values)
    if sd == 0:
        return [None] * len(values)
    return [(v - mu) / sd for v in values]


def log1p_seconds(seconds: float) -> float:
    return math.log1p(max(0.0, seconds))

