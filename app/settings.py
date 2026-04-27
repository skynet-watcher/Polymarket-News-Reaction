from typing import Optional

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Storage
    database_url: str = "sqlite+aiosqlite:///./data.db"

    # Polymarket public endpoints (override as needed)
    polymarket_gamma_base_url: str = "https://gamma-api.polymarket.com"
    polymarket_clob_base_url: str = "https://clob.polymarket.com"

    # HTTP client (Gamma/CLOB/RSS/LLM-friendly)
    http_user_agent: str = "paper-mvp/0.2"
    http_timeout_seconds: float = 45.0
    http_trust_env: bool = True  # respect HTTP_PROXY / system proxy when True
    http_disable_env_proxy: bool = False  # if True, set trust_env=False on clients
    http_max_retries: int = 3
    http_retry_backoff_seconds: float = 0.5

    # On startup, upsert curated RSS feeds in `app/live_feeds.py` and disable the demo fixture when present.
    auto_seed_news_feeds: bool = True

    # Lag focus: when > 0, process_candidates only matches against top-N laggy markets (by MarketLagScore).
    lag_focus_top_n: int = 0

    # Market lag ranking: minimum liquidity (Gamma) to include in lag score table
    lag_rank_min_liquidity: float = 1000.0
    lag_rank_weight_price: float = 0.8
    lag_rank_weight_resolution: float = 0.2

    # LLM (optional)
    openai_api_key: Optional[str] = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model_interpreter: str = "gpt-4o-mini"
    openai_model_verifier: str = "gpt-4o-mini"

    # MVP gating defaults
    min_liquidity: float = 1000.0
    max_spread: float = 0.08
    min_relevance: float = 0.75
    min_confidence: float = 0.90
    min_verifier_confidence: float = 0.85
    max_article_age_minutes: int = 30

    # Snapshotting: full Gamma sync at most once per this many seconds (CLOB-only ticks may be faster).
    # Default tuned for hands-off: visible progress within an hour without hammering public APIs.
    snapshot_interval_seconds: int = 120

    # Adaptive realtime: shorter polls/syncs when open paper positions exist and resolution is near.
    realtime_adaptive_enabled: bool = True
    realtime_poll_min_seconds: int = 30
    realtime_process_min_seconds: int = 15
    realtime_snapshot_min_seconds: int = 10

    # When true, cap background + snapshot intervals toward faster news → signals → paper (more API/LLM).
    # See README "Realtime paper (overnight)".
    realtime_paper_quickstart: bool = False

    # Log a snapshot-loop heartbeat every N successful ticks (0 = off). Uses INFO.
    snapshot_loop_log_every_n_ticks: int = 20

    # LLM candidate processing (parallel workers, each with own DB session). Lower = fewer tokens in flight.
    llm_max_concurrency: int = 2
    llm_call_timeout_seconds: float = 75.0

    # Background automation (asyncio tasks in app startup). Set any interval to 0 to disable that loop.
    # Defaults: moderate RSS + candidate cadence (token-light), hourly lag pipeline, hourly settlement.
    background_poll_news_interval_seconds: int = 600
    background_process_candidates_interval_seconds: int = 540
    background_lag_pipeline_interval_seconds: int = 3600
    background_settle_interval_seconds: int = 3600

    # T+24h paper settlement: require a post-signal snapshot not older than this many
    # seconds before the nominal settle time (avoids marking stale pre-news prices as T+24).
    settle_t24_snapshot_max_skew_seconds: int = 7200

    # Paper trade economics (USD). ``app/paper_economics.py`` + ``maybe_paper_trade``.
    paper_trade_notional_usd: float = 10.0
    # Simplified Polymarket-style taker fee on notional at entry (e.g. 0.003 = 0.3%).
    polymarket_entry_fee_rate: float = 0.003
    # Fee on *positive* settlement PnL (e.g. 0.02 = 2% of winnings — tune to match docs).
    polymarket_winning_profit_fee_rate: float = 0.02

    # Live trading — disabled until execution module is implemented (paper only for MVP).
    trading_enabled: bool = False

    # Dashboard live updates (SSE). Disable if you proxy in a way that breaks chunked streams.
    dashboard_sse_enabled: bool = True
    dashboard_sse_interval_seconds: float = 3.0

    # Lag module defaults
    lag_min_liquidity: float = 2500.0
    lag_min_signal_price: float = 0.05
    lag_max_signal_price: float = 0.95
    lag_max_spread: float = 0.08
    lag_max_window_hours: int = 24

    lag_weight_price: float = 0.8
    lag_weight_closure: float = 0.2
    lag_min_sample_size_for_zscore: int = 10

    @model_validator(mode="after")
    def apply_realtime_paper_quickstart(self):
        if not self.realtime_paper_quickstart:
            return self
        self.background_poll_news_interval_seconds = min(self.background_poll_news_interval_seconds, 120)
        self.background_process_candidates_interval_seconds = min(self.background_process_candidates_interval_seconds, 60)
        self.snapshot_interval_seconds = min(self.snapshot_interval_seconds, 60)
        self.realtime_poll_min_seconds = min(self.realtime_poll_min_seconds, 25)
        self.realtime_process_min_seconds = min(self.realtime_process_min_seconds, 12)
        self.realtime_snapshot_min_seconds = min(self.realtime_snapshot_min_seconds, 8)
        return self


settings = Settings()

