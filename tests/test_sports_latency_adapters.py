import datetime as dt

from app.jobs.sports_latency import _compute_metrics
from app.jobs.sports_latency import _classify_market
from app.models import MarketResolutionRecord
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
