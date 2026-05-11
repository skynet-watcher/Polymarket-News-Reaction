from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from typing import Any, Optional

from sqlalchemy import and_, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clob_depth import orderbook_levels_from_payload
from app.http_client import fetch_clob_orderbook, get_with_retry, polymarket_async_client
from app.jobs import sync_markets
from app.models import (
    Market,
    MarketResolutionRecord,
    NewsArticle,
    NewsSignal,
    NewsSource,
    PaperTrade,
    PollRun,
    SourceObservation,
    SportsWatchlist,
    WatchedEventLog,
)
from app.settings import settings
from app.sports_latency.adapters import (
    FinalStateObservation,
    normalize_espn_event,
    normalize_mlb_stats_api,
    normalize_nba_live_cdn,
    normalize_nhl_web_api,
)
from app.util import new_id, now_utc

logger = logging.getLogger(__name__)

INDEPENDENT_TRIGGER_CONFIDENCE = 0.95
MIN_ENTRY_ASK_SIZE = 1.0
MAX_ENTRY_ASK_PRICE = 0.995
SPORT_TAGS = ("nba", "nhl", "mlb")
SUPPORTED_LEAGUES = {"NBA", "NHL", "MLB"}


def sports_day_window(now: Optional[dt.datetime] = None) -> tuple[dt.date, dt.datetime, dt.datetime]:
    base = now or now_utc()
    base = base if base.tzinfo else base.replace(tzinfo=dt.timezone.utc)
    day = base.date()
    start = dt.datetime.combine(day, dt.time(4, 0), tzinfo=dt.timezone.utc)
    end = start + dt.timedelta(days=1, hours=4)
    return day, start, end


async def log_event(
    session: AsyncSession,
    *,
    market_id: str,
    event_type: str,
    source: Optional[str] = None,
    detail: Optional[dict[str, Any]] = None,
    event_at: Optional[dt.datetime] = None,
) -> None:
    session.add(
        WatchedEventLog(
            id=new_id("watchlog"),
            market_id=market_id,
            event_type=event_type,
            event_at_utc=event_at or now_utc(),
            source=source,
            detail_json=detail or {},
        )
    )


async def begin_poll_run(session: AsyncSession, job_type: str) -> tuple[Optional[PollRun], bool]:
    cutoff = now_utc() - dt.timedelta(seconds=90)
    existing = (
        await session.execute(
            select(PollRun)
            .where(PollRun.job_type == job_type, PollRun.status == "running", PollRun.started_at > cutoff)
            .order_by(desc(PollRun.started_at))
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        await log_event(
            session,
            market_id="system",
            event_type="poll_run_skipped_overlap",
            source=job_type,
            detail={"existing_run_id": existing.run_id, "started_at": existing.started_at.isoformat()},
        )
        await session.commit()
        return None, True
    run = PollRun(run_id=new_id("pollrun"), job_type=job_type, started_at=now_utc(), status="running")
    session.add(run)
    # Commit the run marker immediately so overlapping Vercel/manual invocations
    # can see it before the long job finishes.
    await session.commit()
    return run, False


async def finish_poll_run(
    session: AsyncSession,
    run: PollRun,
    *,
    status: str = "completed",
    markets_polled: int = 0,
    error: Optional[str] = None,
) -> None:
    run.finished_at = now_utc()
    run.status = status
    run.markets_polled = markets_polled
    run.error = error
    await session.commit()


async def build_watchlist(session: AsyncSession) -> dict[str, Any]:
    t0 = time.perf_counter()
    run, skipped = await begin_poll_run(session, "watchlist_build")
    if skipped:
        return {"ok": True, "skipped": True, "reason": "overlapping_run"}
    assert run is not None
    day, start, end = sports_day_window()
    created = 0
    updated = 0
    excluded = 0
    seen = 0

    try:
        async with polymarket_async_client() as client:
            raw_markets: list[dict[str, Any]] = []
            for tag in SPORT_TAGS:
                events = await sync_markets._fetch_gamma_events(  # noqa: SLF001
                    client,
                    limit=100,
                    extra_params={"active": "true", "closed": "false", "tag_slug": tag},
                )
                raw_markets.extend(_flatten_events_with_event_fields(events))

        for raw in _dedupe_raw_markets(raw_markets):
            seen += 1
            market_id = str(raw.get("id") or raw.get("market_id") or "").strip()
            name = str(raw.get("question") or raw.get("title") or raw.get("eventTitle") or "").strip()
            if not market_id or not name:
                continue
            scheduled = _event_time(raw)
            if scheduled is None:
                await _exclude_existing_watchlist_row(
                    session,
                    market_id=market_id,
                    day=day,
                    reason="no_scheduled_start",
                    scheduled=None,
                    name=name,
                )
                continue
            if scheduled and not (start <= scheduled < end):
                await _exclude_existing_watchlist_row(
                    session,
                    market_id=market_id,
                    day=day,
                    reason="outside_watch_window",
                    scheduled=scheduled,
                    name=name,
                )
                continue
            league = _league_from_raw(raw)
            sport = _sport_from_league(league)
            is_clean, reason = _classify_market(raw, league=league)
            if league not in SUPPORTED_LEAGUES:
                is_clean = False
                reason = "unsupported_market_type"

            with session.no_autoflush:
                existing = (
                    await session.execute(
                        select(SportsWatchlist).where(
                            SportsWatchlist.market_id == market_id,
                            SportsWatchlist.watchlist_date == day,
                        )
                    )
                ).scalar_one_or_none()
            token_ids = sync_markets._gamma_token_ids(raw)  # noqa: SLF001
            condition_id = _str_or_none(raw.get("conditionId") or raw.get("condition_id"))
            teams = _teams_from_raw(raw, name)
            source_game_ids = _source_game_ids(raw)
            values = {
                "condition_id": condition_id,
                "token_ids_json": token_ids,
                "market_name": name,
                "event_slug": _str_or_none(raw.get("eventSlug") or raw.get("slug")),
                "league": league,
                "sport": sport,
                "home_team": teams[0],
                "away_team": teams[1],
                "source_game_ids_json": source_game_ids,
                "scheduled_start_utc": scheduled,
                "expected_end_utc": scheduled + dt.timedelta(hours=_expected_duration_hours(league)) if scheduled else None,
                "resolution_rules_raw_text": _rules_text(raw),
                "is_clean": is_clean,
                "exclusion_reason": None if is_clean else reason,
                "status": "active" if is_clean else "excluded",
            }
            if existing is None:
                row = SportsWatchlist(id=new_id("sportswatch"), market_id=market_id, watchlist_date=day, **values)
                session.add(row)
                created += 1
                await log_event(
                    session,
                    market_id=market_id,
                    event_type="watchlist_created",
                    source="watchlist_build",
                    detail={"market_name": name, "league": league, "is_clean": is_clean, "exclusion_reason": reason},
                )
            else:
                for key, value in values.items():
                    setattr(existing, key, value)
                updated += 1
            excluded += 0 if is_clean else 1

            # Keep the existing Market table hydrated enough for CLOB/paper helpers.
            await _upsert_minimal_market(session, raw=raw, market_id=market_id, name=name, token_ids=token_ids)

        await finish_poll_run(session, run, markets_polled=seen)
        return {
            "ok": True,
            "watchlist_date": day.isoformat(),
            "markets_seen": seen,
            "created": created,
            "updated": updated,
            "excluded": excluded,
            "duration_ms": int((time.perf_counter() - t0) * 1000),
        }
    except Exception as exc:
        await session.rollback()
        run.status = "failed"
        run.finished_at = now_utc()
        run.error = str(exc)
        session.add(run)
        await session.commit()
        raise


async def poll_sources(session: AsyncSession) -> dict[str, Any]:
    t0 = time.perf_counter()
    run, skipped = await begin_poll_run(session, "independent_source_poll")
    if skipped:
        return {"ok": True, "skipped": True, "reason": "overlapping_run"}
    assert run is not None
    day, _start, _end = sports_day_window()
    markets_polled = 0
    observations_written = 0
    trades_created = 0
    missed_windows = 0
    try:
        rows = (
            await session.execute(
                select(SportsWatchlist)
                .where(SportsWatchlist.watchlist_date == day, SportsWatchlist.status == "active", SportsWatchlist.is_clean == True)  # noqa: E712
                .order_by(SportsWatchlist.scheduled_start_utc.asc().nulls_last())
            )
        ).scalars().all()

        async with polymarket_async_client() as client:
            for watch in rows:
                if not _within_monitoring_window(watch):
                    continue
                markets_polled += 1
                obs = await _fetch_observations_for_watch(client, watch)
                for ob in obs:
                    observations_written += 1
                    await _store_observation(session, run.run_id, ob)
                    await log_event(
                        session,
                        market_id=watch.market_id,
                        event_type="source_poll",
                        source=ob.source,
                        detail={
                            "normalized_status": ob.normalized_status,
                            "confidence": ob.confidence,
                            "winner": ob.winner,
                            "raw_status": ob.raw_status,
                        },
                        event_at=ob.observed_at,
                    )
                    if ob.normalized_status in {"postponed", "cancelled"}:
                        watch.status = ob.normalized_status
                        await log_event(session, market_id=watch.market_id, event_type="postponement_detected", source=ob.source, detail={})
                    if ob.can_trigger_trade():
                        result = await _handle_final_trigger(session, watch, ob)
                        if result == "paper_trade":
                            trades_created += 1
                        elif result == "missed_window_simulation":
                            missed_windows += 1

        await finish_poll_run(session, run, markets_polled=markets_polled)
        return {
            "ok": True,
            "markets_polled": markets_polled,
            "observations_written": observations_written,
            "trades_created": trades_created,
            "missed_windows": missed_windows,
            "duration_ms": int((time.perf_counter() - t0) * 1000),
        }
    except Exception as exc:
        await session.rollback()
        run.status = "failed"
        run.finished_at = now_utc()
        run.error = str(exc)
        session.add(run)
        await session.commit()
        raise


async def check_settlements(session: AsyncSession) -> dict[str, Any]:
    t0 = time.perf_counter()
    run, skipped = await begin_poll_run(session, "settlement_check")
    if skipped:
        return {"ok": True, "skipped": True, "reason": "overlapping_run"}
    assert run is not None
    checked = 0
    resolved = 0
    try:
        rows = (
            await session.execute(
                select(SportsWatchlist)
                .where(SportsWatchlist.status.in_(["active", "outcome_known", "paper_trade_open"]))
                .order_by(SportsWatchlist.created_at.desc())
                .limit(200)
            )
        ).scalars().all()
        async with polymarket_async_client() as client:
            for watch in rows:
                checked += 1
                data = await _fetch_gamma_market(client, watch.market_id)
                if not data:
                    continue
                closed = bool(data.get("closed"))
                winning = sync_markets._normalize_binary_winner(data.get("winner") or data.get("winningOutcome") or data.get("outcome"))  # noqa: SLF001
                if closed or winning:
                    resolved += 1
                    at = now_utc()
                    rec = await _resolution_record(session, watch.market_id, watch.condition_id)
                    rec.polymarket_market_resolved_at = rec.polymarket_market_resolved_at or at
                    rec.signal_case = _signal_case(rec)
                    _compute_metrics(rec)
                    watch.status = "resolved"
                    await log_event(
                        session,
                        market_id=watch.market_id,
                        event_type="market_resolved",
                        source="settlement_check",
                        detail={"winning_outcome": winning, "closed": closed},
                        event_at=at,
                    )
                    await _finalize_sports_trades(session, watch, winning)
        await finish_poll_run(session, run, markets_polled=checked)
        return {"ok": True, "checked": checked, "resolved": resolved, "duration_ms": int((time.perf_counter() - t0) * 1000)}
    except Exception as exc:
        await session.rollback()
        run.status = "failed"
        run.finished_at = now_utc()
        run.error = str(exc)
        session.add(run)
        await session.commit()
        raise


async def _fetch_observations_for_watch(client, watch: SportsWatchlist) -> list[FinalStateObservation]:
    ids = watch.source_game_ids_json or {}
    tasks = []
    if watch.league == "NBA":
        tasks.append(_fetch_nba(client, watch, _str_or_none(ids.get("nba_live_cdn"))))
    if watch.league == "NHL":
        tasks.append(_fetch_nhl(client, watch, _str_or_none(ids.get("nhl_web_api"))))
    if watch.league == "MLB":
        tasks.append(_fetch_mlb(client, watch, _str_or_none(ids.get("mlb_stats_api"))))
    if watch.league in SUPPORTED_LEAGUES:
        tasks.append(_fetch_espn(client, watch, _str_or_none(ids.get("espn"))))
    if not tasks:
        return []
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return [r for r in results if isinstance(r, FinalStateObservation)]


async def _fetch_nba(client, watch: SportsWatchlist, game_id: Optional[str]) -> FinalStateObservation:
    if game_id:
        try:
            url = f"https://cdn.nba.com/static/json/liveData/boxscore/boxscore_{game_id}.json"
            r = await get_with_retry(client, url, max_retries=0)
            r.raise_for_status()
            return normalize_nba_live_cdn(r.json(), market_id=watch.market_id, condition_id=watch.condition_id, source_game_id=game_id, observed_at=now_utc())
        except Exception:
            pass
    r = await get_with_retry(client, "https://cdn.nba.com/static/json/liveData/scoreboard/todaysScoreboard_00.json", max_retries=0)
    r.raise_for_status()
    games = r.json().get("scoreboard", {}).get("games", [])
    game = _find_game_by_teams(games, watch)
    if not isinstance(game, dict):
        raise ValueError(f"NBA game not found for {watch.market_name}")
    resolved_game_id = str(game.get("gameId") or game_id or "")
    return normalize_nba_live_cdn({"game": game}, market_id=watch.market_id, condition_id=watch.condition_id, source_game_id=resolved_game_id, observed_at=now_utc())


async def _fetch_nhl(client, watch: SportsWatchlist, game_id: Optional[str]) -> FinalStateObservation:
    date_s = (watch.scheduled_start_utc or now_utc()).date().isoformat()
    r = await get_with_retry(client, f"https://api-web.nhle.com/v1/score/{date_s}", max_retries=0)
    r.raise_for_status()
    games = r.json().get("games") if isinstance(r.json(), dict) else []
    game = next((g for g in games if game_id and str(g.get("id")) == game_id), None)
    if game is None:
        game = _find_game_by_teams(games, watch)
    if not isinstance(game, dict):
        raise ValueError(f"NHL game not found for {watch.market_name}")
    return normalize_nhl_web_api(game, market_id=watch.market_id, condition_id=watch.condition_id, source_game_id=str(game.get("id") or game_id or ""), observed_at=now_utc())


async def _fetch_mlb(client, watch: SportsWatchlist, game_id: Optional[str]) -> FinalStateObservation:
    if not game_id:
        date_s = (watch.scheduled_start_utc or now_utc()).date().isoformat()
        r = await get_with_retry(client, "https://statsapi.mlb.com/api/v1/schedule", params={"sportId": "1", "date": date_s}, max_retries=0)
        r.raise_for_status()
        dates = r.json().get("dates") if isinstance(r.json(), dict) else []
        games = []
        for drow in dates or []:
            games.extend(drow.get("games") or [])
        game = _find_game_by_teams(games, watch)
        if not isinstance(game, dict):
            raise ValueError(f"MLB game not found for {watch.market_name}")
        game_id = str(game.get("gamePk") or "")
    url = f"https://statsapi.mlb.com/api/v1.1/game/{game_id}/feed/live"
    r = await get_with_retry(client, url, max_retries=0)
    r.raise_for_status()
    return normalize_mlb_stats_api(r.json(), market_id=watch.market_id, condition_id=watch.condition_id, source_game_id=game_id, observed_at=now_utc())


async def _fetch_espn(client, watch: SportsWatchlist, game_id: Optional[str]) -> FinalStateObservation:
    sport, league = _espn_path(watch.league or "")
    url = f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard"
    r = await get_with_retry(client, url, max_retries=0)
    r.raise_for_status()
    events = r.json().get("events") if isinstance(r.json(), dict) else []
    event = next((e for e in events if game_id and str(e.get("id")) == game_id), None)
    if event is None:
        event = _find_espn_event_by_teams(events, watch)
    if not isinstance(event, dict):
        raise ValueError(f"ESPN game not found for {watch.market_name}")
    return normalize_espn_event(
        event,
        market_id=watch.market_id,
        condition_id=watch.condition_id,
        source_game_id=str(event.get("id") or game_id or ""),
        observed_at=now_utc(),
        sport=sport,
        league=(watch.league or "").upper(),
    )


async def _store_observation(session: AsyncSession, poll_run_id: str, ob: FinalStateObservation) -> None:
    session.add(
        SourceObservation(
            id=new_id("srcobs"),
            poll_run_id=poll_run_id,
            market_id=ob.polymarket_market_id,
            condition_id=ob.polymarket_condition_id,
            source=ob.source,
            source_role=ob.source_role,
            sport=ob.sport,
            league=ob.league,
            source_game_id=ob.source_game_id,
            observed_at=ob.observed_at,
            source_reported_at=ob.source_reported_at,
            timestamp_type=ob.timestamp_type,
            raw_status=ob.raw_status,
            normalized_status=ob.normalized_status,
            home_team=ob.home_team,
            away_team=ob.away_team,
            home_score=ob.home_score,
            away_score=ob.away_score,
            winner=ob.winner,
            confidence=ob.confidence,
            raw_payload_hash=ob.raw_payload_hash,
            raw_payload_json=_small_payload(ob.raw_payload),
        )
    )


async def _handle_final_trigger(session: AsyncSession, watch: SportsWatchlist, ob: FinalStateObservation) -> str:
    if not _observation_after_game_start(watch, ob):
        await log_event(
            session,
            market_id=watch.market_id,
            event_type="trade_skipped",
            source=ob.source,
            detail={"reason": "final_before_scheduled_game_window", "observed_at": ob.observed_at.isoformat()},
            event_at=ob.observed_at,
        )
        return "ignored"
    rec = await _resolution_record(session, watch.market_id, watch.condition_id)
    if rec.independent_source_observed_final_at is None:
        rec.independent_source = ob.source
        rec.independent_source_observed_final_at = ob.observed_at
        rec.independent_source_reported_final_at = ob.source_reported_at
        _compute_metrics(rec)
        await log_event(
            session,
            market_id=watch.market_id,
            event_type="final_detected",
            source=ob.source,
            detail={"winner": ob.winner, "confidence": ob.confidence},
            event_at=ob.observed_at,
        )
    if rec.polymarket_market_resolved_at and rec.polymarket_market_resolved_at <= ob.observed_at:
        await _create_missed_window(session, watch, ob)
        return "missed_window_simulation"
    trade = await _create_paper_trade(session, watch, ob)
    if trade is not None:
        watch.status = "paper_trade_open"
        return "paper_trade"
    return "skipped"


async def _create_paper_trade(session: AsyncSession, watch: SportsWatchlist, ob: FinalStateObservation) -> Optional[PaperTrade]:
    existing = (
        await session.execute(
            select(PaperTrade)
            .where(PaperTrade.sports_watch_market_id == watch.market_id, PaperTrade.sports_trade_type == "paper_trade")
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return None
    market = await session.get(Market, watch.market_id)
    if market is None or market.closed or not market.active:
        await _create_missed_window(session, watch, ob)
        return None
    side = await _outcome_to_yes_no(session, watch, ob)
    if side is None:
        await _trade_skipped(session, watch, "ambiguous_team_mapping", ob)
        return None
    token_id = _token_for_side(watch, side)
    if not token_id:
        await _trade_skipped(session, watch, "no_token_id", ob)
        return None
    async with polymarket_async_client() as client:
        book = await fetch_clob_orderbook(client, token_id)
    bids, asks = orderbook_levels_from_payload(book or {})
    if not asks:
        await _trade_skipped(session, watch, "no_liquidity", ob)
        return None
    entry_ask_price, entry_ask_size = asks[0]
    if entry_ask_price > MAX_ENTRY_ASK_PRICE or entry_ask_size < MIN_ENTRY_ASK_SIZE:
        await _trade_skipped(
            session,
            watch,
            "price_too_high" if entry_ask_price > MAX_ENTRY_ASK_PRICE else "no_liquidity",
            ob,
            {"entry_ask_price": entry_ask_price, "entry_ask_size": entry_ask_size},
        )
        return None

    source = await _ensure_sports_source(session)
    article = NewsArticle(
        id=new_id("sports_art"),
        source_id=source.id,
        source_domain=source.domain,
        source_tier="HARD",
        url=f"https://sports-latency.internal/{watch.market_id}/{ob.raw_payload_hash}",
        title=f"[SPORTS FINAL] {watch.market_name}",
        body=f"{ob.source} reported final for {watch.market_name}; winner={ob.winner}.",
        published_at=ob.observed_at,
        fetched_at=ob.observed_at,
        content_hash=ob.raw_payload_hash,
    )
    session.add(article)
    await session.flush()
    signal = NewsSignal(
        id=new_id("sports_sig"),
        market_id=watch.market_id,
        article_id=article.id,
        relevance_score=1.0,
        interpreted_outcome="YES" if side == "yes" else "NO",
        evidence_type="DIRECT",
        supporting_excerpt=article.body,
        confidence=ob.confidence,
        verifier_agrees=True,
        verifier_confidence=ob.confidence,
        action="ACT",
        signal_source_type="SPORTS_LATENCY_FINAL",
        raw_interpretation={"observation_source": ob.source, "winner": ob.winner, "sports_watch_market_id": watch.market_id},
        raw_verifier={"verifier_agrees": True, "confidence": ob.confidence, "paper_only": True},
        created_at=ob.observed_at,
    )
    session.add(signal)
    notional = float(settings.paper_trade_notional_usd)
    contracts = min(notional / max(entry_ask_price, 0.01), entry_ask_size)
    cash_spent = round(contracts * entry_ask_price, 4)
    trade = PaperTrade(
        id=new_id("trade"),
        market_id=watch.market_id,
        signal_id=signal.id,
        side="BUY_YES" if side == "yes" else "BUY_NO",
        simulated_size=contracts,
        fill_price=entry_ask_price,
        best_ask_at_signal=entry_ask_price,
        confidence=ob.confidence,
        status="OPEN",
        notional_usd=notional,
        cash_spent_usd=cash_spent,
        trade_source="SPORTS_LATENCY",
        sports_watch_market_id=watch.market_id,
        sports_trade_type="paper_trade",
        trigger_source=ob.source,
        trigger_observed_at=ob.observed_at,
        outcome_side=side,
        entry_ask_size=entry_ask_size,
        pnl_status="open",
        execution_context_json={
            "mode": "sports_latency_ask_v1",
            "entry_ask_price": entry_ask_price,
            "entry_ask_size": entry_ask_size,
            "paper_only": True,
            "timestamp_basis": "independent_source_observed_final_at",
        },
    )
    session.add(trade)
    await log_event(
        session,
        market_id=watch.market_id,
        event_type="trade_created",
        source=ob.source,
        detail={"trade_id": trade.id, "side": side, "entry_ask_price": entry_ask_price, "entry_ask_size": entry_ask_size},
        event_at=ob.observed_at,
    )
    await session.commit()
    return trade


async def _create_missed_window(session: AsyncSession, watch: SportsWatchlist, ob: FinalStateObservation) -> None:
    existing = (
        await session.execute(
            select(PaperTrade)
            .where(PaperTrade.sports_watch_market_id == watch.market_id, PaperTrade.sports_trade_type == "missed_window_simulation")
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return
    source = await _ensure_sports_source(session)
    article = NewsArticle(
        id=new_id("missed_art"),
        source_id=source.id,
        source_domain=source.domain,
        source_tier="HARD",
        url=f"https://sports-latency.internal/missed/{watch.market_id}/{ob.raw_payload_hash}",
        title=f"[MISSED WINDOW] {watch.market_name}",
        body=f"{ob.source} reported final after Polymarket had already resolved or no open check remained.",
        published_at=ob.observed_at,
        fetched_at=ob.observed_at,
        content_hash=new_id("hash"),
    )
    session.add(article)
    await session.flush()
    signal = NewsSignal(
        id=new_id("missed_sig"),
        market_id=watch.market_id,
        article_id=article.id,
        relevance_score=1.0,
        interpreted_outcome="UNKNOWN",
        evidence_type="DIRECT",
        confidence=ob.confidence,
        verifier_agrees=False,
        verifier_confidence=ob.confidence,
        action="ABSTAIN",
        rejection_reason="missed_window",
        signal_source_type="SPORTS_LATENCY_MISSED_WINDOW",
        created_at=ob.observed_at,
    )
    session.add(signal)
    session.add(
        PaperTrade(
            id=new_id("trade"),
            market_id=watch.market_id,
            signal_id=signal.id,
            side="BUY_YES",
            simulated_size=0.0,
            fill_price=0.0,
            confidence=ob.confidence,
            status="MISSED_WINDOW",
            trade_source="SPORTS_LATENCY",
            sports_watch_market_id=watch.market_id,
            sports_trade_type="missed_window_simulation",
            trigger_source=ob.source,
            trigger_observed_at=ob.observed_at,
            outcome_side=ob.winner,
            pnl_status="settled",
            execution_context_json={"mode": "missed_window_simulation", "not_a_trade": True},
        )
    )
    rec = await _resolution_record(session, watch.market_id, watch.condition_id)
    rec.signal_case = "missed_window"
    watch.status = "resolved"
    await log_event(session, market_id=watch.market_id, event_type="missed_window", source=ob.source, detail={"winner": ob.winner}, event_at=ob.observed_at)
    await session.commit()


async def _trade_skipped(
    session: AsyncSession,
    watch: SportsWatchlist,
    reason: str,
    ob: FinalStateObservation,
    extra: Optional[dict[str, Any]] = None,
) -> None:
    await log_event(
        session,
        market_id=watch.market_id,
        event_type="trade_skipped",
        source=ob.source,
        detail={"reason": reason, **(extra or {})},
        event_at=ob.observed_at,
    )
    await session.commit()


async def _resolution_record(session: AsyncSession, market_id: str, condition_id: Optional[str]) -> MarketResolutionRecord:
    rec = await session.get(MarketResolutionRecord, market_id)
    if rec is None:
        rec = MarketResolutionRecord(market_id=market_id, condition_id=condition_id)
        session.add(rec)
        await session.flush()
    elif condition_id and not rec.condition_id:
        rec.condition_id = condition_id
    return rec


def _compute_metrics(rec: MarketResolutionRecord) -> None:
    if rec.polymarket_market_resolved_at and rec.independent_source_observed_final_at:
        rec.tradable_window_observed_ms = _ms(rec.polymarket_market_resolved_at - rec.independent_source_observed_final_at)
    if rec.polymarket_market_resolved_at and rec.independent_source_reported_final_at:
        rec.tradable_window_reported_ms = _ms(rec.polymarket_market_resolved_at - rec.independent_source_reported_final_at)
    if rec.polymarket_market_resolved_at and rec.polymarket_sports_ws_final_at:
        rec.polymarket_internal_delay_ms = _ms(rec.polymarket_market_resolved_at - rec.polymarket_sports_ws_final_at)
    rec.signal_case = _signal_case(rec)


def _signal_case(rec: MarketResolutionRecord) -> str:
    if rec.independent_source_observed_final_at and rec.polymarket_market_resolved_at and rec.independent_source_observed_final_at > rec.polymarket_market_resolved_at:
        return "missed_window"
    if rec.independent_source_observed_final_at and rec.polymarket_sports_ws_final_at and rec.polymarket_market_resolved_at:
        return "normal"
    if rec.polymarket_sports_ws_final_at and not rec.polymarket_market_resolved_at:
        return "sports_ws_no_settlement"
    if rec.polymarket_market_resolved_at and not rec.polymarket_sports_ws_final_at:
        return "settlement_without_sports_signal"
    return "expired_unresolved"


def _ms(delta: dt.timedelta) -> int:
    return int(delta.total_seconds() * 1000)


async def _finalize_sports_trades(session: AsyncSession, watch: SportsWatchlist, winning_outcome: Optional[str]) -> None:
    trades = (
        await session.execute(
            select(PaperTrade).where(
                PaperTrade.sports_watch_market_id == watch.market_id,
                PaperTrade.sports_trade_type == "paper_trade",
                PaperTrade.pnl_status == "open",
            )
        )
    ).scalars().all()
    for trade in trades:
        settlement_price = 1.0 if (winning_outcome and trade.side.endswith(winning_outcome)) else 0.0
        trade.resolved_at = now_utc()
        trade.settlement_price = settlement_price
        trade.pnl_final = round((settlement_price - float(trade.fill_price)) * float(trade.simulated_size), 4)
        trade.pnl_current = trade.pnl_final
        trade.pnl_status = "settled"
        trade.status = "SETTLED_RESOLVED"
        await log_event(
            session,
            market_id=watch.market_id,
            event_type="pnl_finalized",
            source="settlement_check",
            detail={"trade_id": trade.id, "settlement_price": settlement_price, "pnl_usd": trade.pnl_final},
        )
    await session.commit()


async def _ensure_sports_source(session: AsyncSession) -> NewsSource:
    source = (
        await session.execute(select(NewsSource).where(NewsSource.domain == "sports-latency.internal").limit(1))
    ).scalar_one_or_none()
    if source:
        return source
    source = NewsSource(
        name="Sports Latency Final-State Monitor",
        domain="sports-latency.internal",
        rss_url="https://sports-latency.internal/rss",
        source_tier="HARD",
        polling_interval_minutes=1,
        active=True,
    )
    session.add(source)
    await session.flush()
    return source


async def _fetch_gamma_market(client, market_id: str) -> Optional[dict[str, Any]]:
    for url in (
        f"{settings.polymarket_gamma_base_url}/markets/{market_id}",
        f"{settings.polymarket_gamma_base_url}/markets",
    ):
        try:
            params = {"id": market_id} if url.endswith("/markets") else None
            r = await get_with_retry(client, url, params=params, max_retries=0)
            if r.status_code >= 400:
                continue
            data = r.json()
            if isinstance(data, list):
                return next((x for x in data if str(x.get("id")) == market_id), None)
            if isinstance(data, dict):
                return data
        except Exception:
            continue
    return None


async def _upsert_minimal_market(
    session: AsyncSession,
    *,
    raw: dict[str, Any],
    market_id: str,
    name: str,
    token_ids: Optional[list[str]],
) -> None:
    existing = await session.get(Market, market_id)
    outcomes = sync_markets._jsonish_list(raw.get("outcomes") or raw.get("outcome")) or ["YES", "NO"]  # noqa: SLF001
    if existing is None:
        existing = Market(
            id=market_id,
            event_id=_str_or_none(raw.get("eventId") or raw.get("event_id")),
            condition_id=_str_or_none(raw.get("conditionId") or raw.get("condition_id")),
            slug=_str_or_none(raw.get("slug")),
            question=name,
            description=_str_or_none(raw.get("description")),
            category=_league_from_raw(raw),
            outcomes_json=outcomes,
            token_ids_json=token_ids,
            active=bool(raw.get("active", True)),
            closed=bool(raw.get("closed", False)),
            end_date=_event_time(raw),
            rules_text=_rules_text(raw),
            resolution_source_text=_str_or_none(raw.get("resolutionSource") or raw.get("resolution_source")),
            market_type="SPORTS_LATENCY",
        )
        session.add(existing)
    else:
        existing.condition_id = existing.condition_id or _str_or_none(raw.get("conditionId") or raw.get("condition_id"))
        existing.token_ids_json = existing.token_ids_json or token_ids
        existing.market_type = existing.market_type or "SPORTS_LATENCY"
        existing.category = existing.category or _league_from_raw(raw)


def _flatten_events_with_event_fields(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        markets = ev.get("markets")
        if not isinstance(markets, list):
            continue
        for market in markets:
            if not isinstance(market, dict):
                continue
            merged = dict(ev)
            merged.pop("markets", None)
            merged.update(market)
            for key in ("gameId", "sportradarGameId", "score", "period", "live", "ended", "eventDate", "seriesSlug"):
                if not merged.get(key) and ev.get(key) is not None:
                    merged[key] = ev.get(key)
            merged.setdefault("eventSlug", ev.get("slug"))
            merged.setdefault("eventTitle", ev.get("title"))
            rows.append(merged)
    return rows


def _dedupe_raw_markets(raw_markets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Gamma sports tag sweeps can return the same market through multiple tags/events."""
    by_id: dict[str, dict[str, Any]] = {}
    for raw in raw_markets:
        mid = str(raw.get("id") or raw.get("market_id") or "").strip()
        if not mid:
            continue
        if mid not in by_id:
            by_id[mid] = raw
            continue
        existing = by_id[mid]
        # Prefer the row with richer game metadata, but keep any fields the
        # existing row already had. This avoids duplicate inserts without losing
        # condition IDs or token IDs.
        merged = dict(raw)
        merged.update({k: v for k, v in existing.items() if v not in (None, "", [], {})})
        by_id[mid] = merged
    return list(by_id.values())


def _league_from_raw(raw: dict[str, Any]) -> str:
    hay = " ".join(str(raw.get(k) or "") for k in ("tagSlug", "tag_slug", "category", "eventSlug", "slug", "eventTitle", "question")).lower()
    if "nba" in hay:
        return "NBA"
    if "nhl" in hay:
        return "NHL"
    if "mlb" in hay or "baseball" in hay:
        return "MLB"
    return "UNKNOWN"


def _sport_from_league(league: str) -> str:
    return {"NBA": "basketball", "NHL": "hockey", "MLB": "baseball"}.get(league, "unknown")


def _event_time(raw: dict[str, Any]) -> Optional[dt.datetime]:
    for key in ("gameStartTime", "game_start_time", "eventDate", "event_date", "endDate", "end_date", "end_date_iso"):
        value = raw.get(key)
        if not value:
            continue
        parsed = sync_markets._parse_dt(_normalize_gamma_datetime(value))  # noqa: SLF001
        if parsed is not None:
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
    return None


def _normalize_gamma_datetime(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if stripped.endswith("+00"):
        return f"{stripped}:00"
    return stripped


def _within_monitoring_window(watch: SportsWatchlist, *, now: Optional[dt.datetime] = None) -> bool:
    if watch.scheduled_start_utc is None:
        return False
    current = now or now_utc()
    current = current if current.tzinfo else current.replace(tzinfo=dt.timezone.utc)
    scheduled = watch.scheduled_start_utc
    scheduled = scheduled if scheduled.tzinfo else scheduled.replace(tzinfo=dt.timezone.utc)
    return scheduled - dt.timedelta(hours=1) <= current <= scheduled + dt.timedelta(hours=8)


def _observation_after_game_start(watch: SportsWatchlist, ob: FinalStateObservation) -> bool:
    if watch.scheduled_start_utc is None:
        return False
    observed = ob.observed_at if ob.observed_at.tzinfo else ob.observed_at.replace(tzinfo=dt.timezone.utc)
    scheduled = watch.scheduled_start_utc
    scheduled = scheduled if scheduled.tzinfo else scheduled.replace(tzinfo=dt.timezone.utc)
    return observed >= scheduled - dt.timedelta(hours=1)


async def _exclude_existing_watchlist_row(
    session: AsyncSession,
    *,
    market_id: str,
    day: dt.date,
    reason: str,
    scheduled: Optional[dt.datetime],
    name: str,
) -> None:
    """Quarantine stale same-day rows when refreshed Gamma data shows they are not today's game."""
    with session.no_autoflush:
        existing = (
            await session.execute(
                select(SportsWatchlist).where(
                    SportsWatchlist.market_id == market_id,
                    SportsWatchlist.watchlist_date == day,
                )
            )
        ).scalar_one_or_none()
    if existing is None:
        return
    existing.status = "excluded"
    existing.is_clean = False
    existing.exclusion_reason = reason
    existing.scheduled_start_utc = scheduled
    await _invalidate_sports_trades_for_market(session, market_id, reason=reason)
    await log_event(
        session,
        market_id=market_id,
        event_type="trade_skipped",
        source="watchlist_build",
        detail={"market_name": name, "reason": reason},
    )


async def _invalidate_sports_trades_for_market(session: AsyncSession, market_id: str, *, reason: str) -> None:
    trades = (
        await session.execute(
            select(PaperTrade).where(
                PaperTrade.sports_watch_market_id == market_id,
                PaperTrade.sports_trade_type == "paper_trade",
                PaperTrade.pnl_status == "open",
            )
        )
    ).scalars().all()
    for trade in trades:
        trade.status = "INVALIDATED"
        trade.pnl_status = "invalidated"
        trade.execution_context_json = {
            **(trade.execution_context_json or {}),
            "invalidated_reason": reason,
            "invalidated_at": now_utc().isoformat(),
        }


def _classify_market(raw: dict[str, Any], *, league: str) -> tuple[bool, Optional[str]]:
    text = " ".join(str(raw.get(k) or "") for k in ("question", "title", "slug", "rules", "description")).lower()
    bad = {
        "spread": "spread",
        "total": "total",
        "over/under": "total",
        "series": "series_market",
        "championship": "futures",
        "tournament": "futures",
        "player": "prop_bet",
        "points": "prop_bet",
        "rebounds": "prop_bet",
        "home run": "prop_bet",
        "draft": "futures",
        "lottery": "futures",
        "mvp": "futures",
        "conference": "futures",
        "champion": "futures",
        "world series": "futures",
        "stanley cup": "futures",
        "finals": "series_market",
        "regular season": "futures",
        "1h": "unsupported_market_type",
        "1h-moneyline": "unsupported_market_type",
        "first half": "unsupported_market_type",
        "halftime": "unsupported_market_type",
    }
    for needle, reason in bad.items():
        if needle in text:
            return False, reason
    if "win" not in text and "moneyline" not in text and " vs " not in text and " v " not in text:
        return False, "unsupported_market_type"
    if league not in SUPPORTED_LEAGUES:
        return False, "unsupported_market_type"
    if not (raw.get("gameId") or raw.get("sportradarGameId")):
        return False, "unsupported_market_type"
    return True, None


def _teams_from_raw(raw: dict[str, Any], name: str) -> tuple[Optional[str], Optional[str]]:
    home = _str_or_none(raw.get("homeTeam") or raw.get("home_team"))
    away = _str_or_none(raw.get("awayTeam") or raw.get("away_team"))
    if home or away:
        return home, away
    for sep in (" vs. ", " vs ", " v. ", " v "):
        if sep in name:
            left, right = name.split(sep, 1)
            return right.strip(" ?"), left.strip(" ?")
    return None, None


def _source_game_ids(raw: dict[str, Any]) -> dict[str, Any]:
    ids: dict[str, Any] = {}
    league = _league_from_raw(raw)
    game_id = raw.get("gameId") or raw.get("game_id")
    if game_id and league == "NBA":
        # Polymarket gameId is useful for matching, but is not always the NBA CDN game id.
        ids["polymarket_game_id"] = str(game_id)
    elif game_id and league == "NHL":
        ids["nhl_web_api"] = str(game_id)
    elif game_id and league == "MLB":
        ids["mlb_stats_api"] = str(game_id)
    if raw.get("nbaGameId"):
        ids["nba_live_cdn"] = str(raw["nbaGameId"])
    for key in ("nhlGameId",):
        if raw.get(key):
            ids["nhl_web_api"] = str(raw[key])
    for key in ("gamePk", "mlbGamePk"):
        if raw.get(key):
            ids["mlb_stats_api"] = str(raw[key])
    if raw.get("espnGameId"):
        ids["espn"] = str(raw["espnGameId"])
    if raw.get("sportradarGameId"):
        ids["sportradar"] = str(raw["sportradarGameId"])
    return ids


def _find_game_by_teams(games: list[dict[str, Any]], watch: SportsWatchlist) -> Optional[dict[str, Any]]:
    wanted = _team_tokens(watch)
    if not wanted:
        return None
    for game in games:
        text = _game_team_text(game)
        if all(tok in text for tok in wanted):
            return game
    return None


def _find_espn_event_by_teams(events: list[dict[str, Any]], watch: SportsWatchlist) -> Optional[dict[str, Any]]:
    wanted = _team_tokens(watch)
    for event in events:
        text = str(event.get("name") or event.get("shortName") or "").lower()
        comps = event.get("competitions") if isinstance(event.get("competitions"), list) else []
        for comp in comps:
            for row in comp.get("competitors") or []:
                team = row.get("team") if isinstance(row.get("team"), dict) else {}
                text += " " + " ".join(str(team.get(k) or "") for k in ("displayName", "shortDisplayName", "name", "abbreviation")).lower()
        if wanted and all(tok in text for tok in wanted):
            return event
    return None


def _team_tokens(watch: SportsWatchlist) -> list[str]:
    vals = [watch.home_team, watch.away_team]
    out: list[str] = []
    for val in vals:
        s = (val or "").lower()
        parts = [p for p in s.replace(".", "").split() if len(p) > 2]
        if parts:
            out.append(parts[-1])
    return out


def _game_team_text(game: dict[str, Any]) -> str:
    chunks: list[str] = []
    for key in ("homeTeam", "awayTeam"):
        team = game.get(key) if isinstance(game.get(key), dict) else {}
        chunks.extend(str(team.get(k) or "") for k in ("teamName", "name", "placeName", "abbrev", "triCode"))
        default_name = team.get("name") if isinstance(team.get("name"), dict) else {}
        if isinstance(default_name, dict):
            chunks.extend(str(default_name.get(k) or "") for k in ("default", "fr"))
    teams = game.get("teams") if isinstance(game.get("teams"), dict) else {}
    for side in ("home", "away"):
        team = teams.get(side, {}).get("team") if isinstance(teams.get(side), dict) else None
        if isinstance(team, dict):
            chunks.extend(str(team.get(k) or "") for k in ("name", "teamName", "abbreviation"))
    return " ".join(chunks).lower().replace(".", "")


def _expected_duration_hours(league: str) -> float:
    return {"NBA": 2.5, "NHL": 2.75, "MLB": 3.25}.get(league, 3.0)


def _rules_text(raw: dict[str, Any]) -> Optional[str]:
    return _str_or_none(raw.get("rules") or raw.get("resolutionRules") or raw.get("description") or raw.get("resolutionSource"))


def _str_or_none(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _small_payload(payload: dict[str, Any]) -> dict[str, Any]:
    text = str(payload)
    return payload if len(text) < 6000 else {"truncated": True, "keys": sorted(payload.keys())}


def _espn_path(league: str) -> tuple[str, str]:
    return {
        "NBA": ("basketball", "nba"),
        "NHL": ("hockey", "nhl"),
        "MLB": ("baseball", "mlb"),
    }.get(league.upper(), ("basketball", "nba"))


async def _outcome_to_yes_no(session: AsyncSession, watch: SportsWatchlist, ob: FinalStateObservation) -> Optional[str]:
    if ob.winner == "draw":
        return None
    winning_team = ob.home_team if ob.winner == "home" else ob.away_team
    if not winning_team:
        return None
    market = await session.get(Market, watch.market_id)
    outcomes = list(market.outcomes_json or []) if market is not None else []
    winner_tokens = set(_name_tokens(winning_team))
    for idx, outcome in enumerate(outcomes[:2]):
        if winner_tokens.intersection(_name_tokens(str(outcome))):
            return "yes" if idx == 0 else "no"
    # Fallback for "Will Team X win?" binary YES/NO markets.
    name = watch.market_name.lower()
    if "will" in name and " win" in name and winner_tokens.intersection(_name_tokens(name.split(" win")[0])):
        return "yes"
    return None


def _token_for_side(watch: SportsWatchlist, side: str) -> Optional[str]:
    tokens = watch.token_ids_json or []
    if not tokens:
        return None
    if side == "yes":
        return str(tokens[0])
    return str(tokens[1] if len(tokens) > 1 else tokens[0])


def _name_tokens(value: str) -> list[str]:
    cleaned = value.lower().replace(".", "").replace("-", " ")
    return [part for part in cleaned.split() if len(part) > 2 and part not in {"the", "vs", "and"}]
