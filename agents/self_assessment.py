"""
Self-Assessment Agent — Autonomous strategy evolution.

This agent:
1. Reviews historical backtest performance
2. Identifies which indicators performed well/poorly
3. Proposes strategy parameter mutations
4. Validates mutations via A/B backtesting
5. Promotes winning configurations

This is the "brain" that makes the system adaptive — it embodies the
concept of an agent that autonomously improves its own decision-making.
"""
import json
from agents.base import BaseAgent, AgentConfig
from tools.trading_tools import (
    run_backtest,
    get_strategy_params,
    save_strategy_params,
    calculate_indicators,
    get_trade_history,
)
import structlog

log = structlog.get_logger()


SELF_ASSESSMENT_PROMPT = """You are an expert quantitative trading strategist performing 
a self-assessment of your trading system's performance.

Your role is to:
1. ANALYZE backtest results to identify what's working and what's not
2. DIAGNOSE root causes of losses (wrong indicators, bad timing, poor risk management)
3. PROPOSE specific, measurable parameter changes to improve performance
4. VALIDATE proposals by comparing current vs. proposed parameters via backtesting
5. DECIDE whether to adopt the new parameters or keep the current ones

CRITICAL RULES:
- Never change more than 2-3 parameters at a time (controlled experimentation)
- Always preserve risk limits (never increase risk per trade beyond 2%)
- ATR stop-loss multiplier must stay between 1.0 and 3.0
- ATR take-profit multipliers: TP1 must be > SL multiplier, TP2 must be > TP1
- Minimum ADX threshold must stay between 15 and 30
- RSI boundaries: oversold between 20-35, overbought between 65-80
- Document your reasoning for every change

OUTPUT FORMAT (JSON):
{
  "assessment": {
    "current_performance": {
      "win_rate": float,
      "profit_factor": float,
      "max_drawdown": float,
      "sharpe_ratio": float,
      "total_trades": int
    },
    "diagnosis": "detailed analysis of what went wrong/right",
    "indicator_effectiveness": {
      "trend_indicators": {"score": 1-10, "notes": "..."},
      "momentum_indicators": {"score": 1-10, "notes": "..."},
      "volatility_indicators": {"score": 1-10, "notes": "..."},
      "volume_indicators": {"score": 1-10, "notes": "..."}
    },
    "key_issues": ["issue1", "issue2"],
    "strengths": ["strength1", "strength2"]
  },
  "proposed_changes": {
    "parameter_name": {"old": value, "new": value, "reasoning": "why"},
    ...
  },
  "proposed_performance": {
    "win_rate": float,
    "profit_factor": float,
    "max_drawdown": float,
    "improvement_pct": float
  },
  "decision": "ADOPT|REJECT|PARTIAL",
  "decision_reasoning": "why this decision",
  "final_params": {full parameter set to use going forward}
}

Always respond with valid JSON only."""


class SelfAssessmentAgent(BaseAgent):
    """
    Autonomous strategy evolution agent.
    
    Workflow:
    1. Load current strategy params
    2. Run backtest with current params
    3. Analyze performance
    4. Generate mutation proposals  
    5. Run backtest with proposed params
    6. Compare A vs B
    7. Promote winner
    """

    def __init__(self, use_openai_gateway: bool = False):
        config = AgentConfig(
            name="self_assessment_agent",
            temperature=0.2,  # slight creativity for strategy exploration
            system_prompt=SELF_ASSESSMENT_PROMPT,
            max_iterations=15,  # needs more iterations for multi-step analysis
            use_openai_gateway=use_openai_gateway,
        )
        tools = [
            run_backtest,
            get_strategy_params,
            save_strategy_params,
            calculate_indicators,
            get_trade_history,
        ]
        super().__init__(config, tools)

    async def assess_and_evolve(
        self,
        symbol: str,
        timeframe: str = "4h",
        backtest_days: int = 30,
        correlation_id: str = None,
    ) -> dict:
        """
        Run the full self-assessment cycle.
        
        :param symbol: Trading symbol to assess
        :param timeframe: Primary timeframe
        :param backtest_days: Days of data for backtesting
        :param correlation_id: Tracing ID
        :returns: Assessment result with decision
        """
        task = f"""Perform a complete self-assessment cycle for {symbol} on {timeframe} timeframe.

STEP 1: Get the current strategy parameters using get_strategy_params tool.

STEP 2: Run a backtest with these current parameters using run_backtest tool.
  - symbol: {symbol}
  - timeframe: {timeframe}
  - days: {backtest_days}
  - strategy_params: (use the current params from step 1)

STEP 3: Analyze the backtest results. Look at:
  - Win rate (target: >55%)
  - Profit factor (target: >1.5)
  - Max drawdown (target: <10%)
  - Exit distribution (are we hitting TP1/TP2 or mostly stopped out?)
  - Individual trade quality

STEP 4: Also check current market conditions using calculate_indicators tool.
  This helps you understand if the market regime has changed.

STEP 5: Based on your analysis, propose 2-3 specific parameter changes.
  Think about:
  - If too many stops are hit → maybe widen SL (increase atr_sl_multiplier)
  - If win rate is low but avg win is high → maybe tighten entry criteria (increase min_adx)
  - If missing good moves → maybe relax RSI filters
  - If getting whipsawed → maybe switch to longer EMAs
  - If volume filter is excluding good trades → consider disabling it

STEP 6: Run a SECOND backtest with your proposed parameters.
  Use run_backtest with the new strategy_params.

STEP 7: Compare results. If the proposed params show improvement:
  - Use save_strategy_params to persist the new configuration
  - Report the improvement metrics

Provide your full assessment as the final JSON output."""

        result_str = await self.run(task, correlation_id=correlation_id)
        
        # Try to parse as JSON
        try:
            # Handle markdown code blocks if present
            clean = result_str.strip()
            if clean.startswith("```"):
                clean = clean.split("\n", 1)[1]
                clean = clean.rsplit("```", 1)[0]
            return json.loads(clean)
        except json.JSONDecodeError:
            log.warning("agent.assessment.json_parse_failed",
                agent="self_assessment_agent",
                correlation_id=correlation_id,
            )
            return {
                "raw_response": result_str,
                "parse_error": True,
            }
