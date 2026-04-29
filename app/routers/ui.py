from __future__ import annotations

import datetime as dt
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, desc, func, literal, select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.dashboard_data import get_dashboard_snapshot
from app.db import get_session
from app.job_status import build_system_status
from app.models import (
    BacktestCase,
    BacktestRun,
    CryptoMarketProfile,
    LagMeasurement,
    LagThresholdCrossing,
    Market,
    MarketLagScore,
    NewsArticle,
    NewsSignal,
    NewsSource,
    PaperTrade,
    PriceSnapshot,
    ResolutionSourceMapping,
    RuntimeSetting,
    SignalDriftWindow,
    ThresholdProfile,
)
from app.paper_economics import aggregate_portfolio, live_net_mark_usd
from app.settings import settings as app_settings
from app.threshold_context import RUNTIME_KEY_THRESHOLD_PROFILE, resolve_trading_thresholds
from app.threshold_profiles_seed import ensure_default_threshold_profiles
from app.util import format_lag_seconds, new_id, now_utc, to_utc_aware


router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["format_lag"] = format_lag_seconds


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    ctx = await get_dashboard_snapshot(session)
    ctx["request"] = request
    ctx["dashboard_sse_enabled"] = app_settings.dashboard_sse_enabled
    ctx["system_status"] = await build_system_status(session)
    # Template expects ORM rows for initial render; reuse snapshot dict + raw rows for table.
    not_fixture = Market.is_fixture.is_not(True)
    recent = (
        await session.execute(
            select(NewsSignal).join(Market).where(not_fixture).order_by(desc(NewsSignal.created_at)).limit(20)
        )
    ).scalars().all()
    ctx["recent_signals"] = recent
    return templates.TemplateResponse("dashboard.html", ctx)


@router.get("/markets", response_class=HTMLResponse)
async def markets(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    rows = (await session.execute(select(Market).order_by(desc(Market.updated_at)).limit(200))).scalars().all()
    return templates.TemplateResponse("markets.html", {"request": request, "markets": rows})


@router.get("/news", response_class=HTMLResponse)
async def news(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    rows = (await session.execute(select(NewsArticle).order_by(desc(NewsArticle.published_at)).limit(200))).scalars().all()
    return templates.TemplateResponse("news.html", {"request": request, "articles": rows})


@router.get("/signals", response_class=HTMLResponse)
async def signals(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    rows = (
        await session.execute(
            select(NewsSignal)
            .options(selectinload(NewsSignal.article))
            .order_by(desc(NewsSignal.created_at))
            .limit(200)
        )
    ).scalars().all()
    src_row = await session.get(RuntimeSetting, "sync_markets_data_source")
    markets_data_source = (src_row.value if src_row is not None else "") or ""
    # Heuristic: DBs synced before we persisted source — if every market id looks like the offline fixture, warn.
    if markets_data_source != "fixture":
        total_m = int((await session.execute(select(func.count()).select_from(Market))).scalar_one() or 0)
        if total_m > 0:
            demo_m = int(
                (
                    await session.execute(select(func.count()).select_from(Market).where(Market.id.like("demo%")))
                ).scalar_one()
                or 0
            )
            if demo_m == total_m:
                markets_data_source = "fixture"
    return templates.TemplateResponse(
        "signals.html",
        {
            "request": request,
            "signals": rows,
            "markets_data_source": markets_data_source,
        },
    )


@router.get("/trades", response_class=HTMLResponse)
async def trades(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    trade_list = (await session.execute(select(PaperTrade).order_by(desc(PaperTrade.created_at)).limit(200))).scalars().all()

    market_ids = list({t.market_id for t in trade_list})
    live_prices: dict[str, float] = {}
    for mid in market_ids:
        snap = (
            await session.execute(
                select(PriceSnapshot)
                .where(PriceSnapshot.market_id == mid)
                .order_by(PriceSnapshot.timestamp.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if snap is not None and snap.mid_yes is not None:
            live_prices[mid] = float(snap.mid_yes)

    portfolio = await aggregate_portfolio(session)

    trades_with_pnl = []
    for t in trade_list:
        mid = live_prices.get(t.market_id)
        live_pnl = None
        if mid is not None and t.status == "OPEN":
            if t.notional_usd is not None:
                live_pnl = live_net_mark_usd(
                    side=t.side,
                    fill_price=float(t.fill_price),
                    contracts=float(t.simulated_size),
                    yes_mid=float(mid),
                    entry_fee_usd=float(t.entry_fee_usd or 0.0),
                    winning_profit_fee_rate=float(app_settings.polymarket_winning_profit_fee_rate),
                )
            elif t.side == "BUY_YES":
                live_pnl = round((mid - t.fill_price) * t.simulated_size, 2)
            else:
                live_pnl = round(((1.0 - mid) - t.fill_price) * t.simulated_size, 2)
        trades_with_pnl.append({"trade": t, "live_pnl": live_pnl})

    return templates.TemplateResponse(
        "trades.html",
        {"request": request, "trades": trades_with_pnl, "portfolio": portfolio},
    )


@router.get("/analysis", response_class=HTMLResponse)
async def analysis(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    not_fixture = Market.is_fixture.is_not(True)
    act_count = (
        await session.execute(
            select(func.count()).select_from(LagMeasurement).join(Market).where(not_fixture)
        )
    ).scalar_one()

    crossed_count = (
        await session.execute(
            select(func.count())
            .select_from(LagMeasurement)
            .join(Market)
            .where(not_fixture)
            .where(LagMeasurement.price_lag_status == "CROSSED")
        )
    ).scalar_one()

    correct_count = (
        await session.execute(
            select(func.count())
            .select_from(LagMeasurement)
            .join(Market)
            .where(not_fixture)
            .where(LagMeasurement.signal_correct == True)  # noqa: E712
        )
    ).scalar_one()

    crossed_lags = (
        await session.execute(
            select(LagThresholdCrossing.lag_seconds)
            .join(LagMeasurement, LagThresholdCrossing.lag_measurement_id == LagMeasurement.id)
            .join(Market, LagMeasurement.market_id == Market.id)
            .where(
                not_fixture,
                LagThresholdCrossing.threshold_label == "10PT",
                LagThresholdCrossing.lag_seconds.is_not(None),
            )
        )
    ).scalars().all()

    median_lag = None
    if crossed_lags:
        s = sorted(float(x) for x in crossed_lags)
        mid_i = len(s) // 2
        raw = s[mid_i] if len(s) % 2 else (s[mid_i - 1] + s[mid_i]) / 2
        median_lag = format_lag_seconds(raw)

    status_rows = (
        await session.execute(
            select(LagMeasurement.price_lag_status, func.count().label("count"))
            .join(Market, LagMeasurement.market_id == Market.id)
            .where(not_fixture)
            .group_by(LagMeasurement.price_lag_status)
        )
    ).all()
    status_counts = [{"status": r[0], "count": r[1]} for r in status_rows]

    return templates.TemplateResponse(
        "analysis.html",
        {
            "request": request,
            "act_count": act_count,
            "crossed_count": crossed_count,
            "correct_count": correct_count,
            "median_lag": median_lag,
            "status_counts": status_counts,
        },
    )


@router.get("/analysis/backtests", response_class=HTMLResponse)
async def backtests(
    request: Request,
    run_id: Optional[str] = Query(default=None),
    signal_action: Optional[str] = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    runs = (
        await session.execute(select(BacktestRun).order_by(desc(BacktestRun.started_at)).limit(20))
    ).scalars().all()
    selected_run = None
    if run_id:
        selected_run = await session.get(BacktestRun, run_id)
    latest = selected_run or (runs[0] if runs else None)
    cases = []
    coverage_counts = []
    action_counts = []
    if latest is not None:
        case_query = (
            select(BacktestCase)
            .join(Market, BacktestCase.market_id == Market.id)
            .where(BacktestCase.run_id == latest.id)
            .where(Market.is_fixture.is_not(True))
        )
        if signal_action:
            if signal_action == "NONE":
                case_query = case_query.where(BacktestCase.signal_action.is_(None))
            else:
                case_query = case_query.where(BacktestCase.signal_action == signal_action)
        cases = (
            await session.execute(
                case_query
                .order_by(desc(BacktestCase.created_at))
                .limit(200)
            )
        ).scalars().all()
        coverage_rows = (
            await session.execute(
                select(BacktestCase.coverage_status, func.count().label("count"))
                .join(Market, BacktestCase.market_id == Market.id)
                .where(BacktestCase.run_id == latest.id)
                .where(Market.is_fixture.is_not(True))
                .group_by(BacktestCase.coverage_status)
            )
        ).all()
        coverage_counts = [{"status": row[0], "count": row[1]} for row in coverage_rows]
        action_rows = (
            await session.execute(
                select(BacktestCase.signal_action, func.count().label("count"))
                .join(Market, BacktestCase.market_id == Market.id)
                .where(BacktestCase.run_id == latest.id)
                .where(Market.is_fixture.is_not(True))
                .group_by(BacktestCase.signal_action)
                .order_by(desc(func.count()))
            )
        ).all()
        action_counts = [{"action": row[0] or "NONE", "count": row[1]} for row in action_rows]

    return templates.TemplateResponse(
        "backtests.html",
        {
            "request": request,
            "runs": runs,
            "latest": latest,
            "cases": cases,
            "coverage_counts": coverage_counts,
            "action_counts": action_counts,
            "selected_run_id": latest.id if latest is not None else None,
            "selected_signal_action": signal_action or "",
        },
    )


@router.get("/analysis/lags", response_class=HTMLResponse)
async def lag_analysis(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    not_fixture = Market.is_fixture.is_not(True)
    lms = (
        await session.execute(
            select(LagMeasurement)
            .join(Market, LagMeasurement.market_id == Market.id)
            .where(not_fixture)
            .order_by(desc(LagMeasurement.created_at))
            .limit(200)
        )
    ).scalars().all()
    if not lms:
        return templates.TemplateResponse("lags.html", {"request": request, "rows": []})

    ids = [lm.id for lm in lms]
    crossings = (
        await session.execute(select(LagThresholdCrossing).where(LagThresholdCrossing.lag_measurement_id.in_(ids)))
    ).scalars().all()
    drifts = (
        await session.execute(select(SignalDriftWindow).where(SignalDriftWindow.lag_measurement_id.in_(ids)))
    ).scalars().all()

    cross_by = {}
    for c in crossings:
        cross_by.setdefault(c.lag_measurement_id, {})[c.threshold_label] = c

    drift_by = {}
    for d in drifts:
        drift_by.setdefault(d.lag_measurement_id, []).append(d)

    rows = []
    for lm in lms:
        c10 = cross_by.get(lm.id, {}).get("10PT")
        rows.append(
            {
                "lm": lm,
                "lag10": c10.lag_seconds if c10 is not None else None,
                "crossed10": c10.crossed if c10 is not None else False,
                "drifts": drift_by.get(lm.id, []),
            }
        )

    return templates.TemplateResponse("lags.html", {"request": request, "rows": rows})


@router.get("/settings", response_class=HTMLResponse)
async def settings(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    rows = (await session.execute(select(NewsSource).order_by(NewsSource.name))).scalars().all()
    mappings = (
        await session.execute(select(ResolutionSourceMapping).order_by(desc(ResolutionSourceMapping.created_at)).limit(200))
    ).scalars().all()
    lag_row = await session.get(RuntimeSetting, "lag_focus_top_n")
    lag_focus = 0
    if lag_row and (lag_row.value or "").strip().isdigit():
        lag_focus = int(lag_row.value.strip())
    profiles = (await session.execute(select(ThresholdProfile).order_by(ThresholdProfile.id))).scalars().all()
    if not profiles:
        await ensure_default_threshold_profiles(session)
        profiles = (await session.execute(select(ThresholdProfile).order_by(ThresholdProfile.id))).scalars().all()
    tctx = await resolve_trading_thresholds(session)
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "sources": rows,
            "mappings": mappings,
            "lag_focus_top_n": lag_focus,
            "threshold_profiles": profiles,
            "active_threshold_profile_id": tctx.profile_id,
        },
    )


@router.post("/settings/sources/add")
async def add_source(request: Request, session: AsyncSession = Depends(get_session)) -> RedirectResponse:
    form = await request.form()
    name = (form.get("name") or "").strip()
    domain = (form.get("domain") or "").strip().lower().removeprefix("www.")
    rss_url = (form.get("rss_url") or "").strip()
    source_tier = (form.get("source_tier") or "SOFT").strip()
    polling_interval_minutes = int(form.get("polling_interval_minutes") or 5)
    active = (form.get("active") or "on") == "on"

    if name and domain and rss_url:
        session.add(
            NewsSource(
                name=name,
                domain=domain,
                rss_url=rss_url,
                source_tier=source_tier,
                polling_interval_minutes=polling_interval_minutes,
                active=active,
            )
        )
        await session.commit()

    return RedirectResponse(url="/settings", status_code=303)


@router.post("/settings/runtime/threshold-profile")
async def save_threshold_profile(request: Request, session: AsyncSession = Depends(get_session)) -> RedirectResponse:
    form = await request.form()
    pid = (form.get("threshold_profile_id") or "").strip()
    if pid:
        prof = await session.get(ThresholdProfile, pid)
        if prof is not None:
            row = await session.get(RuntimeSetting, RUNTIME_KEY_THRESHOLD_PROFILE)
            if row is None:
                session.add(RuntimeSetting(key=RUNTIME_KEY_THRESHOLD_PROFILE, value=pid))
            else:
                row.value = pid
            await session.commit()
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/settings/runtime/lag-focus")
async def save_lag_focus(request: Request, session: AsyncSession = Depends(get_session)) -> RedirectResponse:
    form = await request.form()
    raw = (form.get("lag_focus_top_n") or "0").strip()
    try:
        n = max(0, int(raw))
    except ValueError:
        n = 0
    row = await session.get(RuntimeSetting, "lag_focus_top_n")
    if row is None:
        session.add(RuntimeSetting(key="lag_focus_top_n", value=str(n)))
    else:
        row.value = str(n)
    await session.commit()
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/settings/resolution-mappings/add")
async def add_resolution_mapping(request: Request, session: AsyncSession = Depends(get_session)) -> RedirectResponse:
    form = await request.form()
    market_id = (form.get("market_id") or "").strip() or None
    source_type = (form.get("source_type") or "SOFT").strip().upper()
    domain = (form.get("domain") or "").strip().lower().removeprefix("www.") or None
    url_pattern = (form.get("url_pattern") or "").strip() or None
    notes = (form.get("notes") or "").strip() or None
    if source_type not in ("HARD", "SOFT"):
        source_type = "SOFT"
    if domain or url_pattern or market_id:
        session.add(
            ResolutionSourceMapping(
                id=new_id("rmap"),
                market_id=market_id,
                source_type=source_type,
                domain=domain,
                url_pattern=url_pattern,
                confidence=1.0,
                notes=notes,
                active=True,
            )
        )
        await session.commit()
    return RedirectResponse(url="/settings", status_code=303)


@router.get("/analysis/laggy-markets", response_class=HTMLResponse)
async def laggy_markets_page(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    not_fixture = Market.is_fixture.is_not(True)
    q = (
        select(MarketLagScore, Market)
        .join(Market, Market.id == MarketLagScore.market_id)
        .where(not_fixture)
        .order_by(desc(MarketLagScore.combined_score))
        .limit(200)
    )
    rows = (await session.execute(q)).all()
    lag_row = await session.get(RuntimeSetting, "lag_focus_top_n")
    lag_focus = 0
    if lag_row and (lag_row.value or "").strip().isdigit():
        lag_focus = int(lag_row.value.strip())
    return templates.TemplateResponse(
        "laggy_markets.html",
        {"request": request, "rows": rows, "lag_focus_top_n": lag_focus},
    )


@router.get("/health", response_class=HTMLResponse)
async def health_check(
    request: Request,
    session: AsyncSession = Depends(get_session),
    smoke: Optional[str] = Query(None),
    smoke_detail: Optional[str] = Query(None),
) -> HTMLResponse:
    now = now_utc()

    # Gate 1: Real price data flowing — exclude fixture markets and pre-fix snapshots
    # (snapshots before 2026-04-28T04:37:35Z were captured when CLOB token IDs were null)
    not_fixture = Market.is_fixture.is_not(True)
    good_quality = func.coalesce(PriceSnapshot.data_quality, "OK") != "PRE_TOKENID_FIX"
    real_snap_count = (
        await session.execute(
            select(func.count())
            .select_from(PriceSnapshot)
            .join(Market, Market.id == PriceSnapshot.market_id)
            .where(not_fixture)
            .where(good_quality)
        )
    ).scalar_one() or 0

    real_market_count = (
        await session.execute(
            select(func.count(func.distinct(PriceSnapshot.market_id)))
            .select_from(PriceSnapshot)
            .join(Market, Market.id == PriceSnapshot.market_id)
            .where(not_fixture)
            .where(good_quality)
        )
    ).scalar_one() or 0

    if real_snap_count >= 10:
        gate1_status = "green"
        gate1_message = f"Live prices are being tracked across {real_market_count} real markets."
        gate1_fix = None
    elif real_snap_count > 0:
        gate1_status = "amber"
        gate1_message = f"Some price data is flowing ({real_market_count} markets) — still building up."
        gate1_fix = "Click 'Sync markets' on the Dashboard — price data is still building up."
    else:
        gate1_status = "red"
        gate1_message = "No live market prices yet — the system can't evaluate trades without this."
        gate1_fix = "Click 'Sync markets' on the Dashboard to fetch live prices."

    # Gate 2: Real trade settlement (SETTLED_RESOLVED = actual win/loss from market outcome)
    resolved_count = (
        await session.execute(
            select(func.count()).select_from(PaperTrade).where(PaperTrade.status == "SETTLED_RESOLVED")
        )
    ).scalar_one() or 0

    t24h_count = (
        await session.execute(
            select(func.count()).select_from(PaperTrade).where(PaperTrade.status == "SETTLED_T24H")
        )
    ).scalar_one() or 0

    open_trade_count = (
        await session.execute(
            select(func.count()).select_from(PaperTrade).where(PaperTrade.status == "OPEN")
        )
    ).scalar_one() or 0

    if resolved_count > 0:
        gate2_status = "green"
        gate2_message = f"{resolved_count} trade(s) settled with real win/loss outcomes. P&L numbers are trustworthy."
        gate2_fix = None
    elif t24h_count > 0:
        gate2_status = "green"
        gate2_message = (
            f"{t24h_count} trade(s) settled at 24-hour mark-to-market price. "
            "Full win/loss will appear as each market officially closes."
        )
        gate2_fix = None
    elif open_trade_count > 0:
        gate2_status = "amber"
        gate2_message = (
            f"{open_trade_count} trade(s) are open and waiting to settle. "
            "Settlement happens automatically — check back in 24 hours for the first P&L numbers."
        )
        gate2_fix = None
    else:
        gate2_status = "red"
        gate2_message = "No trades have been placed yet — nothing to settle."
        gate2_fix = "Use the smoke test panel below to place some test trades, then come back tomorrow to see P&L."

    # Gate 3: Live trades firing in the past 7 days
    week_ago = now - dt.timedelta(days=7)
    live_7d = (
        await session.execute(
            select(func.count())
            .select_from(PaperTrade)
            .where(PaperTrade.trade_source == "LIVE")
            .where(PaperTrade.created_at >= week_ago)
        )
    ).scalar_one() or 0

    if live_7d >= 3:
        gate3_status = "green"
        gate3_message = f"{live_7d} live paper trades placed in the past 7 days."
        gate3_fix = None
    elif live_7d > 0:
        gate3_status = "amber"
        gate3_message = f"Only {live_7d} live paper trade(s) in the past 7 days — the system is being very selective."
        gate3_fix = "Go to Settings and switch to the 'Research' trading profile — the current profile may be too strict."
    else:
        gate3_status = "red"
        gate3_message = "No live paper trades in the past 7 days — the system isn't acting on any news signals."
        gate3_fix = "Use the smoke test buttons on this page to place test trades, or go to Settings and switch to the 'Research' trading profile."

    statuses = [gate1_status, gate2_status, gate3_status]
    if "red" in statuses:
        overall = "red"
        overall_message = "Something needs to be fixed before results are meaningful."
    elif "amber" in statuses:
        overall = "amber"
        overall_message = "Everything is running — waiting for trades to settle. No action needed."
    else:
        overall = "green"
        overall_message = "All systems go — results are trustworthy."

    # Stats
    total_trades = (await session.execute(select(func.count()).select_from(PaperTrade))).scalar_one() or 0
    open_trades = (
        await session.execute(select(func.count()).select_from(PaperTrade).where(PaperTrade.status == "OPEN"))
    ).scalar_one() or 0
    win_count = (
        await session.execute(
            select(func.count())
            .select_from(PaperTrade)
            .where(PaperTrade.status == "SETTLED_RESOLVED")
            .where(PaperTrade.pnl_final > 0)
        )
    ).scalar_one() or 0
    pnl_usd = (
        await session.execute(
            select(func.sum(PaperTrade.net_pnl_usd)).where(PaperTrade.status == "SETTLED_RESOLVED")
        )
    ).scalar_one()
    active_markets = (
        await session.execute(select(func.count()).select_from(Market).where(Market.active == True))  # noqa: E712
    ).scalar_one() or 0

    last_article_time = (await session.execute(select(func.max(NewsArticle.fetched_at)))).scalar_one_or_none()
    last_snap_time = (await session.execute(select(func.max(PriceSnapshot.timestamp)))).scalar_one_or_none()

    tctx = await resolve_trading_thresholds(session)

    def _ago(ts: dt.datetime | None) -> str:
        if ts is None:
            return "never"
        delta = int((now - to_utc_aware(ts)).total_seconds())
        if delta < 60:
            return "just now"
        if delta < 3600:
            return f"{delta // 60} min ago"
        if delta < 86400:
            return f"{delta // 3600} hr ago"
        return f"{delta // 86400} day(s) ago"

    return templates.TemplateResponse(
        "health.html",
        {
            "request": request,
            "overall": overall,
            "overall_message": overall_message,
            "gates": [
                {
                    "number": 1,
                    "title": "Live Price Data",
                    "question": "Are we tracking real market prices?",
                    "status": gate1_status,
                    "message": gate1_message,
                    "fix": gate1_fix,
                },
                {
                    "number": 2,
                    "title": "Real Win / Loss Settlement",
                    "question": "Do we know if our trades actually won or lost?",
                    "status": gate2_status,
                    "message": gate2_message,
                    "fix": gate2_fix,
                },
                {
                    "number": 3,
                    "title": "Trades Being Placed",
                    "question": "Is the system actively paper trading?",
                    "status": gate3_status,
                    "message": gate3_message,
                    "fix": gate3_fix,
                },
            ],
            "stats": {
                "total_trades": total_trades,
                "open_trades": open_trades,
                "resolved_trades": resolved_count,
                "win_count": win_count,
                "win_rate": round(100.0 * win_count / resolved_count, 1) if resolved_count > 0 else None,
                "net_pnl": round(float(pnl_usd), 2) if pnl_usd is not None else None,
                "active_markets": active_markets,
                "threshold_profile": tctx.profile_label,
            },
            "last_news_check": _ago(last_article_time),
            "last_price_sync": _ago(last_snap_time),
            "smoke": smoke,
            "smoke_detail": smoke_detail,
        },
    )


@router.get("/analysis/crypto-preflight", response_class=HTMLResponse)
async def crypto_preflight_page(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    """Crypto Market Preflight Scanner — Eric-friendly traffic-light view."""
    profiles = (
        await session.execute(
            select(CryptoMarketProfile).order_by(
                CryptoMarketProfile.monitor_ready.desc(),
                CryptoMarketProfile.parser_confidence.desc().nulls_last(),
                CryptoMarketProfile.updated_at.desc(),
            )
        )
    ).scalars().all()

    counts = {
        "total": len(profiles),
        "ready": sum(1 for p in profiles if p.monitor_status == "READY"),
        "review": sum(1 for p in profiles if p.monitor_status == "PARSER_REVIEW_REQUIRED"),
        "no_orderbook": sum(1 for p in profiles if p.monitor_status == "NO_ORDERBOOK"),
        "future": sum(1 for p in profiles if p.monitor_status == "FUTURE_CANDLE"),
        "unsupported": sum(1 for p in profiles if p.monitor_status == "UNSUPPORTED"),
        "unknown": sum(1 for p in profiles if p.monitor_status == "UNKNOWN"),
    }

    last_run: Optional[dt.datetime] = None
    if profiles:
        last_run = max((p.updated_at for p in profiles), default=None)

    def _status_label(p: CryptoMarketProfile) -> str:
        m = p.monitor_status or "UNKNOWN"
        return {
            "READY": "✅ Ready",
            "FUTURE_CANDLE": "⏳ Future candle",
            "NO_ORDERBOOK": "📭 No orderbook",
            "PARSER_REVIEW_REQUIRED": "🔍 Needs review",
            "UNSUPPORTED": "⛔ Unsupported",
            "UNKNOWN": "❓ Unknown",
        }.get(m, m)

    def _status_color(p: CryptoMarketProfile) -> str:
        m = p.monitor_status or "UNKNOWN"
        return {
            "READY": "green",
            "FUTURE_CANDLE": "amber",
            "NO_ORDERBOOK": "amber",
            "PARSER_REVIEW_REQUIRED": "yellow",
            "UNSUPPORTED": "slate",
            "UNKNOWN": "slate",
        }.get(m, "slate")

    rows = []
    for p in profiles:
        rows.append({
            "id": p.id,
            "market_id": p.market_id,
            "title": (p.title or "")[:90],
            "rule_family": p.rule_family or "UNKNOWN",
            "base_asset": p.base_asset,
            "binance_symbol": p.binance_symbol,
            "candle_interval": p.candle_interval,
            "candle_start": p.candle_start_time_utc.strftime("%Y-%m-%d %H:%M UTC") if p.candle_start_time_utc else None,
            "candle_close": p.candle_close_time_utc.strftime("%Y-%m-%d %H:%M UTC") if p.candle_close_time_utc else None,
            "parser_confidence": round(p.parser_confidence * 100) if p.parser_confidence is not None else None,
            "parser_status": p.parser_status,
            "parser_notes": p.parser_notes,
            "binance_verified": p.binance_verified,
            "binance_open_price": p.binance_open_price,
            "binance_close_price": p.binance_close_price,
            "binance_notes": p.binance_verification_notes,
            "yes_book_usable": p.yes_book_usable,
            "no_book_usable": p.no_book_usable,
            "yes_liquidity": p.yes_liquidity,
            "no_liquidity": p.no_liquidity,
            "yes_best_ask": p.yes_best_ask,
            "monitor_status": p.monitor_status,
            "monitor_ready": p.monitor_ready,
            "status_label": _status_label(p),
            "status_color": _status_color(p),
            "orderbook_notes": p.orderbook_notes,
            "updated_at": p.updated_at.strftime("%Y-%m-%d %H:%M UTC") if p.updated_at else None,
            "polymarket_url": f"https://polymarket.com/event/{p.slug}" if p.slug else None,
        })

    return templates.TemplateResponse(
        "crypto_preflight.html",
        {
            "request": request,
            "counts": counts,
            "rows": rows,
            "last_run": last_run.strftime("%Y-%m-%d %H:%M UTC") if last_run else None,
        },
    )


@router.get("/analysis/soft-accuracy", response_class=HTMLResponse)
async def soft_accuracy_page(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    not_fixture = Market.is_fixture.is_not(True)
    tier_col = func.coalesce(LagMeasurement.source_tier, literal("UNKNOWN"))
    stmt = (
        select(
            tier_col.label("tier"),
            func.count().label("n"),
            func.sum(case((LagMeasurement.signal_correct == True, 1), else_=0)).label("correct"),  # noqa: E712
            func.sum(case((LagMeasurement.signal_correct == False, 1), else_=0)).label("wrong"),
        )
        .select_from(LagMeasurement)
        .join(Market, LagMeasurement.market_id == Market.id)
        .where(LagMeasurement.signal_correct.is_not(None))
        .where(not_fixture)
        .group_by(tier_col)
    )
    raw_rows = (await session.execute(stmt)).all()
    tiers = []
    for r in raw_rows:
        n = int(r.n or 0)
        c = int(r.correct or 0)
        w = int(r.wrong or 0)
        rate = (100.0 * c / (c + w)) if (c + w) > 0 else None
        tiers.append({"tier": r.tier, "n": n, "correct": c, "wrong": w, "rate": rate})
    return templates.TemplateResponse("soft_accuracy.html", {"request": request, "tiers": tiers})
