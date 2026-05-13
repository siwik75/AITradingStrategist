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
# ruff: noqa: E402, I001

# Load .env FIRST — before any project imports so env vars are available
# when config/settings.py evaluates its os.getenv() default_factory lambdas.
from dotenv import load_dotenv

load_dotenv()

import asyncio
import argparse
import json
import os
import signal
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import structlog

# Configure structured logging after dotenv (LOG_LEVEL may come from .env)
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

    # Validate config early; skip for offline data modes (no LLM key needed)
    if args.mode not in ("backtest", "candles", "signals", "report"):
        try:
            config.validate()
        except ValueError as exc:
            log.error("cli.config_invalid", error=str(exc))
            sys.exit(1)

    cid = str(uuid.uuid4())[:8]

    log.info("cli.start",
        mode=args.mode,
        symbol=args.symbol,
        timeframe=args.timeframe,
        correlation_id=cid,
    )

    if args.mode == "analysis":
        from agents.signal_agent import SignalAgent
        from tools.trading_tools import save_signal_notification

        agent = SignalAgent()
        result = await agent.analyze(
            symbol=args.symbol,
            timeframe=args.timeframe,
            correlation_id=cid,
        )
        if result.get("signal") in {"BUY", "SELL"}:
            notification = await save_signal_notification(
                signal_json=result,
                symbol=args.symbol,
                timeframe=args.timeframe,
                correlation_id=cid,
            )
            result["signal_id"] = notification.get("signal_id")
            result["notification_status"] = notification.get("status")
        else:
            result["notification_status"] = "not_actionable"
        print(json.dumps(result, indent=2, default=str))

    elif args.mode == "backtest":
        from tools.trading_tools import get_strategy_params, run_backtest

        params = await get_strategy_params(timeframe=args.timeframe)
        result = await run_backtest(
            symbol=args.symbol,
            timeframe=args.timeframe,
            strategy_params=json.dumps(params),
            days=args.days,
        )
        print(json.dumps(result, indent=2, default=str))

    elif args.mode == "candles":
        from tools.trading_tools import get_ohlcv

        result = await get_ohlcv(
            symbol=args.symbol,
            timeframe=args.timeframe,
            limit=args.limit,
            source=args.source,
        )
        candles = result.get("candles", [])
        first = candles[0] if candles else None
        last = candles[-1] if candles else None

        smoke = {
            "symbol": result.get("symbol", args.symbol),
            "timeframe": result.get("timeframe", args.timeframe),
            "source": result.get("source"),
            "requested_source": args.source,
            "count": len(candles),
            "first_candle": first,
            "last_candle": last,
        }
        print(json.dumps(smoke, indent=2, default=str))

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
            print("\nBACKTEST RESULTS:")
            print(f"  Trades: {bt.get('total_trades', 0)}")
            print(f"  Win Rate: {bt.get('win_rate', 0)}%")
            print(f"  Profit Factor: {bt.get('profit_factor', 0)}")
            print(f"  Total P&L: {bt.get('total_pnl_pct', 0)}%")
            print(f"  Max Drawdown: {bt.get('max_drawdown_pct', 0)}%")

        if result.get("assessment") and not result["assessment"].get("parse_error"):
            assess = result["assessment"]
            print("\nSELF-ASSESSMENT:")
            print(f"  Decision: {assess.get('decision', 'N/A')}")
            print(f"  Reasoning: {assess.get('decision_reasoning', 'N/A')[:200]}")

        print("\n" + "=" * 70)
        print(json.dumps(result, indent=2, default=str))

    elif args.mode == "signals":
        from tools.trading_tools import get_signal_notifications

        result = await get_signal_notifications(
            days=args.days,
            status=args.status,
            limit=args.limit,
        )
        print(json.dumps(result, indent=2, default=str))

    elif args.mode == "report":
        from tools.trading_tools import report_manual_trade_outcome

        if not args.signal_id:
            raise ValueError("--signal-id is required in report mode")
        if not args.result:
            raise ValueError("--result is required in report mode")

        result = await report_manual_trade_outcome(
            signal_id=args.signal_id,
            outcome=args.result,
            notes=args.notes,
            pnl_pct=args.pnl_pct,
            execution_price=args.execution_price,
            exit_price=args.exit_price,
            correlation_id=cid,
        )
        print(json.dumps(result, indent=2, default=str))

    elif args.mode == "kpis":
        from agents.strategy_supervisor import AdaptiveStrategySupervisor
        supervisor = AdaptiveStrategySupervisor()
        result = await supervisor.get_kpi_summary()
        print(json.dumps(result, indent=2, default=str))

    elif args.mode == "adapt":
        from agents.strategy_supervisor import AdaptiveStrategySupervisor
        supervisor = AdaptiveStrategySupervisor()
        result = await supervisor.run_adaptation_cycle(
            symbol=args.symbol,
            timeframe=args.timeframe,
            correlation_id=cid,
        )
        print(json.dumps(result, indent=2, default=str))

    elif args.mode == "predictions":
        from memory.store import get_memory_store
        store = get_memory_store()
        result = await store.get_predictions(
            limit=args.limit,
            status=args.status,
        )
        print(json.dumps(result, indent=2, default=str))


# =============================================================================
# SERVER MODE (FastAPI)
# =============================================================================

def create_app():
    """FastAPI application factory. Separated for testability."""
    from fastapi import FastAPI, Request, Response

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
        return {"status": "healthy", "timestamp": datetime.now(UTC).isoformat()}

    @app.get("/ready")
    async def ready():
        """Readiness probe — validates LLM config and storage reachability."""
        cfg = get_config()
        checks: dict = {}
        failing = False

        # Check 1: LLM API key configured
        if not cfg.llm.api_key:
            checks["llm"] = {"status": "fail", "reason": "API key not configured"}
            failing = True
        else:
            checks["llm"] = {"status": "ok"}

        # Check 2: Redis reachability (only if explicitly configured)
        default_redis = "redis://localhost:6379"
        if cfg.infra.redis_url and cfg.infra.redis_url != default_redis:
            try:
                import redis.asyncio as aioredis
                r = aioredis.from_url(cfg.infra.redis_url, socket_connect_timeout=2)
                await r.ping()
                await r.aclose()
                checks["redis"] = {"status": "ok"}
            except Exception as exc:
                checks["redis"] = {"status": "fail", "reason": str(exc)}
                failing = True

        # Check 3: Data directory writable
        data_dir = Path(os.getenv("TRADING_AGENT_DATA_DIR", "~/.trading-agent")).expanduser()
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
            probe = data_dir / ".ready_check"
            probe.touch()
            probe.unlink()
            checks["storage"] = {"status": "ok"}
        except Exception as exc:
            checks["storage"] = {"status": "fail", "reason": str(exc)}
            failing = True

        body = {
            "status": "not_ready" if failing else "ready",
            "agent_id": cfg.agent_id,
            "environment": cfg.environment,
            "checks": checks,
            "timestamp": datetime.now(UTC).isoformat(),
        }

        if failing:
            return Response(
                content=json.dumps(body),
                status_code=503,
                media_type="application/json",
            )
        return body

    @app.post("/analyze")
    async def analyze(request: Request):
        """Trigger a market analysis. Actionable BUY/SELL signals are saved to /signals."""
        body = await request.json()
        cid = request.headers.get("X-Correlation-ID", str(uuid.uuid4())[:8])

        from agents.signal_agent import SignalAgent
        from tools.trading_tools import save_signal_notification
        agent = SignalAgent()
        result = await agent.analyze(
            symbol=body.get("symbol", "BTC/USDT"),
            timeframe=body.get("timeframe", "4h"),
            correlation_id=cid,
        )

        signal = result.get("signal", "HOLD")
        confidence = float(result.get("confidence", 0) or 0)
        rr = float(result.get("risk_reward_tp2") or result.get("risk_reward_tp1") or 0)
        cfg = get_config()
        if (
            signal in ("BUY", "SELL")
            and confidence >= cfg.trading.min_confidence
            and rr >= cfg.trading.min_risk_reward
        ):
            await save_signal_notification(
                signal_json=result,
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

    @app.get("/signals")
    async def signals(days: int = 30, status: str = "all", limit: int = 20):
        """List suggested trades saved for manual operator review."""
        from tools.trading_tools import get_signal_notifications

        return await get_signal_notifications(days=days, status=status, limit=limit)

    @app.post("/report")
    async def report(request: Request):
        """Record operator feedback for a previously suggested trade."""
        body = await request.json()
        cid = request.headers.get("X-Correlation-ID", str(uuid.uuid4())[:8])

        from tools.trading_tools import report_manual_trade_outcome

        return await report_manual_trade_outcome(
            signal_id=body.get("signal_id", ""),
            outcome=body.get("outcome", ""),
            notes=body.get("notes", ""),
            pnl_pct=body.get("pnl_pct"),
            execution_price=body.get("execution_price"),
            exit_price=body.get("exit_price"),
            correlation_id=cid,
        )

    @app.get("/predictions")
    async def predictions(limit: int = 50, status: str = "all"):
        """List persisted predictions with optional lifecycle status filter."""
        from memory.store import get_memory_store
        store = get_memory_store()
        return await store.get_predictions(limit=limit, status=status)

    @app.get("/kpis")
    async def kpis():
        """Return rolling quality KPIs across short/medium/long windows."""
        from agents.strategy_supervisor import AdaptiveStrategySupervisor
        supervisor = AdaptiveStrategySupervisor()
        return await supervisor.get_kpi_summary()

    @app.post("/adapt")
    async def adapt(request: Request):
        """Trigger one adaptation cycle (bypasses the scheduled interval)."""
        body = await request.json()
        cid = request.headers.get("X-Correlation-ID", str(uuid.uuid4())[:8])

        from agents.strategy_supervisor import AdaptiveStrategySupervisor
        supervisor = AdaptiveStrategySupervisor()
        return await supervisor.run_adaptation_cycle(
            symbol=body.get("symbol", "BTC/USDT"),
            timeframe=body.get("timeframe", "4h"),
            correlation_id=cid,
        )

    @app.get("/strategy/versions")
    async def strategy_versions(limit: int = 20):
        """List strategy version history for audit and rollback inspection."""
        from memory.store import get_memory_store
        store = get_memory_store()
        versions = await store.get_strategy_versions(limit=limit)
        versions.reverse()
        return versions

    @app.get("/supervisor/events")
    async def supervisor_events(limit: int = 50):
        """List supervisor decision events."""
        from memory.store import get_memory_store
        store = get_memory_store()
        events = await store.get_supervisor_events(limit=limit)
        events.reverse()
        return events

    return app


def run_supervisor():
    """Run the autonomous supervisor loop (all four background loops)."""
    import asyncio as _asyncio

    config = get_config()
    try:
        config.validate()
    except ValueError as exc:
        log.error("supervisor.config_invalid", error=str(exc))
        sys.exit(1)

    async def _main():
        loop = _asyncio.get_running_loop()
        shutdown_event = _asyncio.Event()

        def _request_shutdown():
            if not shutdown_event.is_set():
                log.info("supervisor.shutdown_requested")
                shutdown_event.set()
                # Cancel all running tasks so blocking LLM calls are interrupted
                for task in _asyncio.all_tasks(loop):
                    if task is not _asyncio.current_task():
                        task.cancel()

        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, _request_shutdown)

        from workflows.scheduler import SupervisorLoop
        supervisor = SupervisorLoop(shutdown_event=shutdown_event)
        log.info(
            "supervisor.run",
            symbols=config.trading.symbols,
            timeframes=config.trading.timeframes,
        )
        try:
            await supervisor.run()
        except _asyncio.CancelledError:
            pass
        finally:
            log.info("supervisor.stopped")

    _asyncio.run(_main())


def run_server():
    """Run FastAPI server with proper graceful shutdown."""
    import uvicorn

    config = get_config()
    try:
        config.validate()
    except ValueError as exc:
        log.error("server.config_invalid", error=str(exc))
        sys.exit(1)

    app = create_app()
    shutdown_event = asyncio.Event()
    server_ref: list = []

    @asynccontextmanager
    async def lifespan(application):
        log.info("server.start", port=config.infra.port, environment=config.environment)
        # Start the autonomous scan/evaluate/adapt/publish loops alongside the HTTP server
        from workflows.scheduler import SupervisorLoop
        supervisor = SupervisorLoop(shutdown_event=shutdown_event)
        supervisor_task = asyncio.create_task(supervisor.run(), name="supervisor_loop")
        log.info(
            "supervisor.started",
            symbols=config.trading.symbols,
            timeframes=config.trading.timeframes,
        )
        yield
        log.info("server.shutdown_begin")
        shutdown_event.set()
        supervisor_task.cancel()
        try:
            await supervisor_task
        except asyncio.CancelledError:
            pass

    # Rebuild app with lifespan attached
    from fastapi import FastAPI
    app_with_lifespan = FastAPI(
        title="Trading Intelligence Agent",
        version="1.0.0",
        docs_url="/docs" if not config.is_production() else None,
        lifespan=lifespan,
    )
    # Mount all routes from the factory app
    app_with_lifespan.routes.extend(
        r for r in app.routes if r not in app_with_lifespan.routes
    )

    uv_config = uvicorn.Config(
        app_with_lifespan,
        host="0.0.0.0",
        port=config.infra.port,
        log_config=None,  # use structlog
    )
    server = uvicorn.Server(uv_config)
    server_ref.append(server)

    def handle_sigterm(*_):
        log.info("server.sigterm_received")
        server.should_exit = True
        shutdown_event.set()

    signal.signal(signal.SIGTERM, handle_sigterm)
    signal.signal(signal.SIGINT, handle_sigterm)

    asyncio.run(server.serve())


# =============================================================================
# ENTRY POINT
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="Trading Intelligence Agent")
    parser.add_argument(
        "--mode",
        choices=[
            "analysis", "backtest", "candles", "assess", "full",
            "signals", "report", "server", "supervisor",
            "kpis", "adapt", "predictions",
        ],
        default="analysis",
        help="Operation mode",
    )
    parser.add_argument("--symbol", default="BTC/USDT", help="Trading symbol")
    parser.add_argument("--timeframe", default="4h", help="Analysis timeframe")
    parser.add_argument("--days", type=int, default=30, help="Backtest lookback days")
    parser.add_argument("--limit", type=int, default=10, help="OHLCV candle count for candles mode")
    parser.add_argument(
        "--source",
        default="default",
        help="Market data source for candles mode: default, auto, ccxt, yfinance, synthetic",
    )
    parser.add_argument("--status", default="all", help="signals mode filter: all, pending, reported")
    parser.add_argument("--signal-id", default="", help="Signal identifier for report mode")
    parser.add_argument("--result", default="", help="Manual trade result: won, lost, breakeven, skipped, cancelled")
    parser.add_argument("--notes", default="", help="Free-form notes for report mode")
    parser.add_argument("--pnl-pct", type=float, default=None, help="Realized PnL percentage for report mode")
    parser.add_argument("--execution-price", type=float, default=None, help="Manual execution price for report mode")
    parser.add_argument("--exit-price", type=float, default=None, help="Manual exit price for report mode")

    args = parser.parse_args()

    if args.mode == "server":
        run_server()
    elif args.mode == "supervisor":
        run_supervisor()
    else:
        asyncio.run(run_cli(args))


if __name__ == "__main__":
    main()
