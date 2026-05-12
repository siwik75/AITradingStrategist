"""
Instrument Universe Scheduler — SupervisorLoop.

Runs four independent async loops inside a single process:

  1. Scan loop       — analyzes every symbol × timeframe on per-timeframe cadence
  2. Evaluation loop — scores matured predictions against realized candles
  3. Adaptation loop — aggregates KPIs and runs the strategy supervisor
  4. Publication loop — publishes queued Telegram messages (retry of failed sends)

All loops are cooperative (asyncio), share the same event loop, and respect
a global shutdown event so the process terminates cleanly on SIGTERM/SIGINT.

Configuration:
  TRADING_SYMBOLS       — list of instrument symbols (JSON array)
  TRADING_TIMEFRAMES    — list of timeframes (JSON array)
  SCAN_INTERVAL_*       — seconds between scans per timeframe
  ADAPTATION_INTERVAL_HOURS
  ENABLE_AUTONOMOUS_ADAPTATION
  MIN_CONFIDENCE        — minimum confidence to persist + publish a signal
  TELEGRAM_PUBLISH_SIGNALS

Usage (from main.py):
    from workflows.scheduler import SupervisorLoop
    loop = SupervisorLoop()
    await loop.run()
"""
import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import structlog

log = structlog.get_logger()


class _RateLimiter:
    """
    Sliding-window rate limiter backed by an asyncio.Semaphore.

    Each call to `acquire()` consumes one slot. The slot is automatically
    released after `period_seconds`, enforcing at most `max_calls` concurrent
    calls within any rolling window of that duration.

    Example: RateLimiter(max_calls=2, period_seconds=3600) → max 2 LLM calls/hour.
    """

    def __init__(self, max_calls: int, period_seconds: float):
        self._max_calls = max_calls
        self._period = period_seconds
        self._sem = asyncio.Semaphore(max_calls)

    async def acquire(self) -> None:
        await self._sem.acquire()
        # Schedule the slot release after the window expires
        loop = asyncio.get_running_loop()
        loop.call_later(self._period, self._sem.release)

    @property
    def available(self) -> int:
        return self._sem._value  # remaining slots in current window


class SupervisorLoop:
    """
    Orchestrates all four recurring service loops.

    Create one instance per process and call `await run()`.
    The loop respects the shutdown_event and exits all coroutines cleanly.
    """

    def __init__(self, shutdown_event: asyncio.Event | None = None):
        from config.settings import get_config
        import os
        self._config = get_config()
        self._shutdown = shutdown_event or asyncio.Event()
        self._scan_tasks: dict[str, asyncio.Task] = {}  # "symbol:tf" → task

        max_calls = int(os.getenv("SCAN_RATE_LIMIT_MAX_CALLS", "2"))
        period_s = float(os.getenv("SCAN_RATE_LIMIT_PERIOD_SECONDS", "3600"))
        self._rate_limiter = _RateLimiter(max_calls=max_calls, period_seconds=period_s)
        log.info(
            "supervisor_loop.rate_limiter",
            max_calls=max_calls,
            period_seconds=period_s,
        )

    # -------------------------------------------------------------------------
    # MAIN RUNNER
    # -------------------------------------------------------------------------

    async def run(self) -> None:
        """Start all loops and wait until shutdown."""
        log.info(
            "supervisor_loop.starting",
            symbols=self._config.trading.symbols,
            timeframes=self._config.trading.timeframes,
            adaptation_enabled=self._config.adaptation.enabled,
        )

        tasks = [
            asyncio.create_task(self._scan_loop(), name="scan_loop"),
            asyncio.create_task(self._evaluation_loop(), name="evaluation_loop"),
            asyncio.create_task(self._adaptation_loop(), name="adaptation_loop"),
            asyncio.create_task(self._publication_retry_loop(), name="publication_loop"),
        ]

        try:
            # Run until shutdown event is set or any task crashes
            await asyncio.gather(*tasks, return_exceptions=False)
        except (asyncio.CancelledError, Exception):
            pass
        finally:
            # Cancel any still-running tasks and wait up to 5s for them to finish
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.wait(tasks, timeout=5)
            log.info("supervisor_loop.stopped")

    # -------------------------------------------------------------------------
    # 1. SCAN LOOP
    # -------------------------------------------------------------------------

    async def _scan_loop(self) -> None:
        """
        For each symbol × timeframe, launch a dedicated per-pair scan task
        that fires on the appropriate cadence for that timeframe.
        """
        scan_cfg = self._config.scan
        symbols = self._config.trading.symbols
        timeframes = self._config.trading.timeframes

        # Launch one task per symbol/timeframe pair with its own sleep cadence
        pair_tasks = []
        for symbol in symbols:
            for tf in timeframes:
                interval = scan_cfg.for_timeframe(tf)
                task = asyncio.create_task(
                    self._scan_pair_loop(symbol, tf, interval),
                    name=f"scan_{symbol}_{tf}",
                )
                pair_tasks.append(task)

        try:
            await asyncio.gather(*pair_tasks)
        except asyncio.CancelledError:
            for t in pair_tasks:
                t.cancel()
            await asyncio.gather(*pair_tasks, return_exceptions=True)

    async def _scan_pair_loop(self, symbol: str, tf: str, interval: int) -> None:
        """Continuously analyze one symbol/timeframe on a fixed cadence."""
        log.info("scan_pair.started", symbol=symbol, timeframe=tf, interval_s=interval)
        while not self._shutdown.is_set():
            try:
                await self._run_single_scan(symbol, tf)
            except Exception as exc:
                log.error("scan_pair.error", symbol=symbol, timeframe=tf, error=str(exc))
            # Interruptible sleep: wake early on shutdown
            try:
                await asyncio.wait_for(
                    self._shutdown.wait(), timeout=float(interval)
                )
            except asyncio.TimeoutError:
                pass

    async def _run_single_scan(self, symbol: str, tf: str) -> dict | None:
        """
        Run SignalAgent for one symbol/timeframe. Persist and publish if actionable.
        """
        from agents.signal_agent import SignalAgent
        from memory.store import get_memory_store
        from tools.notification_tools import TelegramPublisher

        cid = str(uuid.uuid4())[:8]
        config = self._config

        # Acquire a rate-limit slot before calling the LLM.
        # Blocks here if the configured call budget is exhausted for this window.
        log.debug(
            "scan.waiting_for_rate_limit",
            symbol=symbol,
            timeframe=tf,
            slots_available=self._rate_limiter.available,
        )
        await self._rate_limiter.acquire()

        agent = SignalAgent()
        try:
            result = await agent.analyze(symbol=symbol, timeframe=tf, correlation_id=cid)
        except Exception as exc:
            log.error("scan.analyze_failed", symbol=symbol, tf=tf, error=str(exc))
            return None

        signal = result.get("signal", "HOLD")
        confidence = float(result.get("confidence", 0) or 0)
        rr = _resolve_risk_reward_ratio(result)

        # Build prediction record
        prediction_id = str(uuid.uuid4())[:8]
        eval_due = _eval_due_at(tf, config)
        prediction = {
            "prediction_id": prediction_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "symbol": symbol,
            "timeframe": tf,
            "source": config.agent_id,
            "signal": signal,
            "confidence": confidence,
            "entry_price": result.get("entry_price"),
            "stop_loss": result.get("stop_loss"),
            "take_profit_1": result.get("take_profit_1"),
            "take_profit_2": result.get("take_profit_2"),
            "risk_reward_ratio": rr,
            "regime": result.get("regime"),
            "confluence_analysis": result.get("confluence_analysis"),
            "model": config.llm.model,
            "correlation_id": cid,
            "evaluation_due_at": eval_due,
            "telegram_message_id": None,
            "status": "active",
        }

        store = get_memory_store()

        # Persist BEFORE any external call (Telegram is optional, persistence is not)
        await store.save_prediction(prediction)

        # Save actionable signals to the notifications store (visible on GET /signals)
        if (
            signal in ("BUY", "SELL")
            and confidence >= config.trading.min_confidence
            and rr >= config.trading.min_risk_reward
        ):
            try:
                from tools.trading_tools import save_signal_notification
                await save_signal_notification(
                    signal_json=result,
                    symbol=symbol,
                    timeframe=tf,
                    correlation_id=cid,
                )
            except Exception as exc:
                log.warning("scan.save_notification_failed", prediction_id=prediction_id, error=str(exc))

        # Publish actionable signals to Telegram
        if (
            signal in ("BUY", "SELL")
            and confidence >= config.trading.min_confidence
            and rr >= config.trading.min_risk_reward
            and config.telegram.publish_signals
        ):
            try:
                publisher = TelegramPublisher()
                delivery = await publisher.publish_signal(prediction)
                if delivery.get("success"):
                    await store.update_prediction(
                        prediction_id,
                        {"telegram_message_id": delivery.get("telegram_message_id")},
                    )
            except Exception as exc:
                log.warning("scan.publish_failed", prediction_id=prediction_id, error=str(exc))

        log.info(
            "scan.completed",
            symbol=symbol,
            timeframe=tf,
            signal=signal,
            confidence=confidence,
            risk_reward_ratio=rr,
            prediction_id=prediction_id,
        )
        return prediction

    # -------------------------------------------------------------------------
    # 2. EVALUATION LOOP
    # -------------------------------------------------------------------------

    async def _evaluation_loop(self) -> None:
        """
        Every 5 minutes: find predictions whose evaluation horizon has elapsed
        and score them against realized candles.
        """
        check_interval = 300  # 5 minutes
        while not self._shutdown.is_set():
            try:
                await self._run_evaluations()
            except Exception as exc:
                log.error("evaluation_loop.error", error=str(exc))
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=float(check_interval))
            except asyncio.TimeoutError:
                pass

    async def _run_evaluations(self) -> None:
        """Score all matured predictions that have not yet been evaluated."""
        from memory.store import get_memory_store
        from tools.evaluation_tools import evaluate_prediction
        from tools.trading_tools import get_ohlcv

        store = get_memory_store()
        predictions = await store.get_predictions(status="active")
        now = datetime.now(UTC)

        matured = [
            p for p in predictions
            if p.get("evaluation_due_at") and _is_due(p["evaluation_due_at"], now)
        ]

        if not matured:
            return

        log.info("evaluation_loop.evaluating", count=len(matured))

        for prediction in matured:
            try:
                pid = prediction["prediction_id"]
                symbol = prediction["symbol"]
                tf = prediction["timeframe"]
                ts = prediction["timestamp"]

                # Fetch future candles after the prediction timestamp
                ohlcv_result = await get_ohlcv(
                    symbol=symbol,
                    timeframe=tf,
                    limit=self._config.prediction.horizon_candles(tf) + 10,
                )
                candles = ohlcv_result.get("candles", [])
                future_candles = _filter_future_candles(candles, ts)

                evaluation = await evaluate_prediction(prediction, future_candles)
                await store.save_prediction_evaluation(pid, evaluation)
                await store.update_prediction(pid, {"status": "evaluated"})

                # Index the outcome into the RAG knowledge store (best-effort)
                try:
                    from workflows.knowledge_indexer import index_evaluation
                    index_evaluation(prediction, evaluation)
                except Exception as exc:
                    log.warning("evaluation_loop.knowledge_index_failed", error=str(exc))

                # Optionally publish evaluation to Telegram
                if self._config.telegram.publish_evaluations:
                    try:
                        from tools.notification_tools import TelegramPublisher
                        publisher = TelegramPublisher()
                        await publisher.publish_evaluation_summary(evaluation)
                    except Exception as exc:
                        log.warning("evaluation_loop.publish_failed", error=str(exc))

            except Exception as exc:
                log.error(
                    "evaluation_loop.prediction_failed",
                    prediction_id=prediction.get("prediction_id"),
                    error=str(exc),
                )

    # -------------------------------------------------------------------------
    # 3. ADAPTATION LOOP
    # -------------------------------------------------------------------------

    async def _adaptation_loop(self) -> None:
        """
        Every ADAPTATION_INTERVAL_HOURS: check KPIs and run the strategy
        supervisor if autonomous adaptation is enabled.
        """
        adapt_cfg = self._config.adaptation
        interval_s = adapt_cfg.interval_hours * 3600

        while not self._shutdown.is_set():
            if adapt_cfg.enabled:
                try:
                    await self._run_adaptation()
                except Exception as exc:
                    log.error("adaptation_loop.error", error=str(exc))
            else:
                log.debug("adaptation_loop.disabled")
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=float(interval_s))
            except asyncio.TimeoutError:
                pass

    async def _run_adaptation(self) -> None:
        from agents.strategy_supervisor import AdaptiveStrategySupervisor

        supervisor = AdaptiveStrategySupervisor()
        # Run one cycle per representative timeframe (use first configured symbol + 4h)
        symbol = self._config.trading.symbols[0] if self._config.trading.symbols else "BTC/USDT"
        timeframe = "4h" if "4h" in self._config.trading.timeframes else self._config.trading.timeframes[0]

        cid = str(uuid.uuid4())[:8]
        result = await supervisor.run_adaptation_cycle(
            symbol=symbol, timeframe=timeframe, correlation_id=cid
        )
        log.info("adaptation_loop.cycle_result", status=result.get("status"), cid=cid)

        # Post-promotion rollback check (run immediately after any promotion)
        if result.get("status") == "promoted":
            rollback_result = await supervisor.rollback_if_degraded(
                symbol=symbol, timeframe=timeframe, correlation_id=cid
            )
            if rollback_result.get("status") == "rolled_back":
                log.warning("adaptation_loop.rolled_back", cid=cid)

    # -------------------------------------------------------------------------
    # 4. PUBLICATION RETRY LOOP
    # -------------------------------------------------------------------------

    async def _publication_retry_loop(self) -> None:
        """
        Every 10 minutes: retry failed Telegram deliveries.
        Keeps publication eventually consistent even if Telegram is temporarily down.
        """
        retry_interval = 600  # 10 minutes
        while not self._shutdown.is_set():
            try:
                await self._retry_failed_deliveries()
            except Exception as exc:
                log.error("publication_retry_loop.error", error=str(exc))
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=float(retry_interval))
            except asyncio.TimeoutError:
                pass

    async def _retry_failed_deliveries(self) -> None:
        from tools.notification_tools import get_failed_deliveries, TelegramPublisher
        from memory.store import get_memory_store

        failed = await get_failed_deliveries(limit=10)
        if not failed:
            return

        store = get_memory_store()
        publisher = TelegramPublisher()

        for delivery in failed:
            msg_type = delivery.get("message_type")
            ref_id = delivery.get("reference_id")
            text = delivery.get("text_preview", "")  # only preview stored; skip if truncated

            if not text or len(text) >= 200:
                # We don't store full text; skip retry — operator must replay manually
                continue

            try:
                # Re-deliver via raw send, log new attempt
                telegram_msg_id = await publisher._send_message(text)
                retry_record = {
                    **delivery,
                    "delivery_id": str(uuid.uuid4())[:8],
                    "is_retry": True,
                    "original_delivery_id": delivery.get("delivery_id"),
                    "success": True,
                    "telegram_message_id": telegram_msg_id,
                    "error": None,
                }
                await store.save_telegram_delivery(retry_record)
                log.info(
                    "publication_retry.success",
                    message_type=msg_type,
                    reference_id=ref_id,
                )
            except Exception as exc:
                log.warning(
                    "publication_retry.failed_again",
                    message_type=msg_type,
                    reference_id=ref_id,
                    error=str(exc),
                )


# =============================================================================
# HELPERS
# =============================================================================

def _resolve_risk_reward_ratio(signal_result: dict) -> float:
    """
    Resolve a normalized risk/reward ratio for gating + persistence.

    Canonical meaning:
      risk_reward_ratio = reward-to-TP2 / risk-to-stop-loss

    Resolution order:
      1. explicit risk_reward_ratio from the model
      2. risk_reward_tp2 from the model
      3. computed from entry/stop_loss/take_profit_2 price levels
    """
    explicit_rr = _safe_float(signal_result.get("risk_reward_ratio"))
    if explicit_rr is not None and explicit_rr > 0:
        return round(explicit_rr, 4)

    tp2_rr = _safe_float(signal_result.get("risk_reward_tp2"))
    if tp2_rr is not None and tp2_rr > 0:
        return round(tp2_rr, 4)

    entry = _safe_float(signal_result.get("entry_price"))
    stop_loss = _safe_float(signal_result.get("stop_loss"))
    take_profit_2 = _safe_float(signal_result.get("take_profit_2"))
    signal = str(signal_result.get("signal", "")).upper()

    if entry is None or stop_loss is None or take_profit_2 is None:
        return 0.0

    risk = abs(stop_loss - entry)
    reward = abs(entry - take_profit_2)
    if signal not in {"BUY", "SELL"} or risk <= 0 or reward <= 0:
        return 0.0

    return round(reward / risk, 4)

def _eval_due_at(tf: str, config) -> str:
    """Compute the evaluation due timestamp based on timeframe horizon config."""
    horizon_candles = config.prediction.horizon_candles(tf)
    tf_seconds = {
        "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400
    }
    seconds = tf_seconds.get(tf, 3600) * horizon_candles
    due = datetime.now(UTC) + timedelta(seconds=seconds)
    return due.isoformat()


def _is_due(due_at_str: str, now: datetime) -> bool:
    try:
        due = datetime.fromisoformat(due_at_str)
        if due.tzinfo is None:
            due = due.replace(tzinfo=UTC)
        return now >= due
    except (ValueError, TypeError):
        return False


def _safe_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _filter_future_candles(candles: list[dict], prediction_ts: str) -> list[dict]:
    """Return only candles whose timestamp is strictly after the prediction."""
    try:
        pred_dt = datetime.fromisoformat(prediction_ts)
        if pred_dt.tzinfo is None:
            pred_dt = pred_dt.replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return candles

    result = []
    for c in candles:
        ts = c.get("timestamp") or c.get("time") or c.get("date")
        if ts is None:
            continue
        try:
            c_dt = datetime.fromisoformat(str(ts))
            if c_dt.tzinfo is None:
                c_dt = c_dt.replace(tzinfo=UTC)
            if c_dt > pred_dt:
                result.append(c)
        except (ValueError, TypeError):
            pass
    return result
