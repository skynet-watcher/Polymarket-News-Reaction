from __future__ import annotations

import datetime as dt
from typing import Any
from typing import Optional, List, Dict

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Market(Base):
    __tablename__ = "markets"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # polymarket market id
    event_id: Mapped[Optional[str]] = mapped_column(String, index=True, nullable=True)
    slug: Mapped[Optional[str]] = mapped_column(String, index=True, nullable=True)
    question: Mapped[str] = mapped_column(Text)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    category: Mapped[Optional[str]] = mapped_column(String, index=True, nullable=True)

    outcomes_json: Mapped[List[str]] = mapped_column(JSON)
    token_ids_json: Mapped[Optional[List[str]]] = mapped_column(JSON, nullable=True)

    active: Mapped[bool] = mapped_column(Boolean, default=True)
    closed: Mapped[bool] = mapped_column(Boolean, default=False)
    end_date: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # When Gamma indicates a resolved binary outcome (best-effort field mapping in sync job).
    winning_outcome: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # YES | NO

    liquidity: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    volume: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    last_price_yes: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    best_bid_yes: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    best_ask_yes: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Gamma metadata (events/markets ingestion)
    resolution_source_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    rules_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    enable_orderbook: Mapped[bool] = mapped_column(Boolean, default=True)
    volume_24h: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Classification / research flags (populated by classifier jobs; nullable on legacy rows).
    market_type: Mapped[Optional[str]] = mapped_column(String, index=True, nullable=True)
    is_control_market: Mapped[bool] = mapped_column(Boolean, default=False)
    manipulation_risk_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    # Offline / demo rows (fixture JSON); excluded from headline analytics / lag aggregates.
    is_fixture: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc))
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: dt.datetime.now(dt.timezone.utc),
        onupdate=lambda: dt.datetime.now(dt.timezone.utc),
    )

    signals: Mapped[list["NewsSignal"]] = relationship(back_populates="market")
    trades: Mapped[list["PaperTrade"]] = relationship(back_populates="market")
    snapshots: Mapped[list["PriceSnapshot"]] = relationship(back_populates="market")
    lag_measurements: Mapped[list["LagMeasurement"]] = relationship(back_populates="market")
    resolution_mappings: Mapped[list["ResolutionSourceMapping"]] = relationship(back_populates="market")
    lag_score_row: Mapped[Optional["MarketLagScore"]] = relationship(back_populates="market", uselist=False)


class NewsSource(Base):
    __tablename__ = "news_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String)
    domain: Mapped[str] = mapped_column(String, unique=True, index=True)
    rss_url: Mapped[str] = mapped_column(String, unique=True)
    source_tier: Mapped[str] = mapped_column(String, default="SOFT")
    polling_interval_minutes: Mapped[int] = mapped_column(Integer, default=5)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc))

    articles: Mapped[list["NewsArticle"]] = relationship(back_populates="source_rel")


class NewsArticle(Base):
    __tablename__ = "news_articles"
    __table_args__ = (UniqueConstraint("url", name="uq_article_url"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)  # stable hash id
    source_id: Mapped[int] = mapped_column(ForeignKey("news_sources.id"), index=True)
    source_domain: Mapped[str] = mapped_column(String, index=True)
    source_tier: Mapped[str] = mapped_column(String, default="SOFT")
    url: Mapped[str] = mapped_column(String, unique=True)
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    published_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc))
    content_hash: Mapped[str] = mapped_column(String, index=True)

    source_rel: Mapped["NewsSource"] = relationship(back_populates="articles")
    signals: Mapped[list["NewsSignal"]] = relationship(back_populates="article")


class NewsSignal(Base):
    __tablename__ = "news_signals"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # cuid-ish
    market_id: Mapped[str] = mapped_column(ForeignKey("markets.id"), index=True)
    article_id: Mapped[str] = mapped_column(ForeignKey("news_articles.id"), index=True)

    relevance_score: Mapped[float] = mapped_column(Float, default=0.0)
    interpreted_outcome: Mapped[str] = mapped_column(String, default="UNKNOWN")  # YES/NO/UNKNOWN
    evidence_type: Mapped[str] = mapped_column(String, default="NONE")  # DIRECT/INDIRECT/PRELIMINARY/SPECULATIVE/NONE
    supporting_excerpt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)

    verifier_agrees: Mapped[bool] = mapped_column(Boolean, default=False)
    verifier_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    action: Mapped[str] = mapped_column(String, default="CANDIDATE")  # CANDIDATE/ACT/ABSTAIN/REJECT_*
    rejection_reason: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    signal_source_type: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    raw_interpretation: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    raw_verifier: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc))

    market: Mapped["Market"] = relationship(back_populates="signals")
    article: Mapped["NewsArticle"] = relationship(back_populates="signals")
    trades: Mapped[list["PaperTrade"]] = relationship(back_populates="signal")
    lag_measurement: Mapped[Optional["LagMeasurement"]] = relationship(back_populates="signal")
    signal_metrics: Mapped[list["SignalMetrics"]] = relationship(back_populates="signal")


class ResolutionSourceMapping(Base):
    """
    Manual / curated mapping of domains or URL patterns to HARD (official) vs SOFT news.
    market_id NULL = global hint pattern; non-null = override for a specific market.
    """

    __tablename__ = "resolution_source_mappings"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    market_id: Mapped[Optional[str]] = mapped_column(String, ForeignKey("markets.id"), index=True, nullable=True)
    source_type: Mapped[str] = mapped_column(String, index=True)  # HARD | SOFT
    domain: Mapped[Optional[str]] = mapped_column(String, index=True, nullable=True)
    url_pattern: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc))

    market: Mapped[Optional["Market"]] = relationship(back_populates="resolution_mappings")


class SignalMetrics(Base):
    """Post-signal price path snapshot at fixed offsets (minutes from article/signal time)."""

    __tablename__ = "signal_metrics"
    __table_args__ = (UniqueConstraint("signal_id", "window_minutes", name="uq_signal_metrics_signal_window"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    signal_id: Mapped[str] = mapped_column(String, ForeignKey("news_signals.id"), index=True)
    market_id: Mapped[str] = mapped_column(String, ForeignKey("markets.id"), index=True)
    window_minutes: Mapped[int] = mapped_column(Integer, index=True)
    signal_time: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    snapshot_timestamp: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    mid_yes: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    best_bid_yes: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    best_ask_yes: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    spread: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    mid_at_signal: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    spread_at_signal: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    delta_mid_from_signal: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    delta_spread_from_signal: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc))

    signal: Mapped["NewsSignal"] = relationship(back_populates="signal_metrics")


class MarketLagScore(Base):
    """Rolling combined lag ranking per market for “laggy markets” discovery."""

    __tablename__ = "market_lag_scores"

    market_id: Mapped[str] = mapped_column(String, ForeignKey("markets.id"), primary_key=True)
    median_price_lag_seconds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    median_resolution_lag_seconds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    combined_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    signal_count: Mapped[int] = mapped_column(Integer, default=0)
    volume_24h: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    liquidity: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    category: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: dt.datetime.now(dt.timezone.utc),
        onupdate=lambda: dt.datetime.now(dt.timezone.utc),
    )

    market: Mapped["Market"] = relationship(back_populates="lag_score_row")


class BiasHypothesis(Base):
    """
    PLACEHOLDER — not wired into the signal pipeline yet.

    The bias-hypothesis spec is being designed separately. Do not add new FK references
    or pipeline logic until that spec is delivered.

    The model (and `PaperTrade.hypothesis_id`) can remain for a future sprint.
    """

    __tablename__ = "bias_hypotheses"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    bias_type: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(Text)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc))

    trades: Mapped[list["PaperTrade"]] = relationship(back_populates="hypothesis")


class PaperTrade(Base):
    __tablename__ = "paper_trades"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    market_id: Mapped[str] = mapped_column(ForeignKey("markets.id"), index=True)
    signal_id: Mapped[str] = mapped_column(ForeignKey("news_signals.id"), index=True)
    hypothesis_id: Mapped[Optional[str]] = mapped_column(ForeignKey("bias_hypotheses.id"), index=True, nullable=True)

    side: Mapped[str] = mapped_column(String)  # BUY_YES / BUY_NO
    simulated_size: Mapped[float] = mapped_column(Float)
    fill_price: Mapped[float] = mapped_column(Float)

    best_bid_at_signal: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    best_ask_at_signal: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    mid_at_signal: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    max_slippage: Mapped[float] = mapped_column(Float, default=0.02)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String, default="OPEN")
    pnl_current: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pnl_final: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # USD economics (see ``app/paper_economics.py``). Legacy rows may have NULLs here.
    notional_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    entry_fee_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    settlement_fee_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    cash_spent_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    gross_pnl_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    net_pnl_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Paper execution audit: ladder summary, partial fill, rejection codes, book depth snapshot.
    execution_context_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)

    # How this trade was settled: GAMMA_WINNING_OUTCOME | T24H_MARK_TO_MARKET | None (unsettled)
    settlement_source: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # LIVE = created by the real-time signal pipeline.
    # BACKTEST = simulated by backtest_news_reactions for a missed or counterfactual trade.
    # Legacy rows default to LIVE (they were all created by the live pipeline).
    trade_source: Mapped[str] = mapped_column(String, default="LIVE", index=True)
    # Set when trade_source == "BACKTEST"; links to the BacktestCase that generated this trade.
    backtest_case_id: Mapped[Optional[str]] = mapped_column(
        String, ForeignKey("backtest_cases.id"), index=True, nullable=True
    )

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc))

    market: Mapped["Market"] = relationship(back_populates="trades")
    signal: Mapped["NewsSignal"] = relationship(back_populates="trades")
    hypothesis: Mapped["BiasHypothesis"] = relationship(back_populates="trades")


class PriceSnapshot(Base):
    __tablename__ = "price_snapshots"
    __table_args__ = (Index("ix_price_snapshots_market_timestamp", "market_id", "timestamp"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    market_id: Mapped[str] = mapped_column(ForeignKey("markets.id"), index=True)
    timestamp: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc))

    best_bid_yes: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    best_ask_yes: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    mid_yes: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    last_price_yes: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    spread: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # best_ask_yes - best_bid_yes
    liquidity: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    volume_24h: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # OK = usable  |  PRE_TOKENID_FIX = captured before 2026-04-28T04:37:35Z (CLOB was broken)
    data_quality: Mapped[str] = mapped_column(String, default="OK")

    market: Mapped["Market"] = relationship(back_populates="snapshots")


class LagMeasurement(Base):
    __tablename__ = "lag_measurements"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    signal_id: Mapped[str] = mapped_column(String, ForeignKey("news_signals.id"), unique=True, index=True)
    market_id: Mapped[str] = mapped_column(String, ForeignKey("markets.id"), index=True)

    signal_time: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    implied_outcome: Mapped[str] = mapped_column(String)  # YES | NO

    category: Mapped[Optional[str]] = mapped_column(String, index=True, nullable=True)
    source_tier: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    source_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    verifier_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    p0: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    yes_mid_at_signal: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    yes_best_bid_at_signal: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    yes_best_ask_at_signal: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    spread_at_signal: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    liquidity_at_signal: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    volume_24h_at_signal: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Retrospective metric: max(p_implied) - p0 in the observation window.
    eventual_move: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Accuracy vs resolved market outcome (when known).
    signal_correct: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)

    sufficient_liquidity: Mapped[bool] = mapped_column(Boolean, default=False)
    spread_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    price_within_range: Mapped[bool] = mapped_column(Boolean, default=False)

    clean_signal: Mapped[bool] = mapped_column(Boolean, default=False)
    stale_signal: Mapped[bool] = mapped_column(Boolean, default=False)
    leaky_signal: Mapped[bool] = mapped_column(Boolean, default=False)

    price_lag_status: Mapped[str] = mapped_column(String, default="INSUFFICIENT_DATA")

    closure_lag_seconds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    hard_source_lag_seconds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    soft_to_hard_source_lag_seconds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc))
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: dt.datetime.now(dt.timezone.utc),
        onupdate=lambda: dt.datetime.now(dt.timezone.utc),
    )

    signal: Mapped["NewsSignal"] = relationship(back_populates="lag_measurement")
    market: Mapped["Market"] = relationship(back_populates="lag_measurements")
    threshold_crossings: Mapped[list["LagThresholdCrossing"]] = relationship(
        back_populates="lag_measurement", cascade="all, delete-orphan"
    )
    drift_windows: Mapped[list["SignalDriftWindow"]] = relationship(
        back_populates="lag_measurement", cascade="all, delete-orphan"
    )


class LagThresholdCrossing(Base):
    __tablename__ = "lag_threshold_crossings"
    __table_args__ = (
        UniqueConstraint("lag_measurement_id", "threshold_label", name="uq_crossing_measurement_label"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    lag_measurement_id: Mapped[str] = mapped_column(String, ForeignKey("lag_measurements.id"), index=True)
    threshold_type: Mapped[str] = mapped_column(String)  # POINT_MOVE | EVENTUAL_MOVE
    threshold_label: Mapped[str] = mapped_column(String)  # 5PT | 10PT | 50PCT_EVENTUAL | 90PCT_EVENTUAL
    threshold_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    crossed: Mapped[bool] = mapped_column(Boolean, default=False)
    lag_seconds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    crossed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc))

    lag_measurement: Mapped["LagMeasurement"] = relationship(back_populates="threshold_crossings")


class SignalDriftWindow(Base):
    __tablename__ = "signal_drift_windows"
    __table_args__ = (
        UniqueConstraint(
            "lag_measurement_id", "direction", "window_minutes", name="uq_drift_measurement_direction_window"
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    lag_measurement_id: Mapped[str] = mapped_column(String, ForeignKey("lag_measurements.id"), index=True)
    direction: Mapped[str] = mapped_column(String)  # PRE | POST
    window_minutes: Mapped[int] = mapped_column(Integer)
    observed_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    move_from_p0: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    observed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc))

    lag_measurement: Mapped["LagMeasurement"] = relationship(back_populates="drift_windows")


class LagScoreSnapshot(Base):
    __tablename__ = "lag_score_snapshots"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    score_run_id: Mapped[str] = mapped_column(String, index=True)
    calculated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    lag_measurement_id: Mapped[str] = mapped_column(String, ForeignKey("lag_measurements.id"), index=True)
    metric_name: Mapped[str] = mapped_column(String)
    category: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    scoring_category: Mapped[str] = mapped_column(String)
    raw_value: Mapped[float] = mapped_column(Float)
    transformed_value: Mapped[float] = mapped_column(Float)
    z_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    sample_size: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc))


class ThresholdProfile(Base):
    """
    Named gating presets for paper trading (conservative → aggressive).
    Active profile is selected via RuntimeSetting key ``threshold_profile_id``.
    """

    __tablename__ = "threshold_profiles"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    display_name: Mapped[str] = mapped_column(String)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    min_liquidity: Mapped[float] = mapped_column(Float)
    max_spread: Mapped[float] = mapped_column(Float)
    min_relevance: Mapped[float] = mapped_column(Float)
    min_confidence: Mapped[float] = mapped_column(Float)
    min_verifier_confidence: Mapped[float] = mapped_column(Float)
    max_article_age_minutes: Mapped[int] = mapped_column(Integer)
    allow_indirect_evidence: Mapped[bool] = mapped_column(Boolean, default=False)
    paper_size_multiplier: Mapped[float] = mapped_column(Float, default=1.0)


class RuntimeSetting(Base):
    """Key-value toggles persisted in DB (e.g. lag focus N). Env defaults live in Settings."""

    __tablename__ = "runtime_settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: dt.datetime.now(dt.timezone.utc),
        onupdate=lambda: dt.datetime.now(dt.timezone.utc),
    )


class JobStatus(Base):
    """Last known state for dashboard-visible ingestion/research jobs."""

    __tablename__ = "job_statuses"

    job_name: Mapped[str] = mapped_column(String, primary_key=True)
    label: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="NEVER")  # NEVER | RUNNING | SUCCESS | FAILED
    started_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: dt.datetime.now(dt.timezone.utc),
        onupdate=lambda: dt.datetime.now(dt.timezone.utc),
    )


class BacktestRun(Base):
    """One local-snapshot news reaction backtest run."""

    __tablename__ = "backtest_runs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String, default="RUNNING")  # RUNNING | SUCCESS | FAILED
    params_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    summary_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)

    cases: Mapped[list["BacktestCase"]] = relationship(back_populates="run", cascade="all, delete-orphan")
    events: Mapped[list["BacktestEventLog"]] = relationship(back_populates="run", cascade="all, delete-orphan")


class BacktestCase(Base):
    """One article/market/signal timing and price movement measurement."""

    __tablename__ = "backtest_cases"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    run_id: Mapped[str] = mapped_column(String, ForeignKey("backtest_runs.id"), index=True)
    article_id: Mapped[str] = mapped_column(String, ForeignKey("news_articles.id"), index=True)
    market_id: Mapped[str] = mapped_column(String, ForeignKey("markets.id"), index=True)
    signal_id: Mapped[Optional[str]] = mapped_column(String, ForeignKey("news_signals.id"), index=True, nullable=True)
    lag_measurement_id: Mapped[Optional[str]] = mapped_column(String, ForeignKey("lag_measurements.id"), index=True, nullable=True)

    published_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    signal_created_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    polling_delay_seconds: Mapped[float] = mapped_column(Float)
    signal_delay_seconds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    hours_to_resolution: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    implied_outcome: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # Action the live pipeline took for this signal: ACT | ABSTAIN | CANDIDATE | REJECT_* | None
    signal_action: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    p0: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    price_windows_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    first_5pt_move_seconds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    first_10pt_move_seconds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    max_move_24h: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    move_before_fetch: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    coverage_status: Mapped[str] = mapped_column(String, index=True)  # GOOD | SPARSE | NO_DATA
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc))

    run: Mapped["BacktestRun"] = relationship(back_populates="cases")
    article: Mapped["NewsArticle"] = relationship()
    market: Mapped["Market"] = relationship()
    signal: Mapped[Optional["NewsSignal"]] = relationship()
    lag_measurement: Mapped[Optional["LagMeasurement"]] = relationship()
    events: Mapped[list["BacktestEventLog"]] = relationship(back_populates="case")


class BacktestEventLog(Base):
    """Structured audit event mirrored to JSONL by the backtest runner."""

    __tablename__ = "backtest_event_logs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    run_id: Mapped[str] = mapped_column(String, ForeignKey("backtest_runs.id"), index=True)
    case_id: Mapped[Optional[str]] = mapped_column(String, ForeignKey("backtest_cases.id"), index=True, nullable=True)
    event_type: Mapped[str] = mapped_column(String, index=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc))

    run: Mapped["BacktestRun"] = relationship(back_populates="events")
    case: Mapped[Optional["BacktestCase"]] = relationship(back_populates="events")


class CryptoMarketProfile(Base):
    """
    Result of a crypto preflight scan for one Polymarket market.
    One row per market_id; upserted on each preflight run.
    """

    __tablename__ = "crypto_market_profiles"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    market_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    slug: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    title: Mapped[str] = mapped_column(Text)
    rule_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    resolution_source_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    end_date: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    outcomes_json: Mapped[Optional[List]] = mapped_column(JSON, nullable=True)
    token_ids_json: Mapped[Optional[List]] = mapped_column(JSON, nullable=True)
    raw_gamma_json: Mapped[Optional[Dict]] = mapped_column(JSON, nullable=True)

    # ── Classification ───────────────────────────────────────────────────
    # CRYPTO_INTRAPERIOD_UP_DOWN | CRYPTO_DAILY_COMPARISON |
    # CRYPTO_PRICE_ABOVE_BELOW   | CRYPTO_HIT_HIGH_LOW | UNKNOWN
    rule_family: Mapped[str] = mapped_column(String, default="UNKNOWN", index=True)
    classification_confidence: Mapped[float] = mapped_column(Float, default=0.0)

    # ── Parsed fields (CRYPTO_INTRAPERIOD_UP_DOWN only) ──────────────────
    base_asset: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    quote_asset: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    binance_symbol: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    candle_interval: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    candle_interval_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    candle_start_time_utc: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    candle_close_time_utc: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    yes_token_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    no_token_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    parser_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    parser_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # PARSED | PARSER_REVIEW_REQUIRED | UNSUPPORTED | N_A
    parser_status: Mapped[str] = mapped_column(String, default="N_A")

    # ── Binance kline verification ────────────────────────────────────────
    binance_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    binance_open_time_utc: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    binance_open_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    binance_close_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    binance_verification_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # ── Orderbook ────────────────────────────────────────────────────────
    yes_book_usable: Mapped[bool] = mapped_column(Boolean, default=False)
    no_book_usable: Mapped[bool] = mapped_column(Boolean, default=False)
    yes_best_ask: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    no_best_ask: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    yes_liquidity: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    no_liquidity: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    orderbook_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # ── Overall readiness ────────────────────────────────────────────────
    # READY | PARSER_REVIEW_REQUIRED | UNSUPPORTED | NO_ORDERBOOK |
    # BINANCE_MISMATCH | FUTURE_CANDLE | UNKNOWN
    monitor_status: Mapped[str] = mapped_column(String, default="UNKNOWN", index=True)
    monitor_ready: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: dt.datetime.now(dt.timezone.utc),
        onupdate=lambda: dt.datetime.now(dt.timezone.utc),
    )


class AuditLog(Base):
    """
    FUTURE STATE — not wired to any execution path yet.

    Intended to record every live order attempt, fill, cancellation, and position change
    when/if live trading is enabled. Append-only.
    """

    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    event_type: Mapped[str] = mapped_column(String)  # ORDER_ATTEMPT | FILL | CANCEL | POSITION_CHANGE
    market_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    signal_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    payload_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc))
