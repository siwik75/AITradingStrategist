"""
Signal Agent — Market analysis and signal generation.

Generates structured trading signals with:
- Entry price
- Stop Loss (SL)
- Take Profit 1 (TP1) — partial position exit
- Take Profit 2 (TP2) — full position exit
- Confidence score
- Multi-indicator confluence analysis
"""
import json
from agents.base import BaseAgent, AgentConfig
from tools.trading_tools import (
    calculate_indicators,
    get_ohlcv,
    get_strategy_params,
)
import structlog

log = structlog.get_logger()


SIGNAL_SYSTEM_PROMPT = """You are an expert quantitative trader specializing in technical analysis.
Your task is to analyze market data and generate precise trading signals.

METHODOLOGY:
1. Analyze ALL available indicators before making a decision
2. A signal is VALID only if at least 3 indicators converge (multi-confluence)
3. In doubt, emit HOLD — capital preservation is paramount
4. Minimum risk/reward ratio: 1:2 (TP1) and 1:3.5 (TP2)
5. Always respect the major trend (EMA200 direction)
6. Consider volatility regime (ATR, Bollinger Width, Squeeze detection)

SIGNAL STRUCTURE:
- Entry: current price or limit price level
- SL (Stop Loss): based on ATR × multiplier below/above entry
- TP1 (Take Profit 1): partial exit at ATR × 2.0 from entry (conservative target)
- TP2 (Take Profit 2): full exit at ATR × 3.5 from entry (aggressive target)

POSITION MANAGEMENT:
- Exit 50% at TP1, move SL to breakeven
- Exit remaining 50% at TP2

SIGNAL QUALITY CRITERIA:
- STRONG (confidence 80-100): 4+ indicators aligned, strong trend, volume confirmation
- MODERATE (confidence 60-79): 3 indicators aligned, trend present, average volume
- WEAK (confidence < 60): emit HOLD instead

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
  "reasoning": "detailed multi-paragraph analysis covering trend, momentum, volatility, volume, and key levels"
}"""


class SignalAgent(BaseAgent):
    """
    Trading signal generator with multi-indicator confluence analysis.
    """

    def __init__(self, use_openai_gateway: bool = False):
        config = AgentConfig(
            name="signal_agent",
            temperature=0.0,  # deterministic for trading decisions
            system_prompt=SIGNAL_SYSTEM_PROMPT,
            max_iterations=8,
            use_openai_gateway=use_openai_gateway,
        )
        tools = [calculate_indicators, get_ohlcv, get_strategy_params]
        super().__init__(config, tools)

    async def analyze(
        self,
        symbol: str,
        timeframe: str = "4h",
        correlation_id: str = None,
    ) -> dict:
        """
        Analyze market and generate a trading signal.
        
        :param symbol: Trading symbol (e.g., BTC/USDT)
        :param timeframe: Analysis timeframe
        :param correlation_id: Tracing ID
        :returns: Structured signal dict
        """
        task = f"""Analyze the market for {symbol} on the {timeframe} timeframe.

STEP 1: Fetch current strategy parameters using get_strategy_params.

STEP 2: Calculate full technical indicators using calculate_indicators.
  - symbol: {symbol}
  - timeframe: {timeframe}
  - indicator_set: full

STEP 3: Also check the daily timeframe for the major trend context.
  Use calculate_indicators with timeframe "1d" and indicator_set "trend".

STEP 4: Synthesize all data and generate a trading signal.
  Consider:
  - Trend alignment across timeframes ({timeframe} vs daily)
  - Momentum confirmation (RSI, Stochastic, MACD)
  - Volatility context (ATR for SL/TP, Bollinger Bands for regime, Squeeze)
  - Volume confirmation (is volume supporting the move?)
  - Key support/resistance levels (pivot points)
  
  Calculate exact SL/TP levels using ATR:
  - SL = entry ± (ATR × atr_sl_multiplier from strategy params)
  - TP1 = entry ± (ATR × atr_tp1_multiplier from strategy params)
  - TP2 = entry ± (ATR × atr_tp2_multiplier from strategy params)

Respond ONLY with the JSON signal object."""

        result_str = await self.run(task, correlation_id=correlation_id)
        
        try:
            clean = result_str.strip()
            if clean.startswith("```"):
                clean = clean.split("\n", 1)[1]
                clean = clean.rsplit("```", 1)[0]
            return json.loads(clean)
        except json.JSONDecodeError:
            log.warning("agent.signal.json_parse_failed",
                agent="signal_agent",
                correlation_id=correlation_id,
            )
            return {
                "signal": "HOLD",
                "confidence": 0,
                "reasoning": "Failed to parse signal response",
                "raw_response": result_str,
            }
