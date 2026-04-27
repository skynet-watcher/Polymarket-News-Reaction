from __future__ import annotations

import datetime as dt
import time
from collections.abc import Awaitable, Callable
from typing import Any, Optional

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    JobStatus,
    LagMeasurement,
    Market,
    MarketLagScore,
    NewsArticle,
    NewsSignal,
    PaperTrade,
    PriceSnapshot,
    SignalMetrics,
)
from app.settings import settings
from app.util import format_duration_ms, format_elapsed_since, now_utc, to_utc_aware


JOB_LABELS: dict[str, str] = {
    "seed_live_feeds": "Default feeds",
    "sync_markets": "Market sync",
    "poll_news": "News polling",
    "process_candidates": "Candidate processing",
    "settle_trades": "Settlement",
    "lag_backfill": "Lag backfill",
    "signal_metrics": "Signal metrics",
    "lag_ranks": "Lag ranks",
}

JOB_LINKS: dict[str, str] = {
    "sync_markets": "/markets",
    "poll_news": "/news",
    "process_candidates": "/signals",
    "paper_trades": "/trades",
    "lag_backfill": "/analysis/lags",
    "signal_metrics": "/analysis",
    "lag_ranks": "/analysis/laggy-markets",
    "settle_trades": "/trades",
}

JOB_ACTIONS: dict[str, str] = {
    "sync_markets": "/api/jobs/sync_markets",
    "poll_news": "/api/jobs/poll_news",
    "process_candidates": "/api/jobs/process_candidates",
    "lag_backfill": "/api/lag-measurements/backfill",
    "signal_metrics": "/api/jobs/compute_signal_metrics",
    "lag_ranks": "/api/jobs/compute_lag_ranks",
    "settle_trades": "/api/jobs/settle_trades",
}


async def _get_or_create(session: AsyncSession, job_name: str, *, label: Optional[str] = None) -> JobStatus:
    row = await session.get(JobStatus, job_name)
    if row is None:
        row = JobStatus(job_name=job_name, label=label or JOB_LABELS.get(job_name, job_name), status="NEVER")
        session.add(row)
        await session.flush()
    elif label and row.label != label:
        row.label = label
    return row


async def mark_job_running(session: AsyncSession, job_name: str, *, label: Optional[str] = None) -> None:
    row = await _get_or_create(session, job_name, label=label)
    ts = now_utc()
    row.status = "RUNNING"
    row.started_at = ts
    row.finished_at = None
    row.last_error = None
    row.updated_at = ts
    await session.commit()


async def mark_job_success(
    session: AsyncSession,
    job_name: str,
    *,
    label: Optional[str] = None,
    duration_ms: Optional[int] = None,
) -> None:
    row = await _get_or_create(session, job_name, label=label)
    ts = now_utc()
    row.status = "SUCCESS"
    row.finished_at = ts
    row.last_success_at = ts
    row.last_duration_ms = duration_ms
    row.last_error = None
    row.updated_at = ts
    await session.commit()


async def mark_job_failed(
    session: AsyncSession,
    job_name: str,
    exc: BaseException,
    *,
    label: Optional[str] = None,
    duration_ms: Optional[int] = None,
) -> None:
    await session.rollback()
    row = await _get_or_create(session, job_name, label=label)
    ts = now_utc()
    row.status = "FAILED"
    row.finished_at = ts
    row.last_duration_ms = duration_ms
    row.last_error = f"{type(exc).__name__}: {str(exc)}"[:500]
    row.updated_at = ts
    await session.commit()


async def run_tracked_job(
    session: AsyncSession,
    job_name: str,
    func: Callable[[], Awaitable[dict[str, Any]]],
    *,
    label: Optional[str] = None,
) -> dict[str, Any]:
    await mark_job_running(session, job_name, label=label)
    started = time.perf_counter()
    try:
        payload = await func()
    except Exception as exc:
        duration_ms = int((time.perf_counter() - started) * 1000)
        await mark_job_failed(session, job_name, exc, label=label, duration_ms=duration_ms)
        raise
    duration_ms = int((time.perf_counter() - started) * 1000)
    await mark_job_success(session, job_name, label=label, duration_ms=duration_ms)
    return payload


async def run_tracked_background_job(
    job_name: str,
    func: Callable[[AsyncSession], Awaitable[dict[str, Any]]],
    *,
    label: Optional[str] = None,
    session_factory: Callable[[], Any],
) -> dict[str, Any]:
    async with session_factory() as session:
        return await run_tracked_job(session, job_name, lambda: func(session), label=label)


def _age_seconds(ts: Optional[dt.datetime], now: dt.datetime) -> Optional[int]:
    if ts is None:
        return None
    return max(0, int((now - to_utc_aware(ts)).total_seconds()))


def _dot(status_color: str) -> str:
    return {
        "green": "bg-green-400",
        "yellow": "bg-yellow-400",
        "red": "bg-red-500",
    }.get(status_color, "bg-red-500")


def _status_row(
    *,
    key: str,
    label: str,
    job: Optional[JobStatus],
    data_updated_at: Optional[dt.datetime],
    freshness_seconds: int,
    no_data_label: str = "No data",
    detail: str = "",
    now: dt.datetime,
) -> dict[str, Any]:
    duration = format_duration_ms(job.last_duration_ms) if job is not None else "n/a"
    if job is not None and job.status == "RUNNING":
        ts = job.started_at or job.updated_at
        running_for = format_elapsed_since(ts, now=now).replace("ago", "elapsed")
        return {
            "key": key,
            "label": label,
            "color": "yellow",
            "dot_class": _dot("yellow"),
            "status": "Running",
            "age": running_for,
            "detail": detail or "job in progress",
            "duration": running_for,
            "last_error": None,
        }

    if job is not None and job.status == "FAILED":
        ts = job.finished_at or job.updated_at
        return {
            "key": key,
            "label": label,
            "color": "red",
            "dot_class": _dot("red"),
            "status": "Failed",
            "age": format_elapsed_since(ts, now=now),
            "detail": detail,
            "duration": duration,
            "last_error": job.last_error,
        }

    if data_updated_at is None:
        fallback_ts = job.last_success_at if job is not None else None
        return {
            "key": key,
            "label": label,
            "color": "red",
            "dot_class": _dot("red"),
            "status": no_data_label,
            "age": format_elapsed_since(fallback_ts, now=now),
            "detail": detail,
            "duration": duration,
            "last_error": None,
        }

    age = _age_seconds(data_updated_at, now)
    live = age is not None and age <= freshness_seconds
    color = "green" if live else "red"
    return {
        "key": key,
        "label": label,
        "color": color,
        "dot_class": _dot(color),
        "status": "Live" if live else "Stale",
        "age": format_elapsed_since(data_updated_at, now=now),
        "detail": detail,
        "duration": duration,
        "last_error": None,
    }


def _slow_suffix(job_name: str, job: Optional[JobStatus]) -> str:
    if job is None or job.last_duration_ms is None:
        return ""
    ms = job.last_duration_ms
    if job_name in ("lag_backfill", "process_candidates") and ms >= 300_000:
        return " · slow run"
    return ""


def _with_actions(row: dict[str, Any]) -> dict[str, Any]:
    key = str(row["key"])
    href = JOB_LINKS.get(key, "/")
    action_url = JOB_ACTIONS.get(key)
    row["href"] = href
    row["done_url"] = href
    row["action_url"] = action_url
    row["action_label"] = "Retry" if row.get("status") == "Failed" else "Run"
    row["action_enabled"] = bool(action_url and row.get("status") != "Running")
    return row


async def build_system_status(session: AsyncSession) -> list[dict[str, Any]]:
    now = now_utc()
    paper_fresh_s = 2 * 60 * 60 if settings.realtime_paper_quickstart else 24 * 60 * 60
    job_rows = (await session.execute(select(JobStatus))).scalars().all()
    jobs = {j.job_name: j for j in job_rows}

    last_market_sync = (
        await session.execute(select(func.max(PriceSnapshot.timestamp)))
    ).scalar_one_or_none()
    if last_market_sync is None:
        last_market_sync = (await session.execute(select(func.max(Market.updated_at)))).scalar_one_or_none()

    last_article = (await session.execute(select(func.max(NewsArticle.fetched_at)))).scalar_one_or_none()
    last_signal = (await session.execute(select(func.max(NewsSignal.created_at)))).scalar_one_or_none()
    last_trade = (await session.execute(select(func.max(PaperTrade.created_at)))).scalar_one_or_none()
    last_lag = (
        await session.execute(select(func.max(func.coalesce(LagMeasurement.updated_at, LagMeasurement.created_at))))
    ).scalar_one_or_none()
    last_metric = (await session.execute(select(func.max(SignalMetrics.created_at)))).scalar_one_or_none()
    last_rank = (await session.execute(select(func.max(MarketLagScore.updated_at)))).scalar_one_or_none()
    last_settled_trade = (
        await session.execute(
            select(func.max(PaperTrade.created_at)).where(
                or_(PaperTrade.status == "SETTLED_RESOLVED", PaperTrade.status == "SETTLED_T24H")
            )
        )
    ).scalar_one_or_none()

    counts = {
        "markets": int((await session.execute(select(func.count()).select_from(Market))).scalar_one() or 0),
        "articles": int((await session.execute(select(func.count()).select_from(NewsArticle))).scalar_one() or 0),
        "signals": int((await session.execute(select(func.count()).select_from(NewsSignal))).scalar_one() or 0),
        "trades": int((await session.execute(select(func.count()).select_from(PaperTrade))).scalar_one() or 0),
        "lags": int((await session.execute(select(func.count()).select_from(LagMeasurement))).scalar_one() or 0),
        "metrics": int((await session.execute(select(func.count()).select_from(SignalMetrics))).scalar_one() or 0),
        "ranks": int((await session.execute(select(func.count()).select_from(MarketLagScore))).scalar_one() or 0),
        "settled": int(
            (
                await session.execute(
                    select(func.count()).select_from(PaperTrade).where(PaperTrade.status.in_(["SETTLED_RESOLVED", "SETTLED_T24H"]))
                )
            ).scalar_one()
            or 0
        ),
    }

    rows = [
        _status_row(
            key="sync_markets",
            label="Market sync",
            job=jobs.get("sync_markets"),
            data_updated_at=last_market_sync,
            freshness_seconds=5 * 60,
            detail=f"{counts['markets']} markets",
            now=now,
        ),
        _status_row(
            key="poll_news",
            label="News polling",
            job=jobs.get("poll_news"),
            data_updated_at=last_article,
            freshness_seconds=15 * 60,
            detail=f"{counts['articles']} articles",
            now=now,
        ),
        _status_row(
            key="process_candidates",
            label="Candidate processing",
            job=jobs.get("process_candidates"),
            data_updated_at=last_signal,
            freshness_seconds=15 * 60,
            detail=f"{counts['signals']} signals{_slow_suffix('process_candidates', jobs.get('process_candidates'))}",
            now=now,
        ),
        _status_row(
            key="paper_trades",
            label="Paper trades",
            job=None,
            data_updated_at=last_trade,
            freshness_seconds=paper_fresh_s,
            detail=f"{counts['trades']} trades",
            now=now,
        ),
        _status_row(
            key="lag_backfill",
            label="Lag backfill",
            job=jobs.get("lag_backfill"),
            data_updated_at=last_lag,
            freshness_seconds=24 * 60 * 60,
            detail=f"{counts['lags']} measurements{_slow_suffix('lag_backfill', jobs.get('lag_backfill'))}",
            now=now,
        ),
        _status_row(
            key="signal_metrics",
            label="Signal metrics",
            job=jobs.get("signal_metrics"),
            data_updated_at=last_metric,
            freshness_seconds=24 * 60 * 60,
            detail=f"{counts['metrics']} metric rows",
            now=now,
        ),
        _status_row(
            key="lag_ranks",
            label="Lag ranks",
            job=jobs.get("lag_ranks"),
            data_updated_at=last_rank,
            freshness_seconds=24 * 60 * 60,
            detail=f"{counts['ranks']} ranked markets",
            now=now,
        ),
        _status_row(
            key="settle_trades",
            label="Settlement",
            job=jobs.get("settle_trades"),
            data_updated_at=(jobs.get("settle_trades").last_success_at if jobs.get("settle_trades") else last_settled_trade),
            freshness_seconds=24 * 60 * 60,
            no_data_label="Never run",
            detail=f"{counts['settled']} settled trades",
            now=now,
        ),
    ]
    return [_with_actions(row) for row in rows]
