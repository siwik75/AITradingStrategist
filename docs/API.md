# API Reference

The FastAPI server exposes four HTTP endpoints. Start the server with:

```bash
python main.py --mode server
# or
make dev
```

Interactive docs available at `http://localhost:8080/docs` in non-production mode.

---

## GET /health

**Purpose:** Kubernetes liveness probe. Confirms the process is alive.

**Request:** No body, no parameters.

**Response 200:**
```json
{
  "status": "healthy",
  "timestamp": "2025-01-15T10:30:00.123456"
}
```

**curl:**
```bash
curl http://localhost:8080/health
```

---

## GET /ready

**Purpose:** Kubernetes readiness probe. Validates the agent can actually serve requests before marking the pod ready.

**Request:** No body, no parameters.

**Checks performed:**
1. `LLM_API_KEY` (or `ANTHROPIC_API_KEY`) is non-empty
2. Redis ping if `REDIS_URL` is configured to a non-default value
3. `TRADING_AGENT_DATA_DIR` is writable

**Response 200 — Ready:**
```json
{
  "status": "ready",
  "agent_id": "trading-intelligence-agent",
  "environment": "development",
  "checks": {
    "llm": {"status": "ok"},
    "storage": {"status": "ok"}
  },
  "timestamp": "2025-01-15T10:30:00.123456"
}
```

**Response 503 — Not Ready:**
```json
{
  "status": "not_ready",
  "agent_id": "trading-intelligence-agent",
  "environment": "development",
  "checks": {
    "llm": {"status": "fail", "reason": "API key not configured"},
    "storage": {"status": "ok"}
  },
  "timestamp": "2025-01-15T10:30:00.123456"
}
```

**curl:**
```bash
curl -i http://localhost:8080/ready
```

---

## POST /analyze

**Purpose:** Run a market analysis for a given symbol and timeframe. Calls `SignalAgent.analyze()`.

**Request body:**
```json
{
  "symbol": "BTC/USDT",
  "timeframe": "4h"
}
```

| Field | Type | Default | Description |
|---|---|---|---|
| `symbol` | string | `"BTC/USDT"` | Trading pair or ticker |
| `timeframe` | string | `"4h"` | Candle timeframe: `1m`, `5m`, `15m`, `1h`, `4h`, `1d` |

**Headers:**
- `X-Correlation-ID` (optional): Passed through to logs. Generated automatically if omitted.

**Response 200:**
```json
{
  "signal": "BUY",
  "confidence": 78,
  "entry_price": 67500.0,
  "stop_loss": 66200.0,
  "take_profit_1": 69500.0,
  "take_profit_2": 71800.0,
  "confluence_factors": [
    "EMA 9 above EMA 21 (bullish crossover)",
    "RSI 52 in neutral zone",
    "ADX 28 — strong trend",
    "MACD bullish cross",
    "Volume 1.4x 20-period SMA"
  ],
  "divergences": [],
  "market_regime": "trending",
  "reasoning": "..."
}
```

**curl:**
```bash
curl -X POST http://localhost:8080/analyze \
  -H "Content-Type: application/json" \
  -H "X-Correlation-ID: my-trace-123" \
  -d '{"symbol": "BTC/USDT", "timeframe": "4h"}'
```

**Notes:**
- Requires a valid `LLM_API_KEY` (agent makes live LLM calls)
- Market data comes from the configured default source (`MARKET_DATA_SOURCE`), with optional synthetic fallback

---

## POST /assess

**Purpose:** Trigger the full self-assessment cycle. Loads current strategy params, runs two backtests, and persists improved params if found.

**Request body:**
```json
{
  "symbol": "BTC/USDT",
  "timeframe": "4h",
  "days": 30
}
```

| Field | Type | Default | Description |
|---|---|---|---|
| `symbol` | string | `"BTC/USDT"` | Symbol to backtest |
| `timeframe` | string | `"4h"` | Candle timeframe |
| `days` | integer | `30` | Lookback period for backtests |

**Response 200:**
```json
{
  "decision": "ADOPT",
  "current_performance": {
    "win_rate": 48.5,
    "profit_factor": 1.2,
    "max_drawdown_pct": -12.3
  },
  "proposed_performance": {
    "win_rate": 57.1,
    "profit_factor": 1.8,
    "max_drawdown_pct": -8.4
  },
  "current_params": {
    "rsi_oversold": 30,
    "ema_fast": 9,
    "min_adx": 20
  },
  "proposed_params": {
    "rsi_oversold": 28,
    "ema_fast": 9,
    "min_adx": 23
  },
  "final_params": {
    "rsi_oversold": 28,
    "ema_fast": 9,
    "min_adx": 23
  },
  "decision_reasoning": "Proposed params improve win rate by 8.6pp and reduce drawdown by 3.9pp with no increase in risk.",
  "improvement_metrics": {
    "win_rate_delta": 8.6,
    "profit_factor_delta": 0.6,
    "drawdown_improvement": 3.9
  }
}
```

**curl:**
```bash
curl -X POST http://localhost:8080/assess \
  -H "Content-Type: application/json" \
  -d '{"symbol": "BTC/USDT", "timeframe": "4h", "days": 30}'
```

**Notes:**
- This is a long-running call (two LLM-backed backtests). Typical duration: 30–120 seconds depending on LLM latency.
- Persisted params affect all subsequent `/analyze` calls that use the current strategy.

---

## Error Handling

All endpoints return standard HTTP status codes. Unhandled exceptions in agent/tool execution propagate as `500 Internal Server Error` with a JSON body. The `X-Correlation-ID` from the request (or the auto-generated one) appears in all structlog entries for that request.

```json
{
  "detail": "Internal Server Error"
}
```

For structured error details, check the server logs (structlog JSON output to stdout).
