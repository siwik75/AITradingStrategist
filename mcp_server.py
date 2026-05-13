"""
Trading Intelligence Agent — MCP Server

Exposes the full trading platform as MCP tools so any LLM client
(Claude Desktop, Claude Code, custom agents) can:

  - Generate trading signals with full market context
  - Retrieve news, Fear & Greed, and order-book liquidity
  - Query technical indicators and OHLCV candles
  - Run backtests and inspect strategy parameters
  - Review past performance KPIs and failure patterns
  - Report manual trade outcomes to close the feedback loop

Transport: stdio (default) — works with Claude Desktop and Claude Code.
           Add --transport sse to expose as an HTTP SSE endpoint instead.

Usage (stdio):
  python mcp_server.py

Usage (SSE / HTTP):
  python mcp_server.py --transport sse --port 8083

Claude Desktop config (~/.claude/claude_desktop_config.json):
  {
    "mcpServers": {
      "trading-agent": {
        "command": "/absolute/path/to/.venv/bin/python",
        "args": ["/absolute/path/to/trading-intelligence-agent/mcp_server.py"],
        "env": {}
      }
    }
  }

Claude Code (project-level .claude/settings.json):
  {
    "mcpServers": {
      "trading-agent": {
        "command": ".venv/bin/python",
        "args": ["mcp_server.py"]
      }
    }
  }
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Load .env before any project imports
_project_root = Path(__file__).parent
sys.path.insert(0, str(_project_root))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_project_root / ".env")

from mcp.server.fastmcp import FastMCP  # noqa: E402

mcp = FastMCP(
    name="trading-intelligence-agent",
    instructions=(
        "A professional trading intelligence platform. "
        "Use analyze_signal to generate a full signal with market context and RAG-backed lessons. "
        "Use get_market_context for a quick sentiment + liquidity snapshot without running the LLM. "
        "Use get_signals to see actionable BUY/SELL signals awaiting manual execution. "
        "All data is real-time from Binance (OHLCV, order book) and external news providers."
    ),
)


# =============================================================================
# HELPER
# =============================================================================

def _json(obj) -> str:
    return json.dumps(obj, default=str, indent=2)


# =============================================================================
# 1. SIGNAL GENERATION
# =============================================================================

@mcp.tool()
async def analyze_signal(
    symbol: str,
    timeframe: str = "1h",
) -> str:
    """
    Run a full trading signal analysis for a symbol on a given timeframe.

    Performs multi-confluence technical analysis (EMA, RSI, MACD, VWAP bands,
    ATR, Bollinger Bands), injects live market context (Fear & Greed, news
    sentiment, order-book liquidity), and queries the RAG knowledge store for
    similar past setups before generating a BUY / SELL / HOLD signal.

    Returns a structured JSON signal with entry, stop-loss, TP1, TP2,
    confidence, confluence indicators, and a detailed reasoning narrative.

    :param symbol: Trading pair e.g. BTC/USDT, ETH/USDT, SOL/USDT
    :param timeframe: Candle timeframe — 15m, 30m, 1h, 4h, 1d
    """
    import uuid

    from agents.signal_agent import SignalAgent
    agent = SignalAgent()
    result = await agent.analyze(
        symbol=symbol,
        timeframe=timeframe,
        correlation_id=str(uuid.uuid4()),
    )
    return _json(result)


# =============================================================================
# 2. MARKET CONTEXT (news + sentiment + F&G + liquidity) — no LLM cost
# =============================================================================

@mcp.tool()
async def get_market_context(
    symbol: str,
    timeframe: str = "1h",
) -> str:
    """
    Fetch the full external market context for a symbol without running
    a signal analysis: Fear & Greed index, news headlines + LLM sentiment
    digest, and order-book liquidity snapshot (mid price, spread, walls,
    depth imbalance, slippage probe).

    Useful for a quick sentiment/microstructure read before deciding whether
    to trigger a full analysis.

    :param symbol: Trading pair e.g. BTC/USDT or stock ticker AAPL
    :param timeframe: Used for metadata only; does not affect the fetches
    """
    from workflows.market_context import build_market_context
    ctx = await build_market_context(symbol=symbol, timeframe=timeframe)
    # Strip internal _series fields that are not JSON serialisable
    ctx.pop("_raw", None)
    return _json(ctx)


# =============================================================================
# 3. FEAR & GREED INDEX
# =============================================================================

@mcp.tool()
async def get_fear_greed(
    asset_class: str = "crypto",
) -> str:
    """
    Fetch the current Fear & Greed index.

    Crypto: alternative.me index (0 = extreme fear, 100 = extreme greed).
    Stocks: CNN Fear & Greed index.

    :param asset_class: "crypto" (default) or "stocks"
    """
    from tools.sentiment_tools import get_fear_greed as _fg
    result = await _fg(asset_class=asset_class)
    return _json(result)


# =============================================================================
# 4. NEWS
# =============================================================================

@mcp.tool()
async def get_news(
    symbol: str,
    hours: int = 24,
) -> str:
    """
    Fetch recent news articles for a symbol from all configured providers
    (CryptoPanic, Alpha Vantage, NewsAPI). Returns normalised articles with
    title, URL, published timestamp, and pre-scored sentiment where available.

    :param symbol: Trading symbol e.g. BTC/USDT or AAPL
    :param hours: Lookback window in hours (default 24)
    """
    import os

    from config.settings import reset_config
    # Temporarily override the lookback setting
    original = os.environ.get("NEWS_LOOKBACK_HOURS")
    os.environ["NEWS_LOOKBACK_HOURS"] = str(hours)
    reset_config()
    from tools.news_tools import _clear_cache, fetch_news
    _clear_cache()
    result = await fetch_news(symbol)
    # Restore
    if original is not None:
        os.environ["NEWS_LOOKBACK_HOURS"] = original
    else:
        os.environ.pop("NEWS_LOOKBACK_HOURS", None)
    reset_config()
    return _json(result)


@mcp.tool()
async def summarize_news(
    symbol: str,
) -> str:
    """
    Fetch recent news for a symbol and run an LLM-based sentiment digest.
    Returns overall sentiment (bullish/bearish/neutral), score, confidence,
    key themes, and bullish/bearish drivers.

    Uses the fast summarizer model (Haiku) — minimal cost.

    :param symbol: Trading symbol e.g. BTC/USDT
    """
    from tools.news_tools import fetch_news
    from tools.sentiment_tools import summarize_news_sentiment
    news = await fetch_news(symbol)
    articles = news.get("articles", [])
    if not articles:
        return _json({"available": False, "reason": "no_articles", "symbol": symbol})
    result = await summarize_news_sentiment(symbol, articles)
    result["article_count"] = len(articles)
    return _json(result)


# =============================================================================
# 5. TECHNICAL INDICATORS
# =============================================================================

@mcp.tool()
async def get_indicators(
    symbol: str,
    timeframe: str = "1h",
    indicator_set: str = "full",
) -> str:
    """
    Calculate technical indicators for a symbol.

    Includes trend (EMA 9/21/50/200, ADX, MACD, Ichimoku),
    momentum (RSI, Stochastic, Williams %R, MFI, CCI),
    volatility (ATR, Bollinger Bands, Keltner Channels, squeeze detection),
    volume (OBV, session VWAP with ±1σ/±2σ bands, anchored VWAP, volume ratio),
    and key support/resistance pivot levels.

    :param symbol: Trading pair e.g. BTC/USDT
    :param timeframe: 15m, 30m, 1h, 4h, 1d
    :param indicator_set: "full" | "trend" | "momentum" | "volatility" | "volume"
    """
    from tools.trading_tools import calculate_indicators
    result = await calculate_indicators(
        symbol=symbol,
        timeframe=timeframe,
        indicator_set=indicator_set,
    )
    return _json(result)


# =============================================================================
# 6. OHLCV CANDLES
# =============================================================================

@mcp.tool()
async def get_candles(
    symbol: str,
    timeframe: str = "1h",
    limit: int = 50,
) -> str:
    """
    Fetch OHLCV (Open, High, Low, Close, Volume) candle data.

    Source is determined by MARKET_DATA_SOURCE in your .env
    (ccxt → Binance by default; yfinance for stocks; synthetic for testing).

    :param symbol: Trading pair e.g. BTC/USDT or AAPL
    :param timeframe: 15m, 30m, 1h, 4h, 1d
    :param limit: Number of candles to return (max 500)
    """
    from tools.trading_tools import get_ohlcv
    result = await get_ohlcv(symbol=symbol, timeframe=timeframe, limit=limit)
    return _json(result)


# =============================================================================
# 7. LIQUIDITY & ORDER BOOK
# =============================================================================

@mcp.tool()
async def get_liquidity(
    symbol: str,
) -> str:
    """
    Fetch the live order-book liquidity snapshot for a symbol:
    mid price, bid/ask spread in bps, the largest support/resistance
    bid/ask walls, ±1% depth imbalance (bullish/bearish microstructure),
    and a $50k slippage probe for both buy and sell.

    :param symbol: Trading pair on the configured exchange e.g. BTC/USDT
    """
    from tools.liquidity_tools import get_liquidity_snapshot
    result = await get_liquidity_snapshot(symbol=symbol)
    return _json(result)


# =============================================================================
# 8. BACKTEST
# =============================================================================

@mcp.tool()
async def run_backtest(
    symbol: str,
    timeframe: str = "4h",
    days: int = 30,
) -> str:
    """
    Run a historical backtest for a symbol using the current active strategy
    parameters. Simulates entries based on EMA/RSI/ADX/MACD/volume confluence
    with two-phase exits (TP1 partial at 2×ATR → breakeven SL, TP2 at 3.5×ATR).

    Returns total trades, win rate, profit factor, max drawdown, average
    PnL per trade, and a full trade list with entry/exit/PnL.

    :param symbol: Trading pair e.g. BTC/USDT
    :param timeframe: 15m, 30m, 1h, 4h, 1d
    :param days: Lookback window in days (default 30)
    """
    from tools.trading_tools import get_ohlcv, get_strategy_params
    from tools.trading_tools import run_backtest as _backtest
    await get_ohlcv(symbol=symbol, timeframe=timeframe, limit=days * 24)
    params = await get_strategy_params(timeframe=timeframe)
    result = await _backtest(
        symbol=symbol,
        timeframe=timeframe,
        lookback_days=days,
        params=params or {},
    )
    return _json(result)


# =============================================================================
# 9. STRATEGY PARAMETERS
# =============================================================================

@mcp.tool()
async def get_strategy_params(
    timeframe: str | None = None,
) -> str:
    """
    Return the currently active strategy parameters. These are the ATR
    multipliers, indicator thresholds, and confluence weights that the
    SignalAgent and backtester use. Updated autonomously by the
    SelfAssessmentAgent when adaptation is enabled.

    :param timeframe: Filter by timeframe (e.g. "1h"). None returns all.
    """
    from tools.trading_tools import get_strategy_params as _gsp
    result = await _gsp(timeframe=timeframe)
    if result is None:
        return _json({"available": False, "reason": "no_params_saved_yet"})
    return _json(result)


# =============================================================================
# 10. PENDING SIGNALS
# =============================================================================

@mcp.tool()
async def get_signals(
    status: str = "pending",
    days: int = 7,
) -> str:
    """
    List actionable BUY/SELL signals that meet the confidence and
    risk/reward thresholds. These are signals awaiting manual execution.

    :param status: "pending" (not yet reported), "reported", or "all"
    :param days: How many days back to look (default 7)
    """
    from tools.trading_tools import get_signal_notifications
    result = await get_signal_notifications(status=status, days=days)
    return _json(result)


# =============================================================================
# 11. REPORT TRADE OUTCOME
# =============================================================================

@mcp.tool()
async def report_trade(
    signal_id: str,
    result: str,
    pnl_pct: float | None = None,
    execution_price: float | None = None,
    exit_price: float | None = None,
    notes: str = "",
) -> str:
    """
    Report the outcome of a manually executed signal. This closes the
    feedback loop: outcomes are stored, evaluated, and eventually indexed
    into the RAG knowledge store to improve future signal confidence calibration.

    :param signal_id: The signal ID from get_signals
    :param result: "win", "loss", or "skip"
    :param pnl_pct: Realised PnL percentage (e.g. 1.5 for +1.5%)
    :param execution_price: Actual entry price
    :param exit_price: Actual exit price
    :param notes: Optional free-text notes
    """
    from tools.trading_tools import report_manual_trade_outcome
    outcome = await report_manual_trade_outcome(
        signal_id=signal_id,
        result=result,
        pnl_pct=pnl_pct,
        execution_price=execution_price,
        exit_price=exit_price,
        notes=notes,
    )
    return _json(outcome)


# =============================================================================
# 12. PERFORMANCE KPIs
# =============================================================================

@mcp.tool()
async def get_kpi_summary(
    symbol: str | None = None,
    timeframe: str | None = None,
    window: int = 50,
) -> str:
    """
    Return aggregate performance KPIs over the most recent evaluated signals:
    directional accuracy, win rate, TP1/TP2/SL hit rates, average PnL,
    confidence calibration by bucket (low/mid/high), and per-regime breakdown.

    :param symbol: Filter by symbol (optional, e.g. BTC/USDT)
    :param timeframe: Filter by timeframe (optional, e.g. 1h)
    :param window: Number of recent evaluations to include (default 50)
    """
    from tools.knowledge_tools import get_kpi_summary as _kpis
    result = await _kpis(symbol=symbol, timeframe=timeframe, window=window)
    return _json(result)


@mcp.tool()
async def get_recent_outcomes(
    symbol: str | None = None,
    timeframe: str | None = None,
    limit: int = 20,
) -> str:
    """
    Return the most recent evaluated signal outcomes with their full setup
    context: signal type, confidence, regime, trend direction, confluence
    indicators, and realized direction/TP1/TP2/SL/PnL results.

    :param symbol: Filter by symbol (optional)
    :param timeframe: Filter by timeframe (optional)
    :param limit: Max records to return (default 20)
    """
    from tools.knowledge_tools import get_recent_outcomes as _ro
    result = await _ro(symbol=symbol, timeframe=timeframe, limit=limit)
    return _json(result)


@mcp.tool()
async def get_failure_modes(
    symbol: str | None = None,
    timeframe: str | None = None,
    window: int = 100,
) -> str:
    """
    Analyse the most common patterns in losing trades: which regime/trend
    combinations lose most often, which confluence indicators appear most in
    losses, and which high-confidence signals turned out to be wrong.

    :param symbol: Filter by symbol (optional)
    :param timeframe: Filter by timeframe (optional)
    :param window: Lookback window in number of evaluated trades (default 100)
    """
    from tools.knowledge_tools import get_failure_modes as _fm
    result = await _fm(symbol=symbol, timeframe=timeframe, window=window)
    return _json(result)


# =============================================================================
# 13. SIMILAR PAST SETUPS (RAG)
# =============================================================================

@mcp.tool()
async def query_similar_setups(
    setup_description: str,
    symbol: str | None = None,
    timeframe: str | None = None,
    top_k: int = 5,
) -> str:
    """
    Semantically search the knowledge store for past signals that most
    closely match a described setup. Returns the top-k matches with their
    historical outcome (direction correct, TP1/TP2 hit, PnL) and a summary
    accuracy stat across the hits.

    Requires the RAG layer to be installed (pip install '.[rag]') and
    at least some evaluated signals to be indexed.

    Example setup_description:
      "trending bullish regime, EMA aligned, RSI 58 neutral, above VWAP 1s band,
       bullish OBV, neutral F&G 52, tight spread"

    :param setup_description: Free-text description of the current market setup
    :param symbol: Optionally restrict search to a symbol
    :param timeframe: Optionally restrict search to a timeframe
    :param top_k: Number of similar setups to retrieve (default 5)
    """
    from tools.knowledge_tools import query_similar_setups as _qs
    result = await _qs(
        query_text=setup_description,
        symbol=symbol,
        timeframe=timeframe,
        top_k=top_k,
    )
    return _json(result)


# =============================================================================
# 14. SELF-ASSESSMENT (trigger manual evolution cycle)
# =============================================================================

@mcp.tool()
async def run_self_assessment(
    symbol: str = "BTC/USDT",
    timeframe: str = "1h",
) -> str:
    """
    Manually trigger a strategy self-assessment cycle. The agent reviews
    recent backtest performance, proposes parameter mutations, runs an A/B
    comparison, and promotes the winner if it outperforms.

    This normally runs autonomously every ADAPTATION_INTERVAL_HOURS hours.
    Call this to force an immediate cycle.

    :param symbol: Symbol to assess against (default BTC/USDT)
    :param timeframe: Timeframe to assess (default 1h)
    """
    import uuid

    from agents.self_assessment import SelfAssessmentAgent
    agent = SelfAssessmentAgent()
    result = await agent.assess_and_evolve(
        symbol=symbol,
        timeframe=timeframe,
        correlation_id=str(uuid.uuid4()),
    )
    return _json(result)


# =============================================================================
# 15. PLATFORM STATUS
# =============================================================================

@mcp.tool()
async def get_platform_status() -> str:
    """
    Return the current platform status: configured symbols and timeframes,
    LLM provider and models in use, market data source, news providers
    configured, RAG vector store availability and record count, and
    Telegram notification status.
    """
    from config.settings import get_config
    from memory.vector_store import get_vector_store

    cfg = get_config()
    vs = get_vector_store()

    return _json({
        "trading": {
            "symbols": cfg.trading.symbols,
            "timeframes": cfg.trading.timeframes,
            "min_confidence": cfg.trading.min_confidence,
            "min_risk_reward": cfg.trading.min_risk_reward,
            "dry_run": cfg.trading.dry_run,
        },
        "llm": {
            "provider": "anthropic" if cfg.llm.has_anthropic_credentials() else "gateway",
            "signal_model": cfg.llm.signal_model,
            "assessment_model": cfg.llm.assessment_model,
            "summarizer_model": cfg.llm.summarizer_model,
        },
        "market_data": {
            "source": cfg.market_data.source,
            "exchange": cfg.market_data.ccxt_exchange,
            "fallback_to_synthetic": cfg.market_data.fallback_to_synthetic,
        },
        "news": {
            "enabled": cfg.news.enabled,
            "cryptopanic": bool(cfg.news.cryptopanic_api_key),
            "alpha_vantage": bool(cfg.news.alpha_vantage_api_key),
            "newsapi": bool(cfg.news.newsapi_api_key),
        },
        "rag": {
            "enabled": cfg.vector_store.enabled,
            "available": getattr(vs, "available", False),
            "record_count": vs.count() if getattr(vs, "available", False) else 0,
            "persist_dir": cfg.vector_store.resolved_persist_dir(),
            "embedder": cfg.embedding.provider,
        },
        "telegram": {
            "configured": cfg.telegram.is_configured(),
            "publish_signals": cfg.telegram.publish_signals,
        },
        "adaptation": {
            "enabled": cfg.adaptation.enabled,
            "interval_hours": cfg.adaptation.interval_hours,
        },
    })


# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Trading Intelligence Agent MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="Transport mode: stdio (Claude Desktop/Code) or sse (HTTP)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8083,
        help="Port for SSE transport (default 8083)",
    )
    args = parser.parse_args()

    if args.transport == "sse":
        mcp.run(transport="sse")
    else:
        mcp.run(transport="stdio")
