"""Realtime paper quickstart caps (see app/settings.py)."""

from app.settings import Settings


def test_realtime_paper_quickstart_caps_intervals() -> None:
    s = Settings(
        realtime_paper_quickstart=True,
        background_poll_news_interval_seconds=600,
        background_process_candidates_interval_seconds=540,
        snapshot_interval_seconds=120,
        realtime_poll_min_seconds=30,
        realtime_process_min_seconds=15,
        realtime_snapshot_min_seconds=10,
    )
    assert s.background_poll_news_interval_seconds == 120
    assert s.background_process_candidates_interval_seconds == 60
    assert s.snapshot_interval_seconds == 60
    assert s.realtime_poll_min_seconds == 25
    assert s.realtime_process_min_seconds == 12
    assert s.realtime_snapshot_min_seconds == 8


def test_realtime_paper_quickstart_respects_zero_background() -> None:
    """CI / tests disable loops with 0; quickstart must not raise them."""
    s = Settings(
        realtime_paper_quickstart=True,
        background_poll_news_interval_seconds=0,
        background_process_candidates_interval_seconds=0,
    )
    assert s.background_poll_news_interval_seconds == 0
    assert s.background_process_candidates_interval_seconds == 0


def test_realtime_paper_quickstart_off_leaves_values() -> None:
    s = Settings(
        realtime_paper_quickstart=False,
        background_poll_news_interval_seconds=600,
        background_process_candidates_interval_seconds=540,
    )
    assert s.background_poll_news_interval_seconds == 600
    assert s.background_process_candidates_interval_seconds == 540
