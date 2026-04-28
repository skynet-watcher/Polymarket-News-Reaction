from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from statistics import median
from typing import Any, Dict, Optional, Tuple

from sqlalchemy import and_, desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.lag import p_implied
from app.models import (
    BacktestCase,
    BacktestEventLog,
    BacktestRun,
    LagMeasurement,
    LagThresholdCrossing,
    Market,
    NewsArticle,
    NewsSignal,
    PaperTrade,
    PriceSnapshot,
)
from app.settings import settings
from app.util import new_id, now_utc, to_utc_aware

WINDOWS: list[tuple[str, int]] = [
    ("1m", 1),
    ("5m", 5),
    ("15m", 15),
    ("30m", 30),
    ("1h", 60),
    ("4h", 240),
    ("24h", 1440),
]


def _seconds_between(later: dt.datetime, earlier: dt.datetime) -> float:
    return (to_utc_aware(later) - to_utc_aware(earlier)).total_seconds()


def _price_from_snapshot(snap: Optional[PriceSnapshot], implied_outcome: Optional[str]) -> Optional[float]:
    if snap is None or snap.mid_yes is None:
        return None
    if implied_outcome in ("YES", "NO"):
        return p_implied(yes_mid=float(snap.mid_yes), implied_outcome=implied_outcome)
    return float(snap.mid_yes)


def _coverage_status(*, p0: Optional[float], snapshot_count: int, min_snapshot_coverage: int) -> str:
    if p0 is None or snapshot_count == 0:
        return "NO_DATA"
    if snapshot_count < min_snapshot_coverage:
        return "SPARSE"
    return "GOOD"


async def _nearest_before(session: AsyncSession, *, market_id: str, at: dt.datetime) -> Optional[PriceSnapshot]:
    return (
        await session.execute(
            select(PriceSnapshot)
            .where(PriceSnapshot.market_id == market_id)
            .where(PriceSnapshot.timestamp <= at)
            .order_by(PriceSnapshot.timestamp.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _nearest_after_before(
    session: AsyncSession, *, market_id: str, after: dt.datetime, at_or_before: dt.datetime
) -> Optional[PriceSnapshot]:
    return (
        await session.execute(
            select(PriceSnapshot)
            .where(PriceSnapshot.market_id == market_id)
            .where(PriceSnapshot.timestamp > after)
            .where(PriceSnapshot.timestamp <= at_or_before)
            .order_by(PriceSnapshot.timestamp.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _snapshots_after_until(
    session: AsyncSession, *, market_id: str, after: dt.datetime, until: dt.datetime
) -> list[PriceSnapshot]:
    return list(
        (
            await session.execute(
                select(PriceSnapshot)
                .where(PriceSnapshot.market_id == market_id)
                .where(PriceSnapshot.timestamp > after)
                .where(PriceSnapshot.timestamp <= until)
                .order_by(PriceSnapshot.timestamp.asc())
            )
        )
        .scalars()
        .all()
    )


async def _lag_crossing_seconds(session: AsyncSession, *, lag_id: str, label: str) -> Optional[float]:
    row = (
        await session.execute(
            select(LagThresholdCrossing)
            .where(LagThresholdCrossing.lag_measurement_id == lag_id)
            .where(LagThresholdCrossing.threshold_label == label)
            .limit(1)
        )
    ).scalar_one_or_none()
    return float(row.lag_seconds) if row is not None and row.lag_seconds is not None else None


class _AuditWriter:
    def __init__(self, run_id: str) -> None:
        self.path = Path("logs") / "backtests" / f"backtest_{run_id}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    async def emit(
        self,
        session: AsyncSession,
        *,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        case_id: Optional[str] = None,
    ) -> None:
        ts = now_utc()
        row = BacktestEventLog(
            id=new_id("btlog"),
            run_id=run_id,
            case_id=case_id,
            event_type=event_type,
            payload_json=payload,
            created_at=ts,
        )
        session.add(row)
        line = {
            "id": row.id,
            "run_id": run_id,
            "case_id": case_id,
            "event_type": event_type,
            "payload": payload,
            "created_at": ts.isoformat(),
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, sort_keys=True, default=str) + "\n")


def _simulate_backtest_trade(
    *,
    market: Market,
    signal: NewsSignal,
    case_id: str,
    p0: float,
    baseline_snapshot: Optional[PriceSnapshot],
) -> Optional[PaperTrade]:
    """
    Create a BACKTEST-tagged PaperTrade using the historical price at article
    publication time as the fill. No CLOB depth is used (historical books are
    unavailable); this is a top-of-book simulation using p0.

    Returns None when the outcome is unknown or p0 is degenerate.
    """
    outcome = signal.interpreted_outcome
    if outcome not in ("YES", "NO"):
        return None
    if p0 <= 0.0 or p0 >= 1.0:
        return None

    bid = baseline_snapshot.best_bid_yes if baseline_snapshot else None
    ask = baseline_snapshot.best_ask_yes if baseline_snapshot else None
    mid = float(baseline_snapshot.mid_yes) if baseline_snapshot and baseline_snapshot.mid_yes else p0
    liq = float(baseline_snapshot.liquidity) if baseline_snapshot and baseline_snapshot.liquidity else (market.liquidity or 0.0)

    # Simplified sizing: same formula as paper.py but without CLOB depth.
    from app.paper_economics import contracts_for_notional
    notional_usd = settings.paper_trade_notional_usd
    entry_fee = round(notional_usd * settings.polymarket_entry_fee_rate, 4)

    # ~1 tick slippage vs mid (ALEX_REVIEW L3): aligns directionally with live CLOB fill model; not a full depth walk.
    if outcome == "YES":
        fill = min(0.999, p0 + 0.01)
        side = "BUY_YES"
    else:
        fill = min(0.999, (1.0 - p0) + 0.01)
        side = "BUY_NO"

    contracts = round(contracts_for_notional(notional_usd, float(fill)), 6)
    if contracts <= 0:
        return None
    cash_spent = round(contracts * float(fill) + entry_fee, 4)

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
        notional_usd=notional_usd,
        entry_fee_usd=entry_fee,
        cash_spent_usd=cash_spent,
        trade_source="BACKTEST",
        backtest_case_id=case_id,
        execution_context_json={
            "mode": "backtest_top_of_book",
            "side": side,
            "notional_usd": notional_usd,
            "entry_fee_usd": entry_fee,
            "contracts": contracts,
            "filled_size": contracts,
            "p0": p0,
            "cash_spent_usd": cash_spent,
            "note": "Historical fill — no CLOB depth available",
        },
        created_at=now_utc(),
    )


async def _build_case(
    session: AsyncSession,
    *,
    run_id: str,
    signal: NewsSignal,
    min_snapshot_coverage: int,
) -> tuple[BacktestCase, Optional[PriceSnapshot], dict[str, Any]]:
    article = signal.article
    market = signal.market
    if article is None:
        article = await session.get(NewsArticle, signal.article_id)
    if market is None:
        market = await session.get(Market, signal.market_id)
    if article is None or market is None:
        raise ValueError("signal is missing article or market")

    published_at = to_utc_aware(article.published_at)
    fetched_at = to_utc_aware(article.fetched_at)
    signal_created_at = to_utc_aware(signal.created_at)
    implied_outcome = signal.interpreted_outcome if signal.interpreted_outcome in ("YES", "NO") else None
    hours_to_resolution = None
    if market.end_date is not None:
        hours_to_resolution = _seconds_between(market.end_date, published_at) / 3600.0

    lag = (
        await session.execute(select(LagMeasurement).where(LagMeasurement.signal_id == signal.id).limit(1))
    ).scalar_one_or_none()
    baseline_snapshot = await _nearest_before(session, market_id=market.id, at=published_at)
    p0 = lag.p0 if lag is not None and lag.p0 is not None else _price_from_snapshot(baseline_snapshot, implied_outcome)

    price_windows: dict[str, dict[str, Any]] = {}
    for label, minutes in WINDOWS:
        target = published_at + dt.timedelta(minutes=minutes)
        snap = await _nearest_after_before(session, market_id=market.id, after=published_at, at_or_before=target)
        price = _price_from_snapshot(snap, implied_outcome)
        price_windows[label] = {
            "price": price,
            "move_from_p0": (price - p0) if price is not None and p0 is not None else None,
            "snapshot_at": snap.timestamp.isoformat() if snap is not None else None,
        }

    horizon = published_at + dt.timedelta(hours=24)
    snapshots = await _snapshots_after_until(session, market_id=market.id, after=published_at, until=horizon)
    moves: list[tuple[float, PriceSnapshot]] = []
    for snap in snapshots:
        price = _price_from_snapshot(snap, implied_outcome)
        if price is not None and p0 is not None:
            moves.append((price - p0, snap))

    first_5pt = await _lag_crossing_seconds(session, lag_id=lag.id, label="5PT") if lag is not None else None
    first_10pt = await _lag_crossing_seconds(session, lag_id=lag.id, label="10PT") if lag is not None else None
    if first_5pt is None and moves:
        first_5pt = next((_seconds_between(s.timestamp, published_at) for move, s in moves if move >= 0.05), None)
    if first_10pt is None and moves:
        first_10pt = next((_seconds_between(s.timestamp, published_at) for move, s in moves if move >= 0.10), None)

    max_move_24h = max((move for move, _ in moves), default=None)
    move_before_fetch = None
    if moves:
        move_before_fetch = any(move >= 0.05 and to_utc_aware(s.timestamp) <= fetched_at for move, s in moves)

    coverage = _coverage_status(p0=p0, snapshot_count=len(snapshots), min_snapshot_coverage=min_snapshot_coverage)
    notes = []
    if lag is not None:
        notes.append("linked_to_lag_measurement")
    if implied_outcome is None:
        notes.append("no_binary_interpreted_outcome; yes_mid used")
    if coverage != "GOOD":
        notes.append(f"snapshot_coverage={len(snapshots)}")

    case = BacktestCase(
        id=new_id("btcase"),
        run_id=run_id,
        article_id=article.id,
        market_id=market.id,
        signal_id=signal.id,
        lag_measurement_id=lag.id if lag is not None else None,
        published_at=published_at,
        fetched_at=fetched_at,
        signal_created_at=signal_created_at,
        polling_delay_seconds=_seconds_between(fetched_at, published_at),
        signal_delay_seconds=_seconds_between(signal_created_at, published_at),
        hours_to_resolution=hours_to_resolution,
        implied_outcome=implied_outcome,
        signal_action=signal.action,
        p0=p0,
        price_windows_json=price_windows,
        first_5pt_move_seconds=first_5pt,
        first_10pt_move_seconds=first_10pt,
        max_move_24h=max_move_24h,
        move_before_fetch=move_before_fetch,
        coverage_status=coverage,
        notes=", ".join(notes) if notes else None,
        created_at=now_utc(),
    )
    payload = {
        "article_id": article.id,
        "market_id": market.id,
        "signal_id": signal.id,
        "lag_measurement_id": case.lag_measurement_id,
        "coverage_status": coverage,
        "snapshot_count_24h": len(snapshots),
        "polling_delay_seconds": case.polling_delay_seconds,
        "signal_delay_seconds": case.signal_delay_seconds,
        "hours_to_resolution": hours_to_resolution,
        "first_5pt_move_seconds": first_5pt,
        "first_10pt_move_seconds": first_10pt,
        "max_move_24h": max_move_24h,
        "move_before_fetch": move_before_fetch,
    }
    return case, baseline_snapshot, payload


def _summary(cases: list[BacktestCase]) -> dict[str, Any]:
    tested = len(cases)
    good = [c for c in cases if c.coverage_status == "GOOD"]
    sparse = [c for c in cases if c.coverage_status == "SPARSE"]
    no_data = [c for c in cases if c.coverage_status == "NO_DATA"]
    polling_delays = [float(c.polling_delay_seconds) for c in cases]
    signal_delays = [float(c.signal_delay_seconds) for c in cases if c.signal_delay_seconds is not None]
    lag5 = [float(c.first_5pt_move_seconds) for c in cases if c.first_5pt_move_seconds is not None]
    lag10 = [float(c.first_10pt_move_seconds) for c in cases if c.first_10pt_move_seconds is not None]
    before_fetch = [c for c in cases if c.move_before_fetch is True]
    return {
        "cases": tested,
        "coverage_good": len(good),
        "coverage_sparse": len(sparse),
        "coverage_no_data": len(no_data),
        "median_polling_delay_seconds": median(polling_delays) if polling_delays else None,
        "median_signal_delay_seconds": median(signal_delays) if signal_delays else None,
        "median_first_5pt_move_seconds": median(lag5) if lag5 else None,
        "median_first_10pt_move_seconds": median(lag10) if lag10 else None,
        "move_before_fetch_count": len(before_fetch),
        "move_before_fetch_rate": (len(before_fetch) / tested) if tested else None,
    }


async def run(
    session: AsyncSession,
    *,
    since_hours: int = 72,
    max_articles: int = 50,
    min_snapshot_coverage: int = 3,
) -> dict[str, Any]:
    started = now_utc()
    run_id = new_id("btrun")
    params = {
        "since_hours": since_hours,
        "max_articles": max_articles,
        "min_snapshot_coverage": min_snapshot_coverage,
        "source": "local_price_snapshots",
        "what_if_act": False,
        "historical_price_fetch": False,
    }
    bt_run = BacktestRun(id=run_id, started_at=started, status="RUNNING", params_json=params)
    session.add(bt_run)
    await session.commit()

    audit = _AuditWriter(run_id)
    await audit.emit(session, run_id=run_id, event_type="RUN_STARTED", payload=params)
    await session.commit()

    try:
        cutoff = started - dt.timedelta(hours=max(1, since_hours))
        signals = (
            await session.execute(
                select(NewsSignal)
                .options(selectinload(NewsSignal.article), selectinload(NewsSignal.market))
                .join(NewsArticle, NewsArticle.id == NewsSignal.article_id)
                .join(Market, Market.id == NewsSignal.market_id)
                .where(NewsArticle.published_at >= cutoff)
                .where(Market.is_fixture.is_not(True))
                .order_by(desc(NewsArticle.published_at), desc(NewsSignal.created_at))
                .limit(max(1, max_articles))
            )
        ).scalars().all()

        cases: list[BacktestCase] = []
        backtest_trades_created = 0
        live_trades_found = 0
        for sig in signals:
            try:
                case, baseline_snapshot, payload = await _build_case(
                    session,
                    run_id=run_id,
                    signal=sig,
                    min_snapshot_coverage=max(1, min_snapshot_coverage),
                )
                session.add(case)
                await session.flush()  # assigns case.id before trade simulation

                # ── Trade marking ─────────────────────────────────────────────
                if sig.action == "ACT":
                    # A real LIVE trade was already created by the pipeline.
                    # The BacktestCase links to it via signal_id; no duplicate needed.
                    live_trades_found += 1
                    await audit.emit(
                        session,
                        run_id=run_id,
                        case_id=case.id,
                        event_type="LIVE_TRADE_EXISTS",
                        payload={"signal_id": sig.id, "signal_action": sig.action},
                    )
                elif case.p0 is not None and case.implied_outcome in ("YES", "NO"):
                    # Signal did not result in a live trade — simulate one so we can
                    # see what would have happened under current thresholds.
                    article = sig.article or await session.get(NewsArticle, sig.article_id)
                    market = sig.market or await session.get(Market, sig.market_id)
                    if article is not None and market is not None:
                        bt_trade = _simulate_backtest_trade(
                            market=market,
                            signal=sig,
                            case_id=case.id,
                            p0=case.p0,
                            baseline_snapshot=baseline_snapshot,
                        )
                        if bt_trade is not None:
                            session.add(bt_trade)
                            backtest_trades_created += 1
                            await audit.emit(
                                session,
                                run_id=run_id,
                                case_id=case.id,
                                event_type="BACKTEST_TRADE_CREATED",
                                payload={
                                    "trade_id": bt_trade.id,
                                    "side": bt_trade.side,
                                    "fill_price": bt_trade.fill_price,
                                    "signal_action": sig.action,
                                    "p0": case.p0,
                                },
                            )

                await audit.emit(session, run_id=run_id, case_id=case.id, event_type="CASE_RECORDED", payload=payload)
                cases.append(case)
            except Exception as exc:
                await audit.emit(
                    session,
                    run_id=run_id,
                    event_type="CASE_FAILED",
                    payload={"signal_id": sig.id, "error": f"{type(exc).__name__}: {str(exc)}"},
                )

        summary = _summary(cases)
        summary["backtest_trades_created"] = backtest_trades_created
        summary["live_trades_found"] = live_trades_found
        bt_run.finished_at = now_utc()
        bt_run.status = "SUCCESS"
        bt_run.summary_json = summary
        await audit.emit(session, run_id=run_id, event_type="RUN_FINISHED", payload=summary)
        await session.commit()

        return {
            "ok": True,
            "run_id": run_id,
            "cases": summary["cases"],
            "coverage_good": summary["coverage_good"],
            "coverage_sparse": summary["coverage_sparse"],
            "coverage_no_data": summary["coverage_no_data"],
            "backtest_trades_created": backtest_trades_created,
            "live_trades_found": live_trades_found,
            "jsonl_path": str(audit.path),
            **summary,
        }
    except Exception as exc:
        await session.rollback()
        async with session.begin():
            row = await session.get(BacktestRun, run_id)
            if row is not None:
                row.finished_at = now_utc()
                row.status = "FAILED"
                row.summary_json = {"error": f"{type(exc).__name__}: {str(exc)}"}
            await audit.emit(
                session,
                run_id=run_id,
                event_type="RUN_FAILED",
                payload={"error": f"{type(exc).__name__}: {str(exc)}"},
            )
        raise
