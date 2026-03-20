"""
Configuration module — 16-Factor App Principle: Config esterna e centralizzata.
Reads from environment variables, ConfigMaps, or Vault.
Never hardcode credentials or endpoints.
"""
import os
from dataclasses import dataclass, field
from typing import Optional
import json


@dataclass
class LLMConfig:
    """LLM Gateway configuration (GHO-compatible, OpenAI standard)."""
    gateway_url: str = field(
        default_factory=lambda: os.getenv("LLM_GATEWAY_URL", "https://api.anthropic.com")
    )
    api_key: str = field(
        default_factory=lambda: os.getenv("LLM_API_KEY", os.getenv("ANTHROPIC_API_KEY", ""))
    )
    model: str = field(
        default_factory=lambda: os.getenv("LLM_MODEL", "claude-sonnet-4-20250514")
    )
    model_fast: str = field(
        default_factory=lambda: os.getenv("LLM_MODEL_FAST", "claude-haiku-4-5-20251001")
    )
    max_tokens: int = 4096
    temperature: float = 0.0  # deterministic for trading decisions


@dataclass
class TradingConfig:
    """Trading parameters — externalized for per-environment tuning."""
    symbols: list[str] = field(
        default_factory=lambda: json.loads(
            os.getenv("TRADING_SYMBOLS", '["BTC/USDT", "ETH/USDT"]')
        )
    )
    timeframes: list[str] = field(
        default_factory=lambda: json.loads(
            os.getenv("TRADING_TIMEFRAMES", '["1h", "4h", "1d"]')
        )
    )
    max_risk_pct: float = field(
        default_factory=lambda: float(os.getenv("MAX_RISK_PCT", "1.0"))
    )
    max_position_pct: float = field(
        default_factory=lambda: float(os.getenv("MAX_POSITION_PCT", "5.0"))
    )
    min_confidence: int = field(
        default_factory=lambda: int(os.getenv("MIN_CONFIDENCE", "70"))
    )
    min_risk_reward: float = field(
        default_factory=lambda: float(os.getenv("MIN_RISK_REWARD", "2.0"))
    )
    dry_run: bool = field(
        default_factory=lambda: os.getenv("DRY_RUN", "true").lower() == "true"
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
class InfraConfig:
    """Infrastructure and runtime configuration."""
    port: int = field(
        default_factory=lambda: int(os.getenv("PORT", "8080"))
    )
    log_level: str = field(
        default_factory=lambda: os.getenv("LOG_LEVEL", "INFO")
    )
    log_format: str = "json"  # always structured
    redis_url: str = field(
        default_factory=lambda: os.getenv("REDIS_URL", "redis://localhost:6379")
    )
    s3_bucket: str = field(
        default_factory=lambda: os.getenv("S3_CONVERSATION_BUCKET", "advisor-conversations")
    )
    db_url: str = field(
        default_factory=lambda: os.getenv("DATABASE_URL", "")
    )
    correlation_id_header: str = "X-Correlation-ID"
    graceful_shutdown_timeout: int = 30


@dataclass
class AppConfig:
    """Root configuration — aggregates all sub-configs."""
    llm: LLMConfig = field(default_factory=LLMConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    infra: InfraConfig = field(default_factory=InfraConfig)
    agent_id: str = field(
        default_factory=lambda: os.getenv("AGENT_ID", "trading-intelligence-agent")
    )
    environment: str = field(
        default_factory=lambda: os.getenv("ENVIRONMENT", "development")
    )

    def is_production(self) -> bool:
        return self.environment == "production"


# Singleton
_config: Optional[AppConfig] = None


def get_config() -> AppConfig:
    global _config
    if _config is None:
        _config = AppConfig()
    return _config
