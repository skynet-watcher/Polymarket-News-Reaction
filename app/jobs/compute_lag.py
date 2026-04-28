from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Optional

from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.lag import (
    compute_baseline,
    eventual_move_thresholds,
    first_crossing_after,
    log1p_seconds,
    p_implied,
    zscore,
)
from app.models import (
    LagMeasurement,
    LagScoreSnapshot,
    LagThresholdCrossing,
    Market,
    NewsArticle,
    NewsSignal,
    NewsSource,
    PriceSnapshot,
    SignalDriftWindow,
)
from app.settings import settings
from app.threshold_context import resolve_trading_thresholds
from app.util import new_id, now_utc


PRE_WINDOWS_MIN = [1, 5, 15, 60]
POST_WINDOWS_MIN = [1, 5, 15, 60, 240]


def _is_hard_tier(tier: Optional[str]) -> bool:
    if tier is None:
        return False
    return tier.upper() in {"HARD", "RESOLUTION_SOURCE"}


async def _nearest_snapshot_before(
    session: AsyncSession, *, market_id: str, at: dt.datetime
) -> Optional[PriceSnapshot]:
    res = await session.execute(
        select(PriceSnapshot)
        .where(PriceSnapshot.market_id == market_id)
        .where(PriceSnapshot.timestamp <= at)
        .order_by(PriceSnapshot.timestamp.desc())
        .limit(1)
    )
    return res.scalar_one_or_none()


async def _nearest_snapshot_after_before(
    session: AsyncSession,
    *,
    market_id: str,
    at_or_before: dt.datetime,
    strictly_after: dt.datetime,
) -> Optional[PriceSnapshot]:
    """
    Latest snapshot with strictly_after < timestamp <= at_or_before.

    Used for POST drift windows so we never reuse a pre-signal (or at-signal) snapshot as “post” data.
    """
    res = await session.execute(
        select(PriceSnapshot)
        .where(PriceSnapshot.market_id == market_id)
        .where(PriceSnapshot.timestamp > strictly_after)
        .where(PriceSnapshot.timestamp <= at_or_before)
        .order_by(PriceSnapshot.timestamp.desc())
        .limit(1)
    )
    return res.scalar_one_or_none()


async def _snapshots_in_window(
    session: AsyncSession, *, market_id: str, start: dt.datetime, end: dt.datetime
) -> list[PriceSnapshot]:
    res = await session.execute(
        select(PriceSnapshot)
        .where(PriceSnapshot.market_id == market_id)
        .where(PriceSnapshot.timestamp > start)
        .where(PriceSnapshot.timestamp <= end)
        .order_by(PriceSnapshot.timestamp.asc())
    )
    return list(res.scalars().all())


async def run_backfill(
    session: AsyncSession,
    *,
    limit: int = 200,
    recompute: bool = False,
) -> dict[str, Any]:
    """
    Computes LagMeasurement rows for qualifying ACT signals.
    Idempotent: each signal has at most one LagMeasurement (signal_id UNIQUE).
    """
    tctx = await resolve_trading_thresholds(session)
    q = (
        select(NewsSignal)
        .join(NewsArticle, NewsArticle.id == NewsSignal.article_id)
        .where(
            and_(
                NewsSignal.action == "ACT",
                NewsSignal.interpreted_outcome.in_(["YES", "NO"]),
                NewsSignal.verifier_agrees == True,  # noqa: E712
            )
        )
        # Oldest information-first (stable historical backfills).
        .order_by(NewsArticle.published_at.asc(), NewsSignal.created_at.asc())
        .limit(limit)
    )
    signals = (await session.execute(q)).scalars().all()
    if not signals:
        return {"processed": 0, "created": 0, "updated": 0, "scored": 0}

    created = 0
    updated = 0
    touched_market_outcomes: set[tuple[str, str]] = set()

    for sig in signals:
        existing = (
            await session.execute(select(LagMeasurement).where(LagMeasurement.signal_id == sig.id))
        ).scalar_one_or_none()
        if existing is not None and not recompute:
            continue

        if existing is not None and recompute:
            await session.execute(delete(LagThresholdCrossing).where(LagThresholdCrossing.lag_measurement_id == existing.id))
            await session.execute(delete(SignalDriftWindow).where(SignalDriftWindow.lag_measurement_id == existing.id))
            await session.execute(delete(LagMeasurement).where(LagMeasurement.id == existing.id))
            await session.commit()

        market = (await session.execute(select(Market).where(Market.id == sig.market_id))).scalar_one()
        article = (await session.execute(select(NewsArticle).where(NewsArticle.id == sig.article_id))).scalar_one()
        source = (await session.execute(select(NewsSource).where(NewsSource.id == article.source_id))).scalar_one()

        if market.is_fixture:
            continue

        signal_time = article.published_at
        implied_outcome = sig.interpreted_outcome
        touched_market_outcomes.add((market.id, implied_outcome))

        signal_correct: Optional[bool]
        if market.winning_outcome is None:
            signal_correct = None
        else:
            signal_correct = implied_outcome == market.winning_outcome

        baseline_snapshot = await _nearest_snapshot_before(session, market_id=market.id, at=signal_time)
        baseline = compute_baseline(baseline_snapshot, implied_outcome=implied_outcome)

        sufficient_liquidity = (baseline.liquidity or 0.0) >= settings.lag_min_liquidity
        spread_ok = baseline.spread is not None and baseline.spread <= settings.lag_max_spread
        price_within_range = (
            baseline.p0 is not None
            and settings.lag_min_signal_price <= baseline.p0 <= settings.lag_max_signal_price
        )

        lm = LagMeasurement(
            id=new_id("lag"),
            signal_id=sig.id,
            market_id=market.id,
            signal_time=signal_time,
            implied_outcome=implied_outcome,
            category=market.category,
            source_tier=source.source_tier,
            source_name=source.name,
            confidence=float(sig.confidence),
            verifier_confidence=float(sig.verifier_confidence),
            p0=baseline.p0,
            yes_mid_at_signal=baseline.yes_mid,
            yes_best_bid_at_signal=baseline.yes_bid,
            yes_best_ask_at_signal=baseline.yes_ask,
            spread_at_signal=baseline.spread,
            liquidity_at_signal=baseline.liquidity,
            volume_24h_at_signal=baseline.volume_24h,
            eventual_move=None,
            signal_correct=signal_correct,
            sufficient_liquidity=sufficient_liquidity,
            spread_ok=spread_ok,
            price_within_range=price_within_range,
            created_at=now_utc(),
            updated_at=now_utc(),
        )

        # If we can't compute p0 reliably, keep the row but mark insufficient.
        if not (baseline.p0 is not None and sufficient_liquidity and spread_ok and price_within_range):
            lm.price_lag_status = "INSUFFICIENT_DATA"
            session.add(lm)
            created += 1
            await session.commit()
            continue

        # Drift windows (PRE and POST): nearest snapshot at or before target time.
        pre_moves: dict[int, Optional[float]] = {}
        for w in PRE_WINDOWS_MIN:
            snap = await _nearest_snapshot_before(session, market_id=market.id, at=signal_time - dt.timedelta(minutes=w))
            pv = None
            if snap is not None and snap.mid_yes is not None:
                pv = p_implied(yes_mid=float(snap.mid_yes), implied_outcome=implied_outcome)
            pre_moves[w] = pv
            session.add(
                SignalDriftWindow(
                    id=new_id("drift"),
                    lag_measurement_id=lm.id,
                    direction="PRE",
                    window_minutes=w,
                    observed_price=pv,
                    move_from_p0=(baseline.p0 - pv) if (baseline.p0 is not None and pv is not None) else None,
                    observed_at=snap.timestamp if snap is not None else None,
                    created_at=now_utc(),
                )
            )

        post_moves: dict[int, Optional[float]] = {}
        for w in POST_WINDOWS_MIN:
            snap = await _nearest_snapshot_after_before(
                session,
                market_id=market.id,
                at_or_before=signal_time + dt.timedelta(minutes=w),
                strictly_after=signal_time,
            )
            pv = None
            if snap is not None and snap.mid_yes is not None:
                pv = p_implied(yes_mid=float(snap.mid_yes), implied_outcome=implied_outcome)
            post_moves[w] = pv
            session.add(
                SignalDriftWindow(
                    id=new_id("drift"),
                    lag_measurement_id=lm.id,
                    direction="POST",
                    window_minutes=w,
                    observed_price=pv,
                    move_from_p0=(pv - baseline.p0) if (baseline.p0 is not None and pv is not None) else None,
                    observed_at=snap.timestamp if snap is not None else None,
                    created_at=now_utc(),
                )
            )

        # Clean/stale flags per spec.
        def _mv(w: int) -> Optional[float]:
            pv = pre_moves.get(w)
            return (baseline.p0 - pv) if (baseline.p0 is not None and pv is not None) else None

        mv15 = _mv(15)
        mv60 = _mv(60)
        lm.clean_signal = (mv15 is not None and mv60 is not None and abs(mv15) < 0.03 and abs(mv60) < 0.05)
        lm.stale_signal = (mv15 is not None and mv15 >= 0.05) or (mv60 is not None and mv60 >= 0.10)
        # leaky_signal is computed later as a market/outcome attribute; default False here.
        lm.leaky_signal = False

        # Observation window end: 24h default or market end_date if earlier.
        window_natural_end = signal_time + dt.timedelta(hours=settings.lag_max_window_hours)
        window_end = window_natural_end
        market_ended_early = market.end_date is not None and market.end_date < window_natural_end
        if market.end_date is not None and market.end_date < window_end:
            window_end = market.end_date

        # If the market's end/finalization time is at or before the signal time, there is no
        # post-signal observation window for behavioural lag.
        if market.end_date is not None and market.end_date <= signal_time:
            lm.price_lag_status = "MARKET_CLOSED_FIRST"
            session.add(lm)
            created += 1
            await session.commit()
            continue

        snapshots = await _snapshots_in_window(session, market_id=market.id, start=signal_time, end=window_end)

        # Threshold lags: +5pt and +10pt in implied direction.
        thr5 = baseline.p0 + 0.05
        thr10 = baseline.p0 + 0.10
        t5 = first_crossing_after(snapshots, start_time=signal_time, implied_outcome=implied_outcome, threshold_value=thr5)
        t10 = first_crossing_after(snapshots, start_time=signal_time, implied_outcome=implied_outcome, threshold_value=thr10)

        session.add(
            LagThresholdCrossing(
                id=new_id("cross"),
                lag_measurement_id=lm.id,
                threshold_type="POINT_MOVE",
                threshold_label="5PT",
                threshold_value=float(thr5),
                crossed=t5 is not None,
                lag_seconds=(t5 - signal_time).total_seconds() if t5 is not None else None,
                crossed_at=t5,
                created_at=now_utc(),
            )
        )
        session.add(
            LagThresholdCrossing(
                id=new_id("cross"),
                lag_measurement_id=lm.id,
                threshold_type="POINT_MOVE",
                threshold_label="10PT",
                threshold_value=float(thr10),
                crossed=t10 is not None,
                lag_seconds=(t10 - signal_time).total_seconds() if t10 is not None else None,
                crossed_at=t10,
                created_at=now_utc(),
            )
        )

        # Eventual move thresholds (retrospective).
        eventual_move, thr50, thr90 = eventual_move_thresholds(
            snapshots, start_time=signal_time, implied_outcome=implied_outcome, p0=float(baseline.p0)
        )
        lm.eventual_move = float(eventual_move) if eventual_move is not None else None
        if thr50 is not None:
            t50 = first_crossing_after(snapshots, start_time=signal_time, implied_outcome=implied_outcome, threshold_value=thr50)
        else:
            t50 = None
        if thr90 is not None:
            t90 = first_crossing_after(snapshots, start_time=signal_time, implied_outcome=implied_outcome, threshold_value=thr90)
        else:
            t90 = None

        session.add(
            LagThresholdCrossing(
                id=new_id("cross"),
                lag_measurement_id=lm.id,
                threshold_type="EVENTUAL_MOVE",
                threshold_label="50PCT_EVENTUAL",
                threshold_value=float(thr50) if thr50 is not None else None,
                crossed=t50 is not None,
                lag_seconds=(t50 - signal_time).total_seconds() if t50 is not None else None,
                crossed_at=t50,
                created_at=now_utc(),
            )
        )
        session.add(
            LagThresholdCrossing(
                id=new_id("cross"),
                lag_measurement_id=lm.id,
                threshold_type="EVENTUAL_MOVE",
                threshold_label="90PCT_EVENTUAL",
                threshold_value=float(thr90) if thr90 is not None else None,
                crossed=t90 is not None,
                lag_seconds=(t90 - signal_time).total_seconds() if t90 is not None else None,
                crossed_at=t90,
                created_at=now_utc(),
            )
        )

        # Closure lag: administrative metric (use end_date when closed).
        if market.closed and market.end_date is not None and market.end_date > signal_time:
            lm.closure_lag_seconds = (market.end_date - signal_time).total_seconds()

        # Hard-source lag: later ACT signal from HARD / RESOLUTION_SOURCE.
        later_hard = (
            await session.execute(
                select(NewsSignal, NewsArticle, NewsSource)
                .join(NewsArticle, NewsArticle.id == NewsSignal.article_id)
                .join(NewsSource, NewsSource.id == NewsArticle.source_id)
                .where(
                    and_(
                        NewsSignal.market_id == market.id,
                        NewsSignal.action == "ACT",
                        NewsArticle.published_at > signal_time,
                    )
                )
                .order_by(NewsArticle.published_at.asc(), NewsSignal.created_at.asc())
                .limit(50)
            )
        ).all()
        hard_time = None
        for s_row, a_row, src_row in later_hard:
            if _is_hard_tier(src_row.source_tier):
                hard_time = a_row.published_at
                break
        if hard_time is not None:
            lm.hard_source_lag_seconds = (hard_time - signal_time).total_seconds()
            if not _is_hard_tier(source.source_tier):
                lm.soft_to_hard_source_lag_seconds = (hard_time - signal_time).total_seconds()

        # priceLagStatus
        if t10 is not None or t5 is not None:
            lm.price_lag_status = "CROSSED"
        elif market_ended_early and not snapshots:
            lm.price_lag_status = "MARKET_CLOSED_FIRST"
        elif not snapshots:
            lm.price_lag_status = "INSUFFICIENT_DATA"
        else:
            lm.price_lag_status = "NEVER_CROSSED" if not market_ended_early else "MARKET_CLOSED_FIRST"

        session.add(lm)
        created += 1

    await session.commit()

    # Refresh market/outcome leakage flags now that we have rows.
    for market_id, outcome in touched_market_outcomes:
        await _refresh_leakage_flags_for_market_outcome(
            session,
            market_id=market_id,
            implied_outcome=outcome,
            min_confidence=tctx.min_confidence,
            min_verifier_confidence=tctx.min_verifier_confidence,
        )
    await session.commit()

    scored = await compute_scores(session)
    return {"processed": len(signals), "created": created, "updated": updated, "scored": scored}


async def compute_scores(session: AsyncSession) -> int:
    """
    Computes category-adjusted z-scores for the required lag metrics.
    Persists results as LagScoreSnapshot rows.
    """
    run_id = str(uuid.uuid4())
    calculated_at = now_utc()

    inserted = 0

    async def _score_crossing_metric(*, label: str, metric_name: str) -> int:
        nonlocal inserted
        res = await session.execute(
            select(LagMeasurement, LagThresholdCrossing)
            .join(LagThresholdCrossing, LagThresholdCrossing.lag_measurement_id == LagMeasurement.id)
            .where(
                and_(
                    LagThresholdCrossing.threshold_label == label,
                    LagMeasurement.sufficient_liquidity == True,  # noqa: E712
                    LagMeasurement.spread_ok == True,  # noqa: E712
                    LagMeasurement.clean_signal == True,  # noqa: E712
                    LagMeasurement.price_lag_status == "CROSSED",
                    LagThresholdCrossing.lag_seconds.is_not(None),
                )
            )
        )
        rows = res.all()
        if not rows:
            return 0
        by_cat: dict[str, list[tuple[LagMeasurement, float]]] = {}
        for lm, cross in rows:
            cat = lm.category or "__uncat__"
            by_cat.setdefault(cat, []).append((lm, float(cross.lag_seconds)))  # type: ignore[arg-type]
        for cat, items in by_cat.items():
            if len(items) < settings.lag_min_sample_size_for_zscore:
                continue
            transformed = [log1p_seconds(sec) for _, sec in items]
            zs = zscore(transformed)
            for (lm, sec), tval, z in zip(items, transformed, zs):
                session.add(
                    LagScoreSnapshot(
                        id=new_id("score"),
                        score_run_id=run_id,
                        calculated_at=calculated_at,
                        lag_measurement_id=lm.id,
                        metric_name=metric_name,
                        category=lm.category,
                        scoring_category=cat,
                        raw_value=sec,
                        transformed_value=tval,
                        z_score=z,
                        sample_size=len(items),
                        created_at=now_utc(),
                    )
                )
                inserted += 1
        return 1

    async def _score_closure_metric() -> int:
        nonlocal inserted
        res = await session.execute(
            select(LagMeasurement).where(
                and_(
                    LagMeasurement.sufficient_liquidity == True,  # noqa: E712
                    LagMeasurement.spread_ok == True,  # noqa: E712
                    LagMeasurement.clean_signal == True,  # noqa: E712
                    LagMeasurement.closure_lag_seconds.is_not(None),
                )
            )
        )
        rows = res.scalars().all()
        if not rows:
            return 0
        by_cat: dict[str, list[tuple[LagMeasurement, float]]] = {}
        for lm in rows:
            cat = lm.category or "__uncat__"
            by_cat.setdefault(cat, []).append((lm, float(lm.closure_lag_seconds)))  # type: ignore[arg-type]
        for cat, items in by_cat.items():
            if len(items) < settings.lag_min_sample_size_for_zscore:
                continue
            transformed = [log1p_seconds(sec) for _, sec in items]
            zs = zscore(transformed)
            for (lm, sec), tval, z in zip(items, transformed, zs):
                session.add(
                    LagScoreSnapshot(
                        id=new_id("score"),
                        score_run_id=run_id,
                        calculated_at=calculated_at,
                        lag_measurement_id=lm.id,
                        metric_name="CLOSURE_LAG",
                        category=lm.category,
                        scoring_category=cat,
                        raw_value=sec,
                        transformed_value=tval,
                        z_score=z,
                        sample_size=len(items),
                        created_at=now_utc(),
                    )
                )
                inserted += 1
        return 1

    # Price lag metrics
    await _score_crossing_metric(label="5PT", metric_name="PRICE_LAG_5PT")
    await _score_crossing_metric(label="10PT", metric_name="PRICE_LAG_10PT")
    await _score_crossing_metric(label="50PCT_EVENTUAL", metric_name="PRICE_LAG_50PCT_EVENTUAL")
    await _score_crossing_metric(label="90PCT_EVENTUAL", metric_name="PRICE_LAG_90PCT_EVENTUAL")
    await _score_closure_metric()

    # Composite lag score (optional): combine z-scores from 10pt price lag + closure lag.
    # We persist as LagScoreSnapshot with metric_name=COMPOSITE_LAG.
    if inserted > 0:
        price_rows = (
            await session.execute(
                select(LagScoreSnapshot)
                .where(and_(LagScoreSnapshot.score_run_id == run_id, LagScoreSnapshot.metric_name == "PRICE_LAG_10PT"))
            )
        ).scalars().all()
        closure_rows = (
            await session.execute(
                select(LagScoreSnapshot)
                .where(and_(LagScoreSnapshot.score_run_id == run_id, LagScoreSnapshot.metric_name == "CLOSURE_LAG"))
            )
        ).scalars().all()
        price_by = {r.lag_measurement_id: r for r in price_rows}
        closure_by = {r.lag_measurement_id: r for r in closure_rows}
        for lag_id, pr in price_by.items():
            cr = closure_by.get(lag_id)
            if pr.z_score is None or cr is None or cr.z_score is None:
                continue
            comp = settings.lag_weight_price * float(pr.z_score) + settings.lag_weight_closure * float(cr.z_score)
            session.add(
                LagScoreSnapshot(
                    id=new_id("score"),
                    score_run_id=run_id,
                    calculated_at=calculated_at,
                    lag_measurement_id=lag_id,
                    metric_name="COMPOSITE_LAG",
                    category=pr.category,
                    scoring_category=pr.scoring_category,
                    raw_value=comp,
                    transformed_value=comp,
                    z_score=None,
                    sample_size=min(pr.sample_size, cr.sample_size),
                    created_at=now_utc(),
                )
            )
            inserted += 1

    await session.commit()
    return inserted


async def _refresh_leakage_flags_for_market_outcome(
    session: AsyncSession,
    *,
    market_id: str,
    implied_outcome: str,
    min_confidence: float,
    min_verifier_confidence: float,
) -> None:
    """
    Computes leaky_signal as a market/outcome attribute:
    if p_implied moved >= 0.10 in the 60m before the earliest approved ACT signal.
    Updates LagMeasurement.leaky_signal for all measurements for that market/outcome.
    """
    # Earliest approved ACT signal time (use published time).
    res = await session.execute(
        select(func.min(NewsArticle.published_at))
        .select_from(NewsSignal)
        .join(NewsArticle, NewsArticle.id == NewsSignal.article_id)
        .where(
            and_(
                NewsSignal.market_id == market_id,
                NewsSignal.action == "ACT",
                NewsSignal.interpreted_outcome == implied_outcome,
                NewsSignal.confidence >= min_confidence,
                NewsSignal.verifier_agrees == True,  # noqa: E712
                NewsSignal.verifier_confidence >= min_verifier_confidence,
            )
        )
    )
    earliest = res.scalar_one_or_none()
    if earliest is None:
        return

    snap_now = await _nearest_snapshot_before(session, market_id=market_id, at=earliest)
    snap_prev = await _nearest_snapshot_before(session, market_id=market_id, at=earliest - dt.timedelta(minutes=60))
    if snap_now is None or snap_prev is None or snap_now.mid_yes is None or snap_prev.mid_yes is None:
        return

    p_now = p_implied(yes_mid=float(snap_now.mid_yes), implied_outcome=implied_outcome)
    p_prev = p_implied(yes_mid=float(snap_prev.mid_yes), implied_outcome=implied_outcome)
    move = p_now - p_prev
    leaky = move >= 0.10

    await session.execute(
        update(LagMeasurement)
        .where(and_(LagMeasurement.market_id == market_id, LagMeasurement.implied_outcome == implied_outcome))
        .values(leaky_signal=leaky)
    )

