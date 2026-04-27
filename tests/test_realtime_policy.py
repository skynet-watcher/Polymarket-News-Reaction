from __future__ import annotations

from app.realtime_policy import next_poll_news_sleep_seconds, next_process_candidates_sleep_seconds
from app.settings import settings


def test_adaptive_poll_shortens_when_urgent(monkeypatch) -> None:
    monkeypatch.setattr(settings, "realtime_adaptive_enabled", True)
    monkeypatch.setattr(settings, "realtime_poll_min_seconds", 30)
    base = 300
    calm = next_poll_news_sleep_seconds(base_seconds=base, has_open=False, hours=None)
    assert calm == base
    hot = next_poll_news_sleep_seconds(base_seconds=base, has_open=True, hours=1.0)
    assert hot < base
    assert hot >= 30


def test_adaptive_process_respects_floor(monkeypatch) -> None:
    monkeypatch.setattr(settings, "realtime_adaptive_enabled", True)
    monkeypatch.setattr(settings, "realtime_process_min_seconds", 15)
    s = next_process_candidates_sleep_seconds(base_seconds=120, has_open=True, hours=0.5)
    assert s >= 15
