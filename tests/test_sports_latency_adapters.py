import datetime as dt

from app.jobs.sports_latency import _compute_metrics
from app.jobs.sports_latency import _classify_market
from app.jobs.sports_latency import _event_time
from app.jobs.sports_latency import _observation_after_game_start
from app.jobs.sports_latency import _within_monitoring_window
from app.models import MarketResolutionRecord
from app.models import SportsWatchlist
from app.sports_latency.adapters import (
    normalize_espn_event,
    normalize_mlb_stats_api,
    normalize_nba_live_cdn,
    normalize_nhl_web_api,
)


NOW = dt.datetime(2026, 5, 9, 4, 0, tzinfo=dt.timezone.utc)


def test_nba_final_can_trigger_trade():
    obs = normalize_nba_live_cdn(
        {
            "game": {
                "gameStatusText": "Final",
                "homeTeam": {"teamName": "Knicks", "score": 101},
                "awayTeam": {"teamName": "Celtics", "score": 99},
            }
        },
        market_id="m1",
        condition_id="c1",
        source_game_id="001",
        observed_at=NOW,
    )
    assert obs.normalized_status == "final"
    assert obs.confidence == 1.0
    assert obs.winner == "home"
    assert obs.can_trigger_trade()


def test_nhl_off_maps_to_final():
    obs = normalize_nhl_web_api(
        {
            "id": 123,
            "gameState": "OFF",
            "homeTeam": {"name": {"default": "Canucks"}, "score": 4},
            "awayTeam": {"name": {"default": "Oilers"}, "score": 3},
        },
        market_id="m1",
        condition_id="c1",
        source_game_id="123",
        observed_at=NOW,
    )
    assert obs.normalized_status == "final"
    assert obs.confidence == 1.0
    assert obs.winner == "home"


def test_mlb_requires_strict_final_not_game_over_alone():
    obs = normalize_mlb_stats_api(
        {
            "gameData": {
                "status": {"abstractGameState": "Live", "detailedState": "Game Over"},
                "teams": {"home": {"name": "Yankees"}, "away": {"name": "Red Sox"}},
            },
            "liveData": {"linescore": {"teams": {"home": {"runs": 2}, "away": {"runs": 1}}}},
        },
        market_id="m1",
        condition_id="c1",
        source_game_id="99",
        observed_at=NOW,
    )
    assert obs.normalized_status == "live"
    assert obs.confidence == 0.7
    assert not obs.can_trigger_trade()


def test_espn_completed_is_final_but_below_trigger_confidence():
    obs = normalize_espn_event(
        {
            "status": {"type": {"completed": True, "name": "STATUS_FINAL"}},
            "competitions": [
                {
                    "competitors": [
                        {"homeAway": "home", "score": "5", "team": {"displayName": "Mets"}},
                        {"homeAway": "away", "score": "2", "team": {"displayName": "Braves"}},
                    ]
                }
            ],
        },
        market_id="m1",
        condition_id="c1",
        source_game_id="espn1",
        observed_at=NOW,
        sport="baseball",
        league="MLB",
    )
    assert obs.normalized_status == "final"
    assert obs.confidence == 0.9
    assert not obs.can_trigger_trade()


def test_resolution_metrics_keep_observed_and_reported_separate():
    rec = MarketResolutionRecord(
        market_id="m1",
        independent_source_observed_final_at=NOW,
        independent_source_reported_final_at=NOW - dt.timedelta(seconds=2),
        polymarket_sports_ws_final_at=NOW + dt.timedelta(seconds=5),
        polymarket_market_resolved_at=NOW + dt.timedelta(seconds=20),
    )
    _compute_metrics(rec)
    assert rec.tradable_window_observed_ms == 20_000
    assert rec.tradable_window_reported_ms == 22_000
    assert rec.polymarket_internal_delay_ms == 15_000
    assert rec.signal_case == "normal"


def test_resolution_metrics_normalize_naive_and_aware_timestamps():
    rec = MarketResolutionRecord(
        market_id="m1",
        independent_source_observed_final_at=dt.datetime(2026, 5, 9, 4, 0),
        polymarket_market_resolved_at=dt.datetime(2026, 5, 9, 4, 0, 20, tzinfo=dt.timezone.utc),
    )
    _compute_metrics(rec)
    assert rec.tradable_window_observed_ms == 20_000
    assert rec.signal_case == "settlement_without_sports_signal"


def test_classifier_rejects_futures_and_first_half_markets():
    clean, reason = _classify_market(
        {"question": "Will Cleveland Cavaliers receive the first overall pick at the 2026 NBA Draft Lottery?", "gameId": None},
        league="NBA",
    )
    assert not clean
    assert reason == "futures"

    clean, reason = _classify_market(
        {"question": "Knicks vs. 76ers: 1H Moneyline", "slug": "nba-nyk-phi-2026-05-10-1h-moneyline", "gameId": 123},
        league="NBA",
    )
    assert not clean
    assert reason == "unsupported_market_type"


def test_event_time_prefers_actual_game_time_over_market_start_date():
    parsed = _event_time(
        {
            "gameStartTime": "2026-05-16 04:00:00+00",
            "startDate": "2026-05-10T04:03:21.518534Z",
        }
    )
    assert parsed == dt.datetime(2026, 5, 16, 4, 0, tzinfo=dt.timezone.utc)


def test_event_time_ignores_market_created_start_date():
    assert _event_time({"startDate": "2026-05-10T04:03:21.518534Z"}) is None


def test_monitoring_window_blocks_future_games():
    watch = SportsWatchlist(
        id="w1",
        market_id="m1",
        watchlist_date=NOW.date(),
        market_name="Timberwolves vs. Spurs",
        scheduled_start_utc=NOW + dt.timedelta(days=1),
        status="active",
        is_clean=True,
    )
    assert not _within_monitoring_window(watch, now=NOW)
    assert _within_monitoring_window(watch, now=NOW + dt.timedelta(hours=23, minutes=30))


def test_final_observation_before_game_window_cannot_trigger():
    watch = SportsWatchlist(
        id="w1",
        market_id="m1",
        watchlist_date=NOW.date(),
        market_name="Timberwolves vs. Spurs",
        scheduled_start_utc=NOW + dt.timedelta(days=1),
        status="active",
        is_clean=True,
    )
    obs = normalize_nba_live_cdn(
        {
            "game": {
                "gameStatusText": "Final",
                "homeTeam": {"teamName": "Spurs", "score": 110},
                "awayTeam": {"teamName": "Timberwolves", "score": 100},
            }
        },
        market_id="m1",
        condition_id="c1",
        source_game_id="001",
        observed_at=NOW,
    )
    assert not _observation_after_game_start(watch, obs)
