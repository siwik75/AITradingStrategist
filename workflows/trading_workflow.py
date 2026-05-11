"""
Trading Intelligence Workflow — LangGraph StateGraph orchestration.

Implements the full cycle:
  Market Analysis → Signal Generation → Backtesting → Self-Assessment → Strategy Evolution

This workflow demonstrates the StateGraph pattern from the Generali GOSP blueprint,
showing how multiple specialized agents coordinate through shared state.
"""
from typing import TypedDict, Annotated, Optional
from dataclasses import dataclass
import operator
import json
import structlog

log = structlog.get_logger()


# =============================================================================
# STATE DEFINITION
# =============================================================================

class TradingState(TypedDict):
    """Shared state across all workflow nodes."""
    # Input
    symbol: str
    timeframe: str
    backtest_days: int
    correlation_id: str
    
    # Market data
    indicators: Optional[dict]
    
    # Signal
    signal: Optional[dict]
    
    # Backtest
    backtest_current: Optional[dict]
    backtest_proposed: Optional[dict]
    
    # Self-assessment
    assessment: Optional[dict]
    
    # Strategy params
    current_params: Optional[dict]
    proposed_params: Optional[dict]
    final_params: Optional[dict]
    
    # Control
    iteration: int
    errors: Annotated[list, operator.add]
    completed_steps: Annotated[list, operator.add]


# =============================================================================
# WORKFLOW NODES
# =============================================================================

async def analyze_market(state: TradingState) -> TradingState:
    """Node 1: Fetch market data and calculate indicators."""
    from tools.trading_tools import calculate_indicators
    
    log.info("workflow.node.analyze_market",
        symbol=state["symbol"],
        correlation_id=state["correlation_id"],
    )
    
    try:
        indicators = await calculate_indicators(
            symbol=state["symbol"],
            timeframe=state["timeframe"],
            indicator_set="full",
        )
        return {
            **state,
            "indicators": indicators,
            "completed_steps": ["analyze_market"],
        }
    except Exception as e:
        log.error("workflow.node.error", node="analyze_market", error=str(e))
        return {
            **state,
            "errors": [f"analyze_market: {str(e)}"],
        }


async def generate_signal(state: TradingState) -> TradingState:
    """Node 2: Generate trading signal via SignalAgent."""
    from agents.signal_agent import SignalAgent
    
    log.info("workflow.node.generate_signal",
        symbol=state["symbol"],
        correlation_id=state["correlation_id"],
    )
    
    try:
        agent = SignalAgent()
        signal = await agent.analyze(
            symbol=state["symbol"],
            timeframe=state["timeframe"],
            correlation_id=state["correlation_id"],
        )
        return {
            **state,
            "signal": signal,
            "completed_steps": ["generate_signal"],
        }
    except Exception as e:
        log.error("workflow.node.error", node="generate_signal", error=str(e))
        return {
            **state,
            "errors": [f"generate_signal: {str(e)}"],
        }


async def run_current_backtest(state: TradingState) -> TradingState:
    """Node 3: Backtest with current strategy parameters."""
    from tools.trading_tools import run_backtest, get_strategy_params
    
    log.info("workflow.node.run_current_backtest",
        correlation_id=state["correlation_id"],
    )
    
    try:
        params = await get_strategy_params(timeframe=state["timeframe"])
        backtest = await run_backtest(
            symbol=state["symbol"],
            timeframe=state["timeframe"],
            strategy_params=json.dumps(params),
            days=state["backtest_days"],
        )
        return {
            **state,
            "current_params": params,
            "backtest_current": backtest,
            "completed_steps": ["run_current_backtest"],
        }
    except Exception as e:
        log.error("workflow.node.error", node="run_current_backtest", error=str(e))
        return {
            **state,
            "errors": [f"run_current_backtest: {str(e)}"],
        }


async def self_assess(state: TradingState) -> TradingState:
    """Node 4: Self-assessment and strategy evolution."""
    from agents.self_assessment import SelfAssessmentAgent
    
    log.info("workflow.node.self_assess",
        correlation_id=state["correlation_id"],
    )
    
    try:
        agent = SelfAssessmentAgent()
        assessment = await agent.assess_and_evolve(
            symbol=state["symbol"],
            timeframe=state["timeframe"],
            backtest_days=state["backtest_days"],
            correlation_id=state["correlation_id"],
        )
        
        # Extract final params from assessment
        final_params = state.get("current_params", {})
        if isinstance(assessment, dict) and not assessment.get("parse_error"):
            final_params = assessment.get("final_params", final_params)
            decision = assessment.get("decision", "REJECT")
            if decision in ("ADOPT", "PARTIAL") and final_params:
                try:
                    from tools.trading_tools import save_strategy_params
                    import json as _json
                    await save_strategy_params(
                        _json.dumps(final_params),
                        timeframe=state["timeframe"],
                    )
                    log.info("workflow.strategy_params_persisted", decision=decision)
                except Exception as persist_err:
                    log.warning("workflow.persist_failed", error=str(persist_err))

        return {
            **state,
            "assessment": assessment,
            "final_params": final_params,
            "completed_steps": ["self_assess"],
        }
    except Exception as e:
        log.error("workflow.node.error", node="self_assess", error=str(e))
        return {
            **state,
            "errors": [f"self_assess: {str(e)}"],
        }


def should_self_assess(state: TradingState) -> str:
    """Conditional edge: decide if self-assessment is needed."""
    backtest = state.get("backtest_current")
    if not backtest:
        return "end"
    
    # Trigger self-assessment if performance is below thresholds
    win_rate = backtest.get("win_rate", 0)
    profit_factor = backtest.get("profit_factor", 0)
    max_dd = abs(backtest.get("max_drawdown_pct", 0))
    
    needs_assessment = (
        win_rate < 55
        or profit_factor < 1.5
        or max_dd > 10
        or backtest.get("total_trades", 0) < 5
    )
    
    if needs_assessment:
        log.info("workflow.self_assess_triggered",
            win_rate=win_rate,
            profit_factor=profit_factor,
            max_drawdown=max_dd,
        )
        return "self_assess"
    
    log.info("workflow.self_assess_skipped",
        reason="performance within acceptable range",
    )
    return "end"


# =============================================================================
# GRAPH BUILDER
# =============================================================================

def build_trading_workflow():
    """
    Build the LangGraph StateGraph for the trading intelligence system.
    
    Flow:
        analyze_market → generate_signal → run_current_backtest
            → (conditional) → self_assess → end
                          → end
    """
    try:
        from langgraph.graph import StateGraph, END
        
        graph = StateGraph(TradingState)
        
        # Add nodes
        graph.add_node("analyze_market", analyze_market)
        graph.add_node("generate_signal", generate_signal)
        graph.add_node("run_current_backtest", run_current_backtest)
        graph.add_node("self_assess", self_assess)
        
        # Define edges
        graph.set_entry_point("analyze_market")
        graph.add_edge("analyze_market", "generate_signal")
        graph.add_edge("generate_signal", "run_current_backtest")
        
        # Conditional: self-assess if needed
        graph.add_conditional_edges(
            "run_current_backtest",
            should_self_assess,
            {
                "self_assess": "self_assess",
                "end": END,
            }
        )
        graph.add_edge("self_assess", END)
        
        return graph.compile()
    
    except ImportError:
        log.warning("langgraph not installed, using sequential fallback")
        return None


async def run_workflow_sequential(
    symbol: str,
    timeframe: str = "4h",
    backtest_days: int = 30,
    correlation_id: str = "demo",
) -> TradingState:
    """
    Fallback: run workflow sequentially without LangGraph.
    This ensures the demo works even without langgraph installed.
    """
    state: TradingState = {
        "symbol": symbol,
        "timeframe": timeframe,
        "backtest_days": backtest_days,
        "correlation_id": correlation_id,
        "indicators": None,
        "signal": None,
        "backtest_current": None,
        "backtest_proposed": None,
        "assessment": None,
        "current_params": None,
        "proposed_params": None,
        "final_params": None,
        "iteration": 0,
        "errors": [],
        "completed_steps": [],
    }
    
    # Step 1
    state = await analyze_market(state)
    
    # Step 2
    state = await generate_signal(state)
    
    # Step 3
    state = await run_current_backtest(state)
    
    # Step 4 (conditional)
    decision = should_self_assess(state)
    if decision == "self_assess":
        state = await self_assess(state)
    
    return state
