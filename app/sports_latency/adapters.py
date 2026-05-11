from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class FinalStateObservation:
    source: str
    source_role: str
    sport: str
    league: str
    source_game_id: str
    polymarket_market_id: str
    polymarket_condition_id: Optional[str]
    observed_at: dt.datetime
    source_reported_at: Optional[dt.datetime]
    timestamp_type: str
    raw_status: str
    normalized_status: str
    home_team: Optional[str]
    away_team: Optional[str]
    home_score: Optional[float]
    away_score: Optional[float]
    winner: Optional[str]
    confidence: float
    raw_payload_hash: str
    raw_payload: dict[str, Any]

    def can_trigger_trade(self) -> bool:
        return (
            self.source_role == "independent"
            and self.normalized_status == "final"
            and self.winner is not None
            and self.confidence >= 0.95
        )


def hash_payload(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def parse_dt(value: Any) -> Optional[dt.datetime]:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    s = str(value).strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        out = dt.datetime.fromisoformat(s)
        return out if out.tzinfo else out.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def winner_from_scores(home_score: Optional[float], away_score: Optional[float]) -> Optional[str]:
    if home_score is None or away_score is None:
        return None
    if home_score > away_score:
        return "home"
    if away_score > home_score:
        return "away"
    return "draw"


def normalize_nba_live_cdn(
    payload: dict[str, Any],
    *,
    market_id: str,
    condition_id: Optional[str],
    source_game_id: str,
    observed_at: dt.datetime,
) -> FinalStateObservation:
    game = payload.get("game") if isinstance(payload.get("game"), dict) else payload
    home = game.get("homeTeam") if isinstance(game.get("homeTeam"), dict) else {}
    away = game.get("awayTeam") if isinstance(game.get("awayTeam"), dict) else {}
    raw_status = str(game.get("gameStatusText") or game.get("gameStatus") or "")
    home_score = _float_or_none(home.get("score"))
    away_score = _float_or_none(away.get("score"))
    is_final = raw_status.strip().lower() == "final"
    return FinalStateObservation(
        source="nba_live_cdn",
        source_role="independent",
        sport="basketball",
        league="NBA",
        source_game_id=source_game_id,
        polymarket_market_id=market_id,
        polymarket_condition_id=condition_id,
        observed_at=observed_at,
        source_reported_at=parse_dt(game.get("gameTimeUTC")),
        timestamp_type="source_reported" if game.get("gameTimeUTC") else "app_observed",
        raw_status=raw_status,
        normalized_status="final" if is_final else ("live" if raw_status else "unknown"),
        home_team=_team_name(home),
        away_team=_team_name(away),
        home_score=home_score,
        away_score=away_score,
        winner=winner_from_scores(home_score, away_score) if is_final else None,
        confidence=1.0 if is_final else 0.7,
        raw_payload_hash=hash_payload(payload),
        raw_payload=payload,
    )


def normalize_nhl_web_api(
    payload: dict[str, Any],
    *,
    market_id: str,
    condition_id: Optional[str],
    source_game_id: str,
    observed_at: dt.datetime,
) -> FinalStateObservation:
    game = payload
    raw_status = str(game.get("gameState") or game.get("gameScheduleState") or "")
    home = game.get("homeTeam") if isinstance(game.get("homeTeam"), dict) else {}
    away = game.get("awayTeam") if isinstance(game.get("awayTeam"), dict) else {}
    home_score = _float_or_none(home.get("score"))
    away_score = _float_or_none(away.get("score"))
    is_final = raw_status.upper() in {"OFF", "FINAL"}
    return FinalStateObservation(
        source="nhl_web_api",
        source_role="independent",
        sport="hockey",
        league="NHL",
        source_game_id=source_game_id,
        polymarket_market_id=market_id,
        polymarket_condition_id=condition_id,
        observed_at=observed_at,
        source_reported_at=parse_dt(game.get("gameDate")),
        timestamp_type="source_reported" if game.get("gameDate") else "app_observed",
        raw_status=raw_status,
        normalized_status="final" if is_final else ("live" if raw_status else "unknown"),
        home_team=_team_name(home),
        away_team=_team_name(away),
        home_score=home_score,
        away_score=away_score,
        winner=winner_from_scores(home_score, away_score) if is_final else None,
        confidence=1.0 if is_final else 0.7,
        raw_payload_hash=hash_payload(payload),
        raw_payload=payload,
    )


def normalize_mlb_stats_api(
    payload: dict[str, Any],
    *,
    market_id: str,
    condition_id: Optional[str],
    source_game_id: str,
    observed_at: dt.datetime,
) -> FinalStateObservation:
    game_data = payload.get("gameData") if isinstance(payload.get("gameData"), dict) else {}
    live_data = payload.get("liveData") if isinstance(payload.get("liveData"), dict) else {}
    status = game_data.get("status") if isinstance(game_data.get("status"), dict) else {}
    linescore = live_data.get("linescore") if isinstance(live_data.get("linescore"), dict) else {}
    teams = game_data.get("teams") if isinstance(game_data.get("teams"), dict) else {}
    raw_abstract = str(status.get("abstractGameState") or "")
    raw_detailed = str(status.get("detailedState") or "")
    raw_status = f"{raw_abstract} / {raw_detailed}".strip(" /")
    home = teams.get("home") if isinstance(teams.get("home"), dict) else {}
    away = teams.get("away") if isinstance(teams.get("away"), dict) else {}
    home_score = _float_or_none(linescore.get("teams", {}).get("home", {}).get("runs")) if isinstance(linescore.get("teams"), dict) else None
    away_score = _float_or_none(linescore.get("teams", {}).get("away", {}).get("runs")) if isinstance(linescore.get("teams"), dict) else None
    is_strict_final = raw_abstract == "Final" and raw_detailed == "Final"
    is_partial = raw_detailed in {"Game Over", "Completed Early"}
    return FinalStateObservation(
        source="mlb_stats_api",
        source_role="independent",
        sport="baseball",
        league="MLB",
        source_game_id=source_game_id,
        polymarket_market_id=market_id,
        polymarket_condition_id=condition_id,
        observed_at=observed_at,
        source_reported_at=parse_dt(game_data.get("datetime", {}).get("officialDate")) if isinstance(game_data.get("datetime"), dict) else None,
        timestamp_type="source_reported" if isinstance(game_data.get("datetime"), dict) and game_data["datetime"].get("officialDate") else "app_observed",
        raw_status=raw_status,
        normalized_status="final" if is_strict_final else ("live" if is_partial or raw_status else "unknown"),
        home_team=_team_name(home),
        away_team=_team_name(away),
        home_score=home_score,
        away_score=away_score,
        winner=winner_from_scores(home_score, away_score) if is_strict_final else None,
        confidence=1.0 if is_strict_final else 0.7,
        raw_payload_hash=hash_payload(payload),
        raw_payload=payload,
    )


def normalize_espn_event(
    payload: dict[str, Any],
    *,
    market_id: str,
    condition_id: Optional[str],
    source_game_id: str,
    observed_at: dt.datetime,
    sport: str,
    league: str,
) -> FinalStateObservation:
    competitions = payload.get("competitions") if isinstance(payload.get("competitions"), list) else []
    competition = competitions[0] if competitions and isinstance(competitions[0], dict) else {}
    competitors = competition.get("competitors") if isinstance(competition.get("competitors"), list) else []
    home = next((c for c in competitors if c.get("homeAway") == "home"), {})
    away = next((c for c in competitors if c.get("homeAway") == "away"), {})
    status = payload.get("status") if isinstance(payload.get("status"), dict) else {}
    status_type = status.get("type") if isinstance(status.get("type"), dict) else {}
    completed = bool(status_type.get("completed"))
    raw_status = str(status_type.get("name") or status_type.get("description") or status.get("detail") or "")
    home_score = _float_or_none(home.get("score"))
    away_score = _float_or_none(away.get("score"))
    return FinalStateObservation(
        source="espn",
        source_role="independent",
        sport=sport,
        league=league,
        source_game_id=source_game_id,
        polymarket_market_id=market_id,
        polymarket_condition_id=condition_id,
        observed_at=observed_at,
        source_reported_at=None,
        timestamp_type="app_observed",
        raw_status=raw_status,
        normalized_status="final" if completed else ("live" if raw_status else "unknown"),
        home_team=_espn_team_name(home),
        away_team=_espn_team_name(away),
        home_score=home_score,
        away_score=away_score,
        winner=winner_from_scores(home_score, away_score) if completed else None,
        confidence=0.9 if completed else 0.7,
        raw_payload_hash=hash_payload(payload),
        raw_payload=payload,
    )


def normalize_polymarket_sports_ws(
    payload: dict[str, Any],
    *,
    market_id: str,
    condition_id: Optional[str],
    observed_at: dt.datetime,
) -> FinalStateObservation:
    status = str(payload.get("status") or payload.get("gameStatus") or "")
    ended = bool(payload.get("ended"))
    final = ended or status in {"Final", "F/OT", "F/SO", "finished"}
    source_reported_at = parse_dt(payload.get("finished_timestamp") or payload.get("finishedTimestamp"))
    return FinalStateObservation(
        source="polymarket_sports_ws",
        source_role="market_side",
        sport=str(payload.get("sport") or ""),
        league=str(payload.get("league") or ""),
        source_game_id=str(payload.get("gameId") or payload.get("sportradarGameId") or ""),
        polymarket_market_id=market_id,
        polymarket_condition_id=condition_id,
        observed_at=observed_at,
        source_reported_at=source_reported_at,
        timestamp_type="source_reported" if source_reported_at else "app_observed",
        raw_status=status,
        normalized_status="final" if final else ("live" if status else "unknown"),
        home_team=payload.get("homeTeam") or payload.get("home"),
        away_team=payload.get("awayTeam") or payload.get("away"),
        home_score=_float_or_none(payload.get("homeScore")),
        away_score=_float_or_none(payload.get("awayScore")),
        winner=None,
        confidence=1.0 if final else 0.7,
        raw_payload_hash=hash_payload(payload),
        raw_payload=payload,
    )


def _float_or_none(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _team_name(team: dict[str, Any]) -> Optional[str]:
    for key in ("teamName", "name", "placeName", "abbrev", "triCode"):
        value = team.get(key)
        if isinstance(value, dict):
            nested = value.get("default") or value.get("en") or value.get("fr")
            if nested:
                return str(nested)
        if value:
            return str(value)
    return None


def _espn_team_name(row: dict[str, Any]) -> Optional[str]:
    team = row.get("team") if isinstance(row.get("team"), dict) else {}
    return team.get("displayName") or team.get("shortDisplayName") or team.get("name") or row.get("displayName")
