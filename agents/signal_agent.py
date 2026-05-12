"""
Signal Agent — Market analysis and signal generation.

Generates structured trading signals with:
- Multi-confluence technical analysis (trend, momentum, volatility, volume, VWAP bands)
- Pre-injected market context (news sentiment, Fear & Greed, order-book liquidity)
- A "lessons" block summarising prior performance (KPIs, failure modes)
- On-demand RAG retrieval of similar past setups (via tool call)
"""
import structlog

from agents.base import AgentConfig, BaseAgent
from agents.json_utils import parse_json_response
from config.settings import get_config
from tools.knowledge_tools import (
    build_lesson_card,
    format_lesson_card_text,
    get_failure_modes,
    get_kpi_summary,
    get_recent_outcomes,
    query_similar_setups,
)
from tools.trading_tools import (
    calculate_indicators,
    get_ohlcv,
    get_strategy_params,
)
from workflows.market_context import build_market_context, format_market_context_text

log = structlog.get_logger()


SIGNAL_SYSTEM_PROMPT = """You are an expert quantitative trader specializing in technical analysis,
market microstructure, and sentiment-aware decision making.

METHODOLOGY:
1. Read the MARKET CONTEXT block carefully — Fear & Greed, news sentiment, and order-book
   liquidity must inform your stance even when technicals look clean.
2. Read the LESSONS FROM PAST SIGNALS block — recent KPIs and failure modes are your
   confidence calibrator. If high-confidence signals have been wrong recently, lower yours.
   If a regime/trend combo appears repeatedly in losses, require stronger confirmation.
3. Analyse ALL indicators before deciding. A signal is VALID only if at least 3 indicators
   converge (multi-confluence).
4. In doubt, emit HOLD — capital preservation is paramount.
5. Minimum risk/reward ratio: 1:2 (TP1) and 1:3.5 (TP2).
6. Always respect the major trend (EMA200 direction on the daily timeframe).
7. Consider volatility regime (ATR, Bollinger Width, Squeeze detection) and where price sits
   relative to session VWAP bands (above_2s / below_2s = stretched, mean-reversion bias).

USING MARKET CONTEXT:
- Extreme greed (F&G > 75) + bullish technicals = increased reversal risk → reduce confidence
  or favor partial-size entries. Extreme fear (F&G < 25) + bullish reversal signs = potential
  high-RR long setups.
- News sentiment confidence < 40 means signals conflict — be cautious.
- Order-book imbalance > 0.10 means microstructure agrees with bullish bias (and vice versa).
- Large resistance walls overhead cap upside — if TP1 sits beyond a wall, lower its
  reachability and consider a tighter TP1.
- High spread_bps or partial slippage_probe fills mean liquidity is thin → widen SL or skip.

USING THE KNOWLEDGE TOOLS:
- Call `query_similar_setups` once indicators are computed, passing a terse description of
  the current setup. Use the returned past-outcome stats to calibrate confidence.
- Call `get_recent_outcomes` if you want to inspect specific recent trades.
- Call `get_failure_modes` if your setup matches a known losing pattern.

SIGNAL STRUCTURE:
- Entry: current price or limit price level
- SL (Stop Loss): based on ATR × multiplier below/above entry
- TP1 (Take Profit 1): partial exit at ATR × 2.0 from entry (conservative target)
- TP2 (Take Profit 2): full exit at ATR × 3.5 from entry (aggressive target)

POSITION MANAGEMENT:
- Exit 50% at TP1, move SL to breakeven
- Exit remaining 50% at TP2

SIGNAL QUALITY CRITERIA:
- STRONG (confidence 80-100): 4+ indicators aligned, strong trend, volume + microstructure
  confirmation, no contradicting sentiment, lessons block supports this setup type.
- MODERATE (confidence 60-79): 3 indicators aligned, trend present, average volume,
  neutral-to-supporting context.
- WEAK (confidence < 60): emit HOLD instead.

OUTPUT FORMAT (JSON only, no additional text):
{
  "signal": "BUY|SELL|HOLD",
  "confidence": 0-100,
  "entry_price": float,
  "stop_loss": float,
  "take_profit_1": float,
  "take_profit_2": float,
  "risk_reward_tp1": float,
  "risk_reward_tp2": float,
  "timeframe": "analyzed timeframe",
  "confluence_indicators": ["list of aligned indicators"],
  "divergent_indicators": ["list of opposing indicators"],
  "market_regime": "trending|ranging|volatile|squeeze",
  "trend_direction": "bullish|bearish|neutral",
  "context_factors": {
    "fear_greed_alignment": "supporting|conflicting|neutral",
    "news_sentiment_alignment": "supporting|conflicting|neutral",
    "liquidity_quality": "good|thin|walls_overhead|walls_below",
    "lessons_adjustment": "raised|lowered|unchanged"
  },
  "reasoning": "detailed multi-paragraph analysis covering trend, momentum, volatility, volume, VWAP positioning, sentiment, microstructure, and how prior lessons shaped your confidence"
}"""


class SignalAgent(BaseAgent):
    """
    Trading signal generator with multi-indicator confluence + sentiment + RAG knowledge.
    """

    def __init__(
        self,
        use_openai_gateway: bool = False,
        model: str | None = None,
    ):
        cfg = get_config()
        resolved_model = model or cfg.llm.signal_model
        config = AgentConfig(
            name="signal_agent",
            model=resolved_model,
            temperature=0.0,
            system_prompt=SIGNAL_SYSTEM_PROMPT,
            max_iterations=10,
            use_openai_gateway=use_openai_gateway,
        )
        tools = [
            # Technical / market data
            calculate_indicators,
            get_ohlcv,
            get_strategy_params,
            # Knowledge feedback loop
            get_recent_outcomes,
            get_kpi_summary,
            get_failure_modes,
            query_similar_setups,
        ]
        super().__init__(config, tools)

    async def analyze(
        self,
        symbol: str,
        timeframe: str = "4h",
        correlation_id: str = None,
        *,
        skip_market_context: bool = False,
        skip_lessons: bool = False,
    ) -> dict:
        """
        Analyze market and generate a trading signal.

        :param symbol: Trading symbol (e.g., BTC/USDT or AAPL)
        :param timeframe: Analysis timeframe
        :param correlation_id: Tracing ID
        :param skip_market_context: Skip pre-fetching news/F&G/liquidity (testing only)
        :param skip_lessons: Skip pre-computing the lessons block (testing only)
        """
        # ---------- Phase A: Pre-fetch external context (parallel) ----------
        market_block = ""
        if not skip_market_context:
            try:
                ctx = await build_market_context(
                    symbol=symbol,
                    timeframe=timeframe,
                    correlation_id=correlation_id,
                )
                market_block = format_market_context_text(ctx)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "signal_agent.market_context_failed",
                    error=str(exc),
                    correlation_id=correlation_id,
                )
                market_block = "=== MARKET CONTEXT ===\n- unavailable (pre-fetch error)\n=== END MARKET CONTEXT ==="

        # ---------- Phase B: Pre-compute lesson card (KPIs + failure modes) ----------
        lessons_block = ""
        if not skip_lessons:
            try:
                card = await build_lesson_card(
                    symbol=symbol,
                    timeframe=timeframe,
                    current_setup_text=None,  # RAG happens via tool once indicators are known
                )
                lessons_block = format_lesson_card_text(card)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "signal_agent.lesson_card_failed",
                    error=str(exc),
                    correlation_id=correlation_id,
                )
                lessons_block = "=== LESSONS FROM PAST SIGNALS ===\n- unavailable (pre-fetch error)\n=== END LESSONS ==="

        # ---------- Phase C: Compose the agent task ----------
        task = f"""Analyze the market for {symbol} on the {timeframe} timeframe.

{market_block}

{lessons_block}

STEP 1: Fetch current strategy parameters via get_strategy_params (timeframe={timeframe}).

STEP 2: Calculate full technical indicators via calculate_indicators
  - symbol: {symbol}
  - timeframe: {timeframe}
  - indicator_set: full
  Pay attention to: trend (EMA alignment, ADX, MACD), momentum (RSI, Stoch, MFI),
  volatility (ATR, BB, KC squeeze), volume (OBV, VWAP session bands).

STEP 3: Check the daily timeframe for major trend context
  Use calculate_indicators with timeframe "1d" and indicator_set "trend".

STEP 4: Construct a one-line setup description (regime + trend + key indicator states +
  VWAP band position + microstructure imbalance) and call `query_similar_setups` with it.
  Use the returned historical accuracy and avg PnL to calibrate your confidence.

STEP 5: Synthesize everything (technicals + market context + lessons + RAG hits) and
  produce the JSON signal.

  Calculate exact SL/TP levels using ATR:
  - SL = entry ± (ATR × atr_sl_multiplier from strategy params)
  - TP1 = entry ± (ATR × atr_tp1_multiplier from strategy params)
  - TP2 = entry ± (ATR × atr_tp2_multiplier from strategy params)

  In `context_factors`, report HOW each context block influenced your decision.
  In `reasoning`, explicitly cite the market context numbers and the RAG accuracy you saw.

Respond ONLY with the JSON signal object."""

        result_str = await self.run(task, correlation_id=correlation_id)

        try:
            return parse_json_response(result_str)
        except ValueError:
            log.warning(
                "agent.signal.json_parse_failed",
                agent="signal_agent",
                correlation_id=correlation_id,
            )
            return {
                "signal": "HOLD",
                "confidence": 0,
                "reasoning": "Failed to parse signal response",
                "raw_response": result_str,
            }
