"""
Configuration module — 16-Factor App Principle: Config esterna e centralizzata.
Reads from environment variables, ConfigMaps, or Vault.
Never hardcode credentials or endpoints.
"""

import json
import os
from dataclasses import dataclass, field


@dataclass
class LLMConfig:
    """LLM Gateway configuration (GHO-compatible, OpenAI standard)."""

    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    gateway_api_key: str = field(default_factory=lambda: os.getenv("LLM_API_KEY", ""))
    gateway_url: str = field(
        default_factory=lambda: os.getenv("LLM_GATEWAY_URL", "https://api.anthropic.com")
    )
    model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "claude-sonnet-4-20250514"))
    openai_model: str = field(default_factory=lambda: os.getenv("OPENAI_LLM_MODEL", "gpt-5-mini"))
    model_fast: str = field(
        default_factory=lambda: os.getenv("LLM_MODEL_FAST", "claude-haiku-4-5-20251001")
    )
    # Per-agent model assignment — cost/quality routing
    signal_model: str = field(
        default_factory=lambda: os.getenv("LLM_MODEL_SIGNAL", "claude-haiku-4-5-20251001")
    )
    assessment_model: str = field(
        default_factory=lambda: os.getenv("LLM_MODEL_ASSESSMENT", "claude-sonnet-4-6")
    )
    summarizer_model: str = field(
        default_factory=lambda: os.getenv("LLM_MODEL_SUMMARIZER", "claude-haiku-4-5-20251001")
    )
    max_tokens: int = 4096
    temperature: float = 0.0  # deterministic for trading decisions

    @property
    def api_key(self) -> str:
        """
        Backward-compatible generic key accessor.
        Prefer explicit anthropic_api_key / gateway_api_key at call sites.
        """
        return self.gateway_api_key or self.anthropic_api_key

    def has_anthropic_credentials(self) -> bool:
        return bool(self.anthropic_api_key)

    def has_gateway_credentials(self) -> bool:
        return bool(self.gateway_api_key and self.gateway_url)

    def is_anthropic_gateway_url(self) -> bool:
        normalized = self.gateway_url.rstrip("/").lower()
        return normalized == "https://api.anthropic.com"


@dataclass
class TradingConfig:
    """Trading parameters — externalized for per-environment tuning."""

    symbols: list[str] = field(
        default_factory=lambda: json.loads(os.getenv("TRADING_SYMBOLS", '["BTC/USDT", "ETH/USDT"]'))
    )
    timeframes: list[str] = field(
        default_factory=lambda: json.loads(os.getenv("TRADING_TIMEFRAMES", '["1h", "4h", "1d"]'))
    )
    max_risk_pct: float = field(default_factory=lambda: float(os.getenv("MAX_RISK_PCT", "1.0")))
    max_position_pct: float = field(
        default_factory=lambda: float(os.getenv("MAX_POSITION_PCT", "5.0"))
    )
    min_confidence: int = field(default_factory=lambda: int(os.getenv("MIN_CONFIDENCE", "70")))
    min_risk_reward: float = field(
        default_factory=lambda: float(os.getenv("MIN_RISK_REWARD", "2.0"))
    )
    dry_run: bool = field(default_factory=lambda: os.getenv("DRY_RUN", "true").lower() == "true")


@dataclass
class MarketDataConfig:
    """Market data source selection and provider-specific settings."""

    source: str = field(
        default_factory=lambda: os.getenv("MARKET_DATA_SOURCE", "synthetic").lower()
    )
    fallback_to_synthetic: bool = field(
        default_factory=lambda: os.getenv("MARKET_DATA_FALLBACK_TO_SYNTHETIC", "true").lower()
        == "true"
    )
    ccxt_exchange: str = field(
        default_factory=lambda: os.getenv("CCXT_EXCHANGE", "binance").lower()
    )


@dataclass
class BacktestConfig:
    """Backtesting and self-assessment parameters."""

    lookback_days: int = field(
        default_factory=lambda: int(os.getenv("BACKTEST_LOOKBACK_DAYS", "30"))
    )
    min_trades_for_assessment: int = field(
        default_factory=lambda: int(os.getenv("MIN_TRADES_ASSESSMENT", "10"))
    )
    win_rate_threshold: float = field(
        default_factory=lambda: float(os.getenv("WIN_RATE_THRESHOLD", "0.55"))
    )
    max_drawdown_pct: float = field(
        default_factory=lambda: float(os.getenv("MAX_DRAWDOWN_PCT", "10.0"))
    )
    assessment_interval_hours: int = field(
        default_factory=lambda: int(os.getenv("ASSESSMENT_INTERVAL_HOURS", "24"))
    )


@dataclass
class ScanConfig:
    """Per-timeframe scan cadence (seconds between scans)."""

    interval_5m: int = field(default_factory=lambda: int(os.getenv("SCAN_INTERVAL_5M", "300")))
    interval_15m: int = field(default_factory=lambda: int(os.getenv("SCAN_INTERVAL_15M", "900")))
    interval_30m: int = field(default_factory=lambda: int(os.getenv("SCAN_INTERVAL_30M", "1800")))
    interval_1h: int = field(default_factory=lambda: int(os.getenv("SCAN_INTERVAL_1H", "3600")))
    interval_4h: int = field(default_factory=lambda: int(os.getenv("SCAN_INTERVAL_4H", "14400")))
    interval_1d: int = field(default_factory=lambda: int(os.getenv("SCAN_INTERVAL_1D", "86400")))

    def for_timeframe(self, tf: str) -> int:
        mapping = {
            "5m": self.interval_5m,
            "15m": self.interval_15m,
            "30m": self.interval_30m,
            "1h": self.interval_1h,
            "4h": self.interval_4h,
            "1d": self.interval_1d,
        }
        return mapping.get(tf, self.interval_1h)


@dataclass
class PredictionConfig:
    """Prediction lifecycle and evaluation horizon config."""

    eval_horizon_5m: int = field(
        default_factory=lambda: int(os.getenv("PREDICTION_EVAL_HORIZON_5M", "6"))
    )
    eval_horizon_15m: int = field(
        default_factory=lambda: int(os.getenv("PREDICTION_EVAL_HORIZON_15M", "6"))
    )
    eval_horizon_30m: int = field(
        default_factory=lambda: int(os.getenv("PREDICTION_EVAL_HORIZON_30M", "6"))
    )
    eval_horizon_1h: int = field(
        default_factory=lambda: int(os.getenv("PREDICTION_EVAL_HORIZON_1H", "8"))
    )
    eval_horizon_4h: int = field(
        default_factory=lambda: int(os.getenv("PREDICTION_EVAL_HORIZON_4H", "6"))
    )
    eval_horizon_1d: int = field(
        default_factory=lambda: int(os.getenv("PREDICTION_EVAL_HORIZON_1D", "5"))
    )
    min_evaluated_for_adaptation: int = field(
        default_factory=lambda: int(os.getenv("MIN_EVALUATED_PREDICTIONS_FOR_ADAPTATION", "25"))
    )
    short_window: int = 25
    medium_window: int = 100
    long_window: int = 250

    def horizon_candles(self, tf: str) -> int:
        """Return number of candles to use as evaluation horizon for a timeframe."""
        mapping = {
            "5m": self.eval_horizon_5m,
            "15m": self.eval_horizon_15m,
            "30m": self.eval_horizon_30m,
            "1h": self.eval_horizon_1h,
            "4h": self.eval_horizon_4h,
            "1d": self.eval_horizon_1d,
        }
        return mapping.get(tf, 6)


@dataclass
class AdaptationConfig:
    """Strategy adaptation and mutation controls."""

    enabled: bool = field(
        default_factory=lambda: os.getenv("ENABLE_AUTONOMOUS_ADAPTATION", "false").lower() == "true"
    )
    interval_hours: int = field(
        default_factory=lambda: int(os.getenv("ADAPTATION_INTERVAL_HOURS", "24"))
    )
    max_mutations_per_cycle: int = field(
        default_factory=lambda: int(os.getenv("MAX_PARAMETER_MUTATIONS_PER_CYCLE", "2"))
    )
    rollback_on_short_window_degradation: bool = field(
        default_factory=lambda: os.getenv("ROLLBACK_ON_SHORT_WINDOW_DEGRADATION", "true").lower()
        == "true"
    )
    # KPI thresholds that trigger adaptation
    min_directional_accuracy: float = field(
        default_factory=lambda: float(os.getenv("MIN_DIRECTIONAL_ACCURACY", "0.50"))
    )
    min_tp1_reach_rate: float = field(
        default_factory=lambda: float(os.getenv("MIN_TP1_REACH_RATE", "0.35"))
    )
    max_false_positive_rate: float = field(
        default_factory=lambda: float(os.getenv("MAX_FALSE_POSITIVE_RATE", "0.55"))
    )
    max_failed_promotions_before_freeze: int = field(
        default_factory=lambda: int(os.getenv("MAX_FAILED_PROMOTIONS_BEFORE_FREEZE", "3"))
    )


@dataclass
class NewsConfig:
    """News & sentiment data source configuration."""

    enabled: bool = field(
        default_factory=lambda: os.getenv("NEWS_ENABLED", "true").lower() == "true"
    )
    cryptopanic_api_key: str = field(default_factory=lambda: os.getenv("CRYPTOPANIC_API_KEY", ""))
    alpha_vantage_api_key: str = field(
        default_factory=lambda: os.getenv("ALPHA_VANTAGE_API_KEY", "")
    )
    newsapi_api_key: str = field(default_factory=lambda: os.getenv("NEWSAPI_API_KEY", ""))
    # Lookback for headlines (hours)
    lookback_hours: int = field(default_factory=lambda: int(os.getenv("NEWS_LOOKBACK_HOURS", "24")))
    # Max articles returned per source per call
    max_articles_per_source: int = field(
        default_factory=lambda: int(os.getenv("NEWS_MAX_ARTICLES", "15"))
    )
    # In-process cache TTL for news fetches (seconds)
    cache_ttl_seconds: int = field(
        default_factory=lambda: int(os.getenv("NEWS_CACHE_TTL_SECONDS", "1800"))
    )
    # Fear & Greed cache TTL (the index updates daily for crypto)
    fear_greed_cache_ttl_seconds: int = field(
        default_factory=lambda: int(os.getenv("FEAR_GREED_CACHE_TTL_SECONDS", "21600"))
    )
    http_timeout_seconds: float = field(
        default_factory=lambda: float(os.getenv("NEWS_HTTP_TIMEOUT", "8.0"))
    )

    def any_news_source_configured(self) -> bool:
        return bool(self.cryptopanic_api_key or self.alpha_vantage_api_key or self.newsapi_api_key)


@dataclass
class LiquidityConfig:
    """Order book and liquidity analysis configuration."""

    enabled: bool = field(
        default_factory=lambda: os.getenv("LIQUIDITY_ENABLED", "true").lower() == "true"
    )
    # Levels of order book depth to fetch
    depth: int = field(default_factory=lambda: int(os.getenv("LIQUIDITY_DEPTH", "100")))
    # Cluster threshold: an entry counts as a "wall" if it is N× the mean depth slot
    wall_threshold_multiplier: float = field(
        default_factory=lambda: float(os.getenv("LIQUIDITY_WALL_MULTIPLIER", "5.0"))
    )
    cache_ttl_seconds: int = field(
        default_factory=lambda: int(os.getenv("LIQUIDITY_CACHE_TTL_SECONDS", "60"))
    )


@dataclass
class EmbeddingConfig:
    """Embedding backend selection for the RAG knowledge layer."""

    # Provider: "sentence_transformers" (local, free) or "openai" (gateway)
    provider: str = field(
        default_factory=lambda: os.getenv("EMBED_PROVIDER", "sentence_transformers").lower()
    )
    # Local sentence-transformers model name
    local_model: str = field(
        default_factory=lambda: os.getenv(
            "EMBED_LOCAL_MODEL", "sentence-transformers/all-MiniLM-L6-v2"
        )
    )
    # OpenAI-compatible model name (used when provider="openai")
    openai_model: str = field(
        default_factory=lambda: os.getenv("EMBED_OPENAI_MODEL", "text-embedding-3-small")
    )
    # Dimension hint — informational, actual dim is determined by the model
    dimension: int = field(default_factory=lambda: int(os.getenv("EMBED_DIMENSION", "384")))


@dataclass
class VectorStoreConfig:
    """Chroma vector store configuration for signal knowledge."""

    enabled: bool = field(
        default_factory=lambda: os.getenv("VECTOR_STORE_ENABLED", "true").lower() == "true"
    )
    # Persistence directory — defaults under TRADING_AGENT_DATA_DIR
    persist_dir: str = field(default_factory=lambda: os.getenv("VECTOR_STORE_DIR", ""))
    # Collection names
    outcomes_collection: str = field(
        default_factory=lambda: os.getenv("VECTOR_OUTCOMES_COLLECTION", "signal_outcomes")
    )
    # Default top_k for retrieval
    default_top_k: int = field(default_factory=lambda: int(os.getenv("VECTOR_DEFAULT_TOP_K", "5")))
    # Prune records older than this (days)
    retention_days: int = field(
        default_factory=lambda: int(os.getenv("VECTOR_RETENTION_DAYS", "180"))
    )

    def resolved_persist_dir(self) -> str:
        if self.persist_dir:
            return os.path.expanduser(self.persist_dir)
        base = os.path.expanduser(os.getenv("TRADING_AGENT_DATA_DIR", "~/.trading-agent"))
        return os.path.join(base, "vector_store")


@dataclass
class TelegramConfig:
    """Telegram publishing configuration."""

    bot_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    channel_id: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHANNEL_ID", ""))
    thread_id: str = field(default_factory=lambda: os.getenv("TELEGRAM_THREAD_ID", ""))
    publish_signals: bool = field(
        default_factory=lambda: os.getenv("TELEGRAM_PUBLISH_SIGNALS", "true").lower() == "true"
    )
    publish_evaluations: bool = field(
        default_factory=lambda: os.getenv("TELEGRAM_PUBLISH_EVALUATIONS", "false").lower() == "true"
    )
    publish_strategy_changes: bool = field(
        default_factory=lambda: os.getenv("TELEGRAM_PUBLISH_STRATEGY_CHANGES", "true").lower()
        == "true"
    )
    publish_degradation_alerts: bool = field(
        default_factory=lambda: os.getenv("TELEGRAM_PUBLISH_DEGRADATION_ALERTS", "true").lower()
        == "true"
    )

    def is_configured(self) -> bool:
        return bool(self.bot_token and self.channel_id)


@dataclass
class InfraConfig:
    """Infrastructure and runtime configuration."""

    port: int = field(default_factory=lambda: int(os.getenv("PORT", "8080")))
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))
    log_format: str = "json"  # always structured
    redis_url: str = field(default_factory=lambda: os.getenv("REDIS_URL", "redis://localhost:6379"))
    s3_bucket: str = field(
        default_factory=lambda: os.getenv("S3_CONVERSATION_BUCKET", "advisor-conversations")
    )
    db_url: str = field(default_factory=lambda: os.getenv("DATABASE_URL", ""))
    correlation_id_header: str = "X-Correlation-ID"
    graceful_shutdown_timeout: int = 30


@dataclass
class AppConfig:
    """Root configuration — aggregates all sub-configs."""

    llm: LLMConfig = field(default_factory=LLMConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)
    market_data: MarketDataConfig = field(default_factory=MarketDataConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    scan: ScanConfig = field(default_factory=ScanConfig)
    prediction: PredictionConfig = field(default_factory=PredictionConfig)
    adaptation: AdaptationConfig = field(default_factory=AdaptationConfig)
    news: NewsConfig = field(default_factory=NewsConfig)
    liquidity: LiquidityConfig = field(default_factory=LiquidityConfig)
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    vector_store: VectorStoreConfig = field(default_factory=VectorStoreConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    infra: InfraConfig = field(default_factory=InfraConfig)
    agent_id: str = field(
        default_factory=lambda: os.getenv("AGENT_ID", "trading-intelligence-agent")
    )
    environment: str = field(default_factory=lambda: os.getenv("ENVIRONMENT", "development"))

    def is_production(self) -> bool:
        return self.environment == "production"

    def validate(self) -> None:
        """Validate required configuration. Raises ValueError with actionable message."""
        errors = []

        if not (self.llm.has_anthropic_credentials() or self.llm.has_gateway_credentials()):
            errors.append(
                "No LLM credentials are configured. "
                "Set ANTHROPIC_API_KEY for Anthropic direct access, or set both "
                "LLM_API_KEY and LLM_GATEWAY_URL for the OpenAI-compatible gateway."
            )

        if not (0.0 < self.trading.max_risk_pct <= 10.0):
            errors.append(
                f"MAX_RISK_PCT={self.trading.max_risk_pct} is outside safe range (0.0, 10.0]. "
                "Set MAX_RISK_PCT to a value between 0.01 and 10.0."
            )

        if not (1 <= self.infra.port <= 65535):
            errors.append(
                f"PORT={self.infra.port} is not a valid port number. "
                "Set PORT to an integer between 1 and 65535."
            )

        allowed_market_data_sources = {"synthetic", "ccxt", "yfinance", "auto"}
        if self.market_data.source not in allowed_market_data_sources:
            errors.append(
                f"MARKET_DATA_SOURCE={self.market_data.source!r} is invalid. "
                "Use one of: synthetic, ccxt, yfinance, auto."
            )

        if errors:
            msg = "Configuration validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
            raise ValueError(msg)


# Singleton
_config: AppConfig | None = None


def get_config() -> AppConfig:
    global _config
    if _config is None:
        _config = AppConfig()
    return _config


def reset_config() -> None:
    """Reset the config singleton. For testing only."""
    global _config
    _config = None
