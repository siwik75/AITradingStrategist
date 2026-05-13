"""
Memory Store — Conversation management and trade history.

16-Factor App: Backing store externalised via TRADING_AGENT_DATA_DIR env var.
Supports multiple backends:
- local (file): JSON/JSONL files under TRADING_AGENT_DATA_DIR (default ~/.trading-agent)
- Redis: short-term cache, session state
- S3: conversation history, long-term storage
- Aurora PostgreSQL: structured trade data, analytics
- DynamoDB: fast key-value access, volatile state

The local backend is fully implemented and survives process restarts.
Production backends (redis, dynamodb, postgres, s3) retain their stub structure
for future implementation.
"""

import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import structlog

log = structlog.get_logger()


# =============================================================================
# LOCAL FILE BACKEND
# =============================================================================


class _LocalFileBackend:
    """
    File-backed persistence for the local backend.

    File layout under data_dir:
      strategy_params.json    — single dict, atomic write
      trade_signals.jsonl     — newline-delimited JSON records, append-only
      signal_notifications.jsonl — suggested manual trades, append-only
      manual_trade_reviews.jsonl — operator feedback on suggested trades
      assessments.jsonl       — newline-delimited JSON records, append-only
    """

    def __init__(self, data_dir: Path):
        self._data_dir = data_dir
        self._data_dir.mkdir(parents=True, exist_ok=True)

    def _strategy_params_path(self) -> Path:
        return self._data_dir / "strategy_params.json"

    def _trade_signals_path(self) -> Path:
        return self._data_dir / "trade_signals.jsonl"

    def _assessments_path(self) -> Path:
        return self._data_dir / "assessments.jsonl"

    def _signal_notifications_path(self) -> Path:
        return self._data_dir / "signal_notifications.jsonl"

    def _manual_trade_reviews_path(self) -> Path:
        return self._data_dir / "manual_trade_reviews.jsonl"

    def _predictions_path(self) -> Path:
        return self._data_dir / "predictions.jsonl"

    def _prediction_evaluations_path(self) -> Path:
        return self._data_dir / "prediction_evaluations.jsonl"

    def _strategy_versions_path(self) -> Path:
        return self._data_dir / "strategy_versions.jsonl"

    def _telegram_deliveries_path(self) -> Path:
        return self._data_dir / "telegram_deliveries.jsonl"

    def _supervisor_events_path(self) -> Path:
        return self._data_dir / "supervisor_events.jsonl"

    def _atomic_write(self, path: Path, data: dict) -> None:
        """Write JSON atomically using a temp file + rename."""
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, default=str), encoding="utf-8")
        os.replace(tmp, path)

    def _append_jsonl(self, path: Path, record: dict) -> None:
        """Append one JSON record to a JSONL file."""
        line = json.dumps(record, default=str) + "\n"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)

    def _read_jsonl(self, path: Path) -> list[dict]:
        """Read all records from a JSONL file, silently skip corrupt lines."""
        if not path.exists():
            return []
        records = []
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return records

    def _filter_recent_records(self, records: list[dict], days: int = 30) -> list[dict]:
        """Filter JSON records to those newer than the given window."""
        if days <= 0:
            return records
        cutoff = datetime.now(UTC) - timedelta(days=days)
        result = []
        for record in records:
            ts_str = record.get("timestamp", "")
            try:
                ts = datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
                if ts >= cutoff:
                    result.append(record)
            except (ValueError, TypeError):
                result.append(record)
        return result

    # Strategy params

    def read_strategy_params(self, timeframe: str | None = None) -> dict | None:
        path = self._strategy_params_path()
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

        # Backward-compatible legacy format: plain params dict.
        if not isinstance(payload, dict):
            return None
        if "default" not in payload and "timeframes" not in payload:
            return payload

        default_params = payload.get("default")
        timeframe_params = payload.get("timeframes", {})

        if timeframe and isinstance(timeframe_params, dict):
            scoped = timeframe_params.get(timeframe)
            if isinstance(scoped, dict):
                return scoped

        return default_params if isinstance(default_params, dict) else None

    def write_strategy_params(self, params: dict, timeframe: str | None = None) -> None:
        current = self.read_strategy_params_payload()
        if timeframe:
            current.setdefault("timeframes", {})[timeframe] = params
        else:
            current["default"] = params
        self._atomic_write(self._strategy_params_path(), current)

    def read_strategy_params_payload(self) -> dict:
        """Return the normalized strategy params document for mixed legacy/new formats."""
        path = self._strategy_params_path()
        if not path.exists():
            return {"default": None, "timeframes": {}}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"default": None, "timeframes": {}}

        if not isinstance(payload, dict):
            return {"default": None, "timeframes": {}}
        if "default" not in payload and "timeframes" not in payload:
            return {"default": payload, "timeframes": {}}

        default_params = payload.get("default")
        timeframe_params = payload.get("timeframes", {})
        return {
            "default": default_params if isinstance(default_params, dict) else None,
            "timeframes": timeframe_params if isinstance(timeframe_params, dict) else {},
        }

    # Trade signals

    def append_trade_signal(self, record: dict) -> None:
        self._append_jsonl(self._trade_signals_path(), record)

    def read_trade_signals(self, days: int = 30) -> list[dict]:
        return self._filter_recent_records(self._read_jsonl(self._trade_signals_path()), days=days)

    # Signal notifications

    def append_signal_notification(self, record: dict) -> None:
        self._append_jsonl(self._signal_notifications_path(), record)

    def read_signal_notifications(self, days: int = 30) -> list[dict]:
        return self._filter_recent_records(
            self._read_jsonl(self._signal_notifications_path()), days=days
        )

    # Manual trade reviews

    def append_manual_trade_review(self, record: dict) -> None:
        self._append_jsonl(self._manual_trade_reviews_path(), record)

    def read_manual_trade_reviews(self, days: int = 30) -> list[dict]:
        return self._filter_recent_records(
            self._read_jsonl(self._manual_trade_reviews_path()), days=days
        )

    # Assessments

    def append_assessment(self, record: dict) -> None:
        self._append_jsonl(self._assessments_path(), record)

    def read_assessments(self, limit: int = 10) -> list[dict]:
        records = self._read_jsonl(self._assessments_path())
        return records[-limit:] if limit > 0 else records

    # Predictions

    def append_prediction(self, record: dict) -> None:
        self._append_jsonl(self._predictions_path(), record)

    def read_predictions(self, limit: int = 0, status: str = "all") -> list[dict]:
        records = self._read_jsonl(self._predictions_path())
        if status != "all":
            records = [r for r in records if r.get("status") == status]
        return records[-limit:] if limit > 0 else records

    def update_prediction(self, prediction_id: str, updates: dict) -> bool:
        """Rewrite predictions file with one record updated. Returns True if found."""
        path = self._predictions_path()
        records = self._read_jsonl(path)
        found = False
        for rec in records:
            if rec.get("prediction_id") == prediction_id:
                rec.update(updates)
                found = True
        if found:
            tmp = path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                for rec in records:
                    fh.write(json.dumps(rec, default=str) + "\n")
            os.replace(tmp, path)
        return found

    # Prediction evaluations

    def append_prediction_evaluation(self, record: dict) -> None:
        self._append_jsonl(self._prediction_evaluations_path(), record)

    def read_prediction_evaluations(
        self,
        limit: int = 0,
        timeframe: str | None = None,
    ) -> list[dict]:
        records = self._read_jsonl(self._prediction_evaluations_path())
        if timeframe:
            records = [r for r in records if r.get("timeframe") == timeframe]
        return records[-limit:] if limit > 0 else records

    # Strategy versions

    def append_strategy_version(self, record: dict) -> None:
        self._append_jsonl(self._strategy_versions_path(), record)

    def read_strategy_versions(self, limit: int = 0) -> list[dict]:
        records = self._read_jsonl(self._strategy_versions_path())
        return records[-limit:] if limit > 0 else records

    # Telegram deliveries

    def append_telegram_delivery(self, record: dict) -> None:
        self._append_jsonl(self._telegram_deliveries_path(), record)

    def read_telegram_deliveries(self, limit: int = 0) -> list[dict]:
        records = self._read_jsonl(self._telegram_deliveries_path())
        return records[-limit:] if limit > 0 else records

    # Supervisor events

    def append_supervisor_event(self, record: dict) -> None:
        self._append_jsonl(self._supervisor_events_path(), record)

    def read_supervisor_events(self, limit: int = 0) -> list[dict]:
        records = self._read_jsonl(self._supervisor_events_path())
        return records[-limit:] if limit > 0 else records


# =============================================================================
# MEMORY STORE
# =============================================================================


class MemoryStore:
    """
    Abstract memory interface.
    In production, swap backend via config (MEMORY_BACKEND env var).

    The 'local' backend is file-based and persists across process restarts.
    """

    def __init__(self, agent_id: str, backend: str = "local"):
        self.agent_id = agent_id
        self.backend = backend
        self._file = _LocalFileBackend(
            Path(os.getenv("TRADING_AGENT_DATA_DIR", "~/.trading-agent")).expanduser()
        )
        # In-memory fallback structures (used only when local file backend unavailable)
        self._conversation_history: list[dict] = []

    # =========================================================================
    # TRADE HISTORY (Aurora PostgreSQL in production)
    # =========================================================================

    async def save_trade_signal(self, signal: dict, correlation_id: str = ""):
        """Persist a trading signal with its metadata."""
        record = {
            "agent_id": self.agent_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "correlation_id": correlation_id,
            "signal": signal,
        }

        if self.backend == "local":
            self._file.append_trade_signal(record)
        elif self.backend == "dynamodb":
            await self._save_dynamodb("trade_signals", record)
        elif self.backend == "postgres":
            await self._save_postgres("trade_signals", record)

        log.info(
            "memory.trade_signal_saved",
            agent=self.agent_id,
            signal=signal.get("signal", "N/A"),
            correlation_id=correlation_id,
        )

    async def get_trade_history(self, days: int = 30) -> list[dict]:
        """Retrieve recent trade signals for self-assessment."""
        if self.backend == "local":
            return self._file.read_trade_signals(days=days)
        # Production: query from Aurora/DynamoDB with time filter
        return []

    async def save_signal_notification(
        self,
        signal: dict,
        symbol: str,
        timeframe: str,
        correlation_id: str = "",
    ) -> dict:
        """Persist an actionable signal for manual operator review."""
        record = {
            "agent_id": self.agent_id,
            "signal_id": str(uuid.uuid4())[:8],
            "timestamp": datetime.now(UTC).isoformat(),
            "correlation_id": correlation_id,
            "symbol": symbol,
            "timeframe": timeframe,
            "signal": signal,
        }

        if self.backend == "local":
            self._file.append_signal_notification(record)

        log.info(
            "memory.signal_notification_saved",
            agent=self.agent_id,
            signal_id=record["signal_id"],
            signal=signal.get("signal", "N/A"),
            symbol=symbol,
            timeframe=timeframe,
            correlation_id=correlation_id,
        )
        return record

    async def get_signal_notifications(
        self,
        days: int = 30,
        status: str = "all",
        limit: int = 20,
    ) -> list[dict]:
        """Retrieve suggested trades with derived pending/reported status."""
        if self.backend != "local":
            return []

        notifications = self._file.read_signal_notifications(days=days)
        reviews = self._file.read_manual_trade_reviews(days=days)
        latest_review_by_signal = {
            review.get("signal_id"): review for review in reviews if review.get("signal_id")
        }

        merged = []
        for notification in notifications:
            review = latest_review_by_signal.get(notification.get("signal_id"))
            record = {
                **notification,
                "review_status": "reported" if review else "pending",
            }
            if review:
                record["manual_review"] = review
            merged.append(record)

        if status in {"pending", "reported"}:
            merged = [item for item in merged if item["review_status"] == status]

        if limit > 0:
            merged = merged[-limit:]
        merged.reverse()
        return merged

    async def save_manual_trade_review(
        self,
        signal_id: str,
        outcome: str,
        notes: str = "",
        pnl_pct: float | None = None,
        execution_price: float | None = None,
        exit_price: float | None = None,
        correlation_id: str = "",
    ) -> dict:
        """Persist operator feedback for a previously suggested signal."""
        record = {
            "agent_id": self.agent_id,
            "signal_id": signal_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "correlation_id": correlation_id,
            "outcome": outcome,
            "notes": notes,
            "pnl_pct": pnl_pct,
            "execution_price": execution_price,
            "exit_price": exit_price,
        }

        if self.backend == "local":
            self._file.append_manual_trade_review(record)

        log.info(
            "memory.manual_trade_review_saved",
            agent=self.agent_id,
            signal_id=signal_id,
            outcome=outcome,
            correlation_id=correlation_id,
        )
        return record

    async def get_manual_trade_reviews(self, days: int = 30, limit: int = 50) -> list[dict]:
        """Retrieve operator feedback records."""
        if self.backend != "local":
            return []

        reviews = self._file.read_manual_trade_reviews(days=days)
        if limit > 0:
            reviews = reviews[-limit:]
        reviews.reverse()
        return reviews

    # =========================================================================
    # STRATEGY PARAMS (Redis / DynamoDB in production)
    # =========================================================================

    async def save_strategy_params(self, params: dict, timeframe: str | None = None):
        """Persist strategy parameters after self-assessment."""
        if self.backend == "local":
            self._file.write_strategy_params(params, timeframe=timeframe)
        elif self.backend == "redis":
            key = f"strategy_params:{self.agent_id}:{timeframe or 'default'}"
            await self._save_redis(key, params)

        log.info("memory.strategy_params_saved", agent=self.agent_id)

    async def get_strategy_params(self, timeframe: str | None = None) -> dict | None:
        """Retrieve current strategy parameters. Returns None if none saved yet."""
        if self.backend == "local":
            return self._file.read_strategy_params(timeframe=timeframe)
        return None

    # =========================================================================
    # CONVERSATION HISTORY (S3 in production)
    # =========================================================================

    async def save_conversation(
        self,
        session_id: str,
        messages: list[dict],
        metadata: dict = None,
    ):
        """Save conversation to S3 (Generali pattern)."""
        record = {
            "agent_id": self.agent_id,
            "session_id": session_id,
            "messages": messages,
            "metadata": metadata or {},
            "timestamp": datetime.now(UTC).isoformat(),
        }

        if self.backend == "local":
            self._conversation_history.append(record)
        elif self.backend == "s3":
            await self._save_s3(
                key=f"{self.agent_id}/{session_id}.json",
                data=record,
            )

        log.info(
            "memory.conversation_saved",
            agent=self.agent_id,
            session_id=session_id,
        )

    # =========================================================================
    # ASSESSMENT HISTORY (for tracking strategy evolution)
    # =========================================================================

    async def save_assessment(self, assessment: dict, correlation_id: str = ""):
        """Persist self-assessment results for audit trail."""
        record = {
            "agent_id": self.agent_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "correlation_id": correlation_id,
            "assessment": assessment,
        }

        if self.backend == "local":
            self._file.append_assessment(record)

        log.info(
            "memory.assessment_saved",
            agent=self.agent_id,
            decision=assessment.get("decision", "N/A"),
            correlation_id=correlation_id,
        )

    async def get_assessment_history(self, limit: int = 10) -> list[dict]:
        """Retrieve assessment history for trend analysis."""
        if self.backend == "local":
            return self._file.read_assessments(limit=limit)
        return []

    # =========================================================================
    # PREDICTION REGISTRY
    # =========================================================================

    async def save_prediction(self, prediction: dict) -> dict:
        """Persist a full prediction record before publication."""
        record = {
            "agent_id": self.agent_id,
            "persisted_at": datetime.now(UTC).isoformat(),
            "status": "active",
            **prediction,
        }
        if self.backend == "local":
            self._file.append_prediction(record)
        log.info(
            "memory.prediction_saved",
            prediction_id=prediction.get("prediction_id"),
            symbol=prediction.get("symbol"),
            signal=prediction.get("signal"),
        )
        return record

    async def get_predictions(self, limit: int = 0, status: str = "all") -> list[dict]:
        """Retrieve prediction records, optionally filtered by lifecycle status."""
        if self.backend == "local":
            return self._file.read_predictions(limit=limit, status=status)
        return []

    async def update_prediction(self, prediction_id: str, updates: dict) -> bool:
        """Patch a prediction record in-place (e.g. after evaluation)."""
        if self.backend == "local":
            return self._file.update_prediction(prediction_id, updates)
        return False

    async def save_prediction_evaluation(self, prediction_id: str, evaluation: dict) -> dict:
        """Persist a scored evaluation result for a matured prediction."""
        record = {
            "agent_id": self.agent_id,
            "prediction_id": prediction_id,
            "evaluated_at": datetime.now(UTC).isoformat(),
            **evaluation,
        }
        if self.backend == "local":
            self._file.append_prediction_evaluation(record)
        log.info(
            "memory.prediction_evaluation_saved",
            prediction_id=prediction_id,
            outcome_score=evaluation.get("outcome_score"),
        )
        return record

    async def get_prediction_evaluations(
        self,
        limit: int = 0,
        timeframe: str | None = None,
    ) -> list[dict]:
        """Retrieve evaluation records for KPI computation."""
        if self.backend == "local":
            return self._file.read_prediction_evaluations(limit=limit, timeframe=timeframe)
        return []

    # =========================================================================
    # STRATEGY VERSIONING
    # =========================================================================

    async def save_strategy_version(self, version: dict) -> dict:
        """Persist a strategy version record (candidate / shadow / active)."""
        record = {
            "agent_id": self.agent_id,
            "recorded_at": datetime.now(UTC).isoformat(),
            **version,
        }
        if self.backend == "local":
            self._file.append_strategy_version(record)
        log.info(
            "memory.strategy_version_saved",
            version_id=version.get("version_id"),
            lifecycle=version.get("lifecycle"),
        )
        return record

    async def get_strategy_versions(self, limit: int = 0) -> list[dict]:
        """Retrieve all strategy version records for audit / rollback."""
        if self.backend == "local":
            return self._file.read_strategy_versions(limit=limit)
        return []

    # =========================================================================
    # TELEGRAM DELIVERY LOG
    # =========================================================================

    async def save_telegram_delivery(self, delivery: dict) -> dict:
        """Persist a Telegram delivery attempt (success or failure)."""
        record = {
            "agent_id": self.agent_id,
            "delivered_at": datetime.now(UTC).isoformat(),
            **delivery,
        }
        if self.backend == "local":
            self._file.append_telegram_delivery(record)
        return record

    async def get_telegram_deliveries(self, limit: int = 0) -> list[dict]:
        if self.backend == "local":
            return self._file.read_telegram_deliveries(limit=limit)
        return []

    # =========================================================================
    # SUPERVISOR EVENTS
    # =========================================================================

    async def save_supervisor_event(self, event: dict) -> dict:
        """Persist a supervisor decision event for inspection during failures."""
        record = {
            "agent_id": self.agent_id,
            "occurred_at": datetime.now(UTC).isoformat(),
            **event,
        }
        if self.backend == "local":
            self._file.append_supervisor_event(record)
        log.info(
            "memory.supervisor_event_saved",
            event_type=event.get("event_type"),
        )
        return record

    async def get_supervisor_events(self, limit: int = 0) -> list[dict]:
        if self.backend == "local":
            return self._file.read_supervisor_events(limit=limit)
        return []

    # =========================================================================
    # BACKEND STUBS (implement per backing service)
    # =========================================================================

    async def _save_redis(self, key: str, data: dict):
        """Redis backend — fast key-value."""
        # import redis.asyncio as redis
        # r = redis.from_url(os.getenv("REDIS_URL"))
        # await r.set(key, json.dumps(data))
        pass

    async def _save_dynamodb(self, table: str, item: dict):
        """DynamoDB backend — scalable NoSQL."""
        # import boto3
        # dynamodb = boto3.resource("dynamodb")
        # table = dynamodb.Table(table)
        # table.put_item(Item=item)
        pass

    async def _save_postgres(self, table: str, record: dict):
        """Aurora PostgreSQL backend — relational + pgvector."""
        # import asyncpg
        # conn = await asyncpg.connect(os.getenv("DATABASE_URL"))
        # await conn.execute(...)
        pass

    async def _save_s3(self, key: str, data: dict):
        """S3 backend — conversation management (Generali pattern)."""
        # import boto3
        # s3 = boto3.client("s3")
        # s3.put_object(
        #     Bucket=os.getenv("S3_CONVERSATION_BUCKET"),
        #     Key=key,
        #     Body=json.dumps(data),
        #     ContentType="application/json",
        # )
        pass


# =============================================================================
# PROCESS-LEVEL SINGLETON
# =============================================================================

_store: MemoryStore | None = None


def get_memory_store() -> MemoryStore:
    """Return the shared MemoryStore instance for this process."""
    global _store
    if _store is None:
        from config.settings import get_config

        config = get_config()
        backend = os.getenv("MEMORY_BACKEND", "local")
        _store = MemoryStore(agent_id=config.agent_id, backend=backend)
    return _store


def reset_memory_store() -> None:
    """Reset the singleton. For testing only."""
    global _store
    _store = None
