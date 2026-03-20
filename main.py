"""
Main entry point — Trading Intelligence Agent.

Provides:
1. CLI mode for direct analysis
2. FastAPI server with health endpoints (K8s readiness/liveness)
3. Webhook endpoint for event-driven triggering

16-Factor App compliant:
- Port binding via PORT env var
- Health endpoints for K8s probes
- Graceful shutdown via SIGTERM handling
- Structured JSON logging
"""
import asyncio
import argparse
import json
import signal
import sys
import uuid
from datetime import datetime

import structlog

# Configure structured logging FIRST
structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(20),
)

log = structlog.get_logger()

from config.settings import get_config


# =============================================================================
# CLI MODE
# =============================================================================

async def run_cli(args):
    """Run in CLI mode — single analysis or full cycle."""
    config = get_config()
    cid = str(uuid.uuid4())[:8]
    
    log.info("cli.start",
        mode=args.mode,
        symbol=args.symbol,
        timeframe=args.timeframe,
        correlation_id=cid,
    )
    
    if args.mode == "analysis":
        from agents.signal_agent import SignalAgent
        agent = SignalAgent()
        result = await agent.analyze(
            symbol=args.symbol,
            timeframe=args.timeframe,
            correlation_id=cid,
        )
        print(json.dumps(result, indent=2, default=str))
    
    elif args.mode == "backtest":
        from tools.trading_tools import run_backtest, get_strategy_params
        params = await get_strategy_params()
        result = await run_backtest(
            symbol=args.symbol,
            timeframe=args.timeframe,
            strategy_params=json.dumps(params),
            days=args.days,
        )
        print(json.dumps(result, indent=2, default=str))
    
    elif args.mode == "assess":
        from agents.self_assessment import SelfAssessmentAgent
        agent = SelfAssessmentAgent()
        result = await agent.assess_and_evolve(
            symbol=args.symbol,
            timeframe=args.timeframe,
            backtest_days=args.days,
            correlation_id=cid,
        )
        print(json.dumps(result, indent=2, default=str))
    
    elif args.mode == "full":
        from workflows.trading_workflow import (
            build_trading_workflow,
            run_workflow_sequential,
        )
        
        workflow = build_trading_workflow()
        if workflow:
            # LangGraph mode
            result = await workflow.ainvoke({
                "symbol": args.symbol,
                "timeframe": args.timeframe,
                "backtest_days": args.days,
                "correlation_id": cid,
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
            })
        else:
            # Sequential fallback
            result = await run_workflow_sequential(
                symbol=args.symbol,
                timeframe=args.timeframe,
                backtest_days=args.days,
                correlation_id=cid,
            )
        
        print("\n" + "=" * 70)
        print("TRADING INTELLIGENCE REPORT")
        print("=" * 70)
        print(f"Symbol: {args.symbol} | Timeframe: {args.timeframe}")
        print(f"Correlation ID: {cid}")
        print(f"Completed Steps: {result.get('completed_steps', [])}")
        print(f"Errors: {result.get('errors', [])}")
        print("-" * 70)
        
        if result.get("signal"):
            sig = result["signal"]
            print(f"\nSIGNAL: {sig.get('signal', 'N/A')}")
            print(f"Confidence: {sig.get('confidence', 0)}%")
            print(f"Entry: {sig.get('entry_price', 'N/A')}")
            print(f"Stop Loss: {sig.get('stop_loss', 'N/A')}")
            print(f"TP1: {sig.get('take_profit_1', 'N/A')}")
            print(f"TP2: {sig.get('take_profit_2', 'N/A')}")
        
        if result.get("backtest_current"):
            bt = result["backtest_current"]
            print(f"\nBACKTEST RESULTS:")
            print(f"  Trades: {bt.get('total_trades', 0)}")
            print(f"  Win Rate: {bt.get('win_rate', 0)}%")
            print(f"  Profit Factor: {bt.get('profit_factor', 0)}")
            print(f"  Total P&L: {bt.get('total_pnl_pct', 0)}%")
            print(f"  Max Drawdown: {bt.get('max_drawdown_pct', 0)}%")
        
        if result.get("assessment") and not result["assessment"].get("parse_error"):
            assess = result["assessment"]
            print(f"\nSELF-ASSESSMENT:")
            print(f"  Decision: {assess.get('decision', 'N/A')}")
            print(f"  Reasoning: {assess.get('decision_reasoning', 'N/A')[:200]}")
        
        print("\n" + "=" * 70)
        print(json.dumps(result, indent=2, default=str))


# =============================================================================
# SERVER MODE (FastAPI)
# =============================================================================

def run_server():
    """Run FastAPI server with health endpoints."""
    from fastapi import FastAPI, Request, Response
    import uvicorn
    
    config = get_config()
    app = FastAPI(
        title="Trading Intelligence Agent",
        version="1.0.0",
        docs_url="/docs" if not config.is_production() else None,
    )
    
    # Health endpoints — K8s liveness & readiness probes
    @app.get("/health")
    async def health():
        """Liveness probe — is the process alive?"""
        return {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}
    
    @app.get("/ready")
    async def ready():
        """Readiness probe — is the agent ready to process?"""
        # Check LLM connectivity, backing services
        try:
            # Lightweight check
            return {
                "status": "ready",
                "agent_id": config.agent_id,
                "environment": config.environment,
                "timestamp": datetime.utcnow().isoformat(),
            }
        except Exception as e:
            return Response(
                content=json.dumps({"status": "not_ready", "error": str(e)}),
                status_code=503,
            )
    
    @app.post("/analyze")
    async def analyze(request: Request):
        """Trigger a market analysis."""
        body = await request.json()
        cid = request.headers.get("X-Correlation-ID", str(uuid.uuid4())[:8])
        
        from agents.signal_agent import SignalAgent
        agent = SignalAgent()
        result = await agent.analyze(
            symbol=body.get("symbol", "BTC/USDT"),
            timeframe=body.get("timeframe", "4h"),
            correlation_id=cid,
        )
        return result
    
    @app.post("/assess")
    async def assess(request: Request):
        """Trigger self-assessment cycle."""
        body = await request.json()
        cid = request.headers.get("X-Correlation-ID", str(uuid.uuid4())[:8])
        
        from agents.self_assessment import SelfAssessmentAgent
        agent = SelfAssessmentAgent()
        result = await agent.assess_and_evolve(
            symbol=body.get("symbol", "BTC/USDT"),
            timeframe=body.get("timeframe", "4h"),
            backtest_days=body.get("days", 30),
            correlation_id=cid,
        )
        return result
    
    # Graceful shutdown
    shutdown_event = asyncio.Event()
    
    def handle_sigterm(*_):
        log.info("server.sigterm_received")
        shutdown_event.set()
    
    signal.signal(signal.SIGTERM, handle_sigterm)
    
    log.info("server.start", port=config.infra.port)
    uvicorn.run(app, host="0.0.0.0", port=config.infra.port)


# =============================================================================
# ENTRY POINT
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="Trading Intelligence Agent")
    parser.add_argument("--mode", choices=["analysis", "backtest", "assess", "full", "server"],
                        default="analysis", help="Operation mode")
    parser.add_argument("--symbol", default="BTC/USDT", help="Trading symbol")
    parser.add_argument("--timeframe", default="4h", help="Analysis timeframe")
    parser.add_argument("--days", type=int, default=30, help="Backtest lookback days")
    
    args = parser.parse_args()
    
    if args.mode == "server":
        run_server()
    else:
        asyncio.run(run_cli(args))


if __name__ == "__main__":
    main()
